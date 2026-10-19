"""One follower across the workers, and a leader whose session dies stands down at once.

The election is `pg_try_advisory_lock(20330, 21578)` on a dedicated NullPool connection in
AUTOCOMMIT; the lock is session-level, so a killed worker frees it without a word. The loser
retries every `task_journal_follower_retry_seconds`. The leader heartbeats `SELECT 1` every
fifteen seconds and treats any exception as "the session, and the lock with it, is gone": it
unsubscribes every host, stops its writer, records `task.journal.follower.lost` and goes back to
being a candidate. A graceful stop unlocks explicitly so the hand-over is immediate.
"""

import asyncio
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.services.lxd import registry  # noqa: E402
from app.services.task_journal import follower as follower_module  # noqa: E402
from app.services.task_journal.follower import (  # noqa: E402
    HEARTBEAT_S,
    REGISTRY_INTERVAL_S,
    STATE_CANDIDATE,
    STATE_LEADER,
    SWEEP_TICK_S,
    JournalFollower,
)
from app.services.task_journal.repository_types import FOLLOWER_LOCK  # noqa: E402
from tests._follower_fakes import (  # noqa: E402
    FakeEngine,
    FakeHub,
    LockStore,
    NoIdentities,
    ScriptedSleep,
)
from tests._journal_fakes import FakeSessionMaker  # noqa: E402

# Distinct from HEARTBEAT_S (15.0) so the scripted sleep can tell the two waits apart.
RETRY_S = 7


@pytest.fixture
def audits(monkeypatch):
    recorded = []
    monkeypatch.setattr(follower_module, "_audit", lambda action, **d: recorded.append(action))
    return recorded


@pytest.fixture
def two_clusters(monkeypatch):
    async def ids():
        return frozenset({"nuc1", "nuc2"})

    monkeypatch.setattr(registry, "registered_cluster_ids", ids)


def _follower(store, hub=None, maker=None, *, fail_heartbeat_after=None):
    engine = FakeEngine(store, fail_heartbeat_after=fail_heartbeat_after)
    return JournalFollower(
        maker or FakeSessionMaker(),
        lambda: None,
        retry_seconds=RETRY_S,
        database_url="postgresql+asyncpg://x",
        hub=hub or FakeHub(),
        engine_factory=engine.factory,
        identities=NoIdentities(),
        excluded=frozenset(),
    ), engine


async def test_exactly_one_of_two_followers_takes_the_lock():
    store = LockStore()
    first, _ = _follower(store)
    second, second_engine = _follower(store)

    assert await first.try_acquire() is True
    assert await second.try_acquire() is False

    assert store.attempts == 2
    holder = store.holder
    assert holder is first._lock_conn
    assert holder.options == {"isolation_level": "AUTOCOMMIT"}
    sql, params = holder.executed[0]
    assert "pg_try_advisory_lock(:classid, :objid)" in sql
    assert params == {"classid": FOLLOWER_LOCK[0], "objid": FOLLOWER_LOCK[1]} == {
        "classid": 20330, "objid": 21578,
    }
    loser = store.connections[1]
    assert loser.closed and second_engine.disposed == 1, "the loser holds no connection open"
    assert second._lock_conn is None


