"""Disk I/O is shown only where its counters follow real I/O, and says why where they do not.

A container's disk counters come from its cgroup, and a storage driver that writes from its own
kernel threads never charges them: a ZFS-backed container writing 40 MB a second read 0. A VM's
counters are its guest's block devices and need the guest agent. The dialog used to leave disk
I/O blank in both cases without a word; it now names the reason, and the container gate opens per
driver only once a live probe has proven that driver.
"""

from types import SimpleNamespace

import pytest

from app.services.lxd import container_disk_io, pool_drivers
from app.services.lxd import instance_metrics as metrics_module
from app.services.lxd.instance_metrics import apply_disk_io, read_instance_metrics
from app.services.lxd.metrics_scrape import ProjectScrape
from app.services.lxd.metrics_text import InstanceSeries, parse_instance_series
from app.services.lxd.root_disk import root_pool, root_pool_of
from app.models.instance_metrics import InstanceMetrics
from app.models.vm_status import VMStatus
from tests.test_instance_metrics_memory import _instance


def _metrics() -> InstanceMetrics:
    return InstanceMetrics(instance_name="x", host="c", status=VMStatus.RUNNING)


def _series(kind: str, read: float = 1000.0, written: float = 500.0) -> InstanceSeries:
    return InstanceSeries(instance_type=kind, disk_read=read, disk_written=written, disk_devices=("sda",))


@pytest.mark.parametrize(
    "devices, pool",
    [
        ({"root": {"type": "disk", "path": "/", "pool": "fast"}}, "fast"),
        ({"root": {"type": "disk", "path": "/", "pool": "a"}, "r2": {"type": "disk", "path": "/", "pool": "b"}}, None),
        ({"root": {"type": "disk", "path": "/"}}, None),
        ({"data": {"type": "disk", "path": "/srv", "pool": "fast"}}, None),
        ("not a mapping", None),
    ],
)
def test_the_root_pool_is_the_single_root_disks_pool(devices, pool):
    assert root_pool_of(devices) == pool
    assert root_pool({"expanded_devices": devices if isinstance(devices, dict) else {}}) == (
        pool if isinstance(devices, dict) else None
    )


def test_the_migration_checks_use_the_one_root_pool_rule():
    from app.services.migrations import reconciliation, transfer

    assert transfer.root_pool is root_pool and reconciliation.root_pool is root_pool


def test_a_container_on_an_unproven_driver_says_so_even_with_counters():
    metrics = _metrics()
    scrape = ProjectScrape(collected_at=100.0, series={})

    apply_disk_io(metrics, scrape, _series("container"), "id", "e", agent="not_applicable", storage_driver="zfs")

    assert metrics.disk_io_status == "unavailable"
    assert (metrics.disk_io_reason, metrics.disk_io_storage_driver) == ("storage_driver", "zfs")


def test_a_proven_driver_lets_a_containers_counters_through(monkeypatch):
    monkeypatch.setattr(container_disk_io, "CONTAINER_DISK_IO_DRIVERS", frozenset({"btrfs"}))
    metrics = _metrics()

    apply_disk_io(
        metrics, ProjectScrape(collected_at=100.0, series={}), _series("container"),
        "ct-proven", "epoch-1", agent="not_applicable", storage_driver="btrfs",
    )

    assert metrics.disk_io_reason is None
    assert metrics.disk_io_status in ("calculating", "ready")


@pytest.mark.parametrize("agent", ["unreachable", "disabled"])
def test_a_vm_without_its_agent_needs_the_agent(agent):
    metrics = _metrics()

    apply_disk_io(metrics, None, None, "id", "e", agent=agent)

    assert (metrics.disk_io_status, metrics.disk_io_reason) == ("unavailable", "guest_agent")


def test_a_vm_whose_cluster_sent_no_counters_is_not_reported():
    metrics = _metrics()

    apply_disk_io(metrics, None, None, "id", "e", agent="connected")

    assert metrics.disk_io_reason == "not_reported"


