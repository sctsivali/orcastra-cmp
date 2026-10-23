"""A change made on the cluster itself clears the console's cached lists, through the event stream.

The `lxc` CLI and the LXD UI pass no route and no settle waiter. The elected follower's cache
clearance holds its own firehose subscription per registered, non-excluded cluster, maps
each lifecycle action to the cache families it changes, skips what the dashboard itself
requested (the routes already cleared those), and clears each family at once on the leading edge
and once more on a trailing edge, at most every 10 s for the instance lists and every 30 s for
everything else. Clears run on the module's own thread, never on the event loop.
"""

import asyncio
import os
import threading
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.core.cache import CacheManager  # noqa: E402
from app.services.lxd import registry  # noqa: E402
from app.services.task_journal import cache_clearance  # noqa: E402
from app.services.task_journal import follower as follower_module  # noqa: E402
from app.services.task_journal.cache_clearance import (  # noqa: E402
    ACTION_FAMILIES,
    FAMILIES,
    CacheClearance,
    Coalescer,
    families_for,
)
from app.services.task_journal.follower import (  # noqa: E402
    HEARTBEAT_S,
    REGISTRY_INTERVAL_S,
    SWEEP_TICK_S,
    JournalFollower,
)
from tests._follower_fakes import FakeHub, NoIdentities, ScriptedSleep  # noqa: E402
from tests._journal_fakes import FakeSessionMaker  # noqa: E402

LISTS = ("instance_lists", "instances")
EXPECTED = {
    **{f"instance-{verb}": LISTS for verb in (
        "created", "deleted", "renamed", "started", "stopped", "shutdown", "restarted", "paused",
        "resumed", "updated",
    )},
    "instance-snapshot-created": ("snapshots",),
    "instance-snapshot-deleted": ("snapshots",),
    "instance-snapshot-restored": ("snapshots",),
    "instance-restored": ("instance_lists", "snapshots"),
    **{f"image-{verb}": ("images",) for verb in ("created", "deleted", "refreshed", "updated")},
    **{f"network-{verb}": ("networks",) for verb in ("created", "deleted", "renamed", "updated")},
    **{f"network-acl-{verb}": ("networks",) for verb in ("created", "deleted", "renamed", "updated")},
    **{f"storage-volume-{verb}": ("storage_volumes",) for verb in ("created", "deleted", "renamed", "updated")},
    **{f"storage-pool-{verb}": ("storage_pools",) for verb in ("created", "deleted", "updated")},
    **{f"profile-{verb}": ("profiles",) for verb in ("created", "deleted", "renamed", "updated")},
    **{f"project-{verb}": ("projects",) for verb in ("created", "deleted", "renamed", "updated")},
}
IGNORED = (
    "instance-exec", "instance-console", "instance-console-reset", "instance-console-retrieved",
    "instance-file-pushed", "instance-file-retrieved", "instance-file-deleted",
    "instance-log-retrieved", "instance-log-deleted", "instance-metadata-retrieved",
    "instance-metadata-updated", "instance-metadata-template-created",
    "instance-backup-created", "instance-backup-deleted", "instance-backup-retrieved",
    "instance-ready", "image-retrieved", "cluster-member-added", "cluster-member-updated",
    "cluster-certificate-updated", "warning-acknowledged", "warning-deleted", "warning-reset",
    "certificate-created", "operation-cancelled", "",
)
DASHBOARD = frozenset({"ab12cd"})
UNIX = {"protocol": "unix", "username": "root", "address": "@"}


def lifecycle(action, requestor=None):
    return {"type": "lifecycle", "host": "nuc2", "action": action, "name": "web-01",
            "project": "default", "requestor": requestor if requestor is not None else UNIX}


class FakeTimers:
    """A clock and a `call_later` the test advances by hand."""

    def __init__(self):
        self.now = 0.0
        self.armed = []

    def clock(self):
        return self.now

    def call_later(self, delay, callback):
        handle = SimpleNamespace(at=self.now + delay, callback=callback, cancelled=False)
        handle.cancel = lambda: setattr(handle, "cancelled", True)
        self.armed.append(handle)
        return handle

    def advance(self, seconds):
        self.now += seconds
        for handle in sorted(self.armed, key=lambda h: h.at):
            if not handle.cancelled and handle.at <= self.now:
                self.armed.remove(handle)
                handle.callback()


class Recorder:
    """Records each clear with the fake time it ran at; drain before advancing the clock."""

    def __init__(self, timers=None):
        self.timers = timers
        self.calls = []
        self.threads = []

    def __call__(self, family):
        self.calls.append((family, self.timers.now if self.timers else None))
        self.threads.append(threading.current_thread())

    def families(self):
        return [family for family, _ in self.calls]


