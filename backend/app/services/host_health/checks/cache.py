"""Cache: Redis answers, how fast, and how close it is to its memory limit.

Without Redis every worker caches on its own, shared rate limits fall back per worker, and with
REDIS_ENABLED the WebSocket ticket store refuses tickets, so terminals and consoles stop opening.
"""

from __future__ import annotations

import time
from typing import List

from app.services.host_health.context import CheckContext
from app.services.host_health.format import host_of, size
from app.services.host_health.models import CheckResult, HealthFact, fact

LABEL = "Cache (Redis)"
MEMORY_WARN_RATIO = 0.9


def check(ctx: CheckContext) -> CheckResult:
    settings = ctx.settings
    if not settings.redis_url:
        return CheckResult(id="cache", label=LABEL, status="not_configured", checked_at=ctx.now,
                           summary="Redis is not configured (REDIS_URL); every worker caches on its own.")
    from app.core.cache import get_cache_manager

    client = getattr(get_cache_manager(), "redis_client", None)
    facts: List[HealthFact] = [fact("Address", host_of(settings.redis_url))]
    down = CheckResult(
        id="cache", label=LABEL, status="down", severity="critical", checked_at=ctx.now, facts=facts,
        summary="Redis is not answering. Workers cache on their own, and terminals and consoles may not open.",
        remediation="Check the redis container. A backend that started without Redis keeps running without it until it restarts.",
    )
    if client is None:
        facts.append(fact("Connection", "not connected since this worker started"))
        return down
    started = time.monotonic()
    try:
        client.ping()
        latency = int((time.monotonic() - started) * 1000)
        memory = client.info("memory")
        server = client.info("server")
    except Exception as exc:  # noqa: BLE001 - an unreachable Redis is a result
        facts.append(fact("Reason", type(exc).__name__))
        return down
    used = memory.get("used_memory")
    limit = memory.get("maxmemory") or 0
    facts += [fact("Version", server.get("redis_version")), fact("Response Time", f"{latency}ms"),
              fact("Memory Used", size(used) + (f" of {size(limit)}" if limit else " (no limit set)"))]
    if limit and isinstance(used, (int, float)) and used / limit >= MEMORY_WARN_RATIO:
        return CheckResult(id="cache", label=LABEL, status="degraded", severity="warning", checked_at=ctx.now,
                           summary=f"Redis is using {used / limit:.0%} of its memory limit; it will start evicting keys.",
                           facts=facts, latency_ms=latency,
                           remediation="Raise maxmemory on the redis container or find what is filling it.")
    return CheckResult(id="cache", label=LABEL, status="ok", summary="Redis answers.", facts=facts,
                       checked_at=ctx.now, latency_ms=latency)
