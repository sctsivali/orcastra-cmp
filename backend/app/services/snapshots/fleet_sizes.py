"""The size phase of the fleet snapshot read: one GET per visible snapshot-holding instance.

The bulk listing answers -1 for every snapshot size, so a byte count needs a second read per
instance. It runs after the router's visibility filter, over the visible holders only, through a
per-host cache, bounded per cluster and per fleet, against a deadline inside the request budget.

important: nothing here can mark a cluster unreachable; a failed size read, per holder or per
cluster, leaves rows unmeasured and the cluster answered (the response counters say how far the
phase got). Every LXD read runs on a worker thread after the tenant decisions on the event loop:
holders come from rows that passed project_is_visible and check_tenant_permission, so an instance
the caller cannot see is never read.
"""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.cache import SNAPSHOT_SIZES_KEY_PREFIX, get_cache_manager
from app.services.lxd import snapshot_fleet as fleet_service
from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.lazy_client import connect_or_get_client
from app.services.lxd.snapshot_fleet import Holder
from app.services.lxd_service import LXDService

logger = logging.getLogger(__name__)

# 60 percent of the router's cluster budget, so a last in-flight read bounded by
# SIZE_READ_TIMEOUT has returned by 37s. Clamped further by whatever the listing phase already
# spent (see _deadline). Below the minimum window the phase does not start at all, because a
# read that will certainly be abandoned costs a thread and answers nothing.
SIZES_BUDGET = 24
SIZES_MIN_WINDOW = 2
SIZES_TTL = 600

# Live size reads per request across every cluster: five clusters at 30 holders each. Cache hits
# do not count.
MAX_SIZE_HOLDERS_FLEET = 150

# important: not get_scope_cache_key. A size is a fact about the cluster whoever asks, and the
# map is only ever consulted for rows already past the visibility filter.
#
# important: outside the `snapshots_fleet:` prefix, which every list clear sweeps
# (invalidate_resource_cache("snapshots_fleet"), invalidate_instance_cache); under it, one snapshot
# write on any cluster would make the next fleet read re-measure every holder on every cluster. A
# snapshot write forgets only its own holder (forget_holder), and an entry records each snapshot's
# created_at, so a snapshot or an instance created again under an old name is a miss rather than an
# old size. Deregistering a cluster drops its map (cache.py invalidate_cluster_cache), since the id
# can be registered again.
SIZES_KEY_PREFIX = SNAPSHOT_SIZES_KEY_PREFIX

# Rows of one request grouped by (host, project, source_name), the identity the ACL decided on.
RowKey = Tuple[str, str, str]
# {project: {instance: {"at": epoch seconds, "sizes": {name: size}, "created": {name: created_at}}}}
HostSizesMap = Dict[str, Dict[str, Dict[str, Any]]]


def _measure_one_cluster(
    lxd_service: LXDService,
    host: str,
    holders: List[Holder],
    deadline: float,
) -> Tuple[Dict[Holder, Dict[str, Optional[int]]], List[Holder]]:
    """Sizes for one cluster's visible holders, on a worker thread."""
    client = connect_or_get_client(lxd_service, host)
    if client is None:
        raise LXDConnectionError(f"Could not connect to {host}")
    return fleet_service.measure_holder_sizes(client, host, holders, deadline=deadline)


def _deadline(request_started: float, cluster_budget: float) -> Optional[float]:
    """When the phase must stop, or None when too little of the request budget is left."""
    now = time.monotonic()
    deadline = min(
        now + SIZES_BUDGET,
        request_started + cluster_budget - fleet_service.SIZE_READ_TIMEOUT[1] - 3,
    )
    return deadline if deadline - now >= SIZES_MIN_WINDOW else None


def _cached_map(cache: Any, host: str) -> HostSizesMap:
    stored = cache.get("api_responses", f"{SIZES_KEY_PREFIX}{host}")
    return stored if isinstance(stored, dict) else {}


def _cache_hit(
    entry: Optional[Dict[str, Any]], rows: List[Dict[str, Any]], now: float
) -> Optional[Dict[str, Optional[int]]]:
    """The cached sizes for a holder, or None when the entry is absent, expired or incomplete.

    Every visible snapshot must be present under its name and its created_at: a snapshot taken
    since the entry was written, or one deleted and taken again under the same name, is a miss
    for its whole holder, so it is re-read rather than shown with a size that is not its own.
    """
    if not isinstance(entry, dict):
        return None
    sizes = entry.get("sizes")
    if not isinstance(sizes, dict) or now - float(entry.get("at") or 0) > SIZES_TTL:
        return None
    created = entry.get("created")
    created = created if isinstance(created, dict) else {}
    for row in rows:
        if row["name"] not in sizes or created.get(row["name"]) != row.get("created_at"):
            return None
    return sizes


def _apply(rows: List[Dict[str, Any]], sizes: Dict[str, Optional[int]]) -> None:
    for row in rows:
        row["size_measured"] = True
        if row.get("size") is None:
            row["size"] = sizes.get(row["name"])


def _write_back(
    cache: Any,
    host: str,
    stored: HostSizesMap,
    measured: Dict[Holder, Dict[str, Optional[int]]],
    rows_by_holder: Dict[Holder, List[Dict[str, Any]]],
) -> None:
    """Merge fresh entries over the unexpired ones and write the host's map back.

    Merged rather than replaced because one request measures only its own visible holders, and
    a narrow scope must not drop what a wider one measured a moment ago. Two scopes writing the
    same host at once is last-writer-wins on entries that are each within TTL, which is benign.
    Each entry keeps the created_at of the rows it was measured for, which _cache_hit checks.
    """
    now = time.time()
    merged: HostSizesMap = {}
    for project, instances in stored.items():
        if not isinstance(instances, dict):
            continue
        for name, entry in instances.items():
            if isinstance(entry, dict) and now - float(entry.get("at") or 0) <= SIZES_TTL:
                merged.setdefault(project, {})[name] = entry
    for (project, name), sizes in measured.items():
        created = {row["name"]: row.get("created_at") for row in rows_by_holder.get((project, name), [])}
        merged.setdefault(project, {})[name] = {"at": now, "sizes": sizes, "created": created}
    cache.set("api_responses", f"{SIZES_KEY_PREFIX}{host}", merged, ttl=SIZES_TTL)


