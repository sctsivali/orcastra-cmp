"""Monitoring: the fleet dashboard, per-cluster metrics, metrics history and the push feed."""

from fastapi import APIRouter, HTTPException, Depends, Query, Response, WebSocket, WebSocketDisconnect
from typing import Annotated, Optional, List, Dict, Any
import logging
import time
import asyncio
from datetime import datetime, timezone

from app.models.schemas import (
    ClusterMonitoringMetrics,
    StoragePoolMetrics,
    MonitoringDashboardResponse,
    MetricsHistoryPoint,
)
from app.core.blocking import BlockingPoolFull, BlockingTimeout
from app.services.monitoring_history import (
    build_point,
    claim_history_slot,
    history_key as _monitoring_history_key,
)
from app.services.monitoring_history_store import (
    HistoryRead,
    history_body,
    hydrate_from_postgres,
    import_previous_history,
    needs_hydrate,
    record_point,
)
from app.services.monitoring_memory import apply_member_memory
from app.core.registry_guard import require_known_registry
from app.services.monitoring_sweep import SweepTimer, count_warnings, inventory_note, log_sweep, read_fleet
from app.services.ws_session_guard import close_when_session_ends
from app.core.ws_auth import ws_user
from app.services.monitoring_usage import (
    legacy_top_lists,
    read_instance_usage,
    rollup,
    select_consumers,
    summary as usage_summary,
)
from app.services.lxd.cpu_allocation import allocation_ratio
from app.services.lxd_service import (
    LXDService,
    LXDConnectionError,
    get_lxd_service,
)
from app.core.cache import get_cache_manager
from app.core.type_utils import get_cli_command_prefix, get_trust_add_subcommand
from app.core.node_metadata import get_node_metadata_store
from app.models.cluster_status import ClusterStatus, derive_status
from app.core.dependencies import AuthenticatedUser, require_partner, require_tenant
from app.core.host_detail import monitoring_dashboard_for_tier
from app.core.task_scope import caller_tier
from app.core.org_scope import org_scope_dependency, verify_cluster_access, get_scope_cache_key, check_tenant_permission
from app.core.tenant_visibility import tenant_acl_active
from app.core import swr
from app.core.swr import Built

logger = logging.getLogger(__name__)

_MONITORING_CACHE_KIND = "monitoring"


def _resource_generation(cache: Any, kind: str) -> Optional[str]:
    """The resource list generation token, or None from a cache that keeps none (a stub)."""
    read = getattr(cache, "resource_list_generation", None)
    return read(kind) if callable(read) else None

def _share(used: float, total: float) -> Optional[float]:
    """`used` as a percentage of `total`, or None when nothing reported a total.

    Never 0 for 0/0: a gauge reading 0.0% over a total of zero tells an operator the fleet is idle
    when no cluster reported its capacity at all.
    """
    return round(used / total * 100, 2) if total > 0 else None


