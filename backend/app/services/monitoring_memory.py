"""Memory without reclaimable page cache for the dashboard's running instances.

The fleet list's state reports a container's cgroup usage and a VM's MemTotal - MemFree, both
of which count page cache the guest can reclaim, so Top Consumers ranked file I/O as memory.
One /1.0/metrics read per member carries every instance's meminfo; `honest_memory_for` turns it
into what the guest's programs hold, behind the gate the instance dialog uses. The read is the
member-wide one the dialog shares through the metrics cache, so a sweep and an open dialog on
the same member cost LXD one scrape per cache period.

important: bounded per read, per Cluster and per phase. A member that does not answer in time
leaves its rows on the state figure, flagged as including the cache, for this sweep only.
"""

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.member_names import is_standalone_location
from app.services.lxd.metrics_scrape import MemberScrape, member_metrics
from app.services.lxd.metrics_text import honest_memory_for
from app.services.monitoring_usage import InstanceUsage

logger = logging.getLogger(__name__)

SCRAPE_SECONDS = 6.0
PHASE_SECONDS = 8.0
# A scrape needs this much of its budget; metrics_scrape skips one with less.
_MIN_SCRAPE_SECONDS = 3.5
_PER_CLUSTER = 2
_IN_FLIGHT = 16
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="monitoring_memory")
# Agent states whose metrics describe the guest; see honest_memory_for.
_GUEST_METRICS = ("not_applicable", "connected")


def _member_scrape(lxd_service: Any, cluster: str, member: str, deadline: float) -> Optional[MemberScrape]:
    client = lxd_service.get_client_with_reconnect(cluster)
    if client is None:
        return None
    return member_metrics(client, cluster, member, deadline=deadline)


def _groups(usages: Sequence[InstanceUsage]) -> Dict[Tuple[str, str], List[int]]:
    """Row indexes per (Cluster, member), for the rows whose metrics could be used at all."""
    groups: Dict[Tuple[str, str], List[int]] = {}
    for index, usage in enumerate(usages):
        if usage.agent not in _GUEST_METRICS:
            continue
        location = usage.vm.location
        member = "none" if is_standalone_location(location) else str(location)
        groups.setdefault((usage.vm.host, member), []).append(index)
    return groups


async def apply_member_memory(
    lxd_service: Any, usages: Sequence[InstanceUsage]
) -> Tuple[List[InstanceUsage], Dict[str, int]]:
    """The rows with memory from their metrics where a member answered, and what was read."""
    groups = _groups(usages)
    phase_end = time.monotonic() + PHASE_SECONDS
    # important: run_blocking's per-cluster bound refuses a call past it rather than queueing
    # it, so a third member of one Cluster was refused on every sweep. The gate queues them.
    gates = {cluster: asyncio.Semaphore(_PER_CLUSTER) for cluster, _ in groups}

    async def one(cluster: str, member: str) -> Optional[MemberScrape]:
        async with gates[cluster]:
            return await read(cluster, member)

    async def read(cluster: str, member: str) -> Optional[MemberScrape]:
        budget = min(SCRAPE_SECONDS, phase_end - time.monotonic())
        if budget < _MIN_SCRAPE_SECONDS:
            return None
        try:
            return await run_blocking(
                _member_scrape,
                lxd_service,
                cluster,
                member,
                time.monotonic() + budget,
                timeout=budget,
                label=f"monitoring.memory[{cluster}]",
                executor=_executor,
                max_in_flight=_IN_FLIGHT,
                concurrency_key=cluster,
                max_in_flight_per_key=_PER_CLUSTER,
            )
        except (BlockingTimeout, BlockingPoolFull):
            return None
        except Exception as exc:  # noqa: BLE001 - memory then stays the state read's figure
            logger.debug("Member metrics for %s failed: %s", cluster, str(exc)[:200])
            return None

    keys = list(groups)
    scrapes = await asyncio.gather(*(one(cluster, member) for cluster, member in keys))
    rows = list(usages)
    honest = 0
    for key, scrape in zip(keys, scrapes):
        if scrape is None:
            continue
        for index in groups[key]:
            usage = rows[index]
            series = scrape.series.get((usage.vm.project or "default", usage.vm.name))
            reading = honest_memory_for(series, usage.agent or "")
            if reading is None:
                continue
            rows[index] = replace(usage, memory_bytes=int(reading["used"] or 0), memory_includes_cache=False)
            honest += 1
    counts = {
        "honest": honest,
        "with_cache": len(rows) - honest,
        "members": len(keys),
        "members_read": sum(1 for scrape in scrapes if scrape is not None),
    }
    return rows, counts
