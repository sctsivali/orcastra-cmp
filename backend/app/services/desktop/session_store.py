"""Caching what a probe discovered, without letting the cache become an authority.

important: a session id NEVER carries permission. Every action re-passes every gate against the
API key on that request. If the id granted anything, a leaked or guessed one would be a
guest-control credential and the whole capability model would be bypassed, which is precisely the
failure `/console` already documents: a POST hands back the operation's fd secrets, so a holder
attaches to the guest outside the proxy and outside every session control.

The id is therefore DERIVED rather than random - a hash of the target - so it is not secret, two
callers naming the same guest share one entry, and there is nothing worth stealing.

important: Redis, not a module-level dict. WEB_CONCURRENCY is 4 with no sticky routing, so an
in-process cache means a probe on one worker and a click on another disagree about three requests
in four. Per-worker module state is a documented recurring defect class in this codebase, and this
would be the next instance of it.

note: ONE hash with a field per session, not one key per session. Redis here runs
`--maxmemory-policy allkeys-lru`, which evicts any key regardless of TTL, so N per-session keys
would be N independent eviction candidates. The same reasoning is written out in
`services/session_registry.py`.
"""

import hashlib
import json
import logging
import time
from dataclasses import asdict
from typing import Any, Dict, Optional

from .protocol import Display, ProbeReport, Target
from .runtime import DESKTOP_SESSION_MAX_AGE, DESKTOP_SESSION_TTL

logger = logging.getLogger(__name__)

__all__ = ["session_id", "load", "store", "forget"]

_HASH_KEY = "orcastra:desktop:sessions"

# Redis-is-down fallback only, never a miss path: a value found here is used, a value missing here
# falls through to a re-probe rather than to a denial. Same contract as the session registry's.
_local: Dict[str, Dict[str, Any]] = {}


def session_id(target: Target) -> str:
    """Derived from the target, so it is reproducible and grants nothing."""
    return hashlib.sha256(target.key.encode("utf-8")).hexdigest()[:16]


def _redis() -> Optional[Any]:
    try:
        from app.core.cache import get_cache_manager

        return getattr(get_cache_manager(), "redis", None)
    except Exception:  # noqa: BLE001 - a cache is never allowed to break the feature
        return None


def _fresh(entry: Dict[str, Any]) -> bool:
    now = time.time()
    if now - entry.get("last_seen", 0) > DESKTOP_SESSION_TTL:
        return False
    # An absolute age on top of the idle TTL, so a session kept warm by traffic cannot keep using
    # an environment map for a login session the user ended hours ago.
    return now - entry.get("created_at", 0) <= DESKTOP_SESSION_MAX_AGE


def _to_report(entry: Dict[str, Any]) -> ProbeReport:
    payload = dict(entry.get("report") or {})
    display = payload.pop("display", None)
    report = ProbeReport(**payload)
    if display:
        report.display = Display(**display)
    return report


def load(target: Target) -> Optional[ProbeReport]:
    """The cached probe for this guest, or None to re-probe. Losing it costs one probe."""
    key = session_id(target)
    raw = None
    client = _redis()
    if client is not None:
        try:
            raw = client.hget(_HASH_KEY, key)
        except Exception:  # noqa: BLE001
            raw = None
    entry = json.loads(raw) if raw else _local.get(key)
    if not entry or not _fresh(entry):
        return None
    try:
        return _to_report(entry)
    except (TypeError, ValueError):
        logger.warning("Discarding an unreadable desktop session entry for %s", target.key)
        return None


def store(target: Target, report: ProbeReport) -> str:
    key = session_id(target)
    payload = asdict(report)
    if report.display is not None:
        payload["display"] = asdict(report.display)
    now = time.time()
    existing = _local.get(key) or {}
    entry = {
        "report": payload,
        "created_at": existing.get("created_at", now),
        "last_seen": now,
        "target": target.key,
    }
    _local[key] = entry
    client = _redis()
    if client is not None:
        try:
            client.hset(_HASH_KEY, key, json.dumps(entry))
        except Exception:  # noqa: BLE001
            logger.debug("Desktop session cache write skipped for %s", target.key)
    return key


def forget(target: Target) -> None:
    """End a session. Cleanup, never a security control: a re-probe re-establishes it."""
    key = session_id(target)
    _local.pop(key, None)
    client = _redis()
    if client is not None:
        try:
            client.hdel(_HASH_KEY, key)
        except Exception:  # noqa: BLE001
            pass
