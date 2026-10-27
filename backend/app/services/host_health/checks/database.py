"""Database: Postgres answers, how fast, and whether its schema is the one this build expects."""

from __future__ import annotations

import asyncio
import functools
import time
from typing import List

from sqlalchemy import text

from app.services.host_health.context import CheckContext
from app.services.host_health.models import CheckResult, HealthFact, fact

LABEL = "Database"
TIMEOUT_SECONDS = 5.0


@functools.lru_cache(maxsize=1)
def _heads() -> tuple:
    from app.core.database import _alembic_heads

    return tuple(sorted(_alembic_heads()))


async def _read() -> tuple:
    from app.core.database import engine

    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
        version = (await conn.execute(text("SHOW server_version"))).scalar()
        revisions = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    pool = engine.pool
    in_use = pool.checkedout() if hasattr(pool, "checkedout") else None
    size = pool.size() if hasattr(pool, "size") else None
    return version, tuple(sorted(revisions)), in_use, size


async def check(ctx: CheckContext) -> CheckResult:
    started = time.monotonic()
    try:
        version, revisions, in_use, size = await asyncio.wait_for(_read(), TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return CheckResult(id="database", label=LABEL, status="down", severity="critical", checked_at=ctx.now,
                           summary=f"Postgres did not answer within {TIMEOUT_SECONDS:.0f} s. The dashboard cannot read or save anything.",
                           remediation="Check the postgres container and the connection pool (DB_POOL_SIZE).")
    except Exception as exc:  # noqa: BLE001 - a down database is a result, not an error
        return CheckResult(id="database", label=LABEL, status="down", severity="critical", checked_at=ctx.now,
                           summary="Postgres refused the connection. The dashboard cannot read or save anything.",
                           facts=[fact("Reason", type(exc).__name__)],
                           remediation="Check that the postgres container is running and DATABASE_URL is correct.")
    latency = int((time.monotonic() - started) * 1000)
    facts: List[HealthFact] = [fact("Server Version", version), fact("Response Time", f"{latency} ms"),
                               fact("Schema Revision", ", ".join(revisions) or "none")]
    if in_use is not None and size is not None:
        facts.append(fact("Pool Connections in Use", f"{in_use} of {size} (plus overflow)"))
    heads = _heads()
    if heads and revisions != heads:
        return CheckResult(id="database", label=LABEL, status="degraded", severity="warning", checked_at=ctx.now,
                           summary=f"The schema is at {', '.join(revisions) or 'no revision'} but this build expects {', '.join(heads)}.",
                           facts=facts, latency_ms=latency,
                           remediation="Run `alembic upgrade head` (the backend entrypoint does this at start unless RUN_MIGRATIONS=false).")
    return CheckResult(id="database", label=LABEL, status="ok", summary="Postgres answers and the schema matches this build.",
                       facts=facts, checked_at=ctx.now, latency_ms=latency)
