"""One cluster member's resources, read as a shallow concurrent fan-out.

At most four hops deep regardless of instance count: the resources read, then the pool list and
instance list together, then per pool a detail read (only for a list without recursion), then a
space read. Leaves run through `run_blocking` so they overlap; each wall-clock cap is larger
than the socket timeout it wraps, or a request still inside its budget would report a timeout.

The instance list asks for recursion: at recursion=0 `client.instances.all()` yields name-only
`Instance` objects (pylxd/models/instance.py:305-334), and the first read of `location`
(instance.py:62) goes through `Model.__getattribute__` (_model.py:151-158) into `sync()`
(_model.py:203), one hidden GET per instance in the cluster.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pylxd

from app.core.blocking import run_blocking
from app.services.lxd.project_scope import list_instance_records
from app.core.lxd_path import safe_segment
from app.services.lxd.cpu_allocation import allocated_cpu
from app.services.lxd.cpu_allocation import parse_cpu_limit as _parse_cpu_limit

logger = logging.getLogger(__name__)

# important: the worker count matches the in-flight cap. `run_blocking` starts its wall-clock
# timer at submission, before a thread picks the call up, so with fewer threads than permitted
# calls a request can time out in the executor's queue, as a 504 blaming a cluster that was
# never asked. With the two equal, a call either runs at once or is shed at the door.
_LEAF_CONCURRENCY: int = 16
_leaf_executor = ThreadPoolExecutor(
    max_workers=_LEAF_CONCURRENCY, thread_name_prefix="member_res"
)

# One connect budget (3s) plus one read budget (8s) plus margin. The pooled socket timeout is
# the real bound; this only stops a wedged thread from holding the request open forever.
LEAF_TIMEOUT: float = 12.0
LEAF_MAX_IN_FLIGHT: int = _LEAF_CONCURRENCY

# Labels that may appear in `degraded_parts`. This endpoint applies no tenant ACL, so the set
# stays fixed and carries no instance name, endpoint or error text.
PART_STORAGE = "storage"
PART_INSTANCES = "instances"


@dataclass(frozen=True)
class PoolUsage:
    """One storage pool as this member sees it."""

    name: str
    driver: str
    space_total: int
    space_used: int
    # The members the pool is defined on, from the pool record's `locations`. A shared pool
    # (ceph, cephfs) lists every member; the router reports it once, scoped `shared`, with
    # this list, so the dialog can say which members it serves.
    members: Tuple[str, ...] = ()

    @property
    def space_available(self) -> int:
        return self.space_total - self.space_used

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "driver": self.driver,
            "space_total": self.space_total,
            "space_used": self.space_used,
            "space_available": self.space_available,
        }


@dataclass(frozen=True)
class InstanceUsage:
    """Instance counts and claimed CPU for one member."""

    count: int = 0
    running: int = 0
    cpu_used: int = 0


@dataclass
class MemberFanout:
    """Everything one member read returned, plus what it failed to return."""

    resources_body: Dict[str, Any]
    collected_at: datetime
    pools: List[PoolUsage] = field(default_factory=list)
    instances: List[Any] = field(default_factory=list)
    degraded_parts: List[str] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return bool(self.degraded_parts)


def read_member_resources(
    client: pylxd.Client, member_name: str
) -> Tuple[Dict[str, Any], datetime]:
    """The member's own /1.0/resources body, and the moment it came off the cluster.

    `collected_at` is taken right after the read, so it is collection time; the 60s response
    cache carries it untouched.
    """
    response = client.api.resources.get(params={"target": member_name})
    collected_at = datetime.now(timezone.utc)
    return response.json().get("metadata", {}) or {}, collected_at


def list_member_pools(client: pylxd.Client, member_name: str) -> List[Dict[str, Any]]:
    """Storage pools on this member, with their driver, in one request.

    recursion=1 removes the per-pool detail call. A daemon that ignores it answers with path
    strings; `_collect_pools` then reads each pool's record, since "unknown" for every driver
    would look like data.
    """
    response = client.api["storage-pools"].get(
        params={"target": member_name, "recursion": 1}
    )
    return response.json().get("metadata", []) or []


def read_pool_detail(
    client: pylxd.Client, member_name: str, pool_name: str
) -> Dict[str, Any]:
    """One pool's own record. Only reached when the list came back without recursion."""
    response = client.api["storage-pools"][safe_segment(pool_name)].get(
        params={"target": member_name}
    )
    return response.json().get("metadata", {}) or {}


def read_pool_space(
    client: pylxd.Client, member_name: str, pool_name: str
) -> Dict[str, int]:
    """One pool's space figures on this member."""
    response = client.api["storage-pools"][safe_segment(pool_name)]["resources"].get(
        params={"target": member_name}
    )
    space = (response.json().get("metadata", {}) or {}).get("space", {}) or {}
    return {
        "total": int(space.get("total", 0) or 0),
        "used": int(space.get("used", 0) or 0),
    }


def list_cluster_instances(client: pylxd.Client) -> Tuple[List[Any], bool]:
    """Every instance on the cluster, in every project, fully populated.

    Returns (records, complete).

    important: read every project. `client.instances.all()` reads through the pooled client,
    which pylxd binds to `default`, so a member would be measured by whatever share of its work
    lives there, and `build_gpu_inventory`, fed by the same list, would under-report `gpu_used`,
    which reads as spare capacity.

    important: recursion=1, never the default of 0. At recursion=0 each later attribute read is
    its own HTTP request. `build_gpu_inventory` duck-types `location`, `expanded_config` and
    `expanded_devices`, which `InstanceRecord` exposes; a raw dict would make every getattr miss
    and silently report zero.
    """
    return list_instance_records(client)