def test_a_vm_with_counters_starts_calculating_without_a_reason():
    metrics = _metrics()

    apply_disk_io(
        metrics, ProjectScrape(collected_at=100.0, series={}), _series("virtual-machine"),
        "vm-fresh", "epoch-1", agent="connected",
    )

    assert metrics.disk_io_status == "calculating" and metrics.disk_io_reason is None


class _Cache:
    def __init__(self):
        self.entries, self.ttls = {}, {}

    def get(self, namespace, key):
        return self.entries.get((namespace, key))

    def set(self, namespace, key, value, ttl):
        self.entries[(namespace, key)] = value
        self.ttls[(namespace, key)] = ttl


class _Pools:
    def __init__(self, rows=None, error=None):
        self.rows, self.error, self.calls = rows or [], error, 0

    def get(self, params=None, timeout=None):
        self.calls += 1
        if self.error:
            raise self.error
        return SimpleNamespace(json=lambda: {"metadata": self.rows})


def _client(pools):
    return SimpleNamespace(api={"storage-pools": pools})


@pytest.fixture
def cache(monkeypatch):
    fake = _Cache()
    monkeypatch.setattr(pool_drivers, "_cache", lambda: fake)
    return fake


def test_drivers_are_read_once_per_cluster_and_kept(cache):
    pools = _Pools([{"name": "fast", "driver": "zfs"}, {"name": "bulk", "driver": "btrfs"}])
    deadline = 1e12

    assert pool_drivers.pool_driver(_client(pools), "c", "fast", deadline=deadline) == "zfs"
    assert pool_drivers.pool_driver(_client(pools), "c", "bulk", deadline=deadline) == "btrfs"
    assert pool_drivers.pool_driver(_client(pools), "c", "gone", deadline=deadline) is None

    assert pools.calls == 1
    assert list(cache.ttls.values()) == [pool_drivers.DRIVERS_TTL_SECONDS]


def test_a_failed_read_is_kept_briefly_and_never_raises(cache):
    pools = _Pools(error=ConnectionError("refused"))

    assert pool_drivers.pool_driver(_client(pools), "c", "fast", deadline=1e12) is None
    assert pool_drivers.pool_driver(_client(pools), "c", "fast", deadline=1e12) is None

    assert pools.calls == 1
    assert list(cache.ttls.values()) == [pool_drivers.FAILURE_TTL_SECONDS]


def test_without_a_budget_only_the_cache_answers(cache):
    pools = _Pools([{"name": "fast", "driver": "zfs"}])

    assert pool_drivers.pool_driver(_client(pools), "c", "fast", deadline=None) is None
    assert pools.calls == 0


def test_a_device_label_with_a_trailing_newline_is_not_a_whole_disk():
    body = (
        'lxd_disk_read_bytes_total{device="sda\\n",name="vm",project="p",type="virtual-machine"} 5\n'
        'lxd_disk_read_bytes_total{device="sdb",name="vm",project="p",type="virtual-machine"} 7\n'
    )

    series = parse_instance_series(body, project="p")["vm"]

    assert series.disk_devices == ("sdb",) and series.disk_read == 7.0


def test_the_dialog_reads_a_containers_driver_only_when_it_asked_for_metrics(monkeypatch):
    asked = []
    monkeypatch.setattr(metrics_module, "instance_scrape", lambda *a, **k: None)
    monkeypatch.setattr(
        metrics_module, "pool_driver", lambda client, host, pool, deadline: asked.append(pool) or "zfs"
    )
    container = _instance(kind="container")
    container.expanded_devices = {"root": {"type": "disk", "path": "/", "pool": "fast"}}
    monkeypatch.setattr(metrics_module, "find_instance_in_projects", lambda *a, **k: (container, "default"))

    with_metrics = read_instance_metrics(object(), "c", "vm", "default", deadline=1e12)
    without = read_instance_metrics(object(), "c", "vm", "default", deadline=None)

    assert asked == ["fast"]
    assert (with_metrics.disk_io_reason, with_metrics.disk_io_storage_driver) == ("storage_driver", "zfs")
    assert without.disk_io_reason is None
