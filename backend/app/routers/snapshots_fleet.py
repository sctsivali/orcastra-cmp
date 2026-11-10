"""Fleet view of snapshots, at /snapshots: what snapshots exist, where, and what they cost.

The per-instance router owns the actions; this one only reads. `host` and `project` are optional
and mean "all" when absent; LXD's `instance_all_projects` extension reads a whole cluster in one
call (services/lxd/snapshot_fleet.py). A partial read is reported with its labels, and a read no
cluster answered is never cached.

important: every LXD read happens on a worker thread and every tenant decision back on the event
loop. `check_tenant_permission` reads a context variable that defaults to "no ACL", so it fails
OPEN in a thread that did not inherit the caller's context (see core/blocking.py).
"""
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.cache import get_cache_manager
from app.core.lxd_errors import lxd_http_error
from app.core.lxd_names import LXD_NAME_DESCRIPTION, LXD_NAME_PATTERN
from app.core.org_scope import (
    check_tenant_permission,
    project_is_visible,
    org_scope_dependency,
    verify_cluster_access,
)
from app.core import swr
from app.core.swr import Built
from app.core.dependencies import require_tenant
from app.services.lxd import snapshot_fleet as fleet_service
from app.services.snapshots import fleet_sizes
from app.services.lxd.exceptions import LXDAuthorizationError, LXDConnectionError
from app.services.lxd.lazy_client import connect_or_get_client
from app.routers.instance_snapshots import (
    _assert_snapshot_surface_enabled,
    _enforce_snapshot_access,
)
from app.services.lxd_service import LXDService, get_lxd_service
from app.services.lxd import health
from app.services.lxd.fanout import complete_reasons, down_text, known_down

logger = logging.getLogger(__name__)

# important: not proxy_executor. That pool keeps OrcaHub's traffic apart from the dashboard's own
# fan-outs (services/lxd/proxy.py), and the isolation is symmetric: one operator opening this
# page must not occupy the workers an integration is waiting on.
_FLEET_WORKERS = 12
_FLEET_MAX_IN_FLIGHT = 24
_fleet_executor = ThreadPoolExecutor(
    max_workers=_FLEET_WORKERS, thread_name_prefix="snapshots_fleet"
)

# Wall clock per cluster. The socket timeout inside the read is what frees the worker;
# this only frees the caller, so the two are set together and this one is the larger.
_CLUSTER_BUDGET = 40

# An unpaginated fleet read lands in Redis. This cap guards against growth and is reported in
# `truncated` rather than applied silently.
_MAX_ROWS = 5000

_CACHE_TTL = 30


class FleetSnapshotListResponse(BaseModel):
    total: int = Field(..., description="Number of snapshots returned")
    items: List[Dict[str, Any]] = Field(
        default_factory=list, description="Snapshots across the requested scope"
    )
    unreachable_nodes: List[str] = Field(
        default_factory=list,
        description="Clusters that could not be read. Distinguishes an empty result from a partial one",
    )
    queried_nodes: List[str] = Field(
        default_factory=list, description="Clusters that answered"
    )
    partial_nodes: List[str] = Field(
        default_factory=list,
        description="Clusters that answered, but not for every project they hold",
    )
    node_errors: Dict[str, str] = Field(
        default_factory=dict,
        description="Reason per cluster in unreachable_nodes or partial_nodes. Empty for Tenant callers.",
    )
    truncated: bool = Field(
        default=False, description="The row cap was reached and some rows are not present"
    )
    size_measurement: str = Field(
        default="skipped",
        description=(
            "measured: every visible snapshot-holding instance was read for sizes, from cache or "
            "live. partial: some were not, because of the holder caps, the deadline, a failed "
            "read or a cluster whose size phase failed. skipped: none could be."
        ),
    )
    measured_instances: int = Field(
        default=0,
        description="Distinct visible (cluster, project, instance) holders whose sizes were read",
    )
    unmeasured_instances: int = Field(
        default=0,
        description="Distinct visible holders whose sizes were not read on this request",
    )


