"""
API Router for Monitoring operations.
Provides endpoints for real-time cluster and instance metrics monitoring.
Based on LXD metrics API specification: CPU, memory, disk, network, processes.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional, List, Dict, Any, Tuple
import logging
import time
import asyncio
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from app.models.schemas import (
    ClusterMonitoringMetrics,
    InstanceMonitoringSummary,
    StoragePoolMetrics,
    NetworkMetrics,
    MonitoringDashboardResponse,
    MetricsHistoryPoint,
    WarningsSummary,
)
from app.services.lxd_service import (
    LXDService,
    LXDConnectionError,
    LXDOperationError,
    get_lxd_service,
)
from app.core.cache import get_cache_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/monitoring", tags=["Monitoring"])

# Thread pool for parallel instance state fetching
_executor = ThreadPoolExecutor(max_workers=10)

# In-memory storage for previous CPU samples (for delta calculation)
_previous_cpu_samples: Dict[str, Dict[str, Any]] = {}
_previous_sample_time: float = 0
_last_history_store_time: float = 0  # Track when we last stored to history
_history_min_interval: float = 4.0  # Minimum seconds between history stores

# Lock for thread-safe CPU sample access
import threading
_cpu_samples_lock = threading.Lock()


def _check_single_node_status(lxd_service: "LXDService", node_name: str) -> Optional[Any]:
    """Check status for a single node - runs in thread pool. Includes auto-reconnection."""
    from app.core.config import get_settings
    settings = get_settings()
    try:
        # Get endpoint from Vault metadata
        endpoint = "unknown"
        try:
            metadata = lxd_service.cert_manager.vault.vault_read(
                f"{settings.vault_clusters_path}/{node_name}/metadata"
            )
            endpoint = metadata.get("endpoint", "unknown")
        except Exception:
            pass
        
        # Use get_client_with_reconnect for automatic reconnection
        client = lxd_service.get_client_with_reconnect(node_name)
        if client:
            return lxd_service.resource_manager.check_node_status(client, node_name, endpoint)
        else:
            from app.services.lxd.resources import NodeStatus
            return NodeStatus(
                name=node_name,
                endpoint=endpoint,
                connected=False,
                error="Client not connected"
            )
    except Exception as e:
        from app.services.lxd.resources import NodeStatus
        logger.warning(f"Node {node_name} health check failed: {e}")
        return NodeStatus(
            name=node_name,
            endpoint="unknown",
            connected=False,
            error=str(e)
        )


async def _get_all_node_statuses_parallel(lxd_service: "LXDService") -> List[Any]:
    """Get all node statuses in parallel using thread pool.
    Uses Vault as source of truth for node list (not just cached connections).
    """
    # Use lxd_service.get_node_names() which gets nodes from Vault
    # This ensures we always check all registered nodes, not just cached ones
    node_names = lxd_service.get_node_names()
    if not node_names:
        logger.warning("No nodes found in Vault")
        return []
    
    logger.debug(f"Checking status for {len(node_names)} nodes from Vault: {node_names}")
    
    loop = asyncio.get_event_loop()
    tasks = [
        loop.run_in_executor(_executor, _check_single_node_status, lxd_service, node_name)
        for node_name in node_names
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Filter out exceptions
    statuses = []
    for result in results:
        if isinstance(result, Exception):
            logger.warning(f"Node status check failed with exception: {result}")
        elif result is not None:
            statuses.append(result)
    
    return statuses


def format_bytes(bytes_val: int) -> str:
    """Format bytes to human readable string."""
    if bytes_val == 0:
        return "0 B"
    units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    i = 0
    while bytes_val >= 1024 and i < len(units) - 1:
        bytes_val /= 1024
        i += 1
    return f"{bytes_val:.2f} {units[i]}"


def _fetch_instance_state(client, instance_name: str, project: str, host: str) -> Optional[Dict[str, Any]]:
    """Fetch instance state from LXD - runs in thread pool."""
    try:
        state_resp = client.api.instances[instance_name].state.get(params={'project': project})
        state_data = state_resp.json().get('metadata', {})
        
        # Extract CPU usage in nanoseconds (cumulative)
        cpu_usage_ns = 0
        if state_data.get('cpu'):
            cpu_usage_ns = state_data['cpu'].get('usage', 0)
        
        memory_usage = state_data.get('memory', {}).get('usage', 0)
        memory_peak = state_data.get('memory', {}).get('usage_peak', 0)
        
        disk_usage = {}
        if state_data.get('disk'):
            disk_usage = {k: v.get('usage', 0) for k, v in state_data['disk'].items()}
        
        network_rx = 0
        network_tx = 0
        if state_data.get('network'):
            for iface, data in state_data['network'].items():
                if iface != 'lo':
                    counters = data.get('counters', {})
                    network_rx += counters.get('bytes_received', 0)
                    network_tx += counters.get('bytes_sent', 0)
        
        processes = state_data.get('processes', 0)
        
        return {
            'name': instance_name,
            'host': host,
            'project': project,
            'cpu_usage_ns': cpu_usage_ns,  # Cumulative nanoseconds
            'memory_usage': memory_usage,
            'memory_peak': memory_peak,
            'disk_usage': sum(disk_usage.values()),
            'network_rx': network_rx,
            'network_tx': network_tx,
            'processes': processes,
        }
    except Exception as e:
        logger.debug(f"Could not fetch state for {instance_name}: {e}")
        return None


def _calculate_cpu_percentage(
    instance_key: str,
    current_cpu_ns: int,
    current_time: float,
    num_cpus: int = 1
) -> float:
    """
    Calculate CPU usage percentage based on delta from previous sample.
    
    CPU % = (delta_cpu_ns / (delta_time_ns * num_cpus)) * 100
    
    Returns percentage (0-100) normalized across all CPUs.
    Thread-safe with lock protection.
    """
    global _previous_cpu_samples, _previous_sample_time
    
    with _cpu_samples_lock:
        if instance_key not in _previous_cpu_samples:
            # No previous sample, store current and return 0
            _previous_cpu_samples[instance_key] = {
                'cpu_ns': current_cpu_ns,
                'time': current_time
            }
            return 0.0
        
        prev = _previous_cpu_samples[instance_key]
        prev_cpu_ns = prev['cpu_ns']
        prev_time = prev['time']
        
        # Calculate deltas
        delta_cpu_ns = current_cpu_ns - prev_cpu_ns
        delta_time_s = current_time - prev_time
        
        # Update stored sample
        _previous_cpu_samples[instance_key] = {
            'cpu_ns': current_cpu_ns,
            'time': current_time
        }
    
    # Calculate outside lock for better concurrency
    if delta_time_s <= 0 or delta_cpu_ns < 0:
        return 0.0
    
    # Ensure num_cpus is at least 1
    num_cpus = max(1, num_cpus)
    
    # Convert time delta to nanoseconds
    delta_time_ns = delta_time_s * 1e9
    
    # Calculate percentage normalized by number of CPUs
    # This gives us 0-100% where 100% means all CPUs are fully utilized
    cpu_percent = (delta_cpu_ns / (delta_time_ns * num_cpus)) * 100.0
    
    # Clamp to 0-100%
    return min(max(cpu_percent, 0.0), 100.0)


async def _get_running_instance_metrics(
    lxd_service: LXDService, 
    running_vms: List[Any],
    max_instances: int = 100
) -> Tuple[List[Dict[str, Any]], float]:
    """
    Fetch real-time metrics for running instances in parallel.
    Returns tuple of (metrics_list, current_timestamp).
    Uses balanced sampling across nodes to ensure each node gets representation.
    """
    current_time = time.time()
    instance_metrics = []
    
    # Group instances by host for balanced sampling
    instances_by_host: Dict[str, List[Tuple[str, Any]]] = {}
    for vm in running_vms:
        host = vm.host
        if host not in instances_by_host:
            instances_by_host[host] = []
        instances_by_host[host].append((vm.name, vm))
    
    # Calculate instances per host for balanced sampling
    num_hosts = len(instances_by_host)
    if num_hosts == 0:
        return [], current_time
    
    instances_per_host = max_instances // num_hosts if num_hosts > 0 else max_instances
    remaining = max_instances % num_hosts
    
    # Select instances from each host proportionally
    selected_vms = []
    for idx, (host, host_instances) in enumerate(instances_by_host.items()):
        # Give extra instance to first N hosts to use up remainder
        host_quota = instances_per_host + (1 if idx < remaining else 0)
        selected_vms.extend([vm for _, vm in host_instances[:host_quota]])
    
    logger.debug(f"Balanced sampling: {len(selected_vms)} instances from {num_hosts} hosts (limit={max_instances})")
    
    # Now group selected instances by host for efficient client reuse
    instances_by_host_final: Dict[str, List[Tuple[str, str, int]]] = {}
    for vm in selected_vms:
        host = vm.host
        if host not in instances_by_host_final:
            instances_by_host_final[host] = []
        project = vm.config.get('project', 'default') if vm.config else 'default'
        # Get CPU count from config if available
        cpu_count = 1
        if vm.config:
            cpu_limit = vm.config.get('limits.cpu', '1')
            try:
                cpu_count = int(cpu_limit)
            except (ValueError, TypeError):
                cpu_count = 1
        instances_by_host_final[host].append((vm.name, project, cpu_count))
    
    loop = asyncio.get_event_loop()
    tasks = []
    instance_info = []  # Track (name, host, cpu_count) for each task
    
    for host, instance_list in instances_by_host_final.items():
        # Use get_client_with_reconnect to ensure we have valid connections
        client = lxd_service.get_client_with_reconnect(host)
        if not client:
            logger.warning(f"No client available for host {host}, skipping metrics")
            continue
        
        for instance_name, project, cpu_count in instance_list:
            task = loop.run_in_executor(
                _executor,
                _fetch_instance_state,
                client,
                instance_name,
                project,
                host
            )
            tasks.append(task)
            instance_info.append((instance_name, host, cpu_count))
    
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, dict):
                # Calculate real-time CPU percentage
                instance_key = f"{result['host']}:{result['name']}"
                cpu_count = instance_info[i][2] if i < len(instance_info) else 1
                cpu_percent = _calculate_cpu_percentage(
                    instance_key,
                    result['cpu_usage_ns'],
                    current_time,
                    cpu_count
                )
                result['cpu_percent'] = cpu_percent
                result['cpu_count'] = cpu_count
                instance_metrics.append(result)
    
    # Log summary (debug only)
    if instance_metrics and logger.isEnabledFor(logging.DEBUG):
        top3 = sorted(instance_metrics, key=lambda x: x.get('cpu_percent', 0), reverse=True)[:3]
        for m in top3:
            logger.debug(f"CPU: {m['name']} -> {m['cpu_percent']:.2f}% (cpus={m['cpu_count']})")
    
    return instance_metrics, current_time


# Cache for dashboard response - set slightly longer than background collector interval (30s)
# This ensures cache is always warm from background collection
_dashboard_cache: Dict[str, Any] = {}
_dashboard_cache_time: float = 0
_dashboard_cache_ttl: float = 35.0  # Cache dashboard for 35 seconds (background collector runs every 30s)


@router.get(
    "/dashboard",
    response_model=MonitoringDashboardResponse,
    summary="Get monitoring dashboard data",
    description="Get comprehensive monitoring data including cluster metrics, instance stats, storage, and network info.",
)
async def get_monitoring_dashboard(
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """
    Get comprehensive monitoring dashboard data.
    Returns aggregated metrics across all clusters and instances.
    Uses background collector caching (35s TTL) - cache is pre-warmed on startup.
    Set fresh=true to bypass cache.
    """
    global _dashboard_cache, _dashboard_cache_time
    
    # Check if we have a valid cached response (warm from background collector)
    current_time = time.time()
    if not fresh and _dashboard_cache and (current_time - _dashboard_cache_time) < _dashboard_cache_ttl:
        logger.debug("Returning cached dashboard response (background collector)")
        return _dashboard_cache
    
    try:
        start_time = time.time()
        
        # Get all VMs/instances and node statuses IN PARALLEL
        all_vms_task = lxd_service.list_all_vms()
        node_statuses_task = _get_all_node_statuses_parallel(lxd_service)
        
        # Wait for both to complete
        (all_vms, connected_nodes), node_statuses_list = await asyncio.gather(
            all_vms_task, node_statuses_task
        )
        
        # Calculate instance statistics
        total_instances = len(all_vms)
        running_instances = len([vm for vm in all_vms if vm.status.value == "Running"])
        stopped_instances = len([vm for vm in all_vms if vm.status.value == "Stopped"])
        other_instances = total_instances - running_instances - stopped_instances
        
        containers = len([vm for vm in all_vms if vm.type == "container"])
        virtual_machines = len([vm for vm in all_vms if vm.type == "virtual-machine"])
        
        # Calculate resource totals from all connected nodes
        total_cpu = 0
        used_cpu = 0
        total_memory = 0
        used_memory = 0
        total_storage = 0
        used_storage = 0
        total_gpu = 0
        used_gpu = 0
        
        storage_pools_list: List[StoragePoolMetrics] = []
        nodes_metrics: List[ClusterMonitoringMetrics] = []
        
        online_nodes = 0
        offline_nodes = 0
        
        for node in node_statuses_list:
            if node.connected:
                online_nodes += 1
                if node.resources:
                    resources = node.resources
                    total_cpu += resources.cpu_total
                    used_cpu += resources.cpu_used
                    total_memory += resources.memory_total
                    used_memory += resources.memory_used
                    total_storage += resources.storage_total
                    used_storage += resources.storage_total - resources.storage_available
                    total_gpu += resources.gpu_total
                    used_gpu += resources.gpu_used
                    
                    # Aggregate storage pools
                    for pool in resources.storage_pools:
                        storage_pools_list.append(StoragePoolMetrics(
                            name=pool.get("name", "unknown"),
                            node=node.name,
                            driver=pool.get("driver", "unknown"),
                            total_bytes=pool.get("space_total", 0),
                            used_bytes=pool.get("space_used", 0),
                            available_bytes=pool.get("space_available", 0),
                            usage_percent=round(
                                (pool.get("space_used", 0) / pool.get("space_total", 1)) * 100, 2
                            ) if pool.get("space_total", 0) > 0 else 0,
                        ))
                    
                    # Create node metrics
                    nodes_metrics.append(ClusterMonitoringMetrics(
                        node_name=node.name,
                        node_type=node.node_type or "lxd",
                        is_online=True,
                        cpu_total=resources.cpu_total,
                        cpu_used=resources.cpu_used,
                        cpu_usage_percent=round(
                            (resources.cpu_used / resources.cpu_total * 100) if resources.cpu_total > 0 else 0, 2
                        ),
                        memory_total_bytes=resources.memory_total,
                        memory_used_bytes=resources.memory_used,
                        memory_usage_percent=round(
                            (resources.memory_used / resources.memory_total * 100) if resources.memory_total > 0 else 0, 2
                        ),
                        storage_total_bytes=resources.storage_total,
                        storage_used_bytes=resources.storage_total - resources.storage_available,
                        storage_usage_percent=round(
                            ((resources.storage_total - resources.storage_available) / resources.storage_total * 100) if resources.storage_total > 0 else 0, 2
                        ),
                        gpu_total=resources.gpu_total,
                        gpu_used=resources.gpu_used,
                        instance_count=len([vm for vm in all_vms if vm.host == node.name]),
                        running_instances=len([vm for vm in all_vms if vm.host == node.name and vm.status.value == "Running"]),
                    ))
            else:
                offline_nodes += 1
                nodes_metrics.append(ClusterMonitoringMetrics(
                    node_name=node.name,
                    node_type=node.node_type or "lxd",
                    is_online=False,
                    cpu_total=0,
                    cpu_used=0,
                    cpu_usage_percent=0,
                    memory_total_bytes=0,
                    memory_used_bytes=0,
                    memory_usage_percent=0,
                    storage_total_bytes=0,
                    storage_used_bytes=0,
                    storage_usage_percent=0,
                    gpu_total=0,
                    gpu_used=0,
                    instance_count=0,
                    running_instances=0,
                ))
        
        # Calculate overall percentages
        cpu_usage_percent = round((used_cpu / total_cpu * 100) if total_cpu > 0 else 0, 2)
        memory_usage_percent = round((used_memory / total_memory * 100) if total_memory > 0 else 0, 2)
        storage_usage_percent = round((used_storage / total_storage * 100) if total_storage > 0 else 0, 2)
        
        # Get REAL-TIME metrics for running instances (top consumers)
        running_vms = [vm for vm in all_vms if vm.status.value == "Running"]
        logger.debug(f"Fetching real-time metrics for {len(running_vms)} running instances")
        
        # Increase limit to ensure we get metrics from all nodes proportionally
        # This prevents issue where selecting a specific node shows no data
        # because its instances weren't in the first 30
        max_instances_to_fetch = min(len(running_vms), 100)  # Fetch up to 100 instances
        
        instance_metrics, sample_time = await _get_running_instance_metrics(
            lxd_service, 
            running_vms, 
            max_instances=max_instances_to_fetch
        )
        
        logger.debug(f"Got metrics for {len(instance_metrics)} instances")
        
        # Build instance summaries from real-time metrics
        instance_summaries: List[InstanceMonitoringSummary] = []
        for metrics in instance_metrics:
            # Find the VM info for type
            vm_info = next((vm for vm in running_vms if vm.name == metrics['name'] and vm.host == metrics['host']), None)
            vm_type = vm_info.type if vm_info else "container"
            
            instance_summaries.append(InstanceMonitoringSummary(
                name=metrics['name'],
                host=metrics['host'],
                type=vm_type,
                status="Running",
                cpu_usage=metrics.get('cpu_percent', 0),  # Now using calculated percentage
                memory_usage_bytes=metrics.get('memory_usage', 0),
                memory_limit_bytes=metrics.get('memory_peak', 0),
                disk_usage_bytes=metrics.get('disk_usage', 0),
                network_rx_bytes=metrics.get('network_rx', 0),
                network_tx_bytes=metrics.get('network_tx', 0),
            ))
        
        # Sort by CPU percentage for top consumers (now real-time percentage)
        # Return up to 20 consumers - frontend handles topN slicing
        cpu_sorted = sorted(instance_summaries, key=lambda x: x.cpu_usage or 0, reverse=True)
        top_cpu_consumers = cpu_sorted[:20]
        
        # Sort by memory for top memory consumers  
        # Return up to 20 consumers - frontend handles topN slicing
        mem_sorted = sorted(instance_summaries, key=lambda x: x.memory_usage_bytes or 0, reverse=True)
        top_memory_consumers = mem_sorted[:20]
        
        # Calculate real-time aggregate metrics from instance data
        realtime_cpu_percent = 0.0
        realtime_memory_bytes = 0
        
        if instance_metrics:
            # Average CPU usage across all monitored instances
            cpu_values = [m.get('cpu_percent', 0) for m in instance_metrics]
            if cpu_values:
                realtime_cpu_percent = round(sum(cpu_values) / len(cpu_values), 2)
            
            # Sum of memory usage from all instances
            realtime_memory_bytes = sum(m.get('memory_usage', 0) for m in instance_metrics)
        
        # Calculate real-time memory percentage
        realtime_memory_percent = round(
            (realtime_memory_bytes / total_memory * 100) if total_memory > 0 else 0, 2
        )
        
        # Get warnings summary
        warnings_summary = WarningsSummary(
            total=0,
            critical=0,
            warning=0,
            info=0,
        )
        
        try:
            # Try to get warnings from each connected node
            for node in node_statuses_list:
                if node.connected:
                    try:
                        # Use get_client_with_reconnect for resilience
                        client = lxd_service.get_client_with_reconnect(node.name)
                        if client:
                            warnings_response = client.api.warnings.get()
                            warnings_data = warnings_response.json().get("metadata", [])
                            for warning in warnings_data:
                                if isinstance(warning, dict):
                                    warnings_summary.total += 1
                                    severity = warning.get("severity", "").lower()
                                    if severity in ["critical", "error"]:
                                        warnings_summary.critical += 1
                                    elif severity == "warning":
                                        warnings_summary.warning += 1
                                    else:
                                        warnings_summary.info += 1
                    except Exception as warn_err:
                        logger.debug(f"Could not get warnings for node {node.name}: {warn_err}")
        except Exception as e:
            logger.debug(f"Could not fetch warnings: {e}")
        
        elapsed_time = time.time() - start_time
        
        response = MonitoringDashboardResponse(
            timestamp=datetime.now(timezone.utc),
            
            # Cluster Overview
            total_nodes=len(node_statuses_list),
            online_nodes=online_nodes,
            offline_nodes=offline_nodes,
            
            # Instance Overview
            total_instances=total_instances,
            running_instances=running_instances,
            stopped_instances=stopped_instances,
            other_instances=other_instances,
            container_count=containers,
            vm_count=virtual_machines,
            
            # Resource Totals (Allocation)
            total_cpu=total_cpu,
            used_cpu=used_cpu,
            cpu_usage_percent=cpu_usage_percent,
            
            # Real-time CPU usage
            realtime_cpu_percent=realtime_cpu_percent,
            
            total_memory_bytes=total_memory,
            used_memory_bytes=used_memory,
            memory_usage_percent=memory_usage_percent,
            
            # Real-time memory usage
            realtime_memory_bytes=realtime_memory_bytes,
            realtime_memory_percent=realtime_memory_percent,
            
            total_storage_bytes=total_storage,
            used_storage_bytes=used_storage,
            storage_usage_percent=storage_usage_percent,
            
            total_gpu=total_gpu,
            used_gpu=used_gpu,
            
            # Detailed Metrics
            nodes=nodes_metrics,
            storage_pools=storage_pools_list,
            top_cpu_consumers=top_cpu_consumers[:20],
            top_memory_consumers=top_memory_consumers[:20],
            
            # Warnings
            warnings=warnings_summary,
            
            # Meta
            collection_time_ms=round(elapsed_time * 1000, 2),
        )
        
        # Store metrics in history for charts (append to rolling buffer)
        # Rate-limited to avoid duplicate entries from concurrent calls
        global _last_history_store_time
        current_time = time.time()
        
        if current_time - _last_history_store_time >= _history_min_interval:
            try:
                _last_history_store_time = current_time
                
                cache = get_cache_manager()
                history_key = "monitoring:history"
                history = cache.get("monitoring_history", history_key) or []
                
                # Calculate aggregate real-time CPU usage from all instances
                # This gives us actual CPU activity, not just allocation
                total_instance_cpu_percent = sum(m.get('cpu_percent', 0) for m in instance_metrics)
                avg_instance_cpu_percent = total_instance_cpu_percent / len(instance_metrics) if instance_metrics else 0
                
                # Calculate aggregate memory usage from instances (as % of total cluster memory)
                total_instance_memory = sum(m.get('memory_usage', 0) for m in instance_metrics)
                instance_memory_percent = (total_instance_memory / total_memory * 100) if total_memory > 0 else 0
                
                # Build per-node metrics for the history point
                per_node_metrics = {}
                for node_metric in nodes_metrics:
                    node_name = node_metric.node_name
                    # Get instances for this node
                    node_instances = [m for m in instance_metrics if m.get('host') == node_name]
                    node_cpu_total = sum(m.get('cpu_percent', 0) for m in node_instances)
                    node_cpu_avg = node_cpu_total / len(node_instances) if node_instances else 0
                    node_memory_total = sum(m.get('memory_usage', 0) for m in node_instances)
                    node_memory_percent = (node_memory_total / node_metric.memory_total_bytes * 100) if node_metric.memory_total_bytes > 0 else 0
                    
                    per_node_metrics[node_name] = {
                        "cpu_usage_percent": round(node_cpu_avg, 2),
                        "memory_usage_percent": round(node_memory_percent, 2),
                        "storage_usage_percent": node_metric.storage_usage_percent,
                        "network_rx_bytes": sum(m.get('network_rx', 0) for m in node_instances),
                        "network_tx_bytes": sum(m.get('network_tx', 0) for m in node_instances),
                        "running_instances": len([m for m in node_instances]),
                    }
            
                new_point = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "cpu_usage_percent": round(avg_instance_cpu_percent, 2),  # Real-time avg CPU across instances
                    "memory_usage_percent": round(instance_memory_percent, 2),  # Real-time memory usage
                    "storage_usage_percent": storage_usage_percent,  # Storage usage
                    "network_rx_bytes": sum(m.get('network_rx', 0) for m in instance_metrics),
                    "network_tx_bytes": sum(m.get('network_tx', 0) for m in instance_metrics),
                    "disk_io_bytes": 0,
                    "running_instances": running_instances,
                    "nodes": per_node_metrics,  # Per-node breakdown
                }
                history.append(new_point)
                
                # Keep last 8640 points (24 hours at 10s interval, or 72 hours at 30s)
                if len(history) > 8640:
                    history = history[-8640:]
                
                cache.set("monitoring_history", history_key, history, ttl=86400)  # 24 hour TTL
            except Exception as hist_err:
                logger.debug(f"Could not update history: {hist_err}")
        
        logger.debug(f"Monitoring dashboard collected in {elapsed_time*1000:.0f}ms")
        
        # Cache the response for subsequent requests
        _dashboard_cache.clear()
        _dashboard_cache.update(response.model_dump())
        _dashboard_cache_time = time.time()
        
        return response
        
    except LXDConnectionError as e:
        logger.error(f"LXD connection error in monitoring: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting monitoring dashboard: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/nodes/{node_name}/metrics",
    response_model=ClusterMonitoringMetrics,
    summary="Get metrics for a specific node",
    description="Get detailed metrics for a specific cluster node.",
)
async def get_node_metrics(
    node_name: str,
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get detailed metrics for a specific node."""
    try:
        node_statuses_list = lxd_service.check_nodes_status()
        
        node = next((n for n in node_statuses_list if n.name == node_name), None)
        if not node:
            raise HTTPException(status_code=404, detail=f"Node '{node_name}' not found")
        
        if not node.connected:
            return ClusterMonitoringMetrics(
                node_name=node_name,
                node_type=node.node_type or "lxd",
                is_online=False,
                cpu_total=0,
                cpu_used=0,
                cpu_usage_percent=0,
                memory_total_bytes=0,
                memory_used_bytes=0,
                memory_usage_percent=0,
                storage_total_bytes=0,
                storage_used_bytes=0,
                storage_usage_percent=0,
                gpu_total=0,
                gpu_used=0,
                instance_count=0,
                running_instances=0,
            )
        
        resources = node.resources
        if not resources:
            raise HTTPException(status_code=500, detail="Node resources not available")
        
        # Get instance count for this node
        all_vms, _ = await lxd_service.list_all_vms()
        node_instances = [vm for vm in all_vms if vm.host == node_name]
        
        return ClusterMonitoringMetrics(
            node_name=node_name,
            node_type=node.node_type or "lxd",
            is_online=True,
            cpu_total=resources.cpu_total,
            cpu_used=resources.cpu_used,
            cpu_usage_percent=round(
                (resources.cpu_used / resources.cpu_total * 100) if resources.cpu_total > 0 else 0, 2
            ),
            memory_total_bytes=resources.memory_total,
            memory_used_bytes=resources.memory_used,
            memory_usage_percent=round(
                (resources.memory_used / resources.memory_total * 100) if resources.memory_total > 0 else 0, 2
            ),
            storage_total_bytes=resources.storage_total,
            storage_used_bytes=resources.storage_total - resources.storage_available,
            storage_usage_percent=round(
                ((resources.storage_total - resources.storage_available) / resources.storage_total * 100) if resources.storage_total > 0 else 0, 2
            ),
            gpu_total=resources.gpu_total,
            gpu_used=resources.gpu_used,
            instance_count=len(node_instances),
            running_instances=len([vm for vm in node_instances if vm.status.value == "Running"]),
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting node metrics: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/history",
    response_model=List[MetricsHistoryPoint],
    summary="Get metrics history",
    description="Get historical metrics data for graphing. Returns last N data points.",
)
async def get_metrics_history(
    points: int = Query(default=60, ge=1, le=8640, description="Number of historical points to return (max 8640 for 24h)"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """
    Get historical metrics for graphing.
    History is updated by the dashboard endpoint, this just reads from cache.
    """
    cache = get_cache_manager()
    history_key = "monitoring:history"
    
    # Get existing history from cache (written by dashboard endpoint)
    history = cache.get("monitoring_history", history_key) or []
    
    if not history:
        # If no history, return empty list - dashboard will populate it
        return []
    
    try:
        # Return requested number of points
        result = [MetricsHistoryPoint(**p) for p in history[-points:]]
        return result
    except Exception as e:
        logger.error(f"Error parsing metrics history: {e}")
        return []
        
    except Exception as e:
        logger.error(f"Error getting metrics history: {e}")
        # Return existing history if available
        if history:
            return [MetricsHistoryPoint(**p) for p in history[-points:]]
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/network",
    response_model=NetworkMetrics,
    summary="Get network metrics",
    description="Get aggregated network I/O metrics across all instances.",
)
async def get_network_metrics(
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get aggregated network metrics."""
    try:
        all_vms, _ = await lxd_service.list_all_vms()
        
        total_rx = 0
        total_tx = 0
        total_rx_packets = 0
        total_tx_packets = 0
        
        interface_stats: Dict[str, Dict[str, int]] = {}
        
        for vm in all_vms:
            if vm.status.value == "Running" and vm.resources and vm.resources.network_counters:
                for iface, counters in vm.resources.network_counters.items():
                    rx = counters.get("bytes_received", 0)
                    tx = counters.get("bytes_sent", 0)
                    rx_packets = counters.get("packets_received", 0)
                    tx_packets = counters.get("packets_sent", 0)
                    
                    total_rx += rx
                    total_tx += tx
                    total_rx_packets += rx_packets
                    total_tx_packets += tx_packets
                    
                    if iface not in interface_stats:
                        interface_stats[iface] = {
                            "rx_bytes": 0,
                            "tx_bytes": 0,
                            "rx_packets": 0,
                            "tx_packets": 0,
                        }
                    interface_stats[iface]["rx_bytes"] += rx
                    interface_stats[iface]["tx_bytes"] += tx
                    interface_stats[iface]["rx_packets"] += rx_packets
                    interface_stats[iface]["tx_packets"] += tx_packets
        
        return NetworkMetrics(
            total_rx_bytes=total_rx,
            total_tx_bytes=total_tx,
            total_rx_packets=total_rx_packets,
            total_tx_packets=total_tx_packets,
            interfaces=interface_stats,
            timestamp=datetime.now(timezone.utc),
        )
        
    except Exception as e:
        logger.error(f"Error getting network metrics: {e}")
        raise HTTPException(status_code=500, detail=str(e))