def clearance(timers, recorder, hub=None):
    return CacheClearance(
        hub or FakeHub(), identities=NoIdentities(), clearer=recorder,
        clock=timers.clock, call_later=timers.call_later,
    )


async def drain(subject):
    if subject._inflight:
        await asyncio.gather(*list(subject._inflight))


# ── the classifier ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("action,families", sorted(EXPECTED.items()))
def test_every_listed_action_clears_its_family(action, families):
    assert families_for(lifecycle(action)) == families


def test_the_table_names_nothing_the_test_does_not():
    assert set(ACTION_FAMILIES) == set(EXPECTED)
    assert {family for families in EXPECTED.values() for family in families} == set(FAMILIES)


@pytest.mark.parametrize("action", IGNORED)
def test_an_action_that_changes_no_list_is_ignored(action):
    assert families_for(lifecycle(action)) == ()


def test_operation_and_hub_frames_are_ignored():
    operation = {"type": "operation", "host": "nuc2", "action": "instance-created",
                 "description": "Creating instance", "progress": "rootfs: 48%"}
    assert families_for(operation) == ()
    assert families_for({"type": "hub", "host": "nuc2", "state": "connected"}) == ()


def test_the_dashboards_own_request_is_skipped_and_everyone_elses_clears():
    ours = {"protocol": "tls", "username": "AB12CD", "address": "10.0.0.5:4431"}
    other_tls = {"protocol": "tls", "username": "ffee00", "address": "10.0.0.9:4431"}
    nameless_tls = {"protocol": "tls", "address": "10.0.0.9:4431"}

    assert families_for(lifecycle("instance-started", ours), DASHBOARD) == ()
    assert families_for(lifecycle("instance-started", UNIX), DASHBOARD) == LISTS, "the lxc CLI"
    assert families_for(lifecycle("instance-started", other_tls), DASHBOARD) == LISTS, "the LXD UI"
    assert families_for(lifecycle("instance-started", nameless_tls), DASHBOARD) == LISTS
    unknown = {"type": "lifecycle", "action": "instance-started"}
    assert families_for(unknown, DASHBOARD) == LISTS, "an unknown requestor clears"
    assert families_for(lifecycle("instance-started", ours)) == LISTS, (
        "no identity known for the cluster yet means nothing is ours"
    )


@pytest.mark.parametrize("family,cleared,kept", [
    ("instance_lists", ("instances:list:a", "vms:list:a"), ("profiles:list:a", "ipam:list:a")),
    ("snapshots", ("snapshots_fleet:list:a",), ("instances:list:a",)),
    ("images", ("images:list:a",), ("instances:list:a",)),
    ("networks", ("networks:list:a", "ipam:list:a"), ("profiles:list:a",)),
    ("storage_volumes", ("storage_volumes:list:a",), ("storage_pools:list:a",)),
    ("storage_pools", ("storage_pools:list:a", "storage_pool_resources:all:e1"), ("storage_volumes:list:a",)),
    ("profiles", ("profiles:list:a",), ("networks:list:a",)),
    ("projects", ("projects:list:a", "project_usage:a", "profiles:list:a", "networks:list:a",
                  "images:list:a", "storage_volumes:list:a"), ("instances:list:a",)),
])
def test_each_family_clears_the_lists_it_names(monkeypatch, family, cleared, kept):
    manager = CacheManager(redis_url=None)
    for key in (*cleared, *kept):
        manager.set("api_responses", key, {"stale": True})
    monkeypatch.setattr(cache_clearance, "get_cache_manager", lambda: manager)

    cache_clearance._clear_family(family)

    for key in cleared:
        assert manager.get("api_responses", key) is None, key
    for key in kept:
        assert manager.get("api_responses", key) == {"stale": True}, key


# ── the coalescer ─────────────────────────────────────────────────────────────────────────────


def test_a_burst_clears_on_its_leading_edge_and_exactly_once_on_its_trailing_edge():
    timers, runs = FakeTimers(), []
    coalescer = Coalescer(lambda f: runs.append((f, timers.now)), intervals={"x": 10.0, "y": 30.0},
                          clock=timers.clock, call_later=timers.call_later)

    for _ in range(9):
        coalescer.note("x")
        coalescer.note("y")
        timers.advance(1)

    assert runs == [("x", 0.0), ("y", 0.0)], "the first event clears at once"
    assert len(timers.armed) == 2 and coalescer.pending() == ["x", "y"], "one trailing timer each"
    timers.advance(1)
    assert runs[-1] == ("x", 10.0), "the last event of the burst still clears"
    timers.advance(20)
    assert runs[-1] == ("y", 30.0)
    assert len(runs) == 4 and coalescer.pending() == []
    timers.advance(100)
    assert len(runs) == 4, "a quiet family never clears on its own"
    coalescer.note("x")
    assert runs[-1] == ("x", 130.0), "a family past its interval clears on the leading edge again"


