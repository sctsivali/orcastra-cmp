"""Fleet view of snapshots, at /snapshots.

Separate from the per-instance surface because it answers a different question: not "what can I
roll this instance back to" but "what snapshots exist, where, and what are they costing me". The
per-instance router owns the actions; this one only reads.

`host` and `project` are both optional and both mean "all" when absent. This used to be scoped to
one cluster and one project, on the reasoning that a fleet-wide sweep would fan out across every
cluster on every page load and that a partial failure must never be cached as if it were empty.
The first half of that objection is answered by LXD's `instance_all_projects` extension, which
reads a whole cluster in one call rather than one per project (see services/lxd/snapshot_fleet.py).
The second half is answered here, and it is not theoretical on this fleet: two of five registered
clusters reject the control plane's certificate, so every single read is partial.

important: every LXD read happens on a worker thread and every tenant decision happens back on the
event loop. `check_tenant_permission` reads a context variable that defaults to "no ACL", which
means it fails OPEN in a thread that did not inherit the caller's context (core/blocking.py:119).
Keeping the two apart means the filter cannot be wrong even if the offload ever changes shape.
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
    get_scope_cache_key,
    org_scope_dependency,
    verify_cluster_access,
)
from app.core.single_flight import single_flight
from app.core.dependencies import require_tenant
from app.services.lxd import snapshot_fleet as fleet_service
from app.services.snapshots import fleet_sizes
from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.lazy_client import connect_or_get_client
from app.routers.instance_snapshots import (
    _assert_snapshot_surface_enabled,
    _enforce_snapshot_access,
)
from app.services.lxd_service import LXDService, get_lxd_service

logger = logging.getLogger(__name__)

# important: NOT proxy_executor. That pool exists so OrcaHub's traffic cannot land where the
# dashboard's own fan-outs run (services/lxd/proxy.py:40), and the isolation is symmetric: one
# operator opening this page must not occupy the workers a paid integration is waiting on.
_FLEET_WORKERS = 12
_FLEET_MAX_IN_FLIGHT = 24
_fleet_executor = ThreadPoolExecutor(
    max_workers=_FLEET_WORKERS, thread_name_prefix="snapshots_fleet"
)

# Wall clock per cluster. The socket timeout inside the read is what actually frees the worker;
# this only frees the caller, so the two are set together and this one is the larger.
_CLUSTER_BUDGET = 40

# An unpaginated fleet read lands in Redis. Measured today the whole fleet holds 33 snapshots, so
# this is a guard against growth rather than a limit anyone meets, and it is reported rather than
# applied silently.
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

    note: connect_or_get_client rather than connection_manager.get_client, so a worker that has
    not connected to this cluster yet reads it instead of reporting a healthy cluster as absent.
    """
    client = connect_or_get_client(lxd_service, host)
    if client is None:
        raise LXDConnectionError(f"Could not connect to {host}")
    return fleet_service.read_cluster_snapshots(client, host, project=project)


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
@single_flight("snapshots_fleet:list")
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
    re-measures. The response says how far the phase got rather than leaving the console to
    infer it from the scope.
    """
    # important: `is not None`, not truthiness. `?host=` is a caller who named a cluster and got
    # it wrong, and must get the 422 verify_cluster_access already raises for a blank id. Reading
    # it as "absent" would silently widen a narrowed request into a fleet sweep.
    #
    # `project` gets the same treatment through its pattern, which refuses a blank one. Without
    # that, `?project=` read the `default` project (because _project_params substitutes it) while
    # labelling every row with the empty string, and the resulting SnapshotProjectUnknown was
    # caught per cluster: a healthy fleet answered 200 with every cluster reported unreachable.
    # Validating here also keeps an arbitrary string out of the cache and single-flight keys,
    # where each distinct value would otherwise buy a fresh fan-out that is never cached.
    if host is not None:
        verify_cluster_access(host)

    # The deployment-wide refusal applies to every shape of this read. The scope-specific half
    # only makes sense when there is a single scope to check, and applied fleet-wide it would
    # refuse a tenant who legitimately holds grants on some of the clusters being read.
    _assert_snapshot_surface_enabled()
    if host is not None and project is not None:
        _enforce_snapshot_access(host, project, None)

    cache = get_cache_manager()
    cache_key = get_scope_cache_key(f"snapshots_fleet:list:host={host}:project={project}")
    cached = None if fresh else cache.get("api_responses", cache_key)
    if cached is not None:
        return FleetSnapshotListResponse(**cached)

    # When a host is named, scope was proven above. When it is not, the node list itself is the
    # proof: get_node_names is apply_cluster_filter(list_clusters()), so a cluster outside the
    # caller's organisation never enters the fan-out.
    in_scope = sorted(lxd_service.get_node_names())
    nodes_to_query = [host] if host is not None else in_scope
    request_started = time.monotonic()

    async def _gather(node: str):
        return await run_blocking(
            _read_one_cluster,
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
                # important: still reported as unreachable, because from the caller's side the
                # cluster did contribute nothing. But logged as an error with its traceback, so a
                # defect in the read path cannot spend its life disguised as a flaky cluster.
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

    # important: keyed by (host, project, source), not by source alone. With one cluster and one
    # project per request the short key was accidentally sufficient; across clusters it collides,
    # and the first answer wins for every later instance sharing a name. That is one tenant's
    # snapshots appearing in another's list.
    #
    # important: narrowed by the SNAPSHOT permission, not by "has any rule here".
    # filter_instances_by_acl ignores the permissions on a rule entirely, so a tenant holding
    # `view` on one instance and `snapshot` on another passed the project gate on the second and
    # was then shown the first one's snapshots as well. Admin and Partner carry no ACL and
    # check_tenant_permission answers True for them, so an operator read is unfiltered.
    seen: Dict[Tuple[str, str, str], bool] = {}
    visible: List[Dict[str, Any]] = []
    for row in rows:
        key = (row.get("host") or "", row.get("project") or "", row.get("source_name") or "")
        if key not in seen:
            # important: the tenant ACL alone is no bound for a Partner, whose ACL is always
            # None, and a snapshot row names the instance it came from. On a cluster carrying
            # two organizations that is the other organization's instance inventory. Same gate
            # the network, profile, ACL and instance lists apply.
            seen[key] = project_is_visible(key[0], key[1]) and check_tenant_permission(
                key[0], key[1], key[2], "snapshot"
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

    # A cluster registered and in scope that never entered the fan-out is unread, not empty. The
    # seeding literal is what the structural ratchet scans for, and the shape is deliberate:
    # set(lxd_service.get_node_names()) - set(nodes_to_query)
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
        truncated=truncated,
        size_measurement=size_measurement,
        measured_instances=measured_instances,
        unmeasured_instances=unmeasured_instances,
    )

    # important: do not remember an answer nobody gave. Every cluster failing still returns 200
    # with an empty list, so caching it would pin emptiness for the whole TTL and, on a forced
    # refresh, overwrite good data the operator could still see. The degradation labels are
    # cached WITH the payload, so a cached partial read still renders its banner.
    if answered:
        cache.set("api_responses", cache_key, response.model_dump(), ttl=_CACHE_TTL)
    elif nodes_to_query:
        logger.warning("Not caching fleet snapshots: no cluster answered")
    # else: the caller's scope names no cluster at all, which is a settled answer rather than a
    # failed read. A tenant whose ACL grants nothing would otherwise warn on every request.

    return response
