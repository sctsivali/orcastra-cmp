"""One cluster member's resources, read as a shallow concurrent fan-out.

This used to live inline in `routers/cluster_members.py` and cost, per uncached open:

    1  GET /1.0/resources?target=<member>
    1  GET /1.0/storage-pools?target=<member>          (paths only, no recursion)
    2N GET /1.0/storage-pools/<p>[?/resources]         (sequential, per pool)
    1  GET /1.0/instances                              (recursion=0, so URL strings only)
    M  GET /1.0/instances/<name>                       (one per instance in the CLUSTER)

all sequential, all on the event loop, each bounded by the pooled socket timeout of (3, 8).

The M term is the one nobody wrote. `client.instances.all()` defaults to recursion=0, which
returns a list of URLs, and pylxd fills those into `Instance` objects carrying only a name
(pylxd/models/instance.py:305-334). `location` is a declared model attribute
(instance.py:62), so the first `getattr(instance, 'location', ...)` falls into
`Model.__getattribute__` (_model.py:151-158), which calls `sync()` (_model.py:203), which is
a GET. On a three-member MicroCloud carrying a couple of dozen instances that is the dominant
cost, it scales with the whole cluster rather than with the member being read, and it is
invisible at the call site. Asking for recursion=1 populates every attribute up front and the
term disappears.

What is left is four hops deep regardless of instance count: the resources read, then the pool
list and the instance list together, then the per-pool space reads together.

note: every leaf here is a plain blocking function reached through `run_blocking`, which is
what lets the orchestrator overlap them. The wall-clock cap on each is deliberately larger than
the socket timeout it wraps: the socket is what actually bounds the call, and a cap below it
would report a timeout for a request still inside its own budget.
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

logger = logging.getLogger(__name__)

# important: the worker count matches the in-flight cap on purpose. `run_blocking` starts its
# wall-clock timer when the call is submitted, not when a thread picks it up, so a pool with
# fewer threads than permitted in-flight calls lets a request sit in the executor's queue while
# its own timeout runs down. That surfaces as a 504 blaming a cluster that was never asked
# anything. With the two equal, a call either runs immediately or is shed at the door.
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

    important: `collected_at` is minted at the same statement as the read, so it is collection
    time rather than the time some later consumer looked at it. The 60s response cache carries
    it untouched, which is the whole point of the field.
    """
    response = client.api.resources.get(params={"target": member_name})
    collected_at = datetime.now(timezone.utc)
    return response.json().get("metadata", {}) or {}, collected_at


def list_member_pools(client: pylxd.Client, member_name: str) -> List[Dict[str, Any]]:
    """Storage pools on this member, with their driver, in one request.

    recursion=1 is what removes the per-pool detail call. A daemon that ignores it answers with
    path strings instead of objects; that case is handled by the caller rather than assumed
    away, because reporting driver "unknown" for every pool would look like data rather than a
    failed negotiation.
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

    important: EVERY project, not just default. `client.instances.all()` reads through the
    pooled client, which pylxd binds to `default`, so a member was measured by whatever share
    of its work happened to live there. The EcoBytes audit recorded 1 of 1 on a member holding
    44, and the same list feeds `build_gpu_inventory`, so `gpu_used` was wrong by the same
    margin, which reads as spare capacity.

    important: recursion=2, never the default. At recursion=0 each later attribute read is its
    own HTTP request, and `build_gpu_inventory` duck-types `location`, `expanded_config` and
    `expanded_devices`, which only a recursion=2 body carries. `InstanceRecord` exposes exactly
    those, because a raw dict would make every getattr miss and silently report zero.
    """
    return list_instance_records(client)


def parse_cpu_limit(limits_cpu: Any) -> int:
    """How many cores a `limits.cpu` value claims.

    LXD accepts a count ("4"), a pinned set ("0,1,2") and a pinned range ("0-3"). An instance
    with no limit is counted as one, which is what the surface has always reported.
    """
    if not limits_cpu:
        return 1
    raw = str(limits_cpu)
    try:
        if "-" in raw:
            low, high = raw.split("-", 1)
            return int(high) - int(low) + 1
        if "," in raw:
            return len(raw.split(","))
        return int(raw)
    except (ValueError, IndexError):
        return 1


def count_member_instances(instances: Iterable[Any], member_name: str) -> InstanceUsage:
    """Instance totals for one member, scoped by `instance.location`."""
    count = running = cpu_used = 0
    for instance in instances:
        if getattr(instance, "location", "") != member_name:
            continue
        count += 1
        if getattr(instance, "status", "") != "Running":
            continue
        running += 1
        config = getattr(instance, "config", {}) or {}
        cpu_used += parse_cpu_limit(config.get("limits.cpu"))
    return InstanceUsage(count=count, running=running, cpu_used=cpu_used)


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
    for entry in raw:
        if isinstance(entry, dict):
            name = entry.get("name")
            if name:
                named.append((name, entry.get("driver") or "unknown"))
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
    """Read everything one member's panel needs, overlapping what does not depend on itself.

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

    # Independent of each other, so they overlap. return_exceptions is not needed here because
    # each helper owns its own failure, but a bug in one must not lose the other's result.
    await asyncio.gather(
        _collect_pools(client, node, member_name, fanout),
        _collect_instances(client, node, member_name, fanout),
        return_exceptions=False,
    )
    return fanout