router = APIRouter(
    prefix="/monitoring",
    tags=["Monitoring"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# WebSocket endpoints get their own router: the HTTP auth dependencies (HTTPBearer)
# break the upgrade, since browsers cannot send an Authorization header in the
# handshake. The handler authenticates with ws_user("monitoring") (a single-use ?ticket=).
ws_router = APIRouter(
    prefix="/monitoring",
    tags=["Monitoring"],
)

# Per-process history slots, used only while Redis is unavailable (see claim_history_slot).
_last_history_store_times: Dict[str, float] = {}


# Node status has no private copy here: read_fleet (services/monitoring_sweep.py) runs the same
# per-node check /nodes uses (LXDService.check_node_status), which consults the circuit breaker
# and sets `reachable`, so the dashboard and the node pickers answer from one implementation.
# Per-instance CPU is not sampled here either: usage comes from services/monitoring_usage.py,
# through the instance dialog's own sampler, so Top Consumers and the dialog agree.


# Cache key for the dashboard response, scoped so a partner never reads an admin's entry.
# important: kept in Redis so every worker serves the snapshot the collector warmed. Redis runs
# allkeys-lru, which is survivable only because these keys are few (one per scope) and hot; do
# not split this per cluster. Bump the version when the body changes shape, so an older worker
# in a rolling deploy never parses a body it cannot validate.
MONITORING_DASHBOARD_CACHE_KEY = "monitoring:dashboard:v3"
_DASHBOARD_CACHE_TTL = 35
# The scope the background collector refreshes: admins and the collector itself.
COLLECTED_SCOPE_KEY = "dashboard:scope=all"
# important: the collected scope's entry outlives a whole collection cycle (the sweep plus the
# 30 s pause) and a leader being replaced in the middle of one, so collector followers relaying
# it and admins reading it always find it. Expiring within a cycle would make each follower
# sweep the whole fleet itself.
COLLECTED_DASHBOARD_TTL = 120


def _dashboard_ttl(scope_key: str) -> int:
    return COLLECTED_DASHBOARD_TTL if scope_key == COLLECTED_SCOPE_KEY else _DASHBOARD_CACHE_TTL


def cached_dashboard() -> Optional[MonitoringDashboardResponse]:
    """The current scope's cached dashboard, or None. Never sweeps the fleet."""
    payload = get_cache_manager().get("api_responses", get_scope_cache_key(MONITORING_DASHBOARD_CACHE_KEY))
    if not payload:
        return None
    try:
        return MonitoringDashboardResponse(**payload)
    except Exception as exc:  # noqa: BLE001 - an unreadable entry is a miss, the caller rebuilds
        logger.warning("Ignoring an unreadable cached monitoring dashboard: %s", exc)
        return None

# No herd lock here: swr.serve collapses arrivals per scope key on a miss or a forced read.


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
    """Aggregated metrics across the caller's clusters and instances.

    Served from the scope's cache, which the background collector keeps warm; `fresh=true`
    rebuilds it.
    """
    # One cached snapshot per org scope. The base stays "dashboard" because
    # _monitoring_history_key maps off it.
    scope_key = get_scope_cache_key("dashboard")
    cache = get_cache_manager()

    async def build_dashboard() -> Built:
        monitoring_generation = _resource_generation(cache, _MONITORING_CACHE_KIND)
        start_time = time.time()
        timer = SweepTimer()

        # The fleet's instances and every cluster's status, in parallel, each status read
        # bounded on its own (services/monitoring_sweep.py).
        fleet = await read_fleet(lxd_service, timer)
        all_vms, connected_nodes = fleet.vms, fleet.connected
        node_statuses_list = fleet.statuses
        # An unreadable registry with nothing known is not a fleet of zeros. The background
        # collector builds through here too, so it records no zero point either.
        require_known_registry([node.name for node in node_statuses_list])
        
        # Tenant ACL filtering.
        all_vms = [
            vm for vm in all_vms
            if check_tenant_permission(
                vm.host,
                vm.project or "default",
                vm.name,
                "view",
            )
        ]
        
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
        total_gpu_cards = 0
        total_gpu_display_cards = 0
        total_gpu_integrated_cards = 0
        used_gpu = 0
        
        storage_pools_list: List[StoragePoolMetrics] = []
        nodes_metrics: List[ClusterMonitoringMetrics] = []
        
        online_nodes = 0
        inactive_nodes = 0
        # Clusters whose capacity went into the totals above. Not online_nodes: a connected
        # cluster that sent no resources adds nothing, and the page has to say so.
        reporting_nodes = 0

        # resources.py sets node_type only for a cluster that answered and trusted this client, so
        # on the one path that shows a trust hint it is None, and get_cli_command_prefix would
        # default to lxc and give an Incus operator the wrong command. Registration recorded the
        # real type; read it once, not per node, since this is a file read behind the store.
        registered_types = {
            name: meta.get("type")
            for name, meta in get_node_metadata_store().get_all_node_metadata().items()
        }
        type_counts: Dict[str, Dict[str, int]] = {}
        for vm in all_vms:
            counts = type_counts.setdefault(vm.host, {"container": 0, "virtual-machine": 0})
            if vm.type in counts:
                counts[vm.type] += 1

        for node in node_statuses_list:
            if node.connected:
                online_nodes += 1
                if node.resources:
                    resources = node.resources
                    reporting_nodes += 1
                    total_cpu += resources.cpu_total
                    used_cpu += resources.cpu_used
                    total_memory += resources.memory_total
                    used_memory += resources.memory_used
                    # Usable capacity: a shared backing store behind a MicroCloud is added once,
                    # not once per member. storage_raw_total holds the per-member sum.
                    total_storage += resources.storage_total
                    used_storage += resources.storage_used
                    total_gpu += resources.gpu_total
                    total_gpu_cards += resources.gpu_cards_total
                    total_gpu_display_cards += resources.gpu_display_total
                    total_gpu_integrated_cards += resources.gpu_integrated_total
                    used_gpu += resources.gpu_used
                    
                    for pool in resources.storage_pools:
                        storage_pools_list.append(StoragePoolMetrics(
                            name=pool.get("name", "unknown"),
                            node=node.name,
                            members=[str(m) for m in (pool.get("members") or [])],
                            scope=pool.get("scope"),
                            driver=pool.get("driver", "unknown"),
                            total_bytes=pool.get("space_total", 0),
                            used_bytes=pool.get("space_used", 0),
                            available_bytes=pool.get("space_available", 0),
                            usage_percent=round(
                                (pool.get("space_used", 0) / pool.get("space_total", 1)) * 100, 2
                            ) if pool.get("space_total", 0) > 0 else 0,
                        ))
                    
                    inventory_missed = fleet.timed_out.get(node.name)
                    nodes_metrics.append(ClusterMonitoringMetrics(
                        node_name=node.name,
                        node_type=node.node_type or "lxd",
                        is_online=True,
                        cluster_status=ClusterStatus.ONLINE.value,
                        status_message=(
                            inventory_note(inventory_missed) if inventory_missed else "Connected and authenticated"
                        ),
                        collection_timed_out=bool(inventory_missed),
                        cpu_total=resources.cpu_total,
                        cpu_used=resources.cpu_used,
                        cpu_usage_percent=round(
                            (resources.cpu_used / resources.cpu_total * 100) if resources.cpu_total > 0 else 0, 2
                        ),
                        cpu_allocated=resources.cpu_used,
                        cpu_allocation_ratio=allocation_ratio(resources.cpu_used, resources.cpu_total),
                        cpu_overcommitted=resources.cpu_used > resources.cpu_total,
                        memory_total_bytes=resources.memory_total,
                        memory_used_bytes=resources.memory_used,
                        memory_usage_percent=_share(resources.memory_used, resources.memory_total),
                        storage_total_bytes=resources.storage_total,
                        storage_used_bytes=resources.storage_total - resources.storage_available,
                        storage_usage_percent=_share(
                            resources.storage_total - resources.storage_available, resources.storage_total
                        ),
                        gpu_total=resources.gpu_total,
                        gpu_used=resources.gpu_used,
                        gpu_available=resources.gpu_available,
                        gpu_cards_total=resources.gpu_cards_total,
                        gpu_integrated_total=resources.gpu_integrated_total,
                        gpu_display_total=resources.gpu_display_total,
                        gpu_models=resources.gpu_models,
                        gpu_utilization_supported=resources.gpu_utilization_supported,
                        instance_count=len([vm for vm in all_vms if vm.host == node.name]),
                        running_instances=len([vm for vm in all_vms if vm.host == node.name and vm.status.value == "Running"]),
                        container_count=type_counts.get(node.name, {}).get("container", 0),
                        vm_count=type_counts.get(node.name, {}).get("virtual-machine", 0),
                    ))
            else:
                inactive_nodes += 1
                # important: `reachable` is the discriminator, never the wording of `error`.
                # Matching the error text reads a powered-off host as a certificate problem.
                # derive_status is the one mapping; see models/cluster_status.py.
                status, _reason_code, message = derive_status(
                    connected=node.connected,
                    reachable=node.reachable,
                    auth=node.auth,
                    error=node.error,
                )
                cluster_status = status.value
                if status is ClusterStatus.NOT_AUTHORIZED:
                    # The CLI hint belongs to this state alone; an unreachable host needs no
                    # trust command.
                    node_type = node.node_type or registered_types.get(node.name)
                    cli_prefix = get_cli_command_prefix(node_type)
                    trust_subcmd = get_trust_add_subcommand(node_type)
                    # Written out rather than appended to `message`, whose last sentence is
                    # already a generic "Add trust on the cluster."
                    auth_detail = f" (auth={node.auth})" if node.auth else ""
                    status_message = (
                        f"Cluster answered but rejected our certificate.{auth_detail} "
                        f"Run '{cli_prefix} config trust {trust_subcmd}' on the cluster."
                    )
                else:
                    status_message = message or node.error or "Unknown error"
                
                nodes_metrics.append(ClusterMonitoringMetrics(
                    node_name=node.name,
                    node_type=node.node_type or "lxd",
                    is_online=False,
                    cluster_status=cluster_status,
                    status_message=status_message,
                    collection_timed_out=node.name in fleet.timed_out,
                    cpu_total=0,
                    cpu_used=0,
                    cpu_usage_percent=0,
                    memory_total_bytes=0,
                    memory_used_bytes=0,
                    memory_usage_percent=None,
                    storage_total_bytes=0,
                    storage_used_bytes=0,
                    storage_usage_percent=None,
                    # important: an offline node contributes zero GPU, as it contributes
                    # zero CPU and memory above. A remembered count would read as available
                    # capacity; the fleet total counts only hardware that answered.
                    gpu_total=0,
                    gpu_used=0,
                    gpu_available=0,
                    gpu_cards_total=0,
                    instance_count=0,
                    running_instances=0,
                ))
        
        # A tenant sees no storage pools.
        if tenant_acl_active():
            storage_pools_list = []

        cpu_usage_percent = round((used_cpu / total_cpu * 100) if total_cpu > 0 else 0, 2)
        memory_usage_percent = _share(used_memory, total_memory)
        storage_usage_percent = _share(used_storage, total_storage)
        
        # Live usage for every running instance, from the state the fleet list already carried.
        running_vms = [vm for vm in all_vms if vm.status.value == "Running"]
        host_threads = {
            node.name: node.resources.cpu_total
            for node in node_statuses_list
            if node.connected and node.resources and node.resources.cpu_total > 0
        }
        usages = await timer.timed("rates", read_instance_usage(running_vms, host_threads))
        usages, memory_counts = await timer.timed("scrapes", apply_member_memory(lxd_service, usages))
        fleet_cpu = rollup(nodes_metrics, usages)
        consumers = [usage_summary(usage) for usage in select_consumers(usages)]
        top_cpu_consumers, top_memory_consumers = legacy_top_lists(usages)

        realtime_memory_bytes = sum(usage.memory_bytes for usage in usages)
        realtime_memory_percent = round(
            (realtime_memory_bytes / total_memory * 100) if total_memory > 0 else 0, 2
        )
        
        connected_node_names = [n.name for n in node_statuses_list if n.connected]
        warnings_summary, warnings_skipped = await timer.timed(
            "warnings", count_warnings(lxd_service, connected_node_names)
        )

        elapsed_time = time.time() - start_time
        
        response = MonitoringDashboardResponse(
            timestamp=datetime.now(timezone.utc),
            
            # Cluster Overview
            total_nodes=len(node_statuses_list),
            online_nodes=online_nodes,
            inactive_nodes=inactive_nodes,
            clusters_reporting=reporting_nodes,
            clusters_total=len(node_statuses_list),

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
            
            # CPU utilisation, as busy hardware threads
            realtime_cpu_percent=fleet_cpu.busy_percent or 0.0,
            cpu_busy_threads=fleet_cpu.busy_threads,
            cpu_busy_percent=fleet_cpu.busy_percent,
            cpu_status=fleet_cpu.status,
            cpu_reporting_instances=fleet_cpu.reporting_instances,
            cpu_reporting_threads=fleet_cpu.reporting_threads,
            
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
            total_gpu_cards=total_gpu_cards,
            total_gpu_display_cards=total_gpu_display_cards,
            total_gpu_integrated_cards=total_gpu_integrated_cards,
            used_gpu=used_gpu,
            
            # Detailed Metrics
            nodes=nodes_metrics,
            storage_pools=storage_pools_list,
            consumers=consumers,
            top_cpu_consumers=top_cpu_consumers,
            top_memory_consumers=top_memory_consumers,
            
            # Warnings
            warnings=warnings_summary,
            
            # Meta
            collection_time_ms=round(elapsed_time * 1000, 2),
        )
        
        # Append a point to the scope's history for the charts. Each org scope has its own
        # stream, so charts render without exposing the collector's global data. Not when no
        # cluster answered: that dashboard is all zeros, the misreading the cache below refuses.
        # The slot id differs from the legacy string store's, so an older release writing that
        # store during a rolling deploy never takes this store's slot.
        history_key = _monitoring_history_key(scope_key)
        history_started = time.monotonic()
        history_written = False
        # important: never for a tenant scope. A point carries each host's memory, storage and
        # GPU, which a tenant may not read, and GET /history refuses it anyway; writing them
        # would only keep host figures at rest under its key.
        if (
            connected_nodes
            and not tenant_acl_active()
            and _resource_generation(cache, _MONITORING_CACHE_KIND) == monitoring_generation
            and claim_history_slot(
                cache, f"points:{history_key}", now=time.time(), local=_last_history_store_times
            )
        ):
            try:
                new_point = build_point(
                    nodes_metrics,
                    usages,
                    fleet_cpu_percent=fleet_cpu.busy_percent,
                    # A history point keeps 0.0 for a missing total: its Postgres column is
                    # NOT NULL and the chart reads a number. Only the gauges say "Unavailable".
                    memory_usage_percent=memory_usage_percent if memory_usage_percent is not None else 0.0,
                    storage_usage_percent=storage_usage_percent if storage_usage_percent is not None else 0.0,
                    running_instances=running_instances,
                )
                history_written = await record_point(cache, history_key, new_point)
                if (
                    history_written
                    and scope_key == COLLECTED_SCOPE_KEY
                    and new_point["cpu_usage_percent"] is not None
                ):
                    asyncio.create_task(_persist_snapshot(new_point))
            except Exception as hist_err:
                logger.warning("Could not update history for %s: %s", history_key, hist_err)
        
        timer.record("history", history_started)
        log_sweep(
            collected=scope_key == COLLECTED_SCOPE_KEY,
            timer=timer,
            fleet=fleet,
            running=running_instances,
            history_written=history_written,
            warnings_skipped=warnings_skipped,
            memory=memory_counts,
        )
        
        # important: a dashboard built while no cluster answered is all zeros, which
        # reads as a healthy idle fleet. Caching it would pin that for the TTL, and a
        # fresh=true bypass would replace real data with it; the previous entry stays,
        # stale but true, and the next request retries. A change during the read is
        # refused by swr.serve through the monitoring generation.
        if not connected_nodes:
            logger.warning("Not caching monitoring dashboard: no cluster answered")
        return Built(
            value=response,
            body=response.model_dump(),
            fresh_for=_dashboard_ttl(scope_key) if connected_nodes else None,
        )

    # The collector rebuilds through here with fresh=True every cycle. swr.serve reads the cache
    # before any lock, so a reader never waits behind that rebuild; only a real miss or another
    # forced read collapses onto it.
    try:
        served = await swr.serve(
            cache=cache,
            base_key=MONITORING_DASHBOARD_CACHE_KEY,
            fresh=fresh,
            build=build_dashboard,
            generation=lambda: _resource_generation(cache, _MONITORING_CACHE_KIND),
            label="monitoring dashboard",
        )
        return monitoring_dashboard_for_tier(
            served.as_model(MonitoringDashboardResponse), caller_tier()
        )

    # Nothing to unwind on the error paths: a failed rebuild writes nothing, and the next
    # arrival takes the ordinary miss path.
    except HTTPException:
        raise
    except LXDConnectionError as e:
        logger.warning(f"LXD connection error in monitoring: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting monitoring dashboard: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/nodes/{node_name}/metrics",
    response_model=ClusterMonitoringMetrics,
    summary="Get metrics for a specific Cluster",
    description="Get detailed metrics for a specific registered Cluster.",
)
async def get_node_metrics(
    node_name: str,
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
    # important: one cluster's hardware and load: Partner and above, as GET /nodes. A Tenant
    # monitors its instances, not the hosts.
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Get detailed metrics for a specific registered Cluster. Cached for 30 seconds."""
    verify_cluster_access(node_name)
    
    cache = get_cache_manager()
    cache_key = get_scope_cache_key(f"monitoring:node:{node_name}")
    cached = None if fresh else cache.get("api_responses", cache_key)
    if cached is not None:
        return ClusterMonitoringMetrics(**cached)
    monitoring_generation = _resource_generation(cache, _MONITORING_CACHE_KIND)

    try:
        # Node status and this cluster's instances, in parallel.
        # important: this cluster's instances only. A fleet read fails as a whole when any other
        # cluster times out, which would answer 500 here for a cluster that is fine.
        node_statuses_task = lxd_service.check_nodes_status_async()
        vms_task = lxd_service.list_node_instances(node_name)
        node_statuses_list, all_vms = await asyncio.gather(node_statuses_task, vms_task)

        node = next((n for n in node_statuses_list if n.name == node_name), None)
        if not node:
            raise HTTPException(status_code=404, detail=f"Cluster '{node_name}' not found")
        
        if not node.connected:
            # The same derivation as the dashboard, so the two cannot disagree about this node.
            node_status, _reason, node_message = derive_status(
                connected=node.connected,
                reachable=node.reachable,
                auth=node.auth,
                error=node.error,
            )
            return ClusterMonitoringMetrics(
                node_name=node_name,
                node_type=node.node_type or "lxd",
                is_online=False,
                cluster_status=node_status.value,
                status_message=node_message or node.error or "Unknown error",
                cpu_total=0,
                cpu_used=0,
                cpu_usage_percent=0,
                memory_total_bytes=0,
                memory_used_bytes=0,
                memory_usage_percent=None,
                storage_total_bytes=0,
                storage_used_bytes=0,
                storage_usage_percent=None,
                gpu_total=0,
                gpu_used=0,
                instance_count=0,
                running_instances=0,
            )
        
        resources = node.resources
        if not resources:
            raise HTTPException(status_code=500, detail="Cluster resources are not available")
        
        # The tenant ACL applies to the instance count.
        node_instances = [
            vm for vm in all_vms
            if vm.host == node_name
            and check_tenant_permission(vm.host, vm.project or "default", vm.name, "view")
        ]
        
        result = ClusterMonitoringMetrics(
            node_name=node_name,
            node_type=node.node_type or "lxd",
            is_online=True,
            cpu_total=resources.cpu_total,
            cpu_used=resources.cpu_used,
            cpu_usage_percent=round(
                (resources.cpu_used / resources.cpu_total * 100) if resources.cpu_total > 0 else 0, 2
            ),
            cpu_allocated=resources.cpu_used,
            cpu_allocation_ratio=allocation_ratio(resources.cpu_used, resources.cpu_total),
            cpu_overcommitted=resources.cpu_used > resources.cpu_total,
            memory_total_bytes=resources.memory_total,
            memory_used_bytes=resources.memory_used,
            memory_usage_percent=_share(resources.memory_used, resources.memory_total),
            storage_total_bytes=resources.storage_total,
            storage_used_bytes=resources.storage_total - resources.storage_available,
            storage_usage_percent=_share(
                resources.storage_total - resources.storage_available, resources.storage_total
            ),
            gpu_total=resources.gpu_total,
            gpu_used=resources.gpu_used,
            gpu_available=resources.gpu_available,
            gpu_cards_total=resources.gpu_cards_total,
            gpu_integrated_total=resources.gpu_integrated_total,
            gpu_display_total=resources.gpu_display_total,
            gpu_models=resources.gpu_models,
            gpu_utilization_supported=resources.gpu_utilization_supported,
            instance_count=len(node_instances),
            running_instances=len([vm for vm in node_instances if vm.status.value == "Running"]),
        )
        
        # Cached only if no monitoring write landed during the read.
        if (
            _resource_generation(cache, _MONITORING_CACHE_KIND)
            == monitoring_generation
        ):
            cache.set("api_responses", cache_key, result.model_dump(), ttl=30)
        else:
            logger.info("Not caching node metrics: monitoring data changed during the read")
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting Cluster metrics: {e}")
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


@router.get(
    "/history",
    response_model=List[MetricsHistoryPoint],
    summary="Get metrics history",
    description=(
        "Historical metrics for graphing. `window` selects the points collected in the last N "
        "seconds (default 300); the older `points` parameter returns the last N points."
    ),
)
async def get_metrics_history(
    window: Annotated[
        Optional[int],
        Query(ge=60, le=86400, description="Seconds of history to return, up to 24 hours"),
    ] = None,
    points: Annotated[
        Optional[int],
        Query(ge=1, le=8640, description="Legacy: the last N points, whatever span they cover"),
    ] = None,
    lxd_service: LXDService = Depends(get_lxd_service),
    # important: the series are the hosts' load, which counts every other party's instances.
    # Partner and above; a Tenant monitors its own instances.
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Historical dashboard metrics for the caller's scope, for graphing.

    Data flow (hot -> cold):
      1. Redis sorted set per scope, one member per dashboard collection, trimmed to 24 hours
      2. The legacy string key, merged in once per scope
      3. PostgreSQL (admin scope only, 7-day retention), merged in whenever the stored
         history starts well inside the 24 hours, such as after a Redis restart
      4. One scoped collection when the scope has no point at all yet
    """
    cache = get_cache_manager()
    scope_key = get_scope_cache_key("dashboard")
    history_key = _monitoring_history_key(scope_key)

    async def read() -> HistoryRead:
        return await history_body(cache, history_key, window_seconds=window, points=points)

    try:
        stored = await read()
        if stored.store_ok:
            refilled = await import_previous_history(cache, history_key)
            if scope_key == COLLECTED_SCOPE_KEY and needs_hydrate(stored):
                refilled += await hydrate_from_postgres(cache, history_key)
            if refilled:
                stored = await read()
        if stored.total == 0:
            # First scoped request after startup: collect one scoped dashboard sample
            # so the chart starts filling without reading the global history stream.
            try:
                await get_monitoring_dashboard(lxd_service=lxd_service, fresh=False)
            except Exception as collect_err:
                logger.debug(f"Scoped history warm-up failed: {collect_err}")
            stored = await read()
    except (BlockingTimeout, BlockingPoolFull) as busy:
        logger.warning("Monitoring history read refused for %s: %s", history_key, busy)
        raise HTTPException(
            status_code=503, detail="Monitoring history is busy. The chart retries on its next refresh."
        )
    # The body is already the List[MetricsHistoryPoint] JSON, encoded off the event loop.
    return Response(content=stored.body, media_type="application/json")


@ws_router.websocket("/ws")
async def monitoring_websocket(
    ws: WebSocket,
    user: Optional[Any] = Depends(ws_user("monitoring")),
):
    """Real-time monitoring push: a MonitoringDashboardResponse as JSON on each collector cycle.

    Opened with a ticket from POST /ws-tickets (browsers cannot set Authorization headers on a
    WebSocket upgrade); closes with 4401 when the session ends. Admin only.
    """
    from app.core.config import get_settings

    if get_settings().auth_enabled:
        if user is None:
            await ws.close(code=1008, reason="Authentication required")
            return
        from app.core.rbac import Role, get_highest_role, resolve_roles

        if get_highest_role(resolve_roles(user)) != Role.ADMIN:
            await ws.close(code=1008, reason="Monitoring WebSocket is admin-only")
            return

    from app.services.ws_broadcast import get_monitoring_broadcaster

    broadcaster = get_monitoring_broadcaster()
    await broadcaster.connect(ws)
    session = asyncio.create_task(close_when_session_ends(ws, user))
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
        session.cancel()
        await broadcaster.disconnect(ws)
