"""The monitoring sweep: one log line per collection, bounded status reads, capped warnings.

Per-phase timing lets a slow cycle be attributed, and the status fan-out has a wall clock so a
cluster that accepts a connection and never answers cannot hold every collection behind it.
"""

import asyncio
import logging
import threading

import pytest

from app.models.schemas import NodeStatus
from app.services.lxd.instance import FleetInventory
from app.services import monitoring_sweep
from app.services.monitoring_sweep import (
    FleetRead,
    SweepTimer,
    count_warnings,
    log_sweep,
    read_fleet,
    read_statuses,
)


def _status(name: str) -> NodeStatus:
    return NodeStatus(name=name, endpoint=f"https://{name}.invalid:8443", connected=True, reachable=True)


class _Service:
    def __init__(self, names, *, slow=(), failing=(), warnings=None, hang=None):
        self.names = list(names)
        self.slow = set(slow)
        self.failing = set(failing)
        self.warnings = warnings or {}
        self.hang = hang or threading.Event()
        self.status_threads = []

    def get_node_names(self):
        return list(self.names)

    async def read_fleet_inventory(self):
        return FleetInventory([], [name for name in self.names if name not in self.slow], [], {})

    def check_node_status(self, name):
        self.status_threads.append(threading.current_thread().name)
        if name in self.failing:
            raise RuntimeError("certificate refused")
        if name in self.slow:
            self.hang.wait(30)
        return _status(name)

    def get_client_with_reconnect(self, name):
        rows = self.warnings.get(name)
        if rows == "hang":
            self.hang.wait(30)
            return None

        class _Warnings:
            def get(self, params=None):
                return type("R", (), {"json": lambda _self: {"metadata": rows or []}})()

        class _Api:
            warnings = _Warnings()

        return type("C", (), {"api": _Api()})()


@pytest.mark.asyncio
async def test_a_cluster_past_the_wall_is_not_answering_and_the_rest_report(monkeypatch):
    monkeypatch.setattr(monitoring_sweep, "STATUS_WALL_SECONDS", 0.2)
    service = _Service(["fast-1", "hung-1", "fast-2"], slow={"hung-1"})
    try:
        statuses, status_ms, timed_out = await read_statuses(service, service.get_node_names())
    finally:
        service.hang.set()

    by_name = {status.name: status for status in statuses}
    assert by_name["fast-1"].connected and by_name["fast-2"].connected
    assert not by_name["hung-1"].connected
    assert "asked again on the next one" in (by_name["hung-1"].error or "")
    assert timed_out == {"hung-1": "status"}
    assert set(status_ms) == {"fast-1", "hung-1", "fast-2"}


@pytest.mark.asyncio
async def test_status_reads_run_on_their_own_executor_with_a_per_cluster_cap(monkeypatch):
    seen = []

    async def recorder(fn, *args, **options):
        seen.append(options)
        return fn(*args)

    monkeypatch.setattr(monitoring_sweep, "run_blocking", recorder)
    service = _Service(["a", "b"])

    await read_statuses(service, service.get_node_names())

    assert [options["concurrency_key"] for options in seen] == ["a", "b"]
    for options in seen:
        assert options["executor"] is monitoring_sweep._status_executor
        assert options["max_in_flight_per_key"] == 3
        assert options["timeout"] == monitoring_sweep.STATUS_WALL_SECONDS


@pytest.mark.asyncio
async def test_a_failing_status_read_reports_its_reason_and_never_raises():
    service = _Service(["ok", "refused"], failing={"refused"})

    statuses, _, timed_out = await read_statuses(service, service.get_node_names())

    refused = next(status for status in statuses if status.name == "refused")
    assert refused.connected is False and "certificate refused" in (refused.error or "")
    assert timed_out == {}, "an answer that failed is not a timeout"


