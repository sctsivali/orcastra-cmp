"""Host Health's report: collected by one worker every interval, served to every worker.

A request reads the stored report. A missing or stale one (the collector stopped, Redis lost it),
or `fresh`, runs the checks for that request, one run per worker at a time.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import List, Optional

from app.core.config import get_settings
from app.core.error_sanitizer import sanitize_error
from app.services.collector_leadership import CollectorLease
from app.services.host_health.context import CheckContext
from app.services.host_health.models import CheckResult, HostHealthReport, attention_of, overall_of
from app.services.host_health.runner import run_checks, run_in_pool
from app.services.host_health.store import Transition, _shared_redis, get_store

logger = logging.getLogger(__name__)

LEASE_KEY = "orcastra:host_health:collector_lease"
TRANSITION_ACTION = "host_health.transition"

_live_lock = asyncio.Lock()
_task: Optional[asyncio.Task] = None


def _interval() -> int:
    return int(get_settings().host_health_interval_seconds)


def _emit(transition: Transition, result: CheckResult, shared: bool) -> None:
    # The first sight of a healthy check is not an event; the first sight of a failing one is.
    if transition.previous is None and transition.status in ("ok", "not_configured"):
        return
    level = logging.INFO if transition.status == "ok" else logging.WARNING
    logger.log(level, "Host Health: %s %s -> %s: %s", transition.check_id,
               transition.previous or "unobserved", transition.status, result.summary)
    if not shared:
        # Every worker collects while Redis is down; each would write the same audit row.
        return
    try:
        from app.services.activity_logger import get_activity_logger

        recovered = transition.status in ("ok", "not_configured")
        # important: no target_host. A row without a cluster lands in the cluster log with
        # cluster_id NULL, which only Administrators read (core/task_scope.py).
        get_activity_logger().log_activity(
            action=TRANSITION_ACTION,
            target_type="host_dependency",
            target_id=transition.check_id,
            result="success" if recovered else "failure",
            error_code=None if recovered else transition.status,
            error_message=None if recovered else sanitize_error(result.summary),
            details={"from": transition.previous or "unobserved", "to": transition.status},
        )
    except Exception:  # noqa: BLE001 - the stdout line above stays; the audit copy must not cost the pass
        logger.debug("Host Health transition not audited", exc_info=True)


async def collect(source: str = "cached") -> HostHealthReport:
    settings = get_settings()
    store = get_store()
    ctx = CheckContext(settings=settings, store=store, now=time.time())
    results = await run_checks(ctx)
    marked: List[tuple] = []
    for result in results:
        since, transition = await run_in_pool(store.mark, result.id, result.status, ctx.now)
        result.since = since
        marked.append((transition, result))
    shared = store.mode == "shared"
    report = HostHealthReport(
        checks=results, overall=overall_of(results), attention=attention_of(results),
        collected_at=ctx.now, source=source, store="shared" if shared else "process",
        interval_seconds=int(settings.host_health_interval_seconds),
    )
    await run_in_pool(store.write_report, report.model_dump(mode="json"), _interval() * 3)
    for transition, result in marked:
        if transition is not None:
            _emit(transition, result, shared)
    return report


async def get_report(fresh: bool = False) -> HostHealthReport:
    store = get_store()
    if not fresh:
        cached = await run_in_pool(store.read_report)
        if cached and time.time() - float(cached.get("collected_at") or 0) < _interval() * 3:
            report = HostHealthReport.model_validate(cached)
            report.source = "cached"
            report.store = "shared" if store.mode == "shared" else "process"
            return report
    asked_at = time.time()
    async with _live_lock:
        # A request that waited behind a run which started after it asked takes that run's answer
        # instead of starting a second one: a burst of Check now clicks costs one pass.
        latest = await run_in_pool(store.read_report)
        if latest and float(latest.get("collected_at") or 0) >= asked_at:
            report = HostHealthReport.model_validate(latest)
            report.source = "live"
            return report
        return await collect(source="live")


async def mark_vault_unsealed() -> None:
    """Rewrite the stored report's Vault row the moment an unseal completes.

    The full pass that follows takes seconds (the registry read among them), and until it lands
    every worker would serve the stored "sealed" row, so the page and the dialog would disagree
    right after the operator succeeded. The transition is recorded here, once.
    """
    store = get_store()
    cached = await run_in_pool(store.read_report)
    if not cached:
        return
    now = time.time()
    report = HostHealthReport.model_validate(cached)
    for index, check in enumerate(report.checks):
        if check.id != "vault" or check.status == "ok":
            continue
        since, transition = await run_in_pool(store.mark, "vault", "ok", now)
        report.checks[index] = check.model_copy(update={
            "status": "ok", "severity": "info", "since": since, "checked_at": now, "actions": [],
            "remediation": None,
            "summary": "Vault was unsealed from the dashboard. The full check follows within seconds.",
        })
        if transition is not None:
            _emit(transition, report.checks[index], store.mode == "shared")
    report.overall = overall_of(report.checks)
    report.attention = attention_of(report.checks)
    await run_in_pool(store.write_report, report.model_dump(mode="json"), _interval() * 3)


def refresh_soon() -> None:
    """After an unseal: re-collect now so the stored report and the transition follow at once."""
    async def _run() -> None:
        try:
            async with _live_lock:
                await collect(source="live")
        except Exception:  # noqa: BLE001
            logger.debug("Host Health refresh after unseal failed", exc_info=True)

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        pass


async def _loop(interval: float) -> None:
    lease = CollectorLease(LEASE_KEY, int(interval * 2 + 15), _shared_redis)
    # Jittered so workers started together do not claim in lockstep.
    await asyncio.sleep(random.uniform(1.0, 5.0))
    try:
        while True:
            try:
                if await run_in_pool(lease.claim):
                    await collect(source="cached")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a blip must not end the loop
                logger.warning("Host Health pass failed", exc_info=True)
            await asyncio.sleep(interval)
    finally:
        try:
            lease.release()
        except Exception:  # noqa: BLE001
            pass


def start_host_health_loop() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop(float(_interval())))


async def stop_host_health_loop() -> None:
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
