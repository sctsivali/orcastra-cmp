"""Read Ubuntu's record of every tracked kernel CVE every few hours and keep the last good copy.

Tracked means the ids in kernel_advisory_data.ADVISORIES; no other CVE is discovered here. One
worker at a time (a Redis lease) reads https://ubuntu.com/security/cves/<id>.json, or only the
operator's mirror when KERNEL_CVE_FEED_URL is set. The request carries a fixed User-Agent and
nothing else: no version, no host name, no identifier. A failed read is retried after 1, 2, 4 and
then every 12 hours; the verdicts stop trusting a copy three days after it was last read
(kernel_advisory.UBUNTU_GOOD_FOR).
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from app.core.config import get_settings, kernel_cve_feed_exemptions
from app.core.safe_http import SafeHTTPError, fetch_guarded
from app.services.collector_leadership import CollectorLease
from app.services.kernel_cve_feed import record as record_module
from app.services.kernel_cve_feed.record import UbuntuSnapshot
from app.services.kernel_cve_feed.store import _shared_redis, get_store

logger = logging.getLogger(__name__)

PUBLIC_URL = "https://ubuntu.com/security/cves/{id}.json"
PUBLIC_HOSTS = frozenset({"ubuntu.com"})
USER_AGENT = "Orcastra-CMP-kernel-cve-check/1"
TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=2.0)

TICK_SECONDS = 600
# A read stamped further ahead than this is not believed: it would never go stale.
CLOCK_SKEW = timedelta(hours=1)
BACKOFF_SECONDS = (3600, 2 * 3600, 4 * 3600, 12 * 3600)
CHECK_NOW_COOLDOWN_SECONDS = 60
# Wall clock for one read. httpx's read timeout applies per chunk, so a responder that drips bytes
# could otherwise hold the check lock and an Administrator's request open indefinitely.
READ_DEADLINE_SECONDS = 30.0
LEASE_KEY = "orcastra:kernel_cve_feed:lease"

_check_lock = asyncio.Lock()
_task: Optional[asyncio.Task] = None
# cve id -> ((cve id, last_success_at, record shape), the record read back from the store)
_parsed: Dict[str, Tuple[Tuple[str, Any], Optional[record_module.UbuntuCve]]] = {}


@dataclass(frozen=True)
class CveCheck:
    """What the reads of one CVE's record left: the copy a verdict may use, and the last failure."""

    id: str
    snapshot: Optional[UbuntuSnapshot]
    last_attempt_at: Optional[datetime]
    error: Optional[str]


def _number(value: Any, default: float = 0.0) -> float:
    """A stored epoch or count, or `default`: Redis is shared, so a value there may not be a number."""
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def tracked_ids() -> Tuple[str, ...]:
    from app.services.kernel_advisory_data import ADVISORIES

    return tuple(advisory.id for advisory in ADVISORIES)


def mirror_configured() -> bool:
    return bool(get_settings().kernel_cve_feed_url.strip())


def feed_target(cve_id: str) -> Tuple[str, frozenset, tuple]:
    """(url, hosts every hop must stay on, private networks allowed) for one CVE's record."""
    template = get_settings().kernel_cve_feed_url.strip()
    if not template:
        return PUBLIC_URL.format(id=cve_id), PUBLIC_HOSTS, ()
    if template.count("{id}") != 1:
        raise SafeHTTPError("KERNEL_CVE_FEED_URL must contain {id} once")
    url = template.replace("{id}", cve_id)
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError as exc:
        raise SafeHTTPError("KERNEL_CVE_FEED_URL is not a URL") from exc
    return url, frozenset({host}), kernel_cve_feed_exemptions()


async def _read(cve_id: str) -> record_module.UbuntuCve:
    url, hosts, exemptions = feed_target(cve_id)
    _content_type, body = await fetch_guarded(
        url,
        max_bytes=record_module.MAX_BYTES,
        timeout=TIMEOUT,
        require_https=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        allowed_hosts=hosts,
        exemptions=exemptions,
    )
    return record_module.parse(body, cve_id)


async def _check_one(cve_id: str, state: Dict[str, Any], now: float) -> Dict[str, Any]:
    try:
        parsed = await asyncio.wait_for(_read(cve_id), READ_DEADLINE_SECONDS)
    except (SafeHTTPError, record_module.RecordInvalid) as exc:
        error = str(exc)
    except TimeoutError:
        error = f"no complete answer within {int(READ_DEADLINE_SECONDS)}s"
    except Exception as exc:  # noqa: BLE001 - recorded, so the backoff still applies
        logger.warning("Kernel CVE read of %s failed unexpectedly", cve_id, exc_info=True)
        error = f"unexpected {type(exc).__name__}"
    else:
        return {
            "last_attempt_at": now,
            "last_success_at": now,
            "source": "mirror_custom" if mirror_configured() else "ubuntu",
            "error": None,
            "failures": 0,
            "record": record_module.to_dict(parsed),
        }
    logger.warning("Could not read Ubuntu's record of %s: %s", cve_id, error)
    return {
        **state,
        "last_attempt_at": now,
        "error": error,
        "failures": int(max(0.0, _number(state.get("failures")))) + 1,
    }


