"""The registry loop follows a cluster the moment it is registered and lets go of one that is
deleted, without touching the rows either one already has.

Both directions of the thirty-second sync: a new id gets a firehose subscription and a
consumer, and its first `connected` hub frame seeds it; a removed id has its consumer cancelled,
its subscription dropped and its state row set to `unregistered`. No task or log row is deleted
or rewritten: non-admins lose them with the scope, and the prune loop removes them in time.
"""

import asyncio
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.services.lxd import registry  # noqa: E402
from app.services.task_journal import follower as follower_module  # noqa: E402
from app.services.task_journal.follower import JournalFollower  # noqa: E402
from tests._follower_fakes import FakeHub, NoIdentities  # noqa: E402
from tests._journal_fakes import FakeSessionMaker  # noqa: E402


@pytest.fixture
def registered(monkeypatch):
    current = {"ids": frozenset({"nuc1"})}

    async def ids():
        return current["ids"]

    monkeypatch.setattr(registry, "registered_cluster_ids", ids)
    return current


def _follower(hub, maker, seeds=None):
    follower = JournalFollower(
        maker, lambda: "lxd", retry_seconds=15, database_url="x",
        hub=hub, engine_factory=lambda url: None, identities=NoIdentities(), excluded=frozenset(),
    )
    if seeds is not None:
        async def fake_seed(host):
            seeds.append(host)

        follower._seed = fake_seed  # type: ignore[method-assign]
    return follower


async def _settle():
    for _ in range(10):
        await asyncio.sleep(0)


async def test_a_newly_registered_cluster_is_followed_and_its_connect_frame_seeds_it(registered):
    hub = FakeHub()
    maker = FakeSessionMaker()
    seeds = []
    follower = _follower(hub, maker, seeds)

    await follower.sync_registry()
    assert hub.subscribed == [("nuc1", True, 2000)]

    registered["ids"] = frozenset({"nuc1", "nuc2"})
    await follower.sync_registry()
    assert [h for h, _, _ in hub.subscribed] == ["nuc1", "nuc2"]
    assert set(follower._consumers) == {"nuc1", "nuc2"}

    hub.queues["nuc2"].put_nowait({"type": "hub", "host": "nuc2", "state": "connected", "error": None})
    await _settle()

    assert seeds == ["nuc2"], "the first connected frame seeds the new cluster"
    states = [p for db in maker.sessions for sql, p in db.calls if "task_follower_clusters" in sql]
    assert states[-1]["cluster_id"] == "nuc2" and states[-1]["state"] == "connected"
    rows = [p for db in maker.sessions for sql, p in db.calls if "INSERT INTO cluster_log" in sql]
    assert len(rows) == 1 and rows[0][0]["kind"] == "task"
    assert rows[0][0]["action"] == "journal.upstream.restored"
    await follower.step_down(graceful=True)


async def test_a_deleted_cluster_is_unsubscribed_marked_unregistered_and_its_rows_kept(registered):
    hub = FakeHub()
    maker = FakeSessionMaker()
    follower = _follower(hub, maker, [])
    registered["ids"] = frozenset({"nuc1", "nuc2"})
    await follower.sync_registry()
    consumer = follower._consumers["nuc2"]
    follower._cluster_state["nuc2"] = "connected"

    registered["ids"] = frozenset({"nuc1"})
    await follower.sync_registry()

    assert hub.unsubscribed == ["nuc2"]
    assert consumer.cancelled() or consumer.done()
    assert set(follower._consumers) == {"nuc1"} and "nuc2" not in follower._cluster_state
    states = [p for db in maker.sessions for sql, p in db.calls if "task_follower_clusters" in sql]
    assert states[-1] == {"cluster_id": "nuc2", "state": "unregistered", "error": None, "seeded": False}
    for sql in maker.statements:
        assert not sql.startswith("DELETE"), "its rows are left alone"
        assert "UPDATE task_journal" not in sql and "UPDATE cluster_log" not in sql
    await follower.step_down(graceful=True)


async def test_an_unreadable_registry_keeps_the_current_set(monkeypatch):
    async def unknown():
        return None

    monkeypatch.setattr(registry, "registered_cluster_ids", unknown)
    hub = FakeHub()
    follower = _follower(hub, FakeSessionMaker(), [])
    follower._queues["nuc1"] = asyncio.Queue()

    await follower.sync_registry()

    assert hub.subscribed == [] and hub.unsubscribed == []
    assert set(follower._queues) == {"nuc1"}


async def test_a_disconnect_writes_one_log_row_per_state_change(registered):
    hub = FakeHub()
    maker = FakeSessionMaker()
    follower = _follower(hub, maker, [])
    await follower.sync_registry()
    queue = hub.queues["nuc1"]

    for state, error in (("connected", None), ("disconnected", "reset"), ("disconnected", "reset"), ("connected", None)):
        queue.put_nowait({"type": "hub", "host": "nuc1", "state": state, "error": error})
    await _settle()

    rows = [p[0] for db in maker.sessions for sql, p in db.calls if "INSERT INTO cluster_log" in sql]
    assert [r["action"] for r in rows] == [
        "journal.upstream.restored", "journal.upstream.lost", "journal.upstream.restored",
    ], "the repeated disconnect is not a second line"
    assert rows[1]["severity"] == "warning" and "reset" in rows[1]["message"]
    states = [p for db in maker.sessions for sql, p in db.calls if "INSERT INTO task_follower_clusters" in sql]
    assert [s["state"] for s in states] == ["connected", "disconnected", "disconnected", "connected"]
    assert states[1]["error"] == "reset"
    await follower.step_down(graceful=True)


async def test_the_registry_loop_syncs_every_thirty_seconds(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError()

    monkeypatch.setattr(follower_module, "_sleep", fake_sleep)
    synced = []
    follower = _follower(FakeHub(), FakeSessionMaker(), [])

    async def fake_sync():
        synced.append(True)

    follower.sync_registry = fake_sync  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await follower._registry_loop()
    assert sleeps == [30.0, 30.0] and len(synced) == 2
