"""
Resource management and metrics operations.
Handles resource usage tracking, node resources, and instance metrics.
"""

import logging
from typing import Dict, Any, Optional
from datetime import datetime, timezone
import time
import pylxd

from app.models.schemas import (
    ResourceUsage,
    NodeResources,
    NodeStatus,
    InstanceMetrics,
)
from app.core.node_metadata import get_node_metadata_store
from .utils import parse_vm_status, find_instance_in_projects
from .exceptions import LXDOperationError

logger = logging.getLogger(__name__)


class ResourceManager:
    """
    Manages resource tracking and metrics for LXD instances and nodes.
    """
    
    def __init__(self):
        # CPU usage tracking: {"host:instance": {"usage_ns": int, "timestamp": float}}
        self._cpu_history: Dict[str, Dict[str, any]] = {}
    
    def get_resource_usage(self, instance) -> Optional[ResourceUsage]:
        """Get resource usage for a running instance."""
        try:
            if instance.status != "Running":
                return None
            
            state = instance.state()
            if not state:
                return None
            
            resources = ResourceUsage()
            
            # CPU usage
            if state.cpu:
                resources.cpu_usage = state.cpu.get("usage")
            
            # Memory usage
            if state.memory:
                resources.memory_usage = state.memory.get("usage")
                resources.memory_limit = state.memory.get("usage_peak")
            
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
    
    def get_node_resources(self, client: pylxd.Client, node_name: str) -> Dict[str, Any]:
        """Get available (unused) resources for a specific node."""
        try:
            # get system resources
            resources_data = client.resources
            
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
                instances = client.instances.all()
                cpu_used = 0
                for instance in instances:
                    if instance.status == "Running":
                        # count CPU limits from instance config
                        config = instance.config or {}
                        limits_cpu = config.get("limits.cpu")
                        if limits_cpu:
                            try:
                                cpu_used += int(limits_cpu)
                            except:
                                pass
                cpu_available = cpu_total - cpu_used
                logger.info(f"CPU on {node_name}: {cpu_total} total, {cpu_used} used, {cpu_available} available")
            except Exception as e:
                logger.warning(f"Could not calculate CPU usage for {node_name}: {e}")
                cpu_available = cpu_total
            
            # get storage pools
            storage_pools = []
            try:
                pools = client.storage_pools.all()
                logger.info(f"Found {len(pools)} storage pools on {node_name}")
                for pool in pools:
                    try:
                        # use API directly to get pool resources
                        pool_resources_response = client.api.storage_pools[pool.name].resources.get()
                        pool_resources = pool_resources_response.json().get("metadata", {})
                        space_info = pool_resources.get("space", {})
                        space_total = space_info.get("total", 0)
                        space_used = space_info.get("used", 0)
                        space_available = space_total - space_used
                        logger.info(f"Pool {pool.name}: {space_total} bytes total, {space_used} bytes used, {space_available} available")
                        storage_pools.append({
                            "name": pool.name,
                            "driver": pool.driver,
                            "space_total": space_total,
                            "space_used": space_used,
                            "space_available": space_available,
                        })
                    except Exception as pool_err:
                        logger.warning(f"Could not get resources for pool {pool.name}: {pool_err}")
            except Exception as e:
                logger.warning(f"Could not get storage pools for {node_name}: {e}")
            
            # calculate available storage
            storage_available = sum(p.get("space_available", 0) for p in storage_pools)
            logger.info(f"Storage for {node_name}: {storage_available} bytes available ({len(storage_pools)} pools)")
            
            return {
                "cpu_total": cpu_total,
                "cpu_available": cpu_available,
                "cpu_sockets": cpu_sockets,
                "memory_total": memory_total,
                "memory_available": memory_available,
                "storage_total": sum(p.get("space_total", 0) for p in storage_pools),
                "storage_available": storage_available,
                "storage_pools": storage_pools,
            }
            
        except Exception as e:
            logger.error(f"Error getting resources for {node_name}: {e}")
            raise LXDOperationError(f"Failed to get node resources: {str(e)}")
    
    def check_node_status(self, client: pylxd.Client, node_name: str, endpoint: str) -> NodeStatus:
        """Check connection status and resources for a specific node."""
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
            
            status.connected = True
            status.server_version = host_info.get("environment", {}).get(
                "server_version"
            )
            status.api_version = host_info.get("api_version")
            status.auth = host_info.get("auth")
            status.environment = host_info.get("environment")
            
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
            
            # Detect node type (microcloud vs lxd)
            try:
                cluster_enabled = host_info.get("environment", {}).get("server_clustered", False)
                server_name = host_info.get("environment", {}).get("server_name", "")
                
                # Check if this is part of MicroCloud
                if cluster_enabled and ("micro" in server_name.lower() or "microcloud" in str(host_info).lower()):
                    status.node_type = "microcloud"
                else:
                    status.node_type = "lxd"
            except Exception as type_err:
                logger.debug(f"Could not determine node type for {node_name}: {type_err}")
                status.node_type = "lxd"
            
            # Get node resources
            try:
                resources_data = self.get_node_resources(client, node_name)
                
                # Get GPU information
                gpu_total = 0
                gpu_used = 0
                try:
                    raw_resources = client.resources
                    gpu_info = raw_resources.get("gpu", {})
                    gpu_cards = gpu_info.get("cards", [])
                    gpu_total = len(gpu_cards)
                    
                    # Count GPUs in use
                    instances = client.instances.all()
                    for instance in instances:
                        if instance.status == "Running":
                            devices = getattr(instance, 'devices', {}) or {}
                            for device_name, device_config in devices.items():
                                if device_config.get('type') == 'gpu':
                                    gpu_used += 1
                                    break
                except Exception as gpu_err:
                    logger.debug(f"Could not get GPU info for {node_name}: {gpu_err}")
                
                status.resources = NodeResources(
                    cpu_total=resources_data.get("cpu_total", 0),
                    cpu_used=resources_data.get("cpu_total", 0) - resources_data.get("cpu_available", 0),
                    cpu_available=resources_data.get("cpu_available", 0),
                    cpu_sockets=resources_data.get("cpu_sockets", 0),
                    memory_total=resources_data.get("memory_total", 0),
                    memory_used=resources_data.get("memory_total", 0) - resources_data.get("memory_available", 0),
                    memory_available=resources_data.get("memory_available", 0),
                    gpu_total=gpu_total,
                    gpu_used=gpu_used,
                    gpu_available=gpu_total - gpu_used,
                    storage_total=resources_data.get("storage_total", 0),
                    storage_available=resources_data.get("storage_available", 0),
                    storage_pools=resources_data.get("storage_pools", []),
                )
            except Exception as res_err:
                logger.warning(f"Could not get resources for {node_name}: {res_err}")
        
        except Exception as e:
            status.error = str(e)
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
            # Find instance across all projects
            instance, found_project = find_instance_in_projects(client, instance_name)
            
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
            if state.cpu and state.cpu.get("usage"):
                current_usage_ns = state.cpu.get("usage")
                current_time = time.time()
                
                cache_key = f"{host}:{instance_name}"
                
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
                self._cpu_history[cache_key] = {
                    "usage_ns": current_usage_ns,
                    "timestamp": current_time
                }
            
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