router = APIRouter(
    prefix="/snapshots",
    tags=["Snapshots - Fleet"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)


def _read_one_cluster(
    lxd_service: LXDService,
    host: str,
    project: Optional[str],
) -> Tuple[List[Dict[str, Any]], bool]:
    """One cluster's rows, on a worker thread. Raises rather than returning empty on a failure.

    connect_or_get_client rather than connection_manager.get_client, so a worker that has not
    connected to this cluster yet reads it instead of reporting a healthy cluster as absent.
    """
    client = connect_or_get_client(lxd_service, host)
    if client is None:
        raise LXDConnectionError(f"Could not connect to {host}")
    return fleet_service.read_cluster_snapshots(client, host, project=project)


def _read_fleet_cluster(
    lxd_service: LXDService,
    host: str,
    project: Optional[str],
) -> Tuple[List[Dict[str, Any]], bool]:
    """As _read_one_cluster, for the fleet form: a cluster this worker knows is down is not dialed.

    A request naming one host still dials it (_read_one_cluster), as every single-cluster route
    does; the fleet form leaves that redial to the healer (see fanout.known_down).
    """
    down = known_down(host, getattr(lxd_service, "connection_manager", None))
    if down is not None:
        if down == health.KIND_UNAUTHORIZED:
            raise LXDAuthorizationError(f"{host}: {down_text(down)}")
        raise LXDConnectionError(f"{host}: {down_text(down)}")
    return _read_one_cluster(lxd_service, host, project)


@router.get(
    "",
    response_model=FleetSnapshotListResponse,
    summary="List snapshots across the fleet",
    description=(
        "Every snapshot in scope. Omit host to read every cluster, omit project to read every "
        "project. Sizes are read for every visible snapshot-holding instance in a bounded second "
        "phase; size_measurement and the instance counters say how far it got."
    ),
)
async def list_fleet_snapshots(
    host: Optional[str] = Query(
        None, description="Cluster to read. Omit to read every cluster in scope"
    ),
    project: Annotated[
        Optional[str],
        Query(
            pattern=LXD_NAME_PATTERN,
            description=(
                f"LXD project to read. Omit to read every project. {LXD_NAME_DESCRIPTION}"
            ),
        ),
    ] = None,
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """Every snapshot in scope, one LXD call per cluster, then one size read per visible holder.

    Sizes are a second phase after the visibility filter (services/snapshots/fleet_sizes.py):
    read through a per-host cache with a ten minute life, bounded per cluster and per fleet,
    against a deadline inside the request budget. A forced refresh bypasses that cache and
    re-measures. The response says how far the phase got.
    """
    # important: `is not None` rather than truthiness. A blank `?host=` must get the 422
    # verify_cluster_access raises instead of widening a narrowed request into a fleet sweep.
    # `project` refuses a blank value through its pattern: `?project=` would read `default`
    # (_project_params substitutes it) while labelling every row with an empty project. The
    # pattern also keeps arbitrary strings out of the cache and single-flight keys.
    if host is not None:
        verify_cluster_access(host)

    # The deployment-wide refusal applies to every shape of this read. The scope-specific half
    # only makes sense when there is a single scope to check, and applied fleet-wide it would
    # refuse a tenant who legitimately holds grants on some of the clusters being read.
    _assert_snapshot_surface_enabled()
    if host is not None and project is not None:
        _enforce_snapshot_access(host, project, None)

    cache = get_cache_manager()

    async def build() -> Built:
        # When a host is named, scope was proven above. When it is not, the node list itself is the
        # proof: get_node_names is apply_cluster_filter(list_clusters()), so a cluster outside the
        # caller's organisation never enters the fan-out.
        in_scope = sorted(lxd_service.get_node_names())
        nodes_to_query = [host] if host is not None else in_scope
        request_started = time.monotonic()

        async def _gather(node: str):
            return await run_blocking(
                _read_one_cluster if host is not None else _read_fleet_cluster,
                lxd_service,
                node,
                project,
                timeout=_CLUSTER_BUDGET,
                label=f"snapshots.fleet[{node}]",
                executor=_fleet_executor,
                max_in_flight=_FLEET_MAX_IN_FLIGHT,
            )

        results = await asyncio.gather(
            *(_gather(node) for node in nodes_to_query), return_exceptions=True
        )

        rows: List[Dict[str, Any]] = []
        answered: List[str] = []
        partial: List[str] = []
        failed: List[str] = []
        for node, result in zip(nodes_to_query, results):
            if isinstance(result, BaseException):
                if isinstance(result, HTTPException):
                    raise result
                if host is not None and not isinstance(
                    result, (LXDConnectionError, BlockingTimeout, BlockingPoolFull)
                ):
                    # A named cluster that answered with a real error is that caller's error, not a
                    # fleet-wide degradation. Answering 200 with an empty list would hide it.
                    raise lxd_http_error(result, "Project", project or "*")
                if isinstance(result, (LXDConnectionError, BlockingTimeout, BlockingPoolFull)):
                    logger.info("Snapshot read failed on %s: %s", node, result)
                else:
                    # Reported as unreachable, since the cluster contributed nothing, but logged
                    # as an error with its traceback so a defect in the read path is not mistaken
                    # for a flaky cluster.
                    logger.error(
                        "Snapshot read raised on %s, reporting it unreachable: %s",
                        node, result, exc_info=result,
                    )
                failed.append(node)
                continue
            node_rows, complete = result
            answered.append(node)
            if not complete:
                partial.append(node)
            rows.extend(node_rows)

        # important: keyed by (host, project, source); keyed by source alone, the first answer
        # would win for every later instance sharing a name across clusters, showing one tenant's
        # snapshots in another's list. Each key needs both `view` and `snapshot`:
        # filter_instances_by_acl ignores a rule's permissions, and `snapshot` alone would expose
        # an instance the caller may not `view`. Admin and Partner carry no ACL, so
        # check_tenant_permission passes every row for them.
        seen: Dict[Tuple[str, str, str], bool] = {}
        visible: List[Dict[str, Any]] = []
        for row in rows:
            key = (row.get("host") or "", row.get("project") or "", row.get("source_name") or "")
            if key not in seen:
                # important: the tenant ACL alone is no bound for a Partner, whose ACL is always
                # None, and a snapshot row names the instance it came from. On a cluster carrying
                # two organizations that is the other organization's instance inventory. Same gate
                # the network, profile, ACL and instance lists apply.
                seen[key] = (
                    project_is_visible(key[0], key[1])
                    and check_tenant_permission(key[0], key[1], key[2], "view")
                    and check_tenant_permission(key[0], key[1], key[2], "snapshot")
                )
            if seen[key]:
                visible.append(row)

        truncated = len(visible) > _MAX_ROWS
        if truncated:
            logger.warning("Fleet snapshot read returned %d rows, capped at %d", len(visible), _MAX_ROWS)
            visible = visible[:_MAX_ROWS]

        measured_instances, unmeasured_instances = await fleet_sizes.measure_visible_sizes(
            lxd_service,
            visible,
            fresh=fresh,
            request_started=request_started,
            executor=_fleet_executor,
            max_in_flight=_FLEET_MAX_IN_FLIGHT,
            cluster_budget=_CLUSTER_BUDGET,
        )
        if unmeasured_instances == 0:
            size_measurement = "measured"
        elif measured_instances == 0:
            size_measurement = "skipped"
        else:
            size_measurement = "partial"

        # A cluster registered and in scope that never entered the fan-out is reported unreachable.
        # The structural ratchet in tests/test_fresh_bypass_is_wired.py scans for this seeding shape.
        never_queried = (
            [] if host is not None else sorted(set(lxd_service.get_node_names()) - set(nodes_to_query))
        )
        unreachable_nodes = sorted(set(failed) | set(never_queried))

        response = FleetSnapshotListResponse(
            total=len(visible),
            items=visible,
            unreachable_nodes=unreachable_nodes,
            queried_nodes=sorted(answered),
            partial_nodes=sorted(partial),
            node_errors=complete_reasons(unreachable_nodes, {}, partial=partial),
            truncated=truncated,
            size_measurement=size_measurement,
            measured_instances=measured_instances,
            unmeasured_instances=unmeasured_instances,
        )

        # important: cache nothing when no cluster answered. That read still returns 200 with an
        # empty list, and caching it would pin emptiness for the TTL and, on a forced refresh,
        # overwrite good data. The degradation labels are cached with the payload, so a cached
        # partial read still renders its banner. A write during the read is fenced by swr.serve's
        # generation check.
        if not answered and nodes_to_query:
            logger.warning("Not caching fleet snapshots: no cluster answered")
        # A scope naming no cluster is cheap to read again, so it is not cached either, and the
        # warning above stays quiet for a tenant whose ACL grants nothing.
        return Built(
            value=response,
            body=response.model_dump(),
            fresh_for=_CACHE_TTL if answered else None,
        )

    # Stale-while-revalidate: nothing polls this list, so with a plain TTL nearly every visit would
    # miss and pay the whole fan-out. `v4` marks the SWR envelope in the stored body; the prefix
    # keeps every sweep matching.
    served = await swr.serve(
        cache=cache,
        base_key=f"snapshots_fleet:list:v4:host={host}:project={project}",
        fresh=fresh,
        build=build,
        generation=cache.snapshot_fleet_generation,
        label="fleet snapshots",
    )
    return served.as_model(FleetSnapshotListResponse)
