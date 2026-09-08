"""
Resource management and metrics operations.
Handles resource usage tracking, node resources, and instance metrics.
"""

import logging
from collections import OrderedDict
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
import time
import pylxd

from app.models.schemas import (
    ResourceUsage,
    NodeStatus,
    InstanceMetrics,
)
from app.core.node_metadata import get_node_metadata_store
from app.core.type_utils import (
    detect_cluster_type_from_metadata,
    resolve_clustered,
    normalize_node_type,
)
from .utils import parse_vm_status, find_instance_in_projects
from .gpu import GpuInventory, build_gpu_inventory
from .storage_model import PoolReading, summarise_cluster_storage
from .hardware import build_hardware_profile
from .node_resources import aggregate_microcloud_resources, build_node_resources
from .exceptions import LXDOperationError
from .freshness import LiveRead, read_resources

logger = logging.getLogger(__name__)


# Keys of the LXD environment block that may reach a client.
#
# important: allowlist, not denylist. The raw block also carries the cluster's certificate PEM,
# its fingerprint, every listen address and the server PID, and NodeStatus.environment is typed
# Dict[str, Any] so response_model filters nothing. It rides GET /nodes, which the Technologies
# page reads and which has no role gate, so a Tenant's browser was receiving all of it.
#
# Nothing in the backend reads status.environment: every consumer works off host_info directly,
# so narrowing this is contained to what goes on the wire. Same decision, and the same shape, as
# routers/integrations.py:_public_location_data.
PUBLIC_ENVIRONMENT_KEYS = frozenset(
    {
        "architectures",
        "backup_metadata_version_range",
        "driver",
        "driver_version",
        "firewall",
        "instance_types",
        "kernel",
        "kernel_architecture",
        "kernel_features",
        "kernel_version",
        "lxc_features",
        "os_name",
        "os_version",
        "project",
        "server",
        "server_clustered",
        "server_event_mode",
        "server_lts",
        "server_name",
        "server_version",
        "storage",
        "storage_supported_drivers",
        "storage_version",
    }
)


