"""A collector follower relays the leader's dashboard and never sweeps the fleet itself.

A follower that builds the dashboard on a cache miss sweeps every cluster whenever the
shared entry expires, which happens every cycle, and writes duplicate history points
seconds apart.
"""

import asyncio
from datetime import datetime, timezone

from app.models.schemas import MonitoringDashboardResponse
from app.routers import monitoring
from app.services.metrics_collector import MetricsCollector


class _Broadcaster:
    def __init__(self, clients=1):
        self.client_count = clients
        self.sent = []

    async def broadcast(self, data):
        self.sent.append(data)


def _dashboard(minute):
    return MonitoringDashboardResponse(timestamp=datetime(2026, 10, 7, 12, minute, tzinfo=timezone.utc))


def _collector(monkeypatch, *, leading, broadcaster, stored=None, calls=None):
    calls = calls if calls is not None else []
    collector = MetricsCollector(collection_interval=30)
    collector._lxd_service = object()

    async def _connections():
        calls.append("connections")

    async def _prewarm():
        calls.append("prewarm")

    async def _sweep(**kwargs):
        calls.append(("sweep", kwargs.get("fresh")))
        return _dashboard(0)

    def _cached():
        calls.append("cache read")
        return stored[0] if stored else None

    monkeypatch.setattr(collector, "_ensure_cluster_connections", _connections)
    monkeypatch.setattr(collector, "_prewarm_instances_cache", _prewarm)
    monkeypatch.setattr(collector, "_claim_collection_lease", lambda: (calls.append("claim"), leading)[1])
    monkeypatch.setattr("app.routers.monitoring.get_monitoring_dashboard", _sweep)
    monkeypatch.setattr("app.routers.monitoring.cached_dashboard", _cached)
    monkeypatch.setattr("app.services.ws_broadcast.get_monitoring_broadcaster", lambda: broadcaster)
    return collector, calls


def test_a_follower_relays_the_stored_dashboard_and_sweeps_nothing(monkeypatch):
    broadcaster = _Broadcaster()
    collector, calls = _collector(
        monkeypatch, leading=False, broadcaster=broadcaster, stored=[_dashboard(5)]
    )

    asyncio.run(collector._collect_metrics())

    assert not any(isinstance(call, tuple) for call in calls), "a follower built the dashboard"
    assert "prewarm" not in calls, "a follower ran the prewarm sweeps"
    assert "connections" in calls, "every worker still keeps its own cluster connections"
    assert len(broadcaster.sent) == 1
    assert broadcaster.sent[0]["timestamp"].startswith("2026-10-07T12:05")


def test_a_cache_miss_on_a_follower_relays_nothing_and_still_sweeps_nothing(monkeypatch):
    broadcaster = _Broadcaster()
    collector, calls = _collector(monkeypatch, leading=False, broadcaster=broadcaster, stored=[])

    asyncio.run(collector._collect_metrics())

    assert broadcaster.sent == []
    assert not any(isinstance(call, tuple) for call in calls)


def test_each_collection_is_relayed_once(monkeypatch):
    broadcaster = _Broadcaster()
    stored = [_dashboard(5)]
    collector, _calls = _collector(monkeypatch, leading=False, broadcaster=broadcaster, stored=stored)

    asyncio.run(collector._dashboards.relay())
    asyncio.run(collector._dashboards.relay())
    stored[0] = _dashboard(6)
    asyncio.run(collector._dashboards.relay())

    assert [sent["timestamp"][11:16] for sent in broadcaster.sent] == ["12:05", "12:06"]


def test_a_follower_without_clients_does_not_read_the_cache(monkeypatch):
    collector, calls = _collector(
        monkeypatch, leading=False, broadcaster=_Broadcaster(clients=0), stored=[_dashboard(5)]
    )

    asyncio.run(collector._dashboards.relay())

    assert "cache read" not in calls


def test_the_leader_sweeps_fresh_renews_then_prewarms(monkeypatch):
    broadcaster = _Broadcaster()
    collector, calls = _collector(monkeypatch, leading=True, broadcaster=broadcaster)

    async def _cycle():
        await collector._collect_metrics()
        assert "prewarm" not in calls, "the next sweep must not wait on the prewarm"
        await collector._prewarm_task

    asyncio.run(_cycle())

    assert calls == ["claim", "connections", ("sweep", True), "claim", "prewarm"]
    assert len(broadcaster.sent) == 1


def test_a_prewarm_still_running_is_not_started_twice(monkeypatch):
    collector, calls = _collector(monkeypatch, leading=True, broadcaster=_Broadcaster())
    release = None

    async def _slow_prewarm():
        calls.append("prewarm")
        await release.wait()

    monkeypatch.setattr(collector, "_prewarm_instances_cache", _slow_prewarm)

    async def _two_cycles():
        nonlocal release
        release = asyncio.Event()
        await collector._collect_metrics()
        await asyncio.sleep(0)
        await collector._collect_metrics()
        await asyncio.sleep(0)
        release.set()
        await collector._prewarm_task

    asyncio.run(_two_cycles())

    assert calls.count("prewarm") == 1


def test_a_leader_that_lost_the_lease_during_its_sweep_skips_the_prewarm(monkeypatch):
    broadcaster = _Broadcaster()
    collector, calls = _collector(monkeypatch, leading=True, broadcaster=broadcaster)
    verdicts = iter([True, False])
    monkeypatch.setattr(collector, "_claim_collection_lease", lambda: next(verdicts))

    asyncio.run(collector._collect_metrics())

    assert "prewarm" not in calls
    assert collector._leading is False


def test_the_collected_scope_outlives_a_cycle_and_other_scopes_do_not():
    assert monitoring._dashboard_ttl(monitoring.COLLECTED_SCOPE_KEY) == monitoring.COLLECTED_DASHBOARD_TTL
    assert monitoring._dashboard_ttl("dashboard:scope=abc123:acl=def456") == 35