async def test_the_loser_retries_every_retry_interval(monkeypatch, audits):
    store = LockStore()
    leader, _ = _follower(store)
    assert await leader.try_acquire() is True
    loser, _ = _follower(store)
    sleep = ScriptedSleep(stop_on=RETRY_S, stop_after=3)
    monkeypatch.setattr(follower_module, "_sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await loser.run()

    assert sleep.calls == [RETRY_S, RETRY_S, RETRY_S]
    assert store.attempts == 1 + 3, "one attempt per retry, none of them elected"
    assert loser.state == STATE_CANDIDATE
    assert audits == []


async def test_a_failed_heartbeat_steps_the_leader_down_and_unsubscribes_every_host(
    monkeypatch, audits, two_clusters
):
    store = LockStore()
    hub = FakeHub()
    maker = FakeSessionMaker()
    leader, engine = _follower(store, hub, maker, fail_heartbeat_after=1)
    # The heartbeat wait returns at once; the registry and sweep loops park after their first
    # pass; the second retry wait ends the test.
    sleep = ScriptedSleep(
        instant=(HEARTBEAT_S,), park=(REGISTRY_INTERVAL_S, SWEEP_TICK_S),
        stop_on=RETRY_S, stop_after=2,
    )
    monkeypatch.setattr(follower_module, "_sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await leader.run()

    assert audits[:2] == [
        "task.journal.follower.elected", "task.journal.follower.lost",
    ], "one election, then the lost record the moment the heartbeat raised"
    first_conn = store.connections[0]
    assert first_conn.heartbeats == 2, "the second heartbeat is the one that raised"
    assert first_conn.closed
    assert [h for h, firehose, size in hub.subscribed[:2]] == ["nuc1", "nuc2"]
    assert all(firehose and size == 2000 for _, firehose, size in hub.subscribed)
    assert sorted(hub.unsubscribed[:2]) == ["nuc1", "nuc2"], "step-down unsubscribes every host"
    assert store.attempts >= 2, "after standing down the worker is a candidate again"
    assert leader.state == STATE_CANDIDATE
    assert leader._lock_conn is None and leader._queues == {} and leader._consumers == {}
    statements = maker.statements
    assert any("SET state = 'disconnected'" in sql for sql in statements), (
        "election resets every cluster state before following"
    )


async def test_a_graceful_stop_unlocks_and_unsubscribes(monkeypatch, audits, two_clusters):
    store = LockStore()
    hub = FakeHub()
    engine = FakeEngine(store)
    monkeypatch.setattr(follower_module, "_default_engine", engine.factory)
    monkeypatch.setattr(follower_module, "get_events_hub", lambda: hub)
    monkeypatch.setattr(follower_module, "DashboardIdentities", NoIdentities)
    monkeypatch.setattr(follower_module, "excluded_clusters", lambda raw=None: frozenset())
    sleep = ScriptedSleep(park=(HEARTBEAT_S, REGISTRY_INTERVAL_S, SWEEP_TICK_S, RETRY_S))
    monkeypatch.setattr(follower_module, "_sleep", sleep)

    follower_module.start_follower(
        FakeSessionMaker(), lambda: None, retry_seconds=RETRY_S, database_url="postgresql://x"
    )
    task = follower_module._task
    for _ in range(50):
        await asyncio.sleep(0)
        if len(hub.subscribed) == 2:
            break
    live = follower_module.get_follower()
    assert live is not None and live.state == STATE_LEADER
    assert engine.urls == ["postgresql://x"], "the lock engine is built on the URL main.py passes"

    await follower_module.stop_follower()

    assert task.done()
    conn = store.connections[0]
    assert any("pg_advisory_unlock(:classid, :objid)" in sql for sql, _ in conn.executed), (
        "a graceful stop releases the lock explicitly so another worker takes over at once"
    )
    assert conn.closed and store.holder is None
    assert sorted(hub.unsubscribed) == ["nuc1", "nuc2"]
    assert audits == ["task.journal.follower.elected"], "a graceful stop records no loss"
    assert follower_module.get_follower() is None


def test_the_lock_matches_what_follower_is_active_reads():
    """`follower_is_active` reads pg_locks for classid/objid; the follower must lock the same pair."""
    import inspect

    from app.services.task_journal import repository_sweeps

    assert FOLLOWER_LOCK == (20330, 21578)
    source = inspect.getsource(repository_sweeps.follower_is_active)
    assert "FOLLOWER_LOCK[0]" in source and "FOLLOWER_LOCK[1]" in source
    lock_source = inspect.getsource(follower_module.JournalFollower.try_acquire)
    assert "pg_try_advisory_lock(:classid, :objid)" in lock_source
    assert 'isolation_level="AUTOCOMMIT"' in lock_source
    assert "NullPool" in inspect.getsource(follower_module._default_engine)