def public_environment(environment: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The environment block minus anything a client has no business holding."""
    if not environment:
        return environment
    return {key: value for key, value in environment.items() if key in PUBLIC_ENVIRONMENT_KEYS}


class ResourceManager:
    """
    Manages resource tracking and metrics for LXD instances and nodes.
    """
    
    # important: bounded. One ResourceManager lives for the whole process and this used to be
    # a plain dict with one entry per host:instance ever sampled, never pruned when an
    # instance was deleted, so it grew with lifetime instance churn rather than fleet size.
    _CPU_HISTORY_MAX = 5000

    def __init__(self):
        # CPU usage tracking: {"host:instance": {"usage_ns": int, "timestamp": float}}
        # LRU by insertion order: every sample re-inserts, so the coldest key falls out first.
        self._cpu_history: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    
    def get_resource_usage(self, instance) -> Optional[ResourceUsage]:
        """Get resource usage for a running instance."""
        try:
            if instance.status != "Running":
                return None
            
            state = instance.state()
            if not state:
                return None
            
            resources = ResourceUsage()

            # important: cumulative nanoseconds of CPU time, which is what LXD's cpu.usage is.
            # It was typed and documented as a percentage, so the detail card rendered a
            # 15-digit counter under the label "CPU Usage". A percentage needs two samples and
            # is computed by get_instance_metrics below.
            if state.cpu:
                resources.cpu_time_ns = state.cpu.get("usage")

            # important: `total`, not `usage_peak`. usage_peak is a cgroup high-water mark that
            # every VM reports as 0, so reading it as the limit put "Memory Limit 0" on every VM
            # detail and left the usage figure with no denominator. `total` is the memory the
            # instance can actually reach, and is the denominator get_instance_metrics already
            # divides by at the bottom of this file.
            if state.memory:
                resources.memory_usage = state.memory.get("usage")
                resources.memory_total = state.memory.get("total")
            
            # Disk usage
            if state.disk:
                resources.disk_usage = {
                    k: v.get("usage", 0) for k, v in state.disk.items()
                }
            
            # Network counters
            if state.network:
                resources.network_counters = {}
                for iface, data in state.network.items():
                    if iface != "lo":
                        resources.network_counters[iface] = {
                            "bytes_received": data.get("counters", {}).get(
                                "bytes_received", 0
                            ),
                            "bytes_sent": data.get("counters", {}).get(
                                "bytes_sent", 0
                            ),
                            "packets_received": data.get("counters", {}).get(
                                "packets_received", 0
                            ),
                            "packets_sent": data.get("counters", {}).get(
                                "packets_sent", 0
                            ),
                        }
            
            return resources
            
        except Exception as e:
            logger.debug(f"Could not get resources for {instance.name}: {e}")
            return None
    
    def get_node_gpus(self, client: pylxd.Client, node_name: str) -> GpuInventory:
        """List the GPUs on a node for the instance-create passthrough picker.

        important: this raises on an unreachable host rather than returning an empty list. An
        empty list means "this host has no GPU", and telling the picker that about a host we
        could not reach is a lie the operator cannot see. LXDService.get_node_gpus guards the
        client with require_client, so a breaker-tripped cluster surfaces as a 503.

        Every detected card is returned, including display controllers, each carrying its
        class and an `attachable` flag: hiding a device an operator can see in LXD generates
        the support ticket it was meant to prevent.
        """
        try:
            return build_gpu_inventory(
                read_resources(client, node_name).body,
                # recursion=1: the inventory reads per-instance attributes.
                client.instances.all(recursion=1),
            )
        except Exception as e:
            logger.warning(f"Error listing GPUs for {node_name}: {e}")
            raise LXDOperationError(f"Failed to list GPUs: {str(e)}")

    def get_node_resources(
        self,
        client: pylxd.Client,
        node_name: str,
        resources: Optional[LiveRead] = None,
    ) -> Dict[str, Any]:
        """Get available (unused) resources for a specific node.

        Pass `resources` when the caller has already read the body this request. That is what
        keeps a node-status check at one round trip instead of three: CPU/memory, the hardware
        profile and the GPU inventory all want the same body.
        """
        try:
            # get system resources
            live = resources or read_resources(client, node_name)
            resources_data = live.body
            
            # extract CPU info
            cpu_info = resources_data.get("cpu", {})
            cpu_total = cpu_info.get("total", 0)
            cpu_sockets = len(cpu_info.get("sockets", []))
            
            # extract memory info
            memory_info = resources_data.get("memory", {})
            memory_total = memory_info.get("total", 0)
            memory_used = memory_info.get("used", 0)
            memory_available = memory_total - memory_used
            
            # get all running instances to calculate CPU usage
            try:
                # recursion=1: .status and .config below are lazily synced otherwise.
                instances = client.instances.all(recursion=1)
                cpu_used = 0
                for instance in instances:
                    if instance.status == "Running":
                        # count CPU limits from instance config
                        config = instance.config or {}
                        limits_cpu = config.get("limits.cpu")
                        if limits_cpu:
                            try:
                                cpu_used += int(limits_cpu)
                            except (ValueError, TypeError):
                                # non-integer value (ex; pinning "0-3") - count range
                                try:
                                    parts = limits_cpu.split("-")
                                    cpu_used += int(parts[-1]) - int(parts[0]) + 1
                                except Exception:
                                    cpu_used += 1
                        else:
                            # no CPU limit set - count as 1 allocated vCPU
                            cpu_used += 1
                cpu_available = cpu_total - cpu_used
                logger.debug(f"CPU on {node_name}: {cpu_total} total, {cpu_used} used, {cpu_available} available")
            except Exception as e:
                logger.warning(f"Could not calculate CPU usage for {node_name}: {e}")
                cpu_available = cpu_total
            
            # get storage pools
            #
            # important: this host is standalone, so there is one reading per pool and usable
            # equals raw. It still folds through the same summariser the clustered aggregate
            # uses, so the two paths cannot answer differently for the same shape, which is the
            # mistake gpu-inventory.md records: four surfaces counting the same thing four ways.
            pool_readings: List[PoolReading] = []
            degraded_parts: List[str] = []
            try:
                pools = client.storage_pools.all()
                logger.debug(f"Found {len(pools)} storage pools on {node_name}")
                for pool in pools:
                    try:
                        # use API directly to get pool resources
                        pool_resources_response = client.api.storage_pools[pool.name].resources.get()
                        pool_resources = pool_resources_response.json().get("metadata", {})
                        space_info = pool_resources.get("space", {})
                        space_total = space_info.get("total", 0)
                        space_used = space_info.get("used", 0)
                        logger.debug(f"Pool {pool.name}: {space_total} bytes total, {space_used} bytes used")
                        pool_readings.append(PoolReading(
                            member=node_name,
                            name=pool.name,
                            driver=pool.driver,
                            space_total=space_total,
                            space_used=space_used,
                        ))
                    except Exception as pool_err:
                        logger.warning(f"Could not get resources for pool {pool.name}: {pool_err}")
                        degraded_parts.append(f"storage:{pool.name}")
            except Exception as e:
                logger.warning(f"Could not get storage pools for {node_name}: {e}")
                degraded_parts.append("storage")

            storage = summarise_cluster_storage(pool_readings)
            logger.debug(
                f"Storage for {node_name}: {storage.available} bytes available "
                f"({len(storage.pools)} pools)"
            )
            
            return {
                "cpu_total": cpu_total,
                "cpu_available": cpu_available,
                "cpu_sockets": cpu_sockets,
                "memory_total": memory_total,
                "memory_available": memory_available,
                "storage_total": storage.total,
                "storage_used": storage.used,
                "storage_available": storage.available,
                "storage_raw_total": storage.raw_total,
                "storage_raw_used": storage.raw_used,
                "storage_pools": storage.pools,
                "degraded": bool(degraded_parts),
                "degraded_parts": sorted(set(degraded_parts)),
                # Detected hardware, identifier-stripped. resources_data is the raw
                # /1.0/resources body read above and otherwise discarded, so this costs no
                # extra round trip and, more importantly, is the same sample the CPU and
                # memory figures above came from. Two samples for one fact is how the GPU
                # counts came to disagree across endpoints.
                "hardware": build_hardware_profile(resources_data),
                # important: the moment the body came off the cluster, not the moment a client
                # read it back. Everything downstream must carry this through rather than
                # restamp, or a body served from a 180s cache claims to be current.
                "collected_at": live.collected_at,
            }
            
        except Exception as e:
            # important: WARNING for the same reason as get_node_gpus above. This receives
            # whatever get_client returned, so a breaker-tripped cluster arrives here as a
            # None dereference, which is expected operational noise rather than a fault.
            logger.warning(f"Error getting resources for {node_name}: {e}")
            raise LXDOperationError(f"Failed to get node resources: {str(e)}")
    
    def check_node_status(
        self,
        client: pylxd.Client,
        node_name: str,
        endpoint: str,
        stored_clustered: Optional[bool] = None,
    ) -> NodeStatus:
        """Check connection status and resources for a specific node.

        `stored_clustered` is the last reading check-auth wrote to the cluster record. A live
        trusted payload always outranks it; it only answers for a host that is up but has
        stopped trusting our certificate, where the clusters table would still report the fact.
        Both surfaces resolve through the same helper so they cannot drift apart.
        """
        # Get display name from metadata store
        metadata_store = get_node_metadata_store()
        display_name = metadata_store.get_node_display_name(node_name)
        
        status = NodeStatus(
            name=node_name,
            endpoint=endpoint,
            connected=False,
            display_name=display_name,
        )
        
        try:
            host_info = client.host_info
            # The host answered. Everything below can still fail, but from here on a failure
            # means "answered and refused us", not "never answered", and that is the whole
            # difference between Pending Trust and Offline.
            status.reachable = True
            
            # Don't set connected=True yet, need to verify we can actually access resources
            # Connection successful but need to check if authenticated
            status.server_version = host_info.get("environment", {}).get(
                "server_version"
            )
            status.api_version = host_info.get("api_version")
            status.auth = host_info.get("auth")
            status.environment = public_environment(host_info.get("environment"))
            
            # Parse tags from server config
            # Use direct API call to get fresh data (avoid pylxd caching)
            try:
                response = client.api.get()
                server_data = response.json()["metadata"]
                server_config = server_data.get("config", {})
                tags_str = server_config.get("user.tags", "")
                tags = [tag.strip() for tag in tags_str.split(",") if tag.strip()] if tags_str else []
                status.tags = tags
                logger.debug(f"Loaded tags for {node_name}: {tags}")
            except Exception as tag_err:
                logger.debug(f"Could not get tags for {node_name}: {tag_err}")
                status.tags = []
            
            # Detect hypervisor family and clustering state.
            # Only detect if authenticated (trusted), otherwise leave as None
            auth_status = host_info.get("auth", "")
            if auth_status == "trusted":
                try:
                    # important: the live payload wins over the stored value, not the other way
                    # round. The stored type can be a registration-time guess taken before the
                    # certificate was trusted, and preferring it is what kept a MicroCloud
                    # reading as plain LXD. The stored value is only a fallback for a host that
                    # answers without an environment block.
                    detected_type = detect_cluster_type_from_metadata(host_info)
                    status.node_type = normalize_node_type(detected_type)
                    status.clustered = resolve_clustered(stored_clustered, client)

                    environment = host_info.get('environment', {})
                    if not environment:
                        metadata_store = get_node_metadata_store()
                        stored = metadata_store.get_all_node_metadata().get(node_name, {})
                        if "type" in stored:
                            status.node_type = normalize_node_type(stored["type"])
                            logger.debug(
                                f"Node type from stored metadata for {node_name}: {status.node_type}"
                            )

                    logger.info(
                        f"Node type detected for {node_name}: {status.node_type} "
                        f"(clustered={status.clustered}, "
                        f"server={environment.get('server', 'N/A')}, "
                        f"server_name={environment.get('server_name', 'N/A')})"
                    )
                except Exception as type_err:
                    logger.warning(f"Could not determine node type for {node_name}: {type_err}")
                    status.node_type = None  # Don't fallback to "lxd", keep as None
                    status.clustered = stored_clustered
            else:
                # Not trusted/authenticated - don't detect type. `clustered` still falls back,
                # because only a trusted answer ever wrote it and a host that has stopped
                # trusting us has not stopped being a cluster. `type` deliberately does not:
                # a stored type can predate the change that stopped guessing it, and nothing in
                # the value distinguishes a reading from that old default.
                logger.info(f"Node {node_name} not trusted (auth={auth_status}), skipping type detection")
                status.node_type = None
                status.clustered = stored_clustered

            # Get node resources
            try:
                # important: one live read, threaded into both consumers below. Reading twice
                # would double the fan-out cost per cluster and let the memory figure and the
                # GPU inventory come from two different samples of the same host.
                live = read_resources(client, node_name)
                resources_data = self.get_node_resources(client, node_name, resources=live)

                # GPU capacity, usage and card detail, all from the canonical inventory.
                gpu_inv = GpuInventory.empty()
                try:
                    gpu_inv = build_gpu_inventory(
                        live.body, client.instances.all(recursion=1)
                    )
                except Exception as gpu_err:
                    # A GPU read failure must not zero the node's CPU and memory.
                    logger.debug(f"Could not get GPU info for {node_name}: {gpu_err}")
                
                # Aggregate across members whenever the host is clustered. Gating this on the
                # "microcloud" label under-reported every plain LXD cluster's capacity, since
                # member resources are a property of clustering, not of the distribution.
                if status.clustered:
                    logger.info(f"Detected clustered host {node_name}, aggregating resources from all members...")
                    try:
                        aggregated_resources = aggregate_microcloud_resources(
                            client=client,
                            node_name=node_name,
                            base_resources=resources_data,
                        )
                        if aggregated_resources:
                            status.resources = aggregated_resources
                            logger.info(f"MicroCloud {node_name} aggregated resources: "
                                      f"CPU={aggregated_resources.cpu_total} cores, "
                                      f"RAM={aggregated_resources.memory_total / 1024 / 1024 / 1024:.1f} GB, "
                                      f"GPU={aggregated_resources.gpu_total} compute "
                                      f"of {aggregated_resources.gpu_cards_total} cards")
                        else:
                            # Fallback to single node resources if aggregation fails
                            status.resources = build_node_resources(resources_data, gpu_inv)
                    except Exception as agg_err:
                        logger.warning(f"Failed to aggregate MicroCloud resources: {agg_err}")
                        # Fallback to single node resources
                        status.resources = build_node_resources(resources_data, gpu_inv)
                else:
                    # Standard LXD node - use single node resources
                    status.resources = build_node_resources(resources_data, gpu_inv)
                # Only set connected=True if we successfully got resources (authenticated)
                status.connected = True
            except Exception as res_err:
                # Failed to get resources, likely not authenticated (Forbidden)
                logger.warning(f"Could not get resources for {node_name}: {res_err}")
                status.connected = False
                status.error = f"Not authorized: {str(res_err)}"
        
        except Exception as e:
            status.error = str(e)
            status.connected = False
            # host_info itself raised, so the host never answered.
            status.reachable = False
            logger.warning(f"Node {node_name} health check failed: {e}")
        
        return status
    
    def get_instance_metrics(
        self, 
        client: pylxd.Client,
        host: str, 
        instance_name: str,
        project: str = "default"
    ) -> InstanceMetrics:
        """
        Get real-time metrics for a specific instance.
        Returns CPU, memory, disk, network usage with timestamps.
        """
        try:
            # important: scoped to the project the caller authorized. This searched every project
            # and discarded the `project` argument entirely, so a name resolved to whichever
            # project LXD listed first. The router authorizes one project and this read answered
            # about another, which on a cluster hosting several tenants is one tenant's live CPU,
            # memory, disk and network handed to another.
            instance, found_project = find_instance_in_projects(client, instance_name, project)

            if not instance:
                raise LXDOperationError(f"Instance '{instance_name}' not found on {host}")
            
            # Get current state
            state = instance.state()
            
            # Parse status
            status = parse_vm_status(instance.status, instance.status_code)
            
            # Initialize metrics
            metrics = InstanceMetrics(
                instance_name=instance_name,
                host=host,
                status=status,
            )
            
            # Only get metrics for running instances
            if instance.status != "Running" or not state:
                return metrics
            
            # CPU usage - calculate from cumulative nanoseconds delta
            # important: `is not None`, not truthiness. A cumulative usage of exactly 0 is
            # falsy, which skipped the whole block: no percentage, and no baseline sample stored,
            # so an instance idling at zero never began accumulating history at all.
            if state.cpu and state.cpu.get("usage") is not None:
                current_usage_ns = state.cpu.get("usage")
                current_time = time.time()
                
                # note: the project belongs in the key. A name is unique per project, not per
                # host, so two instances of the same name on one host shared a cumulative counter
                # and each poll subtracted the other's reading.
                cache_key = f"{host}:{found_project or project}:{instance_name}"
                
                if cache_key in self._cpu_history:
                    # Calculate delta
                    prev_data = self._cpu_history[cache_key]
                    prev_usage_ns = prev_data["usage_ns"]
                    prev_time = prev_data["timestamp"]
                    
                    time_delta = current_time - prev_time
                    usage_delta_ns = current_usage_ns - prev_usage_ns
                    
                    # Calculate percentage
                    if time_delta > 0:
                        cpu_percent = (usage_delta_ns / (time_delta * 1e9)) * 100
                        metrics.cpu_usage_percent = round(min(cpu_percent, 800.0), 2)
                
                # Store current values for next calculation
                self._cpu_history.pop(cache_key, None)
                self._cpu_history[cache_key] = {
                    "usage_ns": current_usage_ns,
                    "timestamp": current_time
                }
                while len(self._cpu_history) > self._CPU_HISTORY_MAX:
                    self._cpu_history.popitem(last=False)
            
            # Memory usage
            if state.memory:
                metrics.memory_used_bytes = state.memory.get("usage", 0)
                metrics.memory_total_bytes = state.memory.get("total", 0)
                if metrics.memory_total_bytes > 0:
                    metrics.memory_usage_percent = round(
                        (metrics.memory_used_bytes / metrics.memory_total_bytes) * 100, 2
                    )
            
            # Disk usage
            if state.disk:
                metrics.disk_usage_bytes = {
                    device: data.get("usage", 0)
                    for device, data in state.disk.items()
                }
            
            # Network counters
            if state.network:
                for iface_name, iface_data in state.network.items():
                    if iface_name == "lo":
                        continue
                    counters = iface_data.get("counters", {})
                    metrics.network_rx_bytes[iface_name] = counters.get("bytes_received", 0)
                    metrics.network_tx_bytes[iface_name] = counters.get("bytes_sent", 0)
            
            # Process count
            if state.processes:
                metrics.processes = state.processes
            
            # PID
            if hasattr(state, 'pid') and state.pid:
                metrics.pid = state.pid
            
            # Uptime
            try:
                if hasattr(state, 'started_at') and state.started_at:
                    started_at_str = state.started_at
                    if started_at_str.endswith('Z'):
                        started_at_str = started_at_str[:-1] + '+00:00'
                    started_at = datetime.fromisoformat(started_at_str)
                    now = datetime.now(timezone.utc)
                    uptime_delta = now - started_at
                    metrics.uptime_seconds = int(uptime_delta.total_seconds())
                else:
                    if instance.last_used_at and instance.status == "Running":
                        last_used_str = instance.last_used_at
                        if last_used_str.endswith('Z'):
                            last_used_str = last_used_str[:-1] + '+00:00'
                        last_used = datetime.fromisoformat(last_used_str)
                        now = datetime.now(timezone.utc)
                        uptime_delta = now - last_used
                        metrics.uptime_seconds = int(uptime_delta.total_seconds())
            except Exception as e:
                logger.warning(f"Could not calculate uptime for {instance_name}: {e}")
            
            # Base image
            if instance.config and instance.config.get('image.description'):
                metrics.base_image = instance.config.get('image.description')
            elif instance.config and instance.config.get('volatile.base_image'):
                metrics.base_image = instance.config.get('volatile.base_image')
            
            # IP addresses
            if state.network:
                for iface_name, iface_data in state.network.items():
                    if iface_name == "lo":
                        continue
                    for addr in iface_data.get("addresses", []):
                        ip = addr.get("address")
                        family = addr.get("family")
                        if family == "inet":
                            metrics.ipv4_addresses.append(ip)
                        elif family == "inet6" and not ip.startswith("fe80"):
                            metrics.ipv6_addresses.append(ip)
            
            return metrics
            
        except LXDOperationError:
            raise
        except Exception as e:
            logger.error(f"Error getting metrics for '{instance_name}' on {host}: {e}")
            raise LXDOperationError(f"Failed to get metrics: {str(e)}")


# Keys of the LXD server `config` block that may reach a client.
#
# important: allowlist, for the same reason PUBLIC_ENVIRONMENT_KEYS is one, and arguably a
# sharper need. The raw config block is where an LXD daemon keeps `core.trust_password`, the
# cluster join secret, `core.https_address` and every candid/OIDC setting. It was returned whole
# by GET /server, GET /server/{node} and GET /server/{node}/config, all of which sit on a
# require_tenant() router.
#
# The page that renders this filtered certificate keys in the browser
# (frontend/src/app/inventory/server/page.tsx), which narrows what is displayed and not what is
# sent; the API response carried the lot regardless of what the page chose to draw.
#
# Nothing in the backend reads ServerInfo.config, so narrowing this is contained to the wire.
PUBLIC_CONFIG_KEYS = frozenset(
    {
        "core.https_address",
        "cluster.https_address",
        "core.proxy_ignore_hosts",
        "images.auto_update_cached",
        "images.auto_update_interval",
        "images.compression_algorithm",
        "images.remote_cache_expiry",
        "instances.nic.host_name",
        "storage.backups_volume",
        "storage.images_volume",
    }
)


def public_config(config: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The server config block minus anything a client has no business holding."""
    if not config:
        return config
    return {key: value for key, value in config.items() if key in PUBLIC_CONFIG_KEYS}