@pytest.mark.asyncio
async def test_the_fleet_read_times_inventory_and_status_separately():
    timer = SweepTimer()
    service = _Service(["a"])

    fleet = await read_fleet(service, timer)

    assert fleet.connected == ["a"] and [s.name for s in fleet.statuses] == ["a"]
    assert {"inventory", "status"} <= set(timer.phases)


@pytest.mark.asyncio
async def test_warnings_are_counted_and_a_cluster_past_its_wall_is_named(monkeypatch):
    monkeypatch.setattr(monitoring_sweep, "WARNINGS_WALL_SECONDS", 0.2)
    service = _Service(
        ["a", "b"],
        warnings={
            "a": [{"severity": "critical"}, {"severity": "warning"}, {"severity": "low"}, "junk"],
            "b": "hang",
        },
    )
    try:
        summary, skipped = await count_warnings(service, ["a", "b"])
    finally:
        service.hang.set()

    assert (summary.total, summary.critical, summary.warning, summary.info) == (3, 1, 1, 1)
    assert skipped == ["b"]


def _fleet() -> FleetRead:
    return FleetRead(
        vms=[], connected=["a"], statuses=[_status("a"), _status("b")],
        status_ms={"a": 1200, "b": 30000}, timed_out={"b": "status"},
    )


def test_a_collected_sweep_logs_one_info_line_with_every_phase(caplog):
    timer = SweepTimer()
    timer.phases.update(inventory=900, status=30000, rates=40, warnings=300, history=10)

    with caplog.at_level(logging.DEBUG, logger=monitoring_sweep.logger.name):
        log_sweep(collected=True, timer=timer, fleet=_fleet(), running=12, history_written=True)

    (record,) = [r for r in caplog.records if r.name == monitoring_sweep.logger.name]
    assert record.levelno == logging.INFO
    message = record.getMessage()
    for phase in monitoring_sweep.PHASES:
        assert phase in message
    assert "timed out 1" in message and "running 12" in message
    fields = record.extra_fields["monitoring_sweep"]
    assert fields["status_ms_by_cluster"] == {"a": 1200, "b": 30000}
    assert fields["timed_out"] == {"b": "status"} and fields["history_written"] is True
    assert set(fields["phases_ms"]) == set(monitoring_sweep.PHASES)


def test_other_scopes_log_at_debug(caplog):
    with caplog.at_level(logging.DEBUG, logger=monitoring_sweep.logger.name):
        log_sweep(collected=False, timer=SweepTimer(), fleet=_fleet(), running=0, history_written=False)

    (record,) = [r for r in caplog.records if r.name == monitoring_sweep.logger.name]
    assert record.levelno == logging.DEBUG


def test_two_sweeps_render_two_messages_so_the_dedup_filter_keeps_both():
    first, second = SweepTimer(), SweepTimer()
    first.phases["status"], second.phases["status"] = 30000, 31000
    messages = []

    class _Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    handler = _Capture()
    monitoring_sweep.logger.addHandler(handler)
    previous = monitoring_sweep.logger.level
    monitoring_sweep.logger.setLevel(logging.INFO)
    try:
        for timer in (first, second):
            log_sweep(collected=True, timer=timer, fleet=_fleet(), running=1, history_written=True)
    finally:
        monitoring_sweep.logger.removeHandler(handler)
        monitoring_sweep.logger.setLevel(previous)

    assert len(messages) == 2 and messages[0] != messages[1]


def test_the_dashboard_reads_the_fleet_through_the_sweep_module():
    import inspect

    from app.routers import monitoring

    source = inspect.getsource(monitoring.get_monitoring_dashboard)
    assert "read_fleet(lxd_service, timer)" in source
    assert "check_nodes_status_async" not in source, "the unbounded status fan-out is gone"
    assert "log_sweep(" in source


def test_the_sweep_never_blocks_on_a_missing_answer_for_long():
    assert monitoring_sweep.WARNINGS_WALL_SECONDS <= 10
    assert asyncio.iscoroutinefunction(read_fleet)
