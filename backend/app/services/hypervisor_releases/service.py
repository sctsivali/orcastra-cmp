"""Read the LXD and Incus release feeds a few times a day and keep the newest version per track.

One worker at a time (a Redis lease) reads both: the Snap Store's channel map for the lxd snap
and the lxc/incus release tags on GitHub. The requests carry a fixed User-Agent and nothing
else: no Cluster, no version, no host name. Each engine keeps its own outcome, so a GitHub
outage does not hide what the Snap Store said. A failed read is retried after 1, 2, 4 and then
every 12 hours, and the last good copy is used for 7 days (verdict.GOOD_FOR_SECONDS) before
the Clusters it describes read "Not checked".

Request handlers only read the stored state (`attach`); nothing on a request path reaches the
internet.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from typing import Any, Dict, Iterable, Optional

import httpx

from app.core.config import get_settings
from app.core.safe_http import SafeHTTPError, fetch_guarded
from app.models.schemas import HypervisorRelease
from app.services.collector_leadership import CollectorLease
from app.services.hypervisor_releases import feed
from app.services.hypervisor_releases.verdict import INCUS, LXD, engine_of, evaluate
from app.services.release_check.store import ReleaseCheckStore, _shared_redis

logger = logging.getLogger(__name__)

LXD_URL = "https://api.snapcraft.io/v2/snaps/info/lxd"
INCUS_URL = "https://api.github.com/repos/lxc/incus/git/matching-refs/tags/v"
USER_AGENT = "Orcastra-CMP-hypervisor-release-check/1"
TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=2.0)
WALL_SECONDS = 30.0

# (engine, url, the only host every hop may use, size cap, parser, extra headers)
SOURCES = (
    (LXD, LXD_URL, frozenset({"api.snapcraft.io"}), feed.MAX_LXD_BYTES, feed.parse_lxd,
     # The Snap Store answers 400 without a device series.
     {"Snap-Device-Series": "16"}),
    (INCUS, INCUS_URL, frozenset({"api.github.com"}), feed.MAX_INCUS_BYTES, feed.parse_incus,
     {"X-GitHub-Api-Version": "2022-11-28"}),
)

TICK_SECONDS = 600
BACKOFF_SECONDS = (3600, 2 * 3600, 4 * 3600, 12 * 3600)
STATE_KEY = "orcastra:hypervisor_releases:state"
LEASE_KEY = "orcastra:hypervisor_releases:lease"

_check_lock = asyncio.Lock()
_task: Optional[asyncio.Task] = None
_store: Optional[ReleaseCheckStore] = None


def get_store() -> ReleaseCheckStore:
    global _store
    if _store is None:
        _store = ReleaseCheckStore(state_key=STATE_KEY)
    return _store


async def _read(url: str, hosts: frozenset, max_bytes: int, parser, headers: Dict[str, str]):
    _content_type, body = await asyncio.wait_for(
        fetch_guarded(
            url,
            max_bytes=max_bytes,
            timeout=TIMEOUT,
            require_https=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json", **headers},
            allowed_hosts=hosts,
        ),
        timeout=WALL_SECONDS,
    )
    # Off the event loop: a hostile body is bounded in size, not in how long it takes to parse.
    return await asyncio.to_thread(parser, body)


async def check(now: Optional[float] = None) -> Dict[str, Any]:
    """Read both feeds once, record each outcome, and return the stored state. Never raises."""
    async with _check_lock:
        now = time.time() if now is None else now
        store = get_store()
        state = await asyncio.to_thread(store.read)
        if not isinstance(state, dict):
            state = {}
        for engine, url, hosts, max_bytes, parser, headers in SOURCES:
            previous = state.get(engine) if isinstance(state.get(engine), dict) else {}
            try:
                tracks = await _read(url, hosts, max_bytes, parser, headers)
            except (SafeHTTPError, feed.FeedInvalid, asyncio.TimeoutError) as exc:
                error = str(exc) or type(exc).__name__
            except Exception as exc:  # noqa: BLE001 - recorded, so the backoff still applies
                logger.warning("Hypervisor release read of %s failed unexpectedly", engine, exc_info=True)
                error = f"unexpected {type(exc).__name__}"
            else:
                state[engine] = {
                    "last_attempt_at": now,
                    "last_success_at": now,
                    "error": None,
                    "failures": 0,
                    "tracks": feed.tracks_to_dict(tracks),
                }
                continue
            # The last good tracks stay: one failed read is no reason to forget what was known.
            state[engine] = {
                **previous,
                "last_attempt_at": now,
                "error": error,
                "failures": int(previous.get("failures") or 0) + 1,
            }
            logger.warning("Hypervisor release check could not read the %s feed: %s", engine, error)
        await asyncio.to_thread(store.write, state)
        return state


def _stamp(value: Any) -> Optional[float]:
    """A stored epoch as a finite float, or None. Redis is shared, so a stored number is not trusted."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def due(state: Dict[str, Any], now: float) -> bool:
    if not isinstance(state, dict):
        return True
    every = get_settings().hypervisor_release_check_interval_hours * 3600
    for engine, *_rest in SOURCES:
        entry = state.get(engine)
        attempted = _stamp(entry.get("last_attempt_at")) if isinstance(entry, dict) else None
        # A stamp from the future (a clock that jumped back) is not trusted to postpone a read.
        if not attempted or attempted > now + 60:
            return True
        if not entry.get("error"):
            if now - (_stamp(entry.get("last_success_at")) or 0) >= every:
                return True
            continue
        try:
            failures = max(1, int(entry.get("failures") or 1))
        except (TypeError, ValueError, OverflowError):
            failures = 1
        if now - attempted >= BACKOFF_SECONDS[min(failures, len(BACKOFF_SECONDS)) - 1]:
            return True
    return False


