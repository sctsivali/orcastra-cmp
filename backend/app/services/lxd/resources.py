"""Instance resource usage, node resources and node status read from LXD."""

import logging
from typing import Dict, Any, List, Optional
import pylxd

from app.models.schemas import (
    NodeResources,
    ResourceUsage,
    NodeStatus,
)
from app.core.node_metadata import get_node_metadata_store
from app.core.type_utils import (
    detect_cluster_type_from_metadata,
    resolve_clustered,
    normalize_node_type,
)
from .gpu import GpuInventory, build_gpu_inventory
from . import pool_resource_failures
from .storage_model import PoolReading, summarise_cluster_storage
from .hardware import build_hardware_profile
from .node_resources import aggregate_microcloud_resources, build_node_resources
from .exceptions import LXDOperationError
from .freshness import LiveRead, read_resources, resources_failure
from app.services.lxd.project_scope import list_instance_records
from app.services.lxd.cpu_allocation import allocated_cpu, allocation_ratio, clamp_available
from app.core.lxd_path import safe_segment

logger = logging.getLogger(__name__)

# Keys of the LXD environment block that may reach a client.
#
# important: an allowlist. The raw block also carries the cluster's certificate PEM, its
# fingerprint, every listen address and the server PID. NodeStatus.environment is typed
# Dict[str, Any], so response_model filters nothing, and it rides GET /nodes, which every Partner
# in scope may read. Nothing in the backend reads status.environment (consumers use
# host_info), so this only narrows the wire. Same shape as
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
    
    def get_resource_usage(self, instance) -> Optional[ResourceUsage]:
        """Get resource usage for a running instance."""
        try:
            if instance.status != "Running":
                return None
            
            state = instance.state()
            if not state:
                return None
            
            resources = ResourceUsage()

            # LXD's cpu.usage is cumulative nanoseconds of CPU time. A percentage needs two
            # samples and is computed in instance_metrics.read_instance_metrics.
            if state.cpu:
                resources.cpu_time_ns = state.cpu.get("usage")

            # `total` is the memory the instance can reach, and the denominator
            # instance_metrics.py divides by. `usage_peak` is a cgroup high-water mark that every
            # VM reports as 0, so it cannot serve as the limit.
            if state.memory:
                resources.memory_usage = state.memory.get("usage")
                resources.memory_total = state.memory.get("total")
            
            if state.disk:
                resources.disk_usage = {
                    k: v.get("usage", 0) for k, v in state.disk.items()
                }
            
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

        Raises on an unreachable host instead of returning an empty list, which would claim the
        host has no GPU. LXDService.get_node_gpus guards the client with require_client, so a
        breaker-tripped cluster surfaces as a 503.

        Every detected card is returned, display controllers included, each with its class and
        an `attachable` flag, so the picker matches what an operator sees in LXD.
        """
        try:
            return build_gpu_inventory(
                read_resources(client, node_name).body,
                # recursion=1: the inventory reads per-instance attributes.
                list_instance_records(client)[0],
            )
        except Exception as e:
            logger.warning(f"Error listing GPUs for {node_name}: {e}")
            raise LXDOperationError(f"Could not list GPUs: {str(e)}")

    def get_node_resources(
        self,
        client: pylxd.Client,
        node_name: str,
        resources: Optional[LiveRead] = None,
        instances: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """Available (unused) resources of one node.

        Pass `resources` when the caller has already read the body this request: CPU/memory, the
        hardware profile and the GPU inventory all want the same body, so a node-status check
        stays at one round trip. `instances` does the same for the instance list the CPU
        allocation is counted from.
        """
        try:
            live = resources or read_resources(client, node_name)
            resources_data = live.body
            
            cpu_info = resources_data.get("cpu", {})
            cpu_total = cpu_info.get("total", 0)
            cpu_sockets = len(cpu_info.get("sockets", []))
            
            memory_info = resources_data.get("memory", {})
            memory_total = memory_info.get("total", 0)
            memory_used = memory_info.get("used", 0)
            memory_available = memory_total - memory_used
            
            # CPU allocation of the running instances this body is about. /1.0/resources on a
            # clustered host answers for the member the connection landed on, so only the
            # instances placed there count; the whole cluster against one member's threads
            # would show negative available CPU.
            environment = (getattr(client, "host_info", None) or {}).get("environment") or {}
            member = environment.get("server_name") if environment.get("server_clustered") else None
            cpu_used = 0
            try:
                # recursion=1: .status and .config below are lazily synced otherwise.
                if instances is None:
                    instances, _ = list_instance_records(client)
                cpu_used = allocated_cpu(instances, member=member).cores
                logger.debug(f"CPU on {node_name}: {cpu_total} threads, {cpu_used} allocated")
            except Exception as e:
                logger.warning(f"Could not calculate CPU usage for {node_name}: {e}")
            cpu_available = clamp_available(cpu_total, cpu_used)
            
            # Storage pools. This host is standalone, so there is one reading per pool and
            # usable equals raw; it still folds through the summariser the clustered aggregate
            # uses, so the two paths cannot answer differently for the same shape.
            pool_readings: List[PoolReading] = []
            degraded_parts: List[str] = []
            try:
                pools = client.storage_pools.all()
                logger.debug(f"Found {len(pools)} storage pools on {node_name}")
                for pool in pools:
                    try:
                        # Through pool_resource_failures, so a pool that just failed is not
                        # asked again on every read.
                        pool_resources_response = pool_resource_failures.read(
                            node_name,
                            pool.name,
                            client.api.storage_pools[safe_segment(pool.name)].resources.get,
                        )
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
                "cpu_used": cpu_used,
                "cpu_available": cpu_available,
                "cpu_allocation_ratio": allocation_ratio(cpu_used, cpu_total),
                "cpu_overcommitted": cpu_used > cpu_total,
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
                # Detected hardware, identifier-stripped, from the same /1.0/resources body
                # as the CPU and memory figures: no extra round trip, and one sample for one
                # fact.
                "hardware": build_hardware_profile(resources_data),
                # important: when the body came off the cluster. Everything downstream must
                # carry this through rather than restamp it, or a body served from a 180s cache
                # claims to be current.
                "collected_at": live.collected_at,
            }
            
        except Exception as e:
            # WARNING: this receives whatever get_client returned, so a breaker-tripped cluster
            # arrives here as a None dereference, which is expected operational noise.
            logger.warning(f"Error getting resources for {node_name}: {e}")
            raise LXDOperationError(f"Could not get cluster resources: {str(e)}")
    
    def check_node_status(
        self,
        client: pylxd.Client,
        node_name: str,
        endpoint: str,
        stored_clustered: Optional[bool] = None,
    ) -> NodeStatus:
        """Connection status and resources of one node.

        `stored_clustered` is the last reading check-auth wrote to the cluster record. A live
        trusted payload always outranks it; it only answers for a host that is up but has
        stopped trusting the dashboard's certificate, where the clusters table still reports the
        fact. Both surfaces resolve through the same helper so they cannot drift apart.
        """
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
            # The host answered. From here on a failure means it answered and refused (Pending
            # Trust) rather than never answered (Offline), except for the resources read, whose
            # failure resources_failure classifies.
            status.reachable = True
            
            # `connected` stays False until the resources read proves the certificate is trusted.
            status.server_version = host_info.get("environment", {}).get(
                "server_version"
            )
            status.api_version = host_info.get("api_version")
            status.auth = host_info.get("auth")
            status.environment = public_environment(host_info.get("environment"))
            
            # Tags come from host_info: get_client's validation probe already refreshed it in
            # this read (connection._validate_connection), so a second GET /1.0 would repeat
            # the round trip on every status read.
            try:
                server_config = (host_info or {}).get("config", {}) or {}
                tags_str = server_config.get("user.tags", "")
                tags = [tag.strip() for tag in tags_str.split(",") if tag.strip()] if tags_str else []
                status.tags = tags
                logger.debug(f"Loaded tags for {node_name}: {tags}")
            except Exception as tag_err:
                logger.debug(f"Could not get tags for {node_name}: {tag_err}")
                status.tags = []
            
            # Hypervisor family and clustering state, detected only for a trusted answer.
            auth_status = host_info.get("auth", "")
            if auth_status == "trusted":
                try:
                    # important: the live payload wins over the stored value. The stored type
                    # can be a registration-time guess taken before the certificate was trusted
                    # (ex; a MicroCloud stored as plain LXD); it is only a fallback for a host
                    # that answers without an environment block.
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
                # Not trusted: no type detection. `clustered` still falls back to the stored
                # value, because only a trusted answer ever wrote it and a host that stopped
                # trusting the dashboard is still a cluster. `type` does not: a stored type may be
                # a guessed default, and nothing in the value tells it apart from a reading.
                logger.info(f"Node {node_name} not trusted (auth={auth_status}), skipping type detection")
                status.node_type = None
                status.clustered = stored_clustered

            try:
                # One live read for both consumers below. Reading twice would double the fan-out
                # cost per cluster and let the memory figure and the GPU inventory come from two
                # different samples of the same host.
                live = read_resources(client, node_name)

                def single_host() -> NodeResources:
                    # One instance list for the CPU allocation and the GPU inventory, read only
                    # when this answer is used, so a cluster whose members aggregate reads none.
                    records: Optional[List[Any]] = None
                    try:
                        records = list_instance_records(client)[0]
                    except Exception as list_err:
                        logger.debug(f"Could not list instances on {node_name}: {list_err}")
                    resources_data = self.get_node_resources(
                        client, node_name, resources=live, instances=records
                    )
                    gpu_inv = GpuInventory.empty()
                    try:
                        gpu_inv = build_gpu_inventory(
                            live.body, records if records is not None else list_instance_records(client)[0]
                        )
                    except Exception as gpu_err:
                        # A GPU read failure must not zero the node's CPU and memory.
                        logger.debug(f"Could not get GPU info for {node_name}: {gpu_err}")
                    return build_node_resources(resources_data, gpu_inv)

                # Aggregate across members whenever the host is clustered: member resources are
                # a property of clustering, whatever the distribution.
                if status.clustered:
                    logger.info(f"Detected clustered host {node_name}, aggregating resources from all members...")
                    try:
                        aggregated_resources = aggregate_microcloud_resources(
                            client=client,
                            node_name=node_name,
                            base_resources={},
                        )
                        if aggregated_resources:
                            status.resources = aggregated_resources
                            logger.info(f"MicroCloud {node_name} aggregated resources: "
                                      f"CPU capacity={aggregated_resources.cpu_total} hardware threads, "
                                      f"Memory={aggregated_resources.memory_total / 1024 / 1024 / 1024:.1f} GiB, "
                                      f"GPU={aggregated_resources.gpu_total} compute "
                                      f"of {aggregated_resources.gpu_cards_total} cards")
                        else:
                            status.resources = single_host()
                    except Exception as agg_err:
                        logger.warning(f"Failed to aggregate MicroCloud resources: {agg_err}")
                        status.resources = single_host()
                else:
                    status.resources = single_host()
                # The resources read succeeds only for a trusted certificate.
                status.connected = True
            except Exception as res_err:
                logger.warning(f"Could not get resources for {node_name}: {res_err}")
                status.connected = False
                status.reachable, status.error = resources_failure(res_err)
        
        except Exception as e:
            status.error = str(e)
            status.connected = False
            # host_info itself raised, so the host never answered.
            status.reachable = False
            logger.warning(f"Node {node_name} health check failed: {e}")
        
        return status


# Keys of the LXD server `config` block that may reach a client.
#
# important: an allowlist, like PUBLIC_ENVIRONMENT_KEYS. The raw config block holds
# `core.trust_password`, the cluster join secret and every candid/OIDC setting, and GET /server,
# GET /server/{node} and GET /server/{node}/config answer any Partner in scope. Filtering in
# the browser (frontend/src/app/inventory/server/page.tsx) narrows what is displayed, not what is
# sent. Nothing in the backend reads ServerInfo.config, so this only narrows the wire.
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