async def test_instance_lists_and_the_full_family_keep_independent_intervals():
    timers = FakeTimers()
    recorder = Recorder(timers)
    subject = clearance(timers, recorder)

    subject.note("nuc2", lifecycle("instance-stopped"))
    await drain(subject)
    timers.advance(11)
    subject.note("nuc2", lifecycle("instance-started"))
    await drain(subject)
    assert recorder.calls == [("instance_lists", 0.0), ("instances", 0.0), ("instance_lists", 11.0)]
    assert subject.pending() == ["instances"], "the full family waits for its own 30 s"

    timers.advance(19)
    await drain(subject)
    assert recorder.calls[-1] == ("instances", 30.0)
    assert subject.pending() == []


# ── dirty marks, step-down, the executor ──────────────────────────────────────────────────────


async def test_a_reconnect_marks_every_family_dirty_and_a_first_connect_does_not(monkeypatch):
    async def ids():
        return frozenset({"nuc2"})

    monkeypatch.setattr(registry, "registered_cluster_ids", ids)
    timers, hub = FakeTimers(), FakeHub()
    recorder = Recorder(timers)
    subject = clearance(timers, recorder, hub)
    await subject.sync_registry()
    queue = hub.queues["nuc2"]

    async def feed(frame):
        queue.put_nowait(frame)
        for _ in range(5):
            await asyncio.sleep(0)
        await drain(subject)

    await feed({"type": "hub", "host": "nuc2", "state": "connected"})
    assert recorder.calls == [], "the election already marked everything; a first connect adds nothing"
    await feed({"type": "hub", "host": "nuc2", "state": "disconnected", "error": "gone"})
    assert recorder.calls == []
    await feed({"type": "hub", "host": "nuc2", "state": "connected"})
    assert sorted(recorder.families()) == sorted(FAMILIES), "what the dropped stream hid is cleared"

    await feed(lifecycle("profile-updated"))
    assert subject.pending() == ["profiles"], "inside its interval: one trailing clear"
    await subject.stop(flush_timeout=1.0)