def parse_cpu_limit(limits_cpu: Any) -> int:
    """How many cores a `limits.cpu` value claims.

    LXD accepts a count ("4"), a pinned set ("0,1,2") and a pinned range ("0-3"). An instance
    with no limit counts as one. The grammar lives in services/lxd/cpu_allocation.
    """
    return _parse_cpu_limit(limits_cpu, default=1)


def count_member_instances(instances: Iterable[Any], member_name: str) -> InstanceUsage:
    """Instance totals for one member, scoped by `instance.location`.

    "CPU used" is allocation: the sum of the expanded `limits.cpu` of the running instances
    placed on this member (a limit inherited from a profile counts; one core for an instance
    with no limit). LXD's resources API has no host utilisation figure, so the UI labels it as
    allocated. Counted by services/lxd/cpu_allocation.
    """
    got = allocated_cpu(instances, member=member_name)
    return InstanceUsage(count=got.instances, running=got.running, cpu_used=got.cores)


async def _leaf(fn, *args, label: str):
    """Run one blocking leaf read off the event loop."""
    return await run_blocking(
        fn,
        *args,
        timeout=LEAF_TIMEOUT,
        label=label,
        executor=_leaf_executor,
        max_in_flight=LEAF_MAX_IN_FLIGHT,
    )


async def _collect_pools(
    client: pylxd.Client, node: str, member_name: str, fanout: MemberFanout
) -> None:
    """Fill `fanout.pools`, marking the part degraded rather than reporting zero as a fact."""
    try:
        raw = await _leaf(
            list_member_pools, client, member_name, label=f"member_pools[{node}/{member_name}]"
        )
    except Exception as err:
        logger.warning("Could not list storage pools for %s/%s: %s", node, member_name, err)
        fanout.degraded_parts.append(PART_STORAGE)
        return

    # A daemon that honoured recursion returns objects; one that did not returns paths.
    named: List[Tuple[str, Optional[str]]] = []
    locations: Dict[str, Tuple[str, ...]] = {}
    for entry in raw:
        if isinstance(entry, dict):
            name = entry.get("name")
            if name:
                named.append((name, entry.get("driver") or "unknown"))
                locations[name] = tuple(str(m) for m in (entry.get("locations") or []))
        elif isinstance(entry, str) and entry:
            named.append((entry.rstrip("/").split("/")[-1], None))

    logger.info(
        "Found %d storage pools for member %s: %s",
        len(named),
        member_name,
        [n for n, _ in named],
    )

    async def _one(name: str, driver: Optional[str]) -> PoolUsage:
        if driver is None:
            detail = await _leaf(
                read_pool_detail,
                client,
                member_name,
                name,
                label=f"member_pool_detail[{node}/{member_name}/{name}]",
            )
            driver = detail.get("driver") or "unknown"
        space = await _leaf(
            read_pool_space,
            client,
            member_name,
            name,
            label=f"member_pool_space[{node}/{member_name}/{name}]",
        )
        return PoolUsage(
            name=name,
            driver=driver,
            space_total=space["total"],
            space_used=space["used"],
            members=locations.get(name, ()),
        )

    results = await asyncio.gather(
        *(_one(name, driver) for name, driver in named), return_exceptions=True
    )
    for (name, _), result in zip(named, results):
        if isinstance(result, BaseException):
            logger.warning("Could not read pool %s on %s/%s: %s", name, node, member_name, result)
            fanout.degraded_parts.append(f"{PART_STORAGE}:{name}")
            continue
        fanout.pools.append(result)


async def _collect_instances(
    client: pylxd.Client, node: str, member_name: str, fanout: MemberFanout
) -> None:
    """Fill `fanout.instances`, marking the part degraded on failure.

    important: an instance-scan failure has to be visible. Swallowed, it reports zero instances
    and zero allocated GPUs for a member that is running work, which reads as capacity that is
    free.
    """
    try:
        fanout.instances, complete = await _leaf(
            list_cluster_instances, client, label=f"member_instances[{node}]"
        )
        if not complete:
            # A read that could not cover every project is a floor, not a total, and an
            # undercount here reads as free capacity.
            fanout.degraded_parts.append(PART_INSTANCES)
    except Exception as err:
        logger.warning("Could not list instances for %s/%s: %s", node, member_name, err)
        fanout.degraded_parts.append(PART_INSTANCES)


async def collect_member_fanout(
    client: pylxd.Client, node: str, member_name: str
) -> MemberFanout:
    """Read everything one member's panel needs, overlapping the independent reads.

    The primary resources read is awaited alone and its failure propagates: without it there is
    no answer to give, and the router maps the transport error onto a status the operator can
    act on. Everything after it is best effort and records what it lost.
    """
    body, collected_at = await _leaf(
        read_member_resources,
        client,
        member_name,
        label=f"member_resources[{node}/{member_name}]",
    )
    fanout = MemberFanout(resources_body=body, collected_at=collected_at)

    # Independent of each other, so they overlap. Each helper records its own failure, so only a
    # bug propagates out of the gather.
    await asyncio.gather(
        _collect_pools(client, node, member_name, fanout),
        _collect_instances(client, node, member_name, fanout),
        return_exceptions=False,
    )
    return fanout
