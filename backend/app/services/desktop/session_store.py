"""Caching what a probe discovered, without letting the cache become an authority.

important: a session id never carries permission; every action re-passes every gate against the
API key on that request. An id that granted anything would make a leaked or guessed one a
guest-control credential that bypasses the capability model. The id is derived (a hash of the
target), so it is not secret and two callers naming the same guest share one entry.

important: entries live in Redis. With several workers and no sticky routing, an in-process cache
would let a probe on one worker and a click on another disagree.

One hash with a field per session instead of one key per session: Redis here runs
`--maxmemory-policy allkeys-lru`, which evicts any key regardless of TTL, so per-session keys would
each be an independent eviction candidate (see `services/session_registry.py`).
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

__all__ = ["session_id", "load", "store"]

_HASH_KEY = "orcastra:desktop:sessions"

# Redis-is-down fallback only, never a miss path: a value found here is used, a value missing here
# falls through to a re-probe rather than to a denial. Same contract as the session registry's.
_local: Dict[str, Dict[str, Any]] = {}


def session_id(target: Target) -> str:
    """Derived from the target, so it is reproducible and grants nothing."""
    return hashlib.sha256(target.key.encode("utf-8")).hexdigest()[:16]


# Warned about once per process rather than per call, so a Redis outage is visible in the log
# without a click loop filling it.
_warned_local_only = False


def _redis() -> Optional[Any]:
    """The shared client, or None with a one-time warning.

    important: the attribute is `redis_client`. A wrong name with a `getattr` default returns None
    on every call and silently degrades to per-worker state, which only breaks with more than one
    worker; the fallback therefore logs a warning.
    """
    global _warned_local_only
    try:
        from app.core.cache import get_cache_manager

        client = getattr(get_cache_manager(), "redis_client", None)
    except Exception:  # noqa: BLE001 - a cache is never allowed to break the feature
        client = None
    if client is None and not _warned_local_only:
        _warned_local_only = True
        logger.warning(
            "Desktop sessions have no shared cache and are falling back to per-worker memory. "
            "With more than one worker a probe and an action can land on different workers, so "
            "expect repeated re-probes and a display scale that resets."
        )
    return client


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
