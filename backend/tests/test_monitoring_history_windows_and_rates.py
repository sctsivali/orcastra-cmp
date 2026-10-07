"""The monitoring chart's history: time windows, network as a rate, unknown CPU as a gap."""

import asyncio
from datetime import datetime, timedelta, timezone

from app.models.schemas import MetricsHistoryPoint, VMInfo
from app.services import metrics_history_service
from app.services.lxd.instance import _vm_info_from_row
from app.services.monitoring_history import (
    append_point,
    build_point,
    node_rates,
    read_window,
)
from app.services.monitoring_usage import read_instance_usage
from tests._monitoring_usage_fakes import node, row

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def _points(spacing_s: int, count: int):
    return [
        {"timestamp": (NOW - timedelta(seconds=spacing_s * (count - 1 - i))).isoformat(), "i": i}
        for i in range(count)
    ]


def test_a_window_selects_by_time_whatever_the_cadence():
    """At one point per 30 s, "5m" is ten points, not sixty."""
    history = _points(30, 200)

    selection = read_window(history, window_seconds=300, points=None, now=NOW)

    assert len(selection) == 11
    assert selection[-1] is history[-1]


def test_the_legacy_points_parameter_still_counts():
    history = _points(30, 200)

    assert len(read_window(history, window_seconds=None, points=60, now=NOW)) == 60


def test_append_keeps_24_hours_and_drops_the_rest():
    history = _points(60, 60 * 30)  # 30 hours at one a minute

    kept = append_point(history, {"timestamp": NOW.isoformat()}, now=NOW)

    oldest = datetime.fromisoformat(kept[0]["timestamp"])
    assert NOW - oldest <= timedelta(hours=24)
    assert kept[-1]["timestamp"] == NOW.isoformat()


def _usages(vms):
    return asyncio.run(read_instance_usage(vms, {"c1": 8}))


def test_points_carry_network_rates_and_a_new_instance_adds_no_spike():
    _usages([row("a", rx=0, tx=0, read_at=1000.0)])
    usages = _usages(
        [
            row("a", rx=3_000, tx=1_500, read_at=1030.0),
            # first seen this sweep with a huge lifetime counter: no rate yet, so no spike
            row("new", rx=10**12, tx=10**12, read_at=1030.0),
        ]
    )
    nodes = [node("c1", threads=8, running=2)]

    point = build_point(
        nodes,
        usages,
        fleet_cpu_percent=None,
        total_memory=1000,
        storage_usage_percent=5.0,
        running_instances=2,
        now=NOW,
    )

    assert point["network_rx_bytes_per_sec"] == 100.0
    assert point["network_tx_bytes_per_sec"] == 50.0
    assert point["nodes"]["c1"]["network_rx_bytes_per_sec"] == 100.0
    assert point["network_rx_bytes"] == 0
    assert point["cpu_usage_percent"] is None
    assert MetricsHistoryPoint(**point).cpu_usage_percent is None


def test_postgres_rows_rebuild_fleet_rates_from_their_cluster_entries():
    nodes = {
        "c1": {"network_rx_bytes_per_sec": 10.0, "network_tx_bytes_per_sec": 1.0},
        "c2": {"network_rx_bytes_per_sec": 5.5, "network_tx_bytes_per_sec": None},
        "legacy": {"network_rx_bytes": 99},
    }

    assert node_rates(nodes) == (15.5, 1.0)
    assert node_rates({"old": {"network_rx_bytes": 1}}) == (None, None)
    assert node_rates(None) == (None, None)


def test_a_point_without_cpu_is_never_persisted(monkeypatch):
    """The column is NOT NULL and 0 would read as an idle fleet."""
    sessions = []

    class _Session:
        async def __aenter__(self):
            sessions.append(self)
            return self

        async def __aexit__(self, *_a):
            return None

    monkeypatch.setattr(metrics_history_service, "async_session_maker", _Session)

    asyncio.run(metrics_history_service.persist_metrics_snapshot({"cpu_usage_percent": None}))

    assert sessions == []


def test_list_rows_carry_live_state_only_internally():
    running = _vm_info_from_row(
        {
            "name": "a",
            "project": "p",
            "status": "Running",
            "type": "container",
            "state": {"pid": 1, "cpu": {"usage": 5}},
        },
        "c1",
        "https://c1:8443",
        read_at=42.0,
    )
    stopped = _vm_info_from_row(
        {"name": "b", "project": "p", "status": "Stopped", "type": "container", "state": {}},
        "c1",
        "https://c1:8443",
        read_at=42.0,
    )
    untimed = _vm_info_from_row(
        {"name": "c", "project": "p", "status": "Running", "type": "container", "state": {"pid": 1}},
        "c1",
        "https://c1:8443",
    )

    assert running.live_state == {"pid": 1, "cpu": {"usage": 5}}
    assert running.live_state_read_at == 42.0
    assert "live_state" not in running.model_dump()
    assert "live_state_read_at" not in running.model_dump(mode="json")
    assert stopped.live_state is None and stopped.live_state_read_at is None
    assert untimed.live_state is None, "a row whose read time is unknown cannot be sampled"
    assert isinstance(running, VMInfo)