async def attach(nodes: Iterable[Any], now: Optional[float] = None) -> None:
    """Set `hypervisor_release` on each NodeStatus from the stored feeds. Reads Redis once.

    Turned off (HYPERVISOR_RELEASE_CHECK_ENABLED=false) every node gets None, which the console
    shows as nothing at all: an air-gapped install is not told on every card that it is offline.
    """
    nodes = list(nodes)
    if not nodes:
        return
    if not get_settings().hypervisor_release_check_enabled:
        for node in nodes:
            node.hypervisor_release = None
        return
    now = time.time() if now is None else now
    try:
        state = await asyncio.to_thread(get_store().read)
    except Exception:  # noqa: BLE001 - an unreadable store reads as "not checked", never as a 500
        logger.debug("Hypervisor release state unreadable", exc_info=True)
        state = {}
    if not isinstance(state, dict):
        state = {}
    entries = {engine: state.get(engine) if isinstance(state.get(engine), dict) else {} for engine, *_r in SOURCES}
    tracks = {engine: feed.tracks_from_dict(entry.get("tracks")) for engine, entry in entries.items()}
    for node in nodes:
        if not getattr(node, "connected", False) or not getattr(node, "server_version", None):
            node.hypervisor_release = None
            continue
        environment = getattr(node, "environment", None) or {}
        engine = engine_of(getattr(node, "node_type", None), environment)
        lts = environment.get("server_lts")
        try:
            node.hypervisor_release = HypervisorRelease.model_validate(evaluate(
                engine=engine,
                server_version=node.server_version,
                server_lts=lts if isinstance(lts, bool) else None,
                engine_state=entries.get(engine) if engine else None,
                tracks=tracks.get(engine, {}) if engine else {},
                now=now,
            ))
        except Exception:  # noqa: BLE001 - one odd stored value must not take the Clusters page down
            logger.warning("Hypervisor release verdict failed for %s", getattr(node, "name", "?"), exc_info=True)
            node.hypervisor_release = HypervisorRelease(status="not_checked", reason="feed_unreachable")


async def _loop() -> None:
    lease = CollectorLease(LEASE_KEY, TICK_SECONDS * 2 + 60, _shared_redis)
    # Jittered so workers started together do not claim in lockstep.
    await asyncio.sleep(random.uniform(5.0, 30.0))
    try:
        while True:
            try:
                if get_settings().hypervisor_release_check_enabled and await asyncio.to_thread(lease.claim):
                    state = await asyncio.to_thread(get_store().read)
                    if due(state, time.time()):
                        await check()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a blip must not end the loop
                logger.warning("Hypervisor release check pass failed", exc_info=True)
            await asyncio.sleep(TICK_SECONDS + random.uniform(0, 60))
    finally:
        try:
            lease.release()
        except Exception:  # noqa: BLE001
            pass


def start_hypervisor_release_check() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop())


async def stop_hypervisor_release_check() -> None:
    global _task
    task, _task = _task, None
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
