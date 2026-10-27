"""Run every check at once, each under its own deadline, none able to take the pass down with it.

Blocking checks run on their own small pool through run_blocking: a timed-out thread keeps running
until its socket timeout fires, so each check's HTTP calls carry one, and the pool bound keeps a
hung dependency from queueing behind itself pass after pass.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Awaitable, Callable, List

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.error_sanitizer import sanitize_error
from app.services.host_health.checks import cache, database, identity, logging as logging_check, storage, vault
from app.services.host_health.context import CheckContext
from app.services.host_health.models import CheckResult

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="host_health")

# (id, label, check, deadline seconds). Display order: what stops the most comes first.
_BLOCKING = (
    ("vault", vault.LABEL, vault.check, 25.0),
    ("identity", identity.LABEL, identity.check, 12.0),
    ("logging", logging_check.LABEL, logging_check.check, 15.0),
)
_AFTER = (
    ("cache", cache.LABEL, cache.check, 10.0),
    ("storage", storage.LABEL, storage.check, 10.0),
)


def _unknown(check_id: str, label: str, ctx: CheckContext, reason: str) -> CheckResult:
    return CheckResult(id=check_id, label=label, status="unknown", severity="warning", checked_at=ctx.now,
                       summary=f"The {label.lower()} check did not finish: {reason}.")


async def _blocking(check_id: str, label: str, fn: Callable[[CheckContext], CheckResult],
                    deadline: float, ctx: CheckContext) -> CheckResult:
    try:
        return await run_blocking(fn, ctx, timeout=deadline, label=f"host_health.{check_id}",
                                  executor=_executor, max_in_flight=12)
    except BlockingTimeout:
        return _unknown(check_id, label, ctx, f"no answer within {deadline:.0f} s")
    except BlockingPoolFull:
        return _unknown(check_id, label, ctx, "earlier checks are still waiting on it")
    except Exception as exc:  # noqa: BLE001 - one broken check must not hide the others
        logger.warning("Host Health check %s failed: %s", check_id, sanitize_error(str(exc)), exc_info=True)
        return _unknown(check_id, label, ctx, type(exc).__name__)


async def _guarded(check_id: str, label: str, run: Awaitable[CheckResult], ctx: CheckContext) -> CheckResult:
    try:
        return await run
    except Exception as exc:  # noqa: BLE001
        logger.warning("Host Health check %s failed: %s", check_id, sanitize_error(str(exc)), exc_info=True)
        return _unknown(check_id, label, ctx, type(exc).__name__)


async def run_checks(ctx: CheckContext) -> List[CheckResult]:
    runs = [_blocking(cid, label, fn, deadline, ctx) for cid, label, fn, deadline in _BLOCKING]
    runs.append(_guarded("database", database.LABEL, database.check(ctx), ctx))
    runs += [_blocking(cid, label, fn, deadline, ctx) for cid, label, fn, deadline in _AFTER]
    return list(await asyncio.gather(*runs))


async def run_in_pool(fn: Callable, *args, timeout: float = 10.0, label: str = "host_health.store"):
    """For the store's Redis calls, which are synchronous."""
    return await run_blocking(fn, *args, timeout=timeout, label=label, executor=_executor, max_in_flight=12)
