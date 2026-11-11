"""Template placeholders and secrets too short to trust, reported by variable name.

The env template ships `replace_me_*` placeholders. A deployment that keeps one runs with a
SECRET_KEY anyone can read in the public repository, and it signs API key hashes, WebSocket
tickets and the fallback Redis encryption key. Rotating SECRET_KEY invalidates every API key, so
this never refuses to start: it reports, at boot and in Host Health, which variable to fix. Only
names and the kind of problem are reported, never a value.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List, Mapping, Optional

logger = logging.getLogger(__name__)

PLACEHOLDER_PREFIX = "replace_me"
MIN_SECRET_LENGTH = 32


@dataclass(frozen=True)
class Finding:
    name: str
    problem: str


def _placeholder(value: str) -> bool:
    return value.strip().lower().startswith(PLACEHOLDER_PREFIX)


def _fernet_ok(value: str) -> bool:
    try:
        from cryptography.fernet import Fernet

        Fernet(value.encode())
        return True
    except Exception:  # noqa: BLE001 - an invalid key is the finding, never an error here
        return False


def findings(env: Optional[Mapping[str, str]] = None) -> List[Finding]:
    """What is wrong with the deployment's own secrets, by variable name."""
    env = os.environ if env is None else env
    found: List[Finding] = []

    secret_key = env.get("SECRET_KEY") or ""
    if not secret_key:
        found.append(Finding("SECRET_KEY", "is not set"))
    elif _placeholder(secret_key):
        found.append(Finding("SECRET_KEY", "is still the template placeholder"))
    elif len(secret_key) < MIN_SECRET_LENGTH:
        found.append(Finding("SECRET_KEY", f"is shorter than {MIN_SECRET_LENGTH} characters"))

    # The frontend's session key reaches the backend only through a shared env file; absent here
    # it is the frontend's own check (instrumentation.ts) that reports it.
    nextauth = env.get("NEXTAUTH_SECRET")
    if nextauth is not None:
        if _placeholder(nextauth):
            found.append(Finding("NEXTAUTH_SECRET", "is still the template placeholder"))
        elif len(nextauth) < MIN_SECRET_LENGTH:
            found.append(Finding("NEXTAUTH_SECRET", f"is shorter than {MIN_SECRET_LENGTH} characters"))

    redis_key = env.get("REDIS_ENCRYPTION_KEY")
    if redis_key and (_placeholder(redis_key) or not _fernet_ok(redis_key)):
        found.append(Finding("REDIS_ENCRYPTION_KEY", "is not a valid Fernet key, so SECRET_KEY is used instead"))

    if PLACEHOLDER_PREFIX in (env.get("DATABASE_URL") or "") or _placeholder(env.get("POSTGRES_PASSWORD") or ""):
        found.append(Finding("POSTGRES_PASSWORD", "is still the template placeholder"))
    return found


def report_at_startup() -> List[Finding]:
    """Log one ERROR per finding. Names only."""
    found = findings()
    for finding in found:
        logger.error(
            "SECURITY: %s %s. Generate a new value (openssl rand -hex 32) and recreate the containers; "
            "Host Health lists it until then.", finding.name, finding.problem,
        )
    return found
