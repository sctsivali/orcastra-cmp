"""A cluster in `TASK_JOURNAL_EXCLUDE_CLUSTERS` is never subscribed, seeded or state-tracked.

A partner's production MicroCloud is registered read-only in the development deployment. Its
operations must not be copied into this database, it must never become a cancel target, and
its name must not appear in the dev journal at all. The list is a csv, trimmed, matched without
regard to case, and consulted on every registry sync, so a cluster excluded after boot is
released on the next pass.
"""

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.services.lxd import registry  # noqa: E402
from app.services.task_journal import follower as follower_module  # noqa: E402
from app.services.task_journal.follower import JournalFollower, excluded_clusters  # noqa: E402
from tests._follower_fakes import FakeHub, NoIdentities  # noqa: E402
from tests._journal_fakes import FakeSessionMaker  # noqa: E402


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", frozenset()),
        ("C2", frozenset({"c2"})),
        (" C2 , nuc-old ,, ", frozenset({"c2", "nuc-old"})),
        ("central2,CENTRAL2", frozenset({"central2"})),
    ],
)
def test_the_exclude_list_is_a_trimmed_case_insensitive_csv(raw, expected):
    assert excluded_clusters(raw) == expected


def test_the_default_reads_the_setting(monkeypatch):
    settings = follower_module.get_settings()
    monkeypatch.setattr(settings, "task_journal_exclude_clusters", " C2 ")
    monkeypatch.setattr(follower_module, "get_settings", lambda: settings)
    assert excluded_clusters() == frozenset({"c2"})


def _follower(hub, maker, excluded):
    return JournalFollower(
        maker, lambda: None, retry_seconds=15, database_url="x",
        hub=hub, engine_factory=lambda url: None, identities=NoIdentities(), excluded=excluded,
    )


async def test_an_excluded_cluster_is_never_subscribed_seeded_or_state_tracked(monkeypatch):
    async def ids():
        return frozenset({"nuc1", "C2"})

    monkeypatch.setattr(registry, "registered_cluster_ids", ids)
    hub = FakeHub()
    maker = FakeSessionMaker()
    follower = _follower(hub, maker, frozenset({"c2"}))

    await follower.sync_registry()

    assert [h for h, _, _ in hub.subscribed] == ["nuc1"]
    assert "C2" not in follower._queues and "C2" not in follower._consumers
    assert follower.is_excluded("C2") and follower.is_excluded(" c2 ")
    assert not any("C2" in str(p) for db in maker.sessions for _, p in db.calls), (
        "no state row, no seed, no log line names the excluded cluster"
    )
    await follower.step_down(graceful=True)


async def test_the_constructor_reads_the_exclude_list_from_settings(monkeypatch):
    monkeypatch.setattr(follower_module, "excluded_clusters", lambda raw=None: frozenset({"c2"}))
    follower = JournalFollower(
        FakeSessionMaker(), lambda: None, retry_seconds=15, database_url="x",
        hub=FakeHub(), engine_factory=lambda url: None, identities=NoIdentities(),
    )
    assert follower.is_excluded("C2") and not follower.is_excluded("nuc1")


def test_the_sweeps_only_visit_hosts_the_follower_tracks():
    """Reconcile and warnings iterate the tracked state map, which an excluded host never enters."""
    import inspect

    source = inspect.getsource(follower_module.JournalFollower.run_sweep)
    assert source.count("await self._per_host(") == 2, "reconcile and warnings go through the per-host loop"
    per_host = inspect.getsource(follower_module.JournalFollower._per_host)
    assert "for host in self.connected_hosts():" in per_host
    assert "health.is_tripped(host)" in per_host
    # One host that raises must not starve the hosts sorted after it (a review finding).
    assert "except Exception" in per_host and "continue" in per_host