async def test_election_marks_every_family_dirty_and_the_flag_off_starts_nothing(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(cache_clearance, "_clear_family", recorder)
    monkeypatch.setattr(cache_clearance, "DashboardIdentities", NoIdentities)

    async def no_registry():
        return None

    monkeypatch.setattr(registry, "registered_cluster_ids", no_registry)

    monkeypatch.setattr(cache_clearance, "get_settings",
                        lambda: SimpleNamespace(task_journal_cache_clearance=False))
    hub = FakeHub()
    assert cache_clearance.start_for_leader(hub) is None
    await asyncio.sleep(0)
    assert hub.subscribed == [] and recorder.calls == []

    monkeypatch.setattr(cache_clearance, "get_settings",
                        lambda: SimpleNamespace(task_journal_cache_clearance=True))
    subject = cache_clearance.start_for_leader(hub)
    assert subject is not None
    await drain(subject)
    assert sorted(recorder.families()) == sorted(FAMILIES)
    await subject.stop(flush_timeout=1.0)


async def test_step_down_flushes_the_pending_trailing_clears_and_then_nothing_runs(monkeypatch):
    async def ids():
        return frozenset({"nuc1", "nuc2"})

    monkeypatch.setattr(registry, "registered_cluster_ids", ids)
    timers, hub = FakeTimers(), FakeHub()
    recorder = Recorder(timers)
    subject = clearance(timers, recorder, hub)
    await subject.sync_registry()
    subject.note("nuc2", lifecycle("instance-deleted"))
    await drain(subject)
    timers.advance(2)
    subject.note("nuc2", lifecycle("instance-created"))
    assert subject.pending() == ["instance_lists", "instances"]

    await subject.stop(flush_timeout=1.0)

    assert recorder.calls == [
        ("instance_lists", 0.0), ("instances", 0.0), ("instance_lists", 2.0), ("instances", 2.0),
    ], "the burst's last event is cleared before the leader lets go"
    assert sorted(hub.unsubscribed) == ["nuc1", "nuc2"]
    assert subject.pending() == [] and [h for h in timers.armed if not h.cancelled] == []
    timers.advance(100)
    subject.note("nuc2", lifecycle("instance-stopped"))
    subject.mark_all_dirty("late")
    await drain(subject)
    assert len(recorder.calls) == 4, "nothing runs after step-down"


async def test_a_stuck_clear_cannot_hold_step_down_past_its_flush_timeout():
    from concurrent.futures import ThreadPoolExecutor

    release = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stuck_clear")
    timers = FakeTimers()
    subject = CacheClearance(FakeHub(), identities=NoIdentities(), clearer=lambda f: release.wait(5),
                             clock=timers.clock, call_later=timers.call_later, executor=executor)
    subject.note("nuc2", lifecycle("image-deleted"))
    loop = asyncio.get_running_loop()
    started = loop.time()

    await subject.stop(flush_timeout=0.05)

    assert loop.time() - started < 1.0
    release.set()
    executor.shutdown(wait=True)


async def test_the_clearer_runs_on_the_clearance_thread_not_the_event_loop():
    recorder = Recorder()
    timers = FakeTimers()
    subject = CacheClearance(FakeHub(), identities=NoIdentities(), clearer=recorder,
                             clock=timers.clock, call_later=timers.call_later)
    loop_thread = threading.current_thread()

    subject.note("nuc2", lifecycle("network-created"))
    await drain(subject)

    assert recorder.families() == ["networks"]
    (thread,) = recorder.threads
    assert thread is not loop_thread
    assert thread.name.startswith("cache_clearance"), thread.name


def test_a_frame_that_cannot_be_noted_never_raises():
    subject = CacheClearance(FakeHub(), identities=NoIdentities(), clearer=lambda f: None)
    # No running loop: the trailing timer cannot be armed and the submit cannot run.
    subject.note("nuc2", lifecycle("instance-started"))
    subject.note("nuc2", None)
    subject.mark_all_dirty("no loop")


# ── the leader's wiring ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("enabled", [True, False])
async def test_the_leader_subscribes_every_cluster_and_lets_go_on_step_down(monkeypatch, enabled):
    async def ids():
        return frozenset({"nuc1", "C2"})

    recorder = Recorder()
    monkeypatch.setattr(registry, "registered_cluster_ids", ids)
    monkeypatch.setattr(cache_clearance, "_clear_family", recorder)
    monkeypatch.setattr(cache_clearance, "DashboardIdentities", NoIdentities)
    monkeypatch.setattr(cache_clearance, "get_settings",
                        lambda: SimpleNamespace(task_journal_cache_clearance=enabled))
    monkeypatch.setattr(follower_module, "_audit", lambda action, **d: None)
    monkeypatch.setattr(follower_module, "_sleep", ScriptedSleep(park=(HEARTBEAT_S, REGISTRY_INTERVAL_S, SWEEP_TICK_S)))
    monkeypatch.setattr(cache_clearance, "_sleep", ScriptedSleep(park=(cache_clearance.REGISTRY_INTERVAL_S,)))
    hub = FakeHub()
    follower = JournalFollower(
        FakeSessionMaker(), lambda: None, retry_seconds=7, database_url="postgresql+asyncpg://x",
        hub=hub, identities=NoIdentities(), excluded=frozenset({"c2"}),
    )

    leading = asyncio.create_task(follower.lead())
    expected = ["nuc1", "nuc1"] if enabled else ["nuc1"]
    for _ in range(100):
        await asyncio.sleep(0)
        if len(hub.subscribed) == len(expected):
            break
    clearance_task = follower._clearance

    assert sorted(host for host, _, _ in hub.subscribed) == expected, (
        "an excluded cluster (read-only, someone else's) gets no stream from either"
    )
    assert all(firehose for _, firehose, _ in hub.subscribed)
    if enabled:
        await drain(clearance_task)
        assert sorted(recorder.families()) == sorted(FAMILIES), "election marks every family dirty"
    else:
        assert clearance_task is None and recorder.calls == []

    leading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leading
    await follower.step_down(graceful=True)

    assert sorted(hub.unsubscribed) == expected, "step-down lets go of every subscription"
    assert follower._clearance is None


async def test_a_family_has_at_most_one_clear_queued_and_a_note_during_it_runs_one_more():
    """With Redis down each clear holds the thread for its socket timeouts; a queue must not grow."""
    from concurrent.futures import ThreadPoolExecutor

    release = threading.Event()
    calls = []
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="slow_clear")

    def slow(family):
        calls.append(family)
        release.wait(5)

    subject = CacheClearance(FakeHub(), identities=NoIdentities(), clearer=slow, executor=executor,
                             clock=FakeTimers().clock, call_later=FakeTimers().call_later)
    for _ in range(5):
        subject._submit("images")
    assert len(subject._inflight) == 1, "one clear running, none queued behind it"
    release.set()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if not subject._inflight and len(calls) == 2:
            break
    assert calls == ["images", "images"], "the notes during the clear add exactly one more"
    executor.shutdown(wait=True)