def forget_holder(cache: Any, host: str, project: str, instance: str) -> None:
    """Drop one instance's cached sizes after a snapshot write on it, so the next read measures it.

    Only that holder: a write on one instance changes no other instance's sizes. Best effort and
    never raises, because the write it follows has already happened. A read in flight across the
    write can put the old entry back; the created_at check still catches a new or re-used name,
    and what is left is a neighbour's size up to SIZES_TTL old.
    """
    try:
        stored = _cached_map(cache, host)
        instances = stored.get(project)
        if not isinstance(instances, dict) or instance not in instances:
            return
        del instances[instance]
        cache.set("api_responses", f"{SIZES_KEY_PREFIX}{host}", stored, ttl=SIZES_TTL)
    except Exception as exc:  # noqa: BLE001 - the write already happened; the TTL still bounds it
        logger.debug("Could not forget the cached sizes of %s/%s on %s: %s", project, instance, host, exc)


def _plan_live_reads(
    by_key: Dict[RowKey, List[Dict[str, Any]]],
    stored: Dict[str, HostSizesMap],
    *,
    fresh: bool,
) -> Tuple[Dict[str, List[Holder]], int]:
    """Apply cache hits and choose the live reads. Returns (misses by host, holders left out).

    Holders are visited in sorted order and the caps are applied per cluster first, then across
    the fleet in host order, so the set that gets read is the same on every request that sees
    the same rows.
    """
    now = time.time()
    misses_by_host: Dict[str, List[Holder]] = {}
    left_out = 0
    for host, project, name in sorted(by_key):
        rows = by_key[(host, project, name)]
        instances = stored[host].get(project)
        entry = instances.get(name) if isinstance(instances, dict) else None
        hit = None if fresh else _cache_hit(entry, rows, now)
        if hit is not None:
            _apply(rows, hit)
            continue
        misses = misses_by_host.setdefault(host, [])
        if len(misses) >= fleet_service.MAX_SIZE_HOLDERS_PER_CLUSTER:
            left_out += 1
            continue
        misses.append((project, name))

    budget = MAX_SIZE_HOLDERS_FLEET
    for host in sorted(misses_by_host):
        kept = misses_by_host[host][:budget]
        left_out += len(misses_by_host[host]) - len(kept)
        misses_by_host[host] = kept
        budget -= len(kept)
    return {host: misses for host, misses in misses_by_host.items() if misses}, left_out


async def measure_visible_sizes(
    lxd_service: LXDService,
    visible: List[Dict[str, Any]],
    *,
    fresh: bool,
    request_started: float,
    executor: ThreadPoolExecutor,
    max_in_flight: int,
    cluster_budget: float,
) -> Tuple[int, int]:
    """Stamp sizes onto the visible rows. Returns (measured, unmeasured) holder counts.

    `fresh` bypasses the cache for reads and re-measures; the result is still written back.
    `request_started` is the time.monotonic() instant the listing phase began, which is what
    clamps this phase inside `cluster_budget`.
    """
    by_key: Dict[RowKey, List[Dict[str, Any]]] = {}
    for row in visible:
        key = (row.get("host") or "", row.get("project") or "", row.get("source_name") or "")
        by_key.setdefault(key, []).append(row)
    if not by_key:
        return 0, 0

    cache = get_cache_manager()
    stored = {host: _cached_map(cache, host) for host in {key[0] for key in by_key}}
    misses_by_host, unmeasured = _plan_live_reads(by_key, stored, fresh=fresh)

    deadline = _deadline(request_started, cluster_budget)
    if deadline is None:
        unmeasured += sum(len(misses) for misses in misses_by_host.values())
        return len(by_key) - unmeasured, unmeasured

    async def _gather(host: str, misses: List[Holder]):
        return await run_blocking(
            _measure_one_cluster,
            lxd_service,
            host,
            misses,
            deadline,
            timeout=cluster_budget,
            label=f"snapshots.sizes[{host}]",
            executor=executor,
            max_in_flight=max_in_flight,
        )

    hosts = sorted(misses_by_host)
    results = await asyncio.gather(
        *(_gather(host, misses_by_host[host]) for host in hosts), return_exceptions=True
    )
    for host, result in zip(hosts, results):
        if isinstance(result, BaseException):
            # Logged with the traceback unless it is plain connectivity, so a defect in the
            # size path does not pass for a slow cluster. Never re-raised: the listing already
            # answered and sizes are the one thing this phase may lose.
            connectivity = isinstance(
                result, (LXDConnectionError, BlockingTimeout, BlockingPoolFull)
            )
            logger.warning(
                "Snapshot size read failed on %s, its rows stay unmeasured: %s",
                host, result, exc_info=None if connectivity else result,
            )
            unmeasured += len(misses_by_host[host])
            continue
        measured, missed = result
        unmeasured += len(missed)
        for (project, name), sizes in measured.items():
            _apply(by_key[(host, project, name)], sizes)
        if measured:
            rows_by_holder = {holder: by_key[(host, *holder)] for holder in measured}
            _write_back(cache, host, stored[host], measured, rows_by_holder)
    return len(by_key) - unmeasured, unmeasured
