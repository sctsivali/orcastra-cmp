"""Clear the console's list caches when a cluster reports a change made outside the console.

A change made on the cluster itself (the `lxc` CLI, the LXD UI, another client) passes no route
and no settle waiter, so cached lists would keep the previous state until their TTL. The elected
journal follower starts one `CacheClearance`: a firehose subscription per registered cluster
(except TASK_JOURNAL_EXCLUDE_CLUSTERS, registered read-only for inspection and left to converge on
the TTL), each lifecycle action mapped to the cache families it changes, and at most one clear per
family per interval (`Coalescer`).

important: nothing here may read or write the database, so a database stall cannot delay a clear,
and every clear runs on this module's single thread, because `delete_pattern` walks Redis with a
sync SCAN that would stall the loop. Identities come from the dashboard's certificate alone, so a
frame from a superseded certificate still in its grace period counts as external and clears, the
safe direction.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Set, Tuple

from app.core.cache import get_cache_manager
from app.core.config import get_settings
from app.services.lxd import registry
from app.services.task_journal.follower_writer import DashboardIdentities
from app.services.task_journal.vocabulary import requestor_kind

logger = logging.getLogger(__name__)

INSTANCE_LISTS = "instance_lists"
INSTANCES = "instances"
SNAPSHOTS = "snapshots"
IMAGES = "images"
NETWORKS = "networks"
STORAGE_VOLUMES = "storage_volumes"
STORAGE_POOLS = "storage_pools"
PROFILES = "profiles"
PROJECTS = "projects"

# The instance lists are the fast tier, everything else the slow one. The frontend hides a deleted
# row for DELETED_EVENT_GRACE_MS (lib/instance-reconciliation.ts), which must cover the lists'
# interval.
LISTS_INTERVAL_S = 10.0
FAMILY_INTERVAL_S = 30.0
REGISTRY_INTERVAL_S = 30.0
HUB_QUEUE_MAXSIZE = 2000
STOP_FLUSH_S = 5.0

_INSTANCE_TIER = (INSTANCE_LISTS, INSTANCES)

# Lifecycle actions, named as in LXD's shared/api/event_lifecycle.go, mapped to the cache families
# they change. Unlisted actions are ignored, including exec, console, file, log, metadata, backup,
# image-retrieved, cluster-* and warning-*.
# LXD reports a snapshot restore as `instance-restored`; `instance-snapshot-restored` is the
# journal vocabulary's name for it (vocabulary.LIFECYCLE_TO_ACTION) and is kept so both agree.
ACTION_FAMILIES: Dict[str, Tuple[str, ...]] = {
    "instance-created": _INSTANCE_TIER,
    "instance-deleted": _INSTANCE_TIER,
    "instance-renamed": _INSTANCE_TIER,
    "instance-started": _INSTANCE_TIER,
    "instance-stopped": _INSTANCE_TIER,
    "instance-shutdown": _INSTANCE_TIER,
    "instance-restarted": _INSTANCE_TIER,
    "instance-paused": _INSTANCE_TIER,
    "instance-resumed": _INSTANCE_TIER,
    "instance-updated": _INSTANCE_TIER,
    "instance-snapshot-created": (SNAPSHOTS,),
    "instance-snapshot-deleted": (SNAPSHOTS,),
    "instance-snapshot-restored": (SNAPSHOTS,),
    # A restore also rewrites the instance itself (state, config).
    "instance-restored": (INSTANCE_LISTS, SNAPSHOTS),
    "image-created": (IMAGES,),
    "image-deleted": (IMAGES,),
    "image-refreshed": (IMAGES,),
    "image-updated": (IMAGES,),
    "network-created": (NETWORKS,),
    "network-deleted": (NETWORKS,),
    "network-renamed": (NETWORKS,),
    "network-updated": (NETWORKS,),
    "network-acl-created": (NETWORKS,),
    "network-acl-deleted": (NETWORKS,),
    "network-acl-renamed": (NETWORKS,),
    "network-acl-updated": (NETWORKS,),
    "storage-volume-created": (STORAGE_VOLUMES,),
    "storage-volume-deleted": (STORAGE_VOLUMES,),
    "storage-volume-renamed": (STORAGE_VOLUMES,),
    "storage-volume-updated": (STORAGE_VOLUMES,),
    "storage-pool-created": (STORAGE_POOLS,),
    "storage-pool-deleted": (STORAGE_POOLS,),
    "storage-pool-updated": (STORAGE_POOLS,),
    "profile-created": (PROFILES,),
    "profile-deleted": (PROFILES,),
    "profile-renamed": (PROFILES,),
    "profile-updated": (PROFILES,),
    "project-created": (PROJECTS,),
    "project-deleted": (PROJECTS,),
    "project-renamed": (PROJECTS,),
    "project-updated": (PROJECTS,),
}

_CLEARERS: Dict[str, Callable[[Any], None]] = {
    INSTANCE_LISTS: lambda cache: cache.invalidate_instance_lists(),
    INSTANCES: lambda cache: cache.invalidate_instance_cache(),
    SNAPSHOTS: lambda cache: cache.invalidate_resource_cache("snapshots_fleet"),
    IMAGES: lambda cache: cache.invalidate_resource_cache("images"),
    NETWORKS: lambda cache: cache.invalidate_resource_cache("networks", "ipam"),
    STORAGE_VOLUMES: lambda cache: cache.invalidate_resource_cache("storage_volumes"),
    STORAGE_POOLS: lambda cache: cache.invalidate_resource_cache("storage_pools", "storage_pool_resources"),
    PROFILES: lambda cache: cache.invalidate_resource_cache("profiles"),
    PROJECTS: lambda cache: cache.invalidate_project_caches(),
}
FAMILIES: Tuple[str, ...] = tuple(_CLEARERS)
INTERVALS: Dict[str, float] = {
    family: LISTS_INTERVAL_S if family == INSTANCE_LISTS else FAMILY_INTERVAL_S for family in FAMILIES
}

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cache_clearance")


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _clear_family(family: str) -> None:
    _CLEARERS[family](get_cache_manager())


def _describe(host: str, frame: Mapping[str, Any]) -> str:
    requestor = frame.get("requestor")
    if isinstance(requestor, Mapping):
        who = f"{requestor.get('protocol') or '?'}:{str(requestor.get('username') or '')[:12] or '?'}"
    else:
        who = "no requestor"
    return f"{frame.get('action')} {frame.get('project') or '-'}/{frame.get('name') or frame.get('instance') or '-'} on {host} by {who}"


def is_dashboard_request(requestor: Any, dashboard: FrozenSet[str]) -> bool:
    """True only for a TLS requestor that is one of this dashboard's own identities.

    requestor_kind also answers "system" for a frame without a requestor block; that frame is
    unknown here, and unknown clears.
    """
    return isinstance(requestor, Mapping) and requestor_kind(requestor, dashboard)[0] == "system"


def families_for(frame: Mapping[str, Any], dashboard: FrozenSet[str] = frozenset()) -> Tuple[str, ...]:
    """The cache families a hub frame changes; empty for a frame to ignore."""
    if frame.get("type") != "lifecycle":
        return ()
    families = ACTION_FAMILIES.get(str(frame.get("action") or ""), ())
    if families and is_dashboard_request(frame.get("requestor"), dashboard):
        return ()
    return families


class Coalescer:
    """At most one clear per family per interval, and the last event of a burst still clears.

    Leading edge: a family whose last clear is older than its interval clears at once. Trailing
    edge: otherwise exactly one timer is armed for `last + interval`. Synchronous and loop-bound;
    `run` only submits the clear.
    """

    def __init__(
        self,
        run: Callable[[str], None],
        *,
        intervals: Mapping[str, float],
        clock: Callable[[], float] = time.monotonic,
        call_later: Optional[Callable[[float, Callable[[], None]], Any]] = None,
    ) -> None:
        self._run = run
        self._intervals = dict(intervals)
        self._clock = clock
        self._call_later = call_later
        self._last: Dict[str, float] = {}
        self._timers: Dict[str, Any] = {}
        self.closed = False

    def note(self, family: str) -> None:
        if self.closed:
            return
        now = self._clock()
        last = self._last.get(family)
        interval = self._intervals[family]
        if last is None or now - last >= interval:
            self._fire(family, now)
        elif family not in self._timers:
            schedule = self._call_later or asyncio.get_running_loop().call_later
            self._timers[family] = schedule(last + interval - now, partial(self._trailing, family))

    def pending(self) -> List[str]:
        return sorted(self._timers)

    def flush_and_close(self) -> List[str]:
        """Run every armed trailing clear now, then stop for good. Returns the flushed families."""
        families = sorted(self._timers)
        for family in families:
            self._timers.pop(family).cancel()
            self._last[family] = self._clock()
            self._run(family)
        self.closed = True
        return families

    def _trailing(self, family: str) -> None:
        self._timers.pop(family, None)
        if not self.closed:
            self._fire(family, self._clock())

    def _fire(self, family: str, now: float) -> None:
        timer = self._timers.pop(family, None)
        if timer is not None:
            timer.cancel()
        self._last[family] = now
        self._run(family)


class CacheClearance:
    def __init__(
        self,
        hub: Any,
        *,
        identities: Optional[Any] = None,
        clearer: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        call_later: Optional[Callable[[float, Callable[[], None]], Any]] = None,
        executor: Optional[ThreadPoolExecutor] = None,
        excluded: FrozenSet[str] = frozenset(),
    ) -> None:
        self._hub = hub
        self._excluded = frozenset(h.strip().lower() for h in excluded)
        self._identities = DashboardIdentities() if identities is None else identities
        self._clearer = _clear_family if clearer is None else clearer
        self._executor = executor or _executor
        self._coalescer = Coalescer(self._submit, intervals=INTERVALS, clock=clock, call_later=call_later)
        self._queues: Dict[str, asyncio.Queue] = {}
        self._consumers: Dict[str, asyncio.Task] = {}
        self._hub_state: Dict[str, str] = {}
        self._inflight: Set[asyncio.Future] = set()
        self._running: Set[str] = set()
        self._again: Set[str] = set()
        self._triggers: Dict[str, str] = {}
        self._noted: Dict[str, int] = {}
        self._registry_task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._registry_task is None or self._registry_task.done():
            self._registry_task = asyncio.create_task(self._registry_loop(), name="task-journal-cache-clearance")

    async def stop(self, flush_timeout: float = STOP_FLUSH_S) -> None:
        """Unsubscribe, run the pending trailing clears, wait for them at most `flush_timeout`."""
        await _cancel(self._registry_task)
        self._registry_task = None
        for host in list(self._queues):
            await self._unfollow(host)
        flushed = self._coalescer.flush_and_close()
        pending = set(self._inflight)
        if pending:
            _done, late = await asyncio.wait(pending, timeout=flush_timeout)
            for future in late:
                future.cancel()
            if late:
                logger.warning("Cache clearance stopped with %d clear(s) still running", len(late))
        self._inflight.clear()
        if flushed:
            logger.info("Cache clearance flushed %s on stop", ", ".join(flushed))

    def note(self, host: str, frame: Mapping[str, Any], dashboard: FrozenSet[str] = frozenset()) -> None:
        """Mark the families one hub frame changes. Never raises; never waits."""
        try:
            families = families_for(frame, dashboard)
            if families:
                trigger = _describe(host, frame)
                for family in families:
                    self._triggers[family] = trigger
                    self._noted[family] = self._noted.get(family, 0) + 1
            for family in families:
                self._coalescer.note(family)
        except Exception:  # noqa: BLE001 - a frame that cannot be noted falls back to the TTL
            logger.warning("Cache clearance could not note a frame from %s", host, exc_info=True)

    def mark_all_dirty(self, reason: str) -> None:
        """Every family, through the coalescer: for what a hand-over or a dropped stream hid."""
        logger.info("Cache clearance marking every family dirty (%s)", reason)
        try:
            for family in FAMILIES:
                self._coalescer.note(family)
        except Exception:  # noqa: BLE001
            logger.warning("Cache clearance could not mark every family dirty", exc_info=True)

    def on_hub_state(self, host: str, frame: Mapping[str, Any]) -> None:
        state = "connected" if frame.get("state") == "connected" else "disconnected"
        previous = self._hub_state.get(host)
        self._hub_state[host] = state
        if state == "connected" and previous == "disconnected":
            self.mark_all_dirty(f"{host} reconnected")

    def pending(self) -> List[str]:
        return self._coalescer.pending()

    def _submit(self, family: str) -> None:
        # At most one clear per family queued or running. With Redis down each clear blocks the
        # thread for its socket timeouts, so an unbounded queue would only grow; a note while one
        # runs asks for exactly one more after it, which covers what it missed.
        if family in self._running:
            self._again.add(family)
            return
        self._running.add(family)
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            self._executor, partial(contextvars.copy_context().run, self._run_clear, family)
        )
        self._inflight.add(future)
        future.add_done_callback(partial(self._cleared, family))

    def _cleared(self, family: str, future: asyncio.Future) -> None:
        self._inflight.discard(future)
        self._running.discard(family)
        if family in self._again and not self._coalescer.closed:
            self._again.discard(family)
            self._submit(family)

    def _run_clear(self, family: str) -> None:
        # One log line per clear. Clears are coalesced (at most every 10 s or 30 s per family), so
        # this shows what keeps a list cold without flooding the log.
        trigger = self._triggers.pop(family, "marked dirty")
        noted = self._noted.pop(family, 0)
        try:
            self._clearer(family)
            logger.info("Cache clearance cleared %s (%d event(s); last %s)", family, noted, trigger)
        except Exception:  # noqa: BLE001 - the TTL still bounds the stale window
            logger.warning("Cache clearance of %s failed", family, exc_info=True)

    # ── subscriptions ─────────────────────────────────────────────────────────────────────

    async def _registry_loop(self) -> None:
        while True:
            try:
                await self.sync_registry()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.error("Cache clearance registry sync failed", exc_info=True)
            await _sleep(REGISTRY_INTERVAL_S)

    async def sync_registry(self) -> None:
        ids = await registry.registered_cluster_ids()
        if ids is None:
            return
        wanted = {host for host in ids if host.strip().lower() not in self._excluded}
        current = set(self._queues)
        for host in sorted(wanted - current):
            await self._follow(host)
        for host in sorted(current - wanted):
            await self._unfollow(host)

    async def _follow(self, host: str) -> None:
        queue = await self._hub.subscribe(host, firehose=True, maxsize=HUB_QUEUE_MAXSIZE)
        self._queues[host] = queue
        self._consumers[host] = asyncio.create_task(
            self._consume(host, queue), name=f"task-journal-cache-clearance-{host}"
        )

    async def _unfollow(self, host: str) -> None:
        await _cancel(self._consumers.pop(host, None))
        queue = self._queues.pop(host, None)
        self._hub_state.pop(host, None)
        self._identities.forget(host)
        if queue is not None:
            await self._hub.unsubscribe(host, queue)

    async def _consume(self, host: str, queue: asyncio.Queue) -> None:
        while True:
            frame = await queue.get()
            try:
                if frame.get("type") == "hub":
                    self.on_hub_state(host, frame)
                elif frame.get("type") == "lifecycle" and frame.get("action") in ACTION_FAMILIES:
                    self.note(host, frame, await self._identities.for_host(host))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad frame must not end the consumer
                logger.warning("Cache clearance consumer for %s failed on a frame", host, exc_info=True)


async def _cancel(task: Optional[asyncio.Task]) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


def clearance_enabled() -> bool:
    return bool(get_settings().task_journal_cache_clearance)


def start_for_leader(hub: Any, excluded: FrozenSet[str] = frozenset()) -> Optional[CacheClearance]:
    """The elected follower's clearance, already marked dirty for what the hand-over hid.

    None when TASK_JOURNAL_CACHE_CLEARANCE is off: nothing subscribes and nothing clears.
    """
    if not clearance_enabled():
        return None
    clearance = CacheClearance(hub, excluded=excluded)
    clearance.start()
    clearance.mark_all_dirty("elected")
    return clearance


async def stop(clearance: Optional[CacheClearance]) -> None:
    if clearance is not None:
        await clearance.stop()
