"""A cluster that left the registry reads `unregistered`, across restarts and on any worker.

On dev 15 of 22 follower rows read `disconnected` for clusters deregistered long ago (RC5
acceptance FU-8, 2026-11-06). Two paths put them there: every election reset each row that was
not `disconnected` (so `unregistered` too) back to `disconnected`, and the registry loop only
unfollowed ids it was following in memory, which is nothing after a restart. The task panel
counts a `disconnected` row as a degraded cluster; `unregistered` is the end state it skips.
"""

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.services.lxd import registry  # noqa: E402
from app.services.task_journal import repository_sweeps  # noqa: E402
from app.services.task_journal.follower import JournalFollower  # noqa: E402
from tests._follower_fakes import FakeHub, NoIdentities  # noqa: E402
from tests._journal_fakes import FakeSessionMaker  # noqa: E402


class _Session:
    def __init__(self) -> None:
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))


def _follower(maker):
    return JournalFollower(
        maker, lambda: "lxd", retry_seconds=15, database_url="x",
        hub=FakeHub(), engine_factory=lambda url: None, identities=NoIdentities(), excluded=frozenset(),
    )


@pytest.fixture
def registered(monkeypatch):
    current = {"ids": frozenset({"nuc1"})}

    async def ids(fresh: bool = False):
        return current["ids"]

    monkeypatch.setattr(registry, "registered_cluster_ids", ids)
    return current


async def test_an_election_resets_only_connected_rows() -> None:
    db = _Session()
    await repository_sweeps.reset_cluster_states(db)
    [(sql, _params)] = db.calls
    assert "WHERE state = 'connected'" in sql, sql


async def test_the_registry_pass_marks_rows_of_ids_it_no_longer_holds(registered) -> None:
    maker = FakeSessionMaker()
    await _follower(maker).sync_registry()

    marks = [(sql, params) for s in maker.sessions for sql, params in s.calls
             if "SET state = 'unregistered'" in sql]
    assert len(marks) == 1, "one statement per pass, even on a fresh worker that follows nothing yet"
    sql, params = marks[0]
    assert "<> ALL(CAST(:registered AS text[]))" in sql
    assert "state <> 'unregistered'" in sql, "a row already unregistered is not rewritten every pass"
    assert params == {"registered": ["nuc1"]}


async def test_an_unreadable_registry_marks_nothing(registered) -> None:
    registered["ids"] = None
    maker = FakeSessionMaker()
    await _follower(maker).sync_registry()
    assert not [1 for s in maker.sessions for sql, _ in s.calls if "unregistered" in sql]


async def test_an_excluded_cluster_is_not_named_and_reads_as_unfollowed(monkeypatch, registered) -> None:
    registered["ids"] = frozenset({"nuc1", "prod-lab"})
    maker = FakeSessionMaker()
    follower = JournalFollower(
        maker, lambda: "lxd", retry_seconds=15, database_url="x",
        hub=FakeHub(), engine_factory=lambda url: None, identities=NoIdentities(),
        excluded=frozenset({"prod-lab"}),
    )
    await follower.sync_registry()
    [(_sql, params)] = [(sql, p) for s in maker.sessions for sql, p in s.calls if "SET state = 'unregistered'" in sql]
    # An excluded cluster leaves no trace in the journal, and the follower no longer answers for a row
    # it kept from before the exclusion, so that row reads `unregistered` too.
    assert params == {"registered": ["nuc1"]}