async def check(cve_ids: Optional[Iterable[str]] = None, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    """Read each CVE's record once, record the outcome, and return the stored states. Never raises."""
    async with _check_lock:
        now = time.time() if now is None else now
        ids = tuple(cve_ids) if cve_ids is not None else tracked_ids()
        store = get_store()
        states = await asyncio.to_thread(store.read_many, ids)
        for cve_id in ids:
            states[cve_id] = await _check_one(cve_id, states.get(cve_id) or {}, now)
            await asyncio.to_thread(store.write, cve_id, states[cve_id])
        return states


def due(state: Dict[str, Any], now: float, every_seconds: Optional[int] = None) -> bool:
    attempted = _number(state.get("last_attempt_at"))
    if not attempted:
        return True
    # A stamp from the future (a skewed clock or a forged store) would otherwise postpone every
    # read until the record expired.
    future = now + CLOCK_SKEW.total_seconds()
    if attempted > future or _number(state.get("last_success_at")) > future:
        return True
    if not state.get("error"):
        every = every_seconds if every_seconds is not None else get_settings().kernel_cve_feed_interval_hours * 3600
        return now - _number(state.get("last_success_at")) >= every
    failures = int(min(max(1.0, _number(state.get("failures"), 1.0)), len(BACKOFF_SECONDS)))
    return now - attempted >= BACKOFF_SECONDS[failures - 1]


def _when(epoch: Any) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc) if epoch else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _stored_record(cve_id: str, state: Dict[str, Any]) -> Optional[record_module.UbuntuCve]:
    # The stored copy is checked again on every read (record.from_dict), a few milliseconds for a
    # kernel CVE's ~170 packages; one parse per read Ubuntu answered is enough per process.
    stored = state.get("record")
    packages = stored.get("packages") if isinstance(stored, dict) else None
    shape = (str(stored.get("updated_at")), len(packages)) if isinstance(packages, list) else None
    key = (cve_id, str(state.get("last_success_at")), shape)
    cached = _parsed.get(cve_id)
    if cached is not None and cached[0] == key:
        return cached[1]
    parsed = record_module.from_dict(state.get("record"), cve_id)
    _parsed[cve_id] = (key, parsed)
    return parsed


def view(cve_id: str, state: Dict[str, Any], enabled: bool, now: Optional[datetime] = None) -> CveCheck:
    """One CVE's stored state as the verdict and the API read it. Turned off, no copy is used."""
    record = _stored_record(cve_id, state) if enabled else None
    checked_at = _when(state.get("last_success_at"))
    if checked_at is not None and checked_at > (now or datetime.now(timezone.utc)) + CLOCK_SKEW:
        checked_at = None
    snapshot = UbuntuSnapshot(record, checked_at) if record is not None and checked_at is not None else None
    error = state.get("error")
    return CveCheck(
        id=cve_id,
        snapshot=snapshot,
        last_attempt_at=_when(state.get("last_attempt_at")),
        error=error[:200] if isinstance(error, str) else None,
    )


def read_checks(cve_ids: Optional[Iterable[str]] = None) -> Dict[str, CveCheck]:
    """Every tracked CVE's last reads, from the store only: never reaches the internet."""
    ids = tuple(cve_ids) if cve_ids is not None else tracked_ids()
    enabled = get_settings().kernel_cve_feed_enabled
    states = get_store().read_many(ids)
    now = datetime.now(timezone.utc)
    return {cve_id: view(cve_id, states.get(cve_id) or {}, enabled, now) for cve_id in ids}


async def _loop() -> None:
    lease = CollectorLease(LEASE_KEY, TICK_SECONDS * 2 + 60, _shared_redis)
    # Jittered so workers started together do not claim in lockstep.
    await asyncio.sleep(random.uniform(5.0, 30.0))
    try:
        while True:
            try:
                if get_settings().kernel_cve_feed_enabled and await asyncio.to_thread(lease.claim):
                    ids = tracked_ids()
                    states = await asyncio.to_thread(get_store().read_many, ids)
                    now = time.time()
                    stale = [cve_id for cve_id in ids if due(states.get(cve_id) or {}, now)]
                    if stale:
                        await check(stale)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a blip must not end the loop
                logger.warning("Kernel CVE feed pass failed", exc_info=True)
            await asyncio.sleep(TICK_SECONDS + random.uniform(0, 60))
    finally:
        try:
            lease.release()
        except Exception:  # noqa: BLE001
            pass


def start_kernel_cve_feed() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop())


async def stop_kernel_cve_feed() -> None:
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
