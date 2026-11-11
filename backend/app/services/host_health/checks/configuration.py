"""Configuration: secrets still at their template placeholder, and a Redis key out of step.

A placeholder SECRET_KEY is readable by anyone in the public repository and signs API key hashes and
WebSocket tickets; it is reported, not refused, because rotating it invalidates every API key
(core/secret_hygiene.py). A worker that could not read its Redis encryption key from Vault at start
holds a fallback key its siblings may not share. Names only, never a value.
"""

from __future__ import annotations

from typing import List

from app.core import secret_hygiene
from app.core.encryption import encryption_state
from app.services.host_health.context import CheckContext
from app.services.host_health.models import CheckResult, HealthFact, fact

LABEL = "Secrets and Keys"


def check(ctx: CheckContext) -> CheckResult:
    found = secret_hygiene.findings()
    facts: List[HealthFact] = [fact(f.name, f.problem) for f in found]
    state = encryption_state()
    if state:
        facts.append(fact("Redis Encryption Key", {
            "vault": "from Vault", "environment": "from REDIS_ENCRYPTION_KEY",
            "derived": "derived from SECRET_KEY", "provided": "provided at start",
        }.get(state["key_source"], state["key_source"])))
    if found:
        names = ", ".join(f.name for f in found)
        return CheckResult(
            id="configuration", label=LABEL, status="degraded", severity="critical", checked_at=ctx.now,
            facts=facts,
            summary=f"Secrets still at their template placeholder or too short: {names}.",
            remediation=(
                "Generate each with `openssl rand -hex 32` (NEXTAUTH_SECRET: `openssl rand -base64 32`), "
                "set it in .env and recreate the containers. A new SECRET_KEY invalidates every API key: "
                "issue new keys to the integrations afterwards."
            ),
        )
    if state and state["vault_unreadable"] and state["key_source"] != "vault":
        return CheckResult(
            id="configuration", label=LABEL, status="degraded", severity="warning", checked_at=ctx.now,
            facts=facts,
            summary="This worker could not read the Redis encryption key from Vault at start and uses a fallback key.",
            remediation="Once Vault answers, restart the backend so every worker reads the same key.",
        )
    return CheckResult(id="configuration", label=LABEL, status="ok", checked_at=ctx.now, facts=facts,
                       summary="No secret is a template placeholder or too short.")
