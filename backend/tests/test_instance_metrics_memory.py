"""Memory and disk I/O from /1.0/metrics, folded into the dialog's one sample.

LXD's state endpoint counts page cache as used: a VM whose programs hold 1 GiB of 4 GiB reads
79% when the rest of the RAM is cache. /1.0/metrics carries MemAvailable, so used becomes
what the guest's programs hold, with the cache shown beside it.
"""

from types import SimpleNamespace

import pytest

from app.services.lxd import instance_metrics as metrics_module
from app.services.lxd.instance_metrics import read_instance_metrics
from app.services.lxd.metrics_scrape import ProjectScrape
from app.services.lxd.metrics_text import InstanceSeries

GIB = 1024**3


def _instance(kind="virtual-machine", processes=20, expanded=None):
    state = SimpleNamespace(
        cpu={"usage": 0 if processes < 0 else 1_000},
        memory={"usage": int(3.2 * GIB), "total": 4 * GIB},
        disk={"root": {"usage": 5 * GIB, "total": 20 * GIB}},
        network={},
        processes=processes,
        pid=4242,
        started_at=None,
    )
    return SimpleNamespace(
        name="vm",
        status="Running",
        status_code=103,
        type=kind,
        config={"limits.cpu": "2", "volatile.uuid": "u"},
        expanded_config=expanded,
        last_used_at="2026-10-07T00:00:00Z",
        location="none",
        state=lambda: state,
    )


def _series(disk_read=1_000.0, disk_written=500.0, cpu_effective=0.0):
    return InstanceSeries(
        instance_type="virtual-machine",
        cpu_effective=cpu_effective,
        mem_total=4 * GIB,
        mem_available=3 * GIB,
        mem_free=GIB // 2,
        mem_cached=int(2.2 * GIB),
        disk_read=disk_read,
        disk_written=disk_written,
        disk_devices=("sda",),
    )


@pytest.fixture
def scrapes(monkeypatch):
    """Each read takes the next scrape; None means the metrics read was skipped or failed."""
    queue = []
    monkeypatch.setattr(
        metrics_module, "instance_scrape", lambda *_a, **_k: queue.pop(0) if queue else None
    )
    return queue


def _read(monkeypatch, instance, *, deadline=1e12):
    monkeypatch.setattr(
        metrics_module, "find_instance_in_projects", lambda *_a, **_k: (instance, "default")
    )
    return read_instance_metrics(object(), "cluster-a", "vm", "default", deadline=deadline)


def test_memory_excludes_reclaimable_cache_when_metrics_answer(monkeypatch, scrapes):
    scrapes.append(ProjectScrape(collected_at=100.0, series={"vm": _series()}))

    metrics = _read(monkeypatch, _instance())

    assert metrics.memory_used_bytes == GIB
    assert metrics.memory_total_bytes == 4 * GIB
    assert metrics.memory_usage_percent == 25.0
    assert metrics.memory_available_bytes == 3 * GIB
    assert metrics.memory_cache_bytes == int(2.2 * GIB)
    assert metrics.memory_includes_cache is False


def test_without_metrics_the_state_reading_is_kept_and_labelled(monkeypatch, scrapes):
    metrics = _read(monkeypatch, _instance())

    assert metrics.memory_used_bytes == int(3.2 * GIB)
    assert metrics.memory_includes_cache is True
    assert metrics.memory_available_bytes is None
    assert metrics.disk_io_status == "unavailable"


def test_no_deadline_never_scrapes(monkeypatch):
    called = []
    monkeypatch.setattr(metrics_module, "instance_scrape", lambda *a, **k: called.append(1))

    _read(monkeypatch, _instance(), deadline=None)

    assert called == []


def test_an_unreachable_agent_never_takes_qemu_memory(monkeypatch, scrapes):
    """Without lxd-agent, LXD's metrics memory is QEMU's host RSS rather than the guest's."""
    scrapes.append(ProjectScrape(collected_at=100.0, series={"vm": _series()}))

    metrics = _read(monkeypatch, _instance(processes=-1))

    assert "memory" in metrics.unavailable_sections
    assert metrics.memory_available_bytes is None
    assert metrics.memory_includes_cache is True


def test_disk_io_needs_two_scrapes_ten_seconds_apart(monkeypatch, scrapes):
    scrapes.extend(
        [
            ProjectScrape(collected_at=100.0, series={"vm": _series(1_000.0, 500.0)}),
            ProjectScrape(collected_at=100.0, series={"vm": _series(1_000.0, 500.0)}),
            ProjectScrape(collected_at=112.0, series={"vm": _series(13_000.0, 2_900.0)}),
        ]
    )

    first = _read(monkeypatch, _instance())
    cached = _read(monkeypatch, _instance())
    ready = _read(monkeypatch, _instance())

    assert first.disk_io_status == "calculating"
    assert cached.disk_io_status == "calculating", "the same cached scrape is not a second sample"
    assert ready.disk_io_status == "ready"
    assert ready.disk_read_bytes_per_second == 1_000.0
    assert ready.disk_write_bytes_per_second == 200.0
    assert ready.disk_io_window_seconds == 12.0


def test_a_container_takes_its_effective_vcpus_from_metrics(monkeypatch, scrapes):
    instance = _instance(kind="container")
    instance.config = {"volatile.uuid": "u"}
    scrapes.append(ProjectScrape(collected_at=100.0, series={"vm": _series(cpu_effective=6.0)}))

    metrics = _read(monkeypatch, instance)

    assert metrics.cpu_effective_vcpus == 6


def test_container_disk_io_is_never_reported_as_a_measured_zero(monkeypatch, scrapes):
    """A ZFS-backed container's writes never reach its cgroup's disk counters."""
    instance = _instance(kind="container")
    series = _series()
    container_series = InstanceSeries(
        instance_type="container",
        mem_total=series.mem_total,
        mem_available=series.mem_available,
        mem_free=series.mem_free,
        mem_cached=series.mem_cached,
        mem_shmem=0.0,
        disk_read=0.0,
        disk_written=0.0,
        disk_devices=("nvme0n1",),
    )
    scrapes.extend(
        [
            ProjectScrape(collected_at=100.0, series={"vm": container_series}),
            ProjectScrape(collected_at=112.0, series={"vm": container_series}),
        ]
    )

    _read(monkeypatch, instance)
    metrics = _read(monkeypatch, instance)

    assert metrics.disk_io_status == "unavailable"
    assert metrics.disk_write_bytes_per_second is None


def test_an_unreachable_agent_reports_no_disk_io(monkeypatch, scrapes):
    scrapes.extend(
        [
            ProjectScrape(collected_at=100.0, series={"vm": _series()}),
            ProjectScrape(collected_at=112.0, series={"vm": _series(5_000.0, 5_000.0)}),
        ]
    )

    _read(monkeypatch, _instance(processes=-1))
    metrics = _read(monkeypatch, _instance(processes=-1))

    assert metrics.disk_io_status == "unavailable"
