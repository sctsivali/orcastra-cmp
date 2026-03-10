"""
API Router for Monitoring operations.
Provides endpoints for real-time cluster and instance metrics monitoring.
Based on LXD metrics API specification: CPU, memory, disk, network, processes.
"""

from fastapi import APIRouter, HTTPException, Depends, Query, WebSocket, WebSocketDisconnect
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
from app.core.type_utils import get_cli_command_prefix, get_trust_add_subcommand
from app.core.dependencies import require_tenant
from app.core.org_scope import org_scope_dependency, verify_cluster_access, get_scope_cache_key, check_tenant_permission

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/monitoring",
    tags=["Monitoring"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

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


# Cache for dashboard response - keyed by org-scope suffix
# This ensures partner users never get admin-cached data
_dashboard_cache: Dict[str, Dict[str, Any]] = {}
_dashboard_cache_times: Dict[str, float] = {}
_dashboard_cache_ttl: float = 35.0  # Cache dashboard for 35 seconds (background collector runs every 30s)
_dashboard_cache_lock = asyncio.Lock()  # Prevent thundering herd on concurrent requests


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
    global _dashboard_cache, _dashboard_cache_times
    
    # Build a scope key so each org gets its own cached snapshot
    scope_key = get_scope_cache_key("dashboard")
    
    # Check cache outside lock first (fast path)
    current_time = time.time()
    cached_time = _dashboard_cache_times.get(scope_key, 0)
    if not fresh and scope_key in _dashboard_cache and (current_time - cached_time) < _dashboard_cache_ttl:
        logger.debug("Returning cached dashboard response (background collector)")
        return _dashboard_cache[scope_key]
    
    # Acquire lock to prevent thundering herd - only one request fetches fresh data
    async with _dashboard_cache_lock:
        # Double-check cache inside lock (another request may have populated it)
        current_time = time.time()
        cached_time = _dashboard_cache_times.get(scope_key, 0)
        if not fresh and scope_key in _dashboard_cache and (current_time - cached_time) < _dashboard_cache_ttl:
            return _dashboard_cache[scope_key]
        # Pre-set timestamp to prevent other requests from also starting computation
        _dashboard_cache_times[scope_key] = current_time
    
    try:
        start_time = time.time()
        
        # Get all VMs/instances and node statuses IN PARALLEL
        all_vms_task = lxd_service.list_all_vms()
        node_statuses_task = _get_all_node_statuses_parallel(lxd_service)
        
        # Wait for both to complete
        (all_vms, connected_nodes), node_statuses_list = await asyncio.gather(
            all_vms_task, node_statuses_task
        )
        
        # apply tenant ACL filtering
        all_vms = [
            vm for vm in all_vms
            if check_tenant_permission(
                vm.host,
                vm.project or "default",
                vm.name,
                "view",
            )
        ]
        
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
        inactive_nodes = 0
        
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
                        cluster_status="online",
                        status_message="Connected and authenticated",
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
                inactive_nodes += 1
                # Determine cluster status based on error message
                error_msg = node.error or ""
                cli_prefix = get_cli_command_prefix(node.node_type)
                trust_subcmd = get_trust_add_subcommand(node.node_type)
                if "not authorized" in error_msg.lower() or "untrusted" in error_msg.lower() or "certificate" in error_msg.lower():
                    cluster_status = "pending_trust"
                    status_message = f"Certificate not yet trusted. Run '{cli_prefix} config trust {trust_subcmd}' on the cluster."
                elif not error_msg:
                    # No error but not connected - likely pending trust
                    cluster_status = "pending_trust"
                    status_message = "Waiting for certificate trust. Check cluster registration."
                elif "connection refused" in error_msg.lower() or "timeout" in error_msg.lower():
                    cluster_status = "offline"
                    status_message = f"Node unreachable: {error_msg}"
                else:
                    cluster_status = "error"
                    status_message = error_msg or "Unknown error"
                
                nodes_metrics.append(ClusterMonitoringMetrics(
                    node_name=node.name,
                    node_type=node.node_type or "lxd",
                    is_online=False,
                    cluster_status=cluster_status,
                    status_message=status_message,
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
            # fetch warnings from all connected nodes in parallel
            def _fetch_node_warnings(node_name: str) -> List[Dict[str, Any]]:
                """Fetch warnings for a single node (runs in thread pool)."""
                try:
                    client = lxd_service.get_client_with_reconnect(node_name)
                    if client:
                        warnings_response = client.api.warnings.get(params={'recursion': '1'})
                        return warnings_response.json().get("metadata", [])
                except Exception as warn_err:
                    logger.debug(f"Could not get warnings for node {node_name}: {warn_err}")
                return []

            connected_node_names = [n.name for n in node_statuses_list if n.connected]
            if connected_node_names:
                loop = asyncio.get_event_loop()
                warning_tasks = [
                    loop.run_in_executor(_executor, _fetch_node_warnings, name)
                    for name in connected_node_names
                ]
                warning_results = await asyncio.gather(*warning_tasks, return_exceptions=True)
                for result in warning_results:
                    if isinstance(result, list):
                        for warning in result:
                            if isinstance(warning, dict):
                                warnings_summary.total += 1
                                severity = warning.get("severity", "").lower()
                                if severity in ["critical", "error"]:
                                    warnings_summary.critical += 1
                                elif severity == "warning":
                                    warnings_summary.warning += 1
                                else:
                                    warnings_summary.info += 1
        except Exception as e:
            logger.debug(f"Could not fetch warnings: {e}")
        
        elapsed_time = time.time() - start_time
        
        response = MonitoringDashboardResponse(
            timestamp=datetime.now(timezone.utc),
            
            # Cluster Overview
            total_nodes=len(node_statuses_list),
            online_nodes=online_nodes,
            inactive_nodes=inactive_nodes,
            
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
        # Only write from admin/background scope (scope=all) to ensure continuous
        # history without gaps. Scoped user requests read from this same key.
        # This fixes the issue where history had gaps when users navigated away,
        # because user-scoped history was only written during active sessions.
        global _last_history_store_time
        current_time = time.time()
        
        if scope_key == "dashboard:scope=all" and current_time - _last_history_store_time >= _history_min_interval:
            try:
                _last_history_store_time = current_time
                
                cache = get_cache_manager()
                history_key = "monitoring:history:scope=all"
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
                
                # Persist to PostgreSQL (fire-and-forget, non-blocking)
                # DB is source of truth; cache is hot-path for performance
                asyncio.create_task(_persist_snapshot(new_point))
                
            except Exception as hist_err:
                logger.debug(f"Could not update history: {hist_err}")
        
        logger.debug(f"Monitoring dashboard collected in {elapsed_time*1000:.0f}ms")
        
        # Cache the response for subsequent requests (per-scope)
        _dashboard_cache[scope_key] = response.model_dump()
        _dashboard_cache_times[scope_key] = time.time()
        
        return response
        
    except LXDConnectionError as e:
        _dashboard_cache_times.pop(scope_key, None)  # Allow retry on next request
        logger.error(f"LXD connection error in monitoring: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        _dashboard_cache_times.pop(scope_key, None)  # Allow retry on next request
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
    """Get detailed metrics for a specific node. Cached for 30 seconds."""
    verify_cluster_access(node_name)
    
    # check cache first
    cache = get_cache_manager()
    cache_key = get_scope_cache_key(f"monitoring:node:{node_name}")
    cached = cache.get("api_responses", cache_key)
    if cached is not None:
        return ClusterMonitoringMetrics(**cached)

    try:
        # run node status + VM list in parallel to eliminate waterfall
        node_statuses_task = _get_all_node_statuses_parallel(lxd_service)
        vms_task = lxd_service.list_all_vms()
        node_statuses_list, (all_vms, _) = await asyncio.gather(node_statuses_task, vms_task)

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
        
        # instance count already fetched in parallel above
        node_instances = [vm for vm in all_vms if vm.host == node_name]
        
        result = ClusterMonitoringMetrics(
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
        
        # cache for 30 seconds
        cache.set("api_responses", cache_key, result.model_dump(), ttl=30)
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting node metrics: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ──────────────────────────────────────────────
# DB persistence helper (fire-and-forget)
# ──────────────────────────────────────────────

async def _persist_snapshot(point: Dict[str, Any]) -> None:
    """Non-blocking write to PostgreSQL. Errors are logged, never raised."""
    try:
        from app.services.metrics_history_service import persist_metrics_snapshot
        await persist_metrics_snapshot(point, scope="all")
    except Exception as e:
        logger.debug(f"Snapshot DB write failed (cache still has the data): {e}")


# Cache for history endpoint responses to avoid repeated Pydantic model creation
# Key: (scope_key, points) -> Value: (history_len, list of MetricsHistoryPoint)
# Bounded to 50 entries to prevent unbounded memory growth
_history_response_cache: Dict[str, Tuple[int, List[MetricsHistoryPoint]]] = {}
_HISTORY_CACHE_MAX_SIZE = 50


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

    Data flow (hot -> cold):
      1. In-memory cache (instant, populated by background collector)
      2. PostgreSQL (survives restarts, 7-day retention)
      3. Empty list (first startup, dashboard will populate on next cycle)

    All users read from the global 'scope=all' stream so charts are
    gap-free regardless of individual session activity.
    """
    cache = get_cache_manager()
    history_key = "monitoring:history:scope=all"
    history = cache.get("monitoring_history", history_key) or []
    
    if not history:
        # Cache empty (backend just restarted?) - hydrate from PostgreSQL
        try:
            from app.services.metrics_history_service import load_metrics_history
            history = await load_metrics_history(points=points, scope="all")
            if history:
                # Re-populate the in-memory cache so subsequent requests are instant
                cache.set("monitoring_history", history_key, history, ttl=86400)
                logger.info(f"Hydrated metrics cache from DB ({len(history)} points)")
        except Exception as db_err:
            logger.warning(f"DB history fallback failed: {db_err}")
    
    if not history:
        return []
    
    try:
        # Check response cache: avoid re-creating Pydantic models if history hasn't changed
        cache_key = f"{history_key}:{points}"
        current_len = len(history)
        cached_entry = _history_response_cache.get(cache_key)
        if cached_entry and cached_entry[0] == current_len:
            return cached_entry[1]
        
        # Create Pydantic models and cache the result
        result = [MetricsHistoryPoint(**p) for p in history[-points:]]
        # Evict oldest entries if cache exceeds max size
        if len(_history_response_cache) >= _HISTORY_CACHE_MAX_SIZE:
            oldest_key = next(iter(_history_response_cache))
            del _history_response_cache[oldest_key]
        _history_response_cache[cache_key] = (current_len, result)
        return result
    except Exception as e:
        logger.error(f"Error parsing metrics history: {e}")
        if history:
            try:
                return [MetricsHistoryPoint(**p) for p in history[-points:]]
            except Exception:
                pass
        return []


@router.get(
    "/network",
    response_model=NetworkMetrics,
    summary="Get network metrics",
    description="Get aggregated network I/O metrics across all instances.",
)
async def get_network_metrics(
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get aggregated network metrics. Cached for 30 seconds."""
    # check cache first
    cache = get_cache_manager()
    cache_key = get_scope_cache_key("monitoring:network")
    cached = cache.get("api_responses", cache_key)
    if cached is not None:
        return NetworkMetrics(**cached)

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
        
        response = NetworkMetrics(
            total_rx_bytes=total_rx,
            total_tx_bytes=total_tx,
            total_rx_packets=total_rx_packets,
            total_tx_packets=total_tx_packets,
            interfaces=interface_stats,
            timestamp=datetime.now(timezone.utc),
        )
        
        # cache for 30 seconds
        cache.set("api_responses", cache_key, response.model_dump(), ttl=30)
        return response
        
    except Exception as e:
        logger.error(f"Error getting network metrics: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.websocket("/ws")
async def monitoring_websocket(ws: WebSocket):
    """
    WebSocket endpoint for real-time monitoring push.
    Clients connect here to receive dashboard updates pushed by the
    background metrics collector, eliminating the need for polling.
    The server sends JSON-serialized MonitoringDashboardResponse on each cycle.
    """
    from app.services.ws_broadcast import get_monitoring_broadcaster

    broadcaster = get_monitoring_broadcaster()
    await broadcaster.connect(ws)
    try:
        # Keep connection alive; ignore incoming messages (server-push only)
        while True:
            # Wait for pings/close frames from the client
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await broadcaster.disconnect(ws)
