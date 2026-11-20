"""Read the release manifest twice a day and say what it means for the running version.

One worker at a time (a Redis lease) reads the feed: the published manifest on GitHub, then the
documentation site's copy if GitHub does not answer, or only the operator's mirror when
RELEASE_FEED_URL is set. The request carries a fixed User-Agent and nothing else: no version, no
host name, no identifier. A failed read is retried after 1, 2, 4 and then every 12 hours, and the
last manifest that parsed is used for 7 days before the notice goes quiet.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from app.core.config import get_settings, release_feed_exemptions
from app.core.safe_http import SafeHTTPError, fetch_guarded
from app.services.collector_leadership import CollectorLease
from app.services.release_check import comparator, manifest as manifest_module
from app.services.release_check.store import (
    _shared_redis,
    get_store,
    manifest_from_dict,
    manifest_to_dict,
)

logger = logging.getLogger(__name__)

PRIMARY_URL = "https://raw.githubusercontent.com/sctsivali/orcastra-docs/main/docs/releases/manifest.json"
MIRROR_URL = "https://docs.orcastra.io/en/latest/releases/manifest.json"
PUBLIC_HOSTS = frozenset({"raw.githubusercontent.com", "docs.orcastra.io", "orcastra.readthedocs.io"})
USER_AGENT = "Orcastra-CMP-release-check/1"
TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=2.0)

TICK_SECONDS = 600
READ_EVERY_SECONDS = 12 * 3600
BACKOFF_SECONDS = (3600, 2 * 3600, 4 * 3600, 12 * 3600)
GOOD_FOR_SECONDS = 7 * 86400
CHECK_NOW_COOLDOWN_SECONDS = 60
LEASE_KEY = "orcastra:release_check:lease"

_check_lock = asyncio.Lock()
_task: Optional[asyncio.Task] = None


def feeds() -> List[Tuple[str, str, frozenset, tuple]]:
    """(source, url, hosts every hop must stay on, private networks allowed)."""
    custom = get_settings().release_feed_url.strip()
    if custom:
        host = (urlsplit(custom).hostname or "").lower()
        return [("mirror_custom", custom, frozenset({host}), release_feed_exemptions())]
    from app.editions import current_edition

    edition_feeds = current_edition().release_feeds
    if edition_feeds:
        return [(source, url, PUBLIC_HOSTS, ()) for source, url in edition_feeds]
    return [("github", PRIMARY_URL, PUBLIC_HOSTS, ()), ("docs", MIRROR_URL, PUBLIC_HOSTS, ())]


async def _read(url: str, hosts: frozenset, exemptions: tuple) -> manifest_module.Manifest:
    _content_type, body = await fetch_guarded(
        url,
        max_bytes=manifest_module.MAX_BYTES,
        timeout=TIMEOUT,
        require_https=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        allowed_hosts=hosts,
        exemptions=exemptions,
    )
    return manifest_module.parse(body)


async def check(now: Optional[float] = None) -> Dict[str, Any]:
    """Read the feed once, record the outcome, and return the stored state. Never raises."""
    async with _check_lock:
        now = time.time() if now is None else now
        store = get_store()
        state = await asyncio.to_thread(store.read)
        errors = []
        for source, url, hosts, exemptions in feeds():
            try:
                parsed = await _read(url, hosts, exemptions)
            except (SafeHTTPError, manifest_module.ManifestInvalid) as exc:
                errors.append(f"{source}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - recorded, so the backoff still applies
                logger.warning("Release check read of %s failed unexpectedly", source, exc_info=True)
                errors.append(f"{source}: unexpected {type(exc).__name__}")
                continue
            state = {
                "last_attempt_at": now,
                "last_success_at": now,
                "source": source,
                "error": None,
                "failures": 0,
                "manifest": manifest_to_dict(parsed),
            }
            break
        else:
            state = {
                **state,
                "last_attempt_at": now,
                "error": "; ".join(errors) or "no feed is configured",
                "failures": int(state.get("failures") or 0) + 1,
            }
            logger.warning("Release check could not read the feed: %s", state["error"])
        await asyncio.to_thread(store.write, state)
        return state


def due(state: Dict[str, Any], now: float) -> bool:
    attempted = state.get("last_attempt_at")
    if not attempted:
        return True
    if not state.get("error"):
        return now - float(state.get("last_success_at") or 0) >= READ_EVERY_SECONDS
    failures = max(1, int(state.get("failures") or 1))
    return now - float(attempted) >= BACKOFF_SECONDS[min(failures, len(BACKOFF_SECONDS)) - 1]


def _iso(epoch: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat() if epoch else None


def describe(state: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """The answer GET /system/release-status gives: the verdict, and how fresh it is."""
    now = time.time() if now is None else now
    settings = get_settings()
    running = settings.app_version or None
    out: Dict[str, Any] = {
        "enabled": settings.release_check_enabled,
        "running_version": running,
        "level": "none",
        "reasons": [],
        "running_status": None,
        "latest_version": None,
        "security_version": None,
        "notes_url": None,
        "upgrade_url": None,
        "checked_at": _iso(state.get("last_attempt_at")),
        "last_success_at": _iso(state.get("last_success_at")),
        "source": state.get("source"),
        "error": state.get("error"),
        "stale": False,
    }
    if not settings.release_check_enabled:
        out["reasons"] = ["disabled"]
        return out
    manifest = manifest_from_dict(state.get("manifest"))
    if manifest is None:
        out["reasons"] = ["no_feed"]
        return out
    if now - float(state.get("last_success_at") or 0) > GOOD_FOR_SECONDS:
        out.update(stale=True, reasons=["stale_feed"], latest_version=manifest.latest.version)
        return out
    verdict = comparator.evaluate(running, manifest)
    out.update(
        level=verdict.level,
        reasons=list(verdict.reasons),
        running_status=verdict.running_status,
        latest_version=verdict.latest_version,
        security_version=verdict.security_version,
        notes_url=verdict.notes_url,
        upgrade_url=verdict.upgrade_url,
    )
    return out


async def _loop() -> None:
    lease = CollectorLease(LEASE_KEY, TICK_SECONDS * 2 + 60, _shared_redis)
    # Jittered so workers started together do not claim in lockstep.
    await asyncio.sleep(random.uniform(5.0, 30.0))
    try:
        while True:
            try:
                if get_settings().release_check_enabled and await asyncio.to_thread(lease.claim):
                    state = await asyncio.to_thread(get_store().read)
                    if due(state, time.time()):
                        await check()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a blip must not end the loop
                logger.warning("Release check pass failed", exc_info=True)
            await asyncio.sleep(TICK_SECONDS + random.uniform(0, 60))
    finally:
        try:
            lease.release()
        except Exception:  # noqa: BLE001
            pass


def start_release_check() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop())


async def stop_release_check() -> None:
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
