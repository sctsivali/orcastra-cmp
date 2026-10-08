"""The monitoring sweep's fleet read, its per-phase timing and one log line per collection.

important: the dashboard reads cluster status through this module, one bounded read per cluster
on its own executor, instead of check_nodes_status_async. A cluster whose read runs past the wall
is reported as not answering for this sweep only: it is never cached as offline here and never
recorded on the breaker, and a slow cluster can no longer hold the whole sweep.

note: the line carries the phase timings in its message, so the logging dedup filter (identical
messages within 60 s are folded) never swallows a sweep.
"""

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Awaitable, Dict, List, Optional, Tuple, TypeVar

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.models.schemas import NodeStatus, WarningsSummary

logger = logging.getLogger(__name__)

T = TypeVar("T")

STATUS_WALL_SECONDS = float(os.environ.get("ORCASTRA_MONITORING_STATUS_WALL_SECONDS", "120"))
WARNINGS_WALL_SECONDS = 8.0
_STATUS_PER_CLUSTER = 3
_STATUS_IN_FLIGHT = 16
_STATUS_PER_SWEEP = 8
_status_executor = ThreadPoolExecutor(max_workers=_STATUS_IN_FLIGHT, thread_name_prefix="monitoring_status")
_warnings_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="monitoring_warnings")
PHASES = ("inventory", "status", "rates", "scrapes", "warnings", "history")


class SweepTimer:
    """Wall-clock milliseconds per named phase of one dashboard build."""

    def __init__(self) -> None:
        self._started = time.monotonic()
        self.phases: Dict[str, int] = {}

    async def timed(self, name: str, awaitable: Awaitable[T]) -> T:
        started = time.monotonic()
        try:
            return await awaitable
        finally:
            self.record(name, started)

    def record(self, name: str, started: float) -> None:
        self.phases[name] = round((time.monotonic() - started) * 1000)

    def elapsed_ms(self) -> int:
        return round((time.monotonic() - self._started) * 1000)


@dataclass
class FleetRead:
    vms: List[Any]
    connected: List[str]
    statuses: List[NodeStatus]
    status_ms: Dict[str, int] = field(default_factory=dict)
    # cluster -> why this sweep has no answer from it ("status" timed out, "busy" pool full)
    timed_out: Dict[str, str] = field(default_factory=dict)


def _not_answering(name: str, reason: str) -> NodeStatus:
    return NodeStatus(name=name, endpoint="unknown", connected=False, reachable=False, error=reason)


async def read_statuses(
    lxd_service: Any, names: List[str]
) -> Tuple[List[NodeStatus], Dict[str, int], Dict[str, str]]:
    """Each cluster's status, read in parallel and bounded per cluster."""
    gate = asyncio.Semaphore(_STATUS_PER_SWEEP)
    status_ms: Dict[str, int] = {}
    timed_out: Dict[str, str] = {}

    async def one(name: str) -> NodeStatus:
        async with gate:
            started = time.monotonic()
            try:
                return await run_blocking(
                    lxd_service.check_node_status,
                    name,
                    timeout=STATUS_WALL_SECONDS,
                    label=f"monitoring.status[{name}]",
                    executor=_status_executor,
                    max_in_flight=_STATUS_IN_FLIGHT,
                    concurrency_key=name,
                    max_in_flight_per_key=_STATUS_PER_CLUSTER,
                )
            except BlockingTimeout:
                timed_out[name] = "status"
                return _not_answering(
                    name,
                    f"Did not answer within {int(STATUS_WALL_SECONDS)} s during this collection. "
                    "It is asked again on the next one.",
                )
            except BlockingPoolFull:
                timed_out[name] = "busy"
                return _not_answering(name, "Still being read by an earlier collection.")
            except Exception as exc:  # noqa: BLE001 - same shape check_nodes_status_async answers
                logger.warning("Node check failed for %s: %s", name, exc)
                return _not_answering(name, str(exc))
            finally:
                status_ms[name] = round((time.monotonic() - started) * 1000)

    statuses = await asyncio.gather(*(one(name) for name in names))
    return list(statuses), status_ms, timed_out


async def read_fleet(lxd_service: Any, timer: SweepTimer) -> FleetRead:
    """The fleet's instances and every cluster's status, read concurrently and timed."""
    names = list(lxd_service.get_node_names())
    inventory = timer.timed("inventory", lxd_service.list_all_vms())
    status = timer.timed("status", read_statuses(lxd_service, names))
    (vms, connected), (statuses, status_ms, timed_out) = await asyncio.gather(inventory, status)
    return FleetRead(list(vms), list(connected), statuses, status_ms, timed_out)


def _node_warnings(lxd_service: Any, name: str) -> List[Dict[str, Any]]:
    try:
        client = lxd_service.get_client_with_reconnect(name)
        if client:
            return client.api.warnings.get(params={"recursion": "1"}).json().get("metadata", [])
    except Exception as exc:  # noqa: BLE001 - a cluster's warnings are optional to the sweep
        logger.debug("Could not get warnings for node %s: %s", name, exc)
    return []


async def count_warnings(lxd_service: Any, names: List[str]) -> Tuple[WarningsSummary, List[str]]:
    """Warnings across the answering clusters; a cluster past its wall is left out and named."""
    summary = WarningsSummary(total=0, critical=0, warning=0, info=0)
    skipped: List[str] = []

    async def one(name: str) -> List[Dict[str, Any]]:
        try:
            return await run_blocking(
                _node_warnings,
                lxd_service,
                name,
                timeout=WARNINGS_WALL_SECONDS,
                label=f"monitoring.warnings[{name}]",
                executor=_warnings_executor,
                max_in_flight=8,
                concurrency_key=name,
                max_in_flight_per_key=2,
            )
        except (BlockingTimeout, BlockingPoolFull):
            skipped.append(name)
            return []

    for rows in await asyncio.gather(*(one(name) for name in names)):
        for warning in rows:
            if not isinstance(warning, dict):
                continue
            summary.total += 1
            severity = str(warning.get("severity", "")).lower()
            if severity in ("critical", "error"):
                summary.critical += 1
            elif severity == "warning":
                summary.warning += 1
            else:
                summary.info += 1
    return summary, skipped


def log_sweep(
    *,
    collected: bool,
    timer: SweepTimer,
    fleet: FleetRead,
    running: int,
    history_written: bool,
    warnings_skipped: Optional[List[str]] = None,
    memory: Optional[Dict[str, int]] = None,
) -> None:
    """One INFO line per collected sweep (the collector and admins); other scopes log at DEBUG."""
    total = timer.elapsed_ms()
    phases = {name: timer.phases.get(name, 0) for name in PHASES}
    memory = memory or {}
    message = (
        f"Monitoring sweep {total} ms: "
        + ", ".join(f"{name} {phases[name]}" for name in PHASES)
        + f"; clusters {len(fleet.statuses)}, answered {len(fleet.connected)}, "
        f"timed out {len(fleet.timed_out)}; running {running}, "
        f"memory from metrics {memory.get('honest', 0)}"
    )
    fields = {
        "monitoring_sweep": {
            "total_ms": total,
            "phases_ms": phases,
            "status_ms_by_cluster": dict(fleet.status_ms),
            "timed_out": dict(fleet.timed_out),
            "warnings_skipped": list(warnings_skipped or []),
            "running": running,
            "memory": dict(memory),
            "history_written": history_written,
        }
    }
    logger.log(logging.INFO if collected else logging.DEBUG, message, extra={"extra_fields": fields})
