"""Host resources panel: a host scope and a container scope.

The legacy /system/resource-usage keys keep their shape: memory.total is the cgroup memory
limit, cpu.count_logical the cpu quota, and process the worker that answered. `host` adds the
kernel's view, and `container` gains the cgroup scope with its limits, the docker stats memory
formula, a cpu percent of the quota and the worker set.

Hermetic: a fake cgroup tree under tmp_path, a fake psutil module backed by a dict process
tree, a monkeypatched monotonic clock, the real router with get_current_user overridden.
"""

import contextlib
import platform
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import cgroup
from app.services import system_resources as sr

HOST_TOTAL = 12512849920
LIMIT = 4294967296
CURRENT = 2149875712
ANON = 2069168128
FILE = 30281728
INACTIVE_FILE = 24000000
USAGE_USEC = 123456789
ME = 21
DISK_TOTAL = 132011507712
BOOT_EPOCH = 1_700_000_000.0


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _point_platform_files(tmp_path: Path, monkeypatch, root: Path, proc_cgroup: str) -> None:
    monkeypatch.setattr(cgroup, "CGROUP_ROOT", root)
    monkeypatch.setattr(cgroup, "PROC_SELF_CGROUP", _write(tmp_path / "proc/self/cgroup", proc_cgroup))
    monkeypatch.setattr(cgroup, "PROC_1_CGROUP", _write(tmp_path / "proc/1/cgroup", proc_cgroup))
    monkeypatch.setattr(cgroup, "DOCKERENV", _write(tmp_path / ".dockerenv", ""))
    monkeypatch.setattr(cgroup, "DMI_SYS_VENDOR", _write(tmp_path / "sys/class/dmi/id/sys_vendor", "QEMU\n"))
    monkeypatch.setattr(
        cgroup, "PROC_CPUINFO",
        _write(tmp_path / "proc/cpuinfo", "processor\t: 0\nvendor_id\t: GenuineIntel\nflags\t\t: fpu vme hypervisor lahf_lm\n"),
    )
    monkeypatch.delenv("container", raising=False)


def _write_cpu_stat_v2(root: Path, usage_usec: int) -> None:
    _write(
        root / "cpu.stat",
        f"usage_usec {usage_usec}\nuser_usec 1\nsystem_usec 1\nnr_periods 100\nnr_throttled 1535\nthrottled_usec 999\n",
    )


@pytest.fixture
def fake_cgroup(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "sys/fs/cgroup"
    _write(root / "cgroup.controllers", "cpuset cpu io memory pids\n")
    _write(root / "memory.max", f"{LIMIT}\n")
    _write(root / "memory.current", f"{CURRENT}\n")
    _write(root / "memory.stat", f"anon {ANON}\nfile {FILE}\nkernel 1\ninactive_file {INACTIVE_FILE}\nactive_file 1\n")
    _write(root / "cpu.max", "200000 100000\n")
    _write_cpu_stat_v2(root, USAGE_USEC)
    _point_platform_files(tmp_path, monkeypatch, root, "0::/\n")
    return root


@pytest.fixture
def fake_cgroup_v1(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "sys/fs/cgroup"
    _write(root / "memory/memory.limit_in_bytes", f"{LIMIT}\n")
    _write(root / "memory/memory.usage_in_bytes", f"{CURRENT}\n")
    _write(
        root / "memory/memory.stat",
        f"cache {FILE}\nrss {ANON}\ntotal_rss {ANON}\ntotal_cache {FILE}\ntotal_inactive_file {INACTIVE_FILE}\n",
    )
    _write(root / "cpu/cpu.cfs_quota_us", "200000\n")
    _write(root / "cpu/cpu.cfs_period_us", "100000\n")
    _write(root / "cpuacct/cpuacct.usage", f"{USAGE_USEC * 1000}\n")
    _write(root / "cpu/cpu.stat", "nr_periods 100\nnr_throttled 1535\nthrottled_time 999000\n")
    _point_platform_files(tmp_path, monkeypatch, root, "4:memory:/\n3:cpu,cpuacct:/\n")
    return root


# --- fake psutil ------------------------------------------------------------------------

class _AccessDenied(Exception):
    pass


class _NoSuchProcess(Exception):
    pass


def _process_tree() -> Dict[int, Dict[str, Any]]:
    python = "/usr/local/bin/python"
    tree: Dict[int, Dict[str, Any]] = {
        1: {"exe": python, "cmdline": ["python", "-m", "uvicorn", "app.main:app"], "parent": None,
            "rss": 40_000_000, "create_time": BOOT_EPOCH + 10},
        5: {"exe": "/bin/bash", "cmdline": ["bash"], "parent": None, "rss": 3_000_000, "create_time": BOOT_EPOCH + 5},
        19: {"exe": python, "cmdline": ["python", "-c", "from multiprocessing.resource_tracker import main;main(5)"],
             "parent": 1, "rss": 9_000_000, "create_time": BOOT_EPOCH + 11},
    }
    for pid in (20, 21, 22, 23):
        tree[pid] = {"exe": python, "cmdline": ["python", "-c", "from multiprocessing.spawn import spawn_main; spawn_main(9)"],
                     "parent": 1, "rss": 100_000_000 + pid, "create_time": BOOT_EPOCH + 12}
    return tree


class _FakeProcess:
    def __init__(self, tree: Dict[int, Dict[str, Any]], pid: Optional[int] = None):
        self._tree = tree
        self.pid = ME if pid is None else pid

    @property
    def _row(self) -> Dict[str, Any]:
        try:
            return self._tree[self.pid]
        except KeyError:
            raise _NoSuchProcess(self.pid)

    def exe(self) -> str:
        return self._row["exe"]

    def name(self) -> str:
        return Path(self._row["exe"]).name

    def cmdline(self) -> List[str]:
        return list(self._row["cmdline"])

    def parent(self) -> Optional["_FakeProcess"]:
        parent = self._row["parent"]
        return None if parent is None else _FakeProcess(self._tree, parent)

    def children(self) -> List["_FakeProcess"]:
        return [_FakeProcess(self._tree, pid) for pid, row in self._tree.items() if row["parent"] == self.pid]

    def memory_info(self) -> SimpleNamespace:
        return SimpleNamespace(rss=self._row["rss"], vms=self._row["rss"] * 2)

    def create_time(self) -> float:
        return self._row["create_time"]

    @contextlib.contextmanager
    def oneshot(self):
        yield

    def cpu_percent(self, interval: Optional[float] = None) -> float:
        if interval:
            raise AssertionError("Process.cpu_percent must never be asked to sleep")
        return 1.5

    def memory_percent(self) -> float:
        return 0.5

    def num_threads(self) -> int:
        return 4

    def open_files(self) -> list:
        return []

    def net_connections(self) -> list:
        return []

    def io_counters(self) -> SimpleNamespace:
        return SimpleNamespace(read_bytes=1, write_bytes=2)


@pytest.fixture
def fake_psutil(monkeypatch) -> SimpleNamespace:
    state: Dict[str, Any] = {
        "times": {"user": 100.0, "nice": 0.0, "system": 50.0, "idle": 800.0, "iowait": 10.0,
                  "irq": 0.0, "softirq": 0.0, "steal": 0.0},
        "cpu_percent_calls": [],
        "tree": _process_tree(),
    }
    tree = state["tree"]
    sda1 = [("/app/config", "ext4"), ("/app/uploads", "ext4"), ("/etc/resolv.conf", "ext4"),
            ("/etc/hostname", "ext4"), ("/etc/hosts", "ext4")]

    def cpu_percent(interval: Optional[float] = None, percpu: bool = False) -> float:
        state["cpu_percent_calls"].append(interval)
        if interval:
            raise AssertionError("psutil.cpu_percent must never be asked to sleep")
        return 0.0

    fake = SimpleNamespace(
        state=state,
        AccessDenied=_AccessDenied,
        NoSuchProcess=_NoSuchProcess,
        Process=_make_process_class(tree),
        virtual_memory=lambda: SimpleNamespace(total=HOST_TOTAL, used=7809937408, available=4702912512, percent=62.4),
        swap_memory=lambda: SimpleNamespace(total=0, used=0, percent=0.0),
        cpu_count=lambda logical=True: 6,
        cpu_times=lambda: SimpleNamespace(**state["times"]),
        cpu_percent=cpu_percent,
        cpu_freq=lambda: SimpleNamespace(current=2495.31, max=0.0),
        getloadavg=lambda: (0.8, 1.2, 0.99),
        boot_time=lambda: BOOT_EPOCH,
        disk_partitions=lambda all=False: [
            SimpleNamespace(device="/dev/sda1", mountpoint=mountpoint, fstype=fstype) for mountpoint, fstype in sda1
        ],
        disk_usage=lambda mountpoint: SimpleNamespace(total=DISK_TOTAL, used=105874051072, free=26120679424, percent=80.2),
        net_io_counters=lambda pernic=False: {
            "eth0": SimpleNamespace(bytes_sent=10, bytes_recv=20, packets_sent=1, packets_recv=2),
        },
        net_if_addrs=lambda: {"eth0": [SimpleNamespace(family=SimpleNamespace(name="AF_INET"), address="172.18.0.5")]},
        net_if_stats=lambda: {"eth0": SimpleNamespace(isup=True, speed=10000)},
    )
    monkeypatch.setattr(sr, "psutil", fake)
    sr._HOST_CPU.reset()
    sr._CONTAINER_CPU.reset()
    monkeypatch.setattr(sr, "_SELF_PROCESS", None)
    yield fake
    sr._HOST_CPU.reset()
    sr._CONTAINER_CPU.reset()


def _make_process_class(tree: Dict[int, Dict[str, Any]]):
    # _self_process() checks isinstance against psutil.Process, so the fake needs a class.
    class Process(_FakeProcess):
        def __init__(self, pid: Optional[int] = None):
            super().__init__(tree, pid)
    return Process


def _collect():
    return sr.collect_resource_usage()


# --- memory ---------------------------------------------------------------------------------

def test_host_memory_is_the_kernel_total_and_the_container_limit_is_the_cap(fake_cgroup, fake_psutil):
    out = _collect()
    assert out.host.memory.total == HOST_TOTAL
    assert out.container.memory.limit == LIMIT
    assert out.container.memory_limit == LIMIT
    assert out.memory.total == LIMIT, "legacy memory.total keeps reporting the cgroup limit"


def test_container_used_excludes_reclaimable_page_cache(fake_cgroup, fake_psutil):
    out = _collect()
    memory = out.container.memory
    assert memory.current == CURRENT
    assert memory.anon == ANON
    assert memory.file == FILE
    assert memory.used == CURRENT - INACTIVE_FILE
    assert memory.percent == round((CURRENT - INACTIVE_FILE) / LIMIT * 100, 1)
    assert out.memory.used == CURRENT, "legacy memory.used keeps the raw memory.current"


# --- cpu ------------------------------------------------------------------------------------

def test_host_cpu_count_is_not_clamped_while_the_legacy_count_is(fake_cgroup, fake_psutil):
    out = _collect()
    assert out.host.cpu.count_logical == 6
    assert out.host.cpu.count_physical == 6
    assert out.cpu.count_logical == 2
    assert out.cpu.count_physical == 2
    assert out.container.cpu.limit == 2.0
    assert out.container.cpu_limit == 2.0


def test_container_cpu_percent_is_the_delta_between_two_cpu_stat_readings(fake_cgroup, fake_psutil, monkeypatch):
    sr._CONTAINER_CPU.reset()
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    first = _collect().container.cpu
    assert first.usage_usec == USAGE_USEC
    assert first.percent is None and first.window_seconds is None

    _write_cpu_stat_v2(fake_cgroup, USAGE_USEC + 1_000_000)
    monkeypatch.setattr(time, "monotonic", lambda: 105.0)
    second = _collect().container.cpu
    assert second.usage_usec == USAGE_USEC + 1_000_000
    assert second.percent == 10.0, "1 cpu-second of a 2 cpu quota over 5 s"
    assert second.window_seconds == 5.0
    assert second.nr_throttled == 1535
    assert second.throttled_usec == 999


def test_a_poll_inside_the_counter_resolution_repeats_the_last_window_not_zero():
    sampler = sr.DeltaSampler()
    assert sampler.sample(busy=10.0, total=100.0) is None
    first = sampler.sample(busy=15.0, total=110.0)
    assert first is not None and first[0] == 50.0
    # The same counters again: nothing moved, so the last answer stands and the baseline stays.
    assert sampler.sample(busy=15.0, total=110.0) == first
    later = sampler.sample(busy=25.0, total=120.0)
    assert later is not None and later[0] == 100.0


def test_host_cpu_percent_never_sleeps(fake_cgroup, fake_psutil):
    sr._HOST_CPU.reset()
    first = _collect()
    assert first.host.cpu.percent == 0.0, "no baseline yet, and no sleep to make one"

    times = fake_psutil.state["times"]
    times["user"] += 30.0
    times["idle"] += 70.0
    second = _collect()
    assert second.host.cpu.percent == 30.0
    assert second.cpu.percent == 30.0, "legacy cpu.percent comes from the same sampler"
    assert fake_psutil.state["cpu_percent_calls"] == [], "psutil.cpu_percent is never called"


def test_host_cpu_counters_go_out_so_a_client_can_measure_its_own_window(fake_cgroup, fake_psutil, monkeypatch):
    # The sampler is per worker: with four workers a poll can land on one that last answered
    # minutes ago and report a percent over that whole gap. The raw counters are machine-wide,
    # so the delta between any two answers is the busy share over the client's own window.
    sr._HOST_CPU.reset()
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    first = _collect().host
    assert first.cpu_times.busy_seconds == 150.0, "user + system"
    assert first.cpu_times.total_seconds == 960.0, "busy + idle + iowait"
    assert first.cpu_window_seconds is None, "no previous sample, so no window"

    times = fake_psutil.state["times"]
    times["user"] += 30.0
    times["idle"] += 70.0
    monkeypatch.setattr(time, "monotonic", lambda: 107.0)
    second = _collect().host
    busy = second.cpu_times.busy_seconds - first.cpu_times.busy_seconds
    total = second.cpu_times.total_seconds - first.cpu_times.total_seconds
    assert (busy, total) == (30.0, 100.0)
    assert round(busy / total * 100, 1) == second.cpu.percent, "same formula as the server's own percent"
    assert second.cpu_window_seconds == 7.0, "the window the server's percent covers is stated"


# --- filesystems ----------------------------------------------------------------------------

def test_the_disk_row_is_the_host_filesystem_behind_the_bind_mounts(fake_cgroup, fake_psutil):
    out = _collect()
    assert len(out.host.filesystems) == 1
    row = out.host.filesystems[0]
    assert row.device == "/dev/sda1"
    assert row.fstype == "ext4"
    assert row.mountpoints == ["/app/config", "/app/uploads"]
    assert row.total == DISK_TOTAL
    assert "/etc/hosts" not in row.mountpoints
    assert [disk.mountpoint for disk in out.disks] == ["/app/config"], "legacy dedupe keeps one row"


# --- workers --------------------------------------------------------------------------------

def test_workers_are_the_siblings_not_every_child_of_pid_1(fake_cgroup, fake_psutil, monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    out = _collect()
    workers = out.container.workers
    assert workers.count == 4
    assert workers.expected == 4
    assert [worker.pid for worker in workers.workers] == [20, 21, 22, 23]
    assert workers.answering_pid == ME
    assert workers.master_pid == 1
    assert workers.master_uptime_seconds is not None and workers.master_uptime_seconds > 0
    assert workers.total_rss == sum(100_000_000 + pid for pid in (20, 21, 22, 23))
    assert out.process.pid == ME


def test_a_single_process_uvicorn_reports_itself(fake_cgroup, fake_psutil, monkeypatch):
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    fake_psutil.state["tree"][ME]["parent"] = 5
    out = _collect()
    workers = out.container.workers
    assert workers.count == 1
    assert workers.expected is None
    assert workers.master_pid is None
    assert workers.master_uptime_seconds is None
    assert [worker.pid for worker in workers.workers] == [ME]
    assert workers.total_rss == 100_000_000 + ME


# --- resilience and cgroup v1 ---------------------------------------------------------------

def test_a_missing_cgroup_file_is_a_null_not_a_500(fake_cgroup, fake_psutil):
    (fake_cgroup / "memory.stat").unlink()
    (fake_cgroup / "cpu.stat").unlink()
    out = _collect()
    memory = out.container.memory
    assert memory.limit == LIMIT and memory.current == CURRENT
    assert memory.anon is None and memory.used is None and memory.percent is None
    cpu = out.container.cpu
    assert cpu.limit == 2.0
    assert cpu.usage_usec is None and cpu.percent is None and cpu.nr_throttled is None
    assert out.memory.total == LIMIT


def test_an_unlimited_container_in_the_host_cgroup_namespace_reads_its_own_cgroup(tmp_path, monkeypatch, fake_psutil):
    # cgroupns=host: the root is the whole machine, the process lives under /docker/<id>, and
    # no cpu.max or memory.max is set anywhere. The fallback must land on the container's own
    # cgroup instead of the root.
    root = tmp_path / "sys/fs/cgroup"
    own = root / "docker" / "abc123"
    _write(root / "cgroup.controllers", "cpuset cpu io memory pids\n")
    _write(root / "memory.current", "999999999\n")
    _write_cpu_stat_v2(root, 10 * USAGE_USEC)
    _write(own / "memory.max", "max\n")
    _write(own / "memory.current", f"{CURRENT}\n")
    _write(own / "memory.stat", f"anon {ANON}\nfile {FILE}\ninactive_file {INACTIVE_FILE}\n")
    _write(own / "cpu.max", "max 100000\n")
    _write_cpu_stat_v2(own, USAGE_USEC)
    _point_platform_files(tmp_path, monkeypatch, root, "0::/docker/abc123\n")

    assert cgroup.select_memory_scope().path == own
    assert cgroup.select_cpu_scope().path == own
    out = _collect()
    assert out.container.memory.limit is None and out.container.memory.current == CURRENT
    assert out.container.cpu.limit is None and out.container.cpu.usage_usec == USAGE_USEC


def test_cgroup_v1_reads_the_v1_files(fake_cgroup_v1, fake_psutil):
    out = _collect()
    assert out.container.cgroup_version == "v1"
    assert out.container.memory.limit == LIMIT
    assert out.container.memory.current == CURRENT
    assert out.container.memory.anon == ANON
    assert out.container.memory.used == CURRENT - INACTIVE_FILE
    assert out.container.cpu.limit == 2.0
    assert out.container.cpu.usage_usec == USAGE_USEC
    assert out.container.cpu.nr_throttled == 1535
    assert out.container.cpu.throttled_usec == 999
    assert out.memory.total == LIMIT and out.cpu.count_logical == 2


def test_network_scope_is_container_when_containerised(fake_cgroup, fake_psutil, monkeypatch, tmp_path):
    out = _collect()
    assert out.network_scope == "container"
    assert out.container.is_containerized is True
    assert out.container.container_id == platform.node()
    assert out.host.is_virtual_machine is True
    assert out.host.platform_vendor == "QEMU"

    monkeypatch.setattr(cgroup, "DOCKERENV", tmp_path / "missing")
    monkeypatch.setattr(cgroup, "PROC_1_CGROUP", _write(tmp_path / "proc/1/cgroup", "0::/init.scope\n"))
    bare = _collect()
    assert bare.network_scope == "host"
    assert bare.container.is_containerized is False
    assert bare.container.container_id is None


# --- the legacy contract --------------------------------------------------------------------

LEGACY_TOP_LEVEL = ["timestamp", "container", "cpu", "memory", "disks", "network", "process", "python", "os"]
LEGACY_NESTED = {
    "container": ["is_containerized", "runtime", "cgroup_version", "memory_limit", "cpu_limit"],
    "cpu": ["percent", "count_logical", "count_physical", "frequency_current", "frequency_max",
            "load_avg_1m", "load_avg_5m", "load_avg_15m"],
    "memory": ["total", "available", "used", "percent", "swap_total", "swap_used", "swap_percent"],
    "process": ["pid", "cpu_percent", "memory_percent", "memory_rss", "memory_vms", "threads", "open_files",
                "connections", "create_time", "uptime_seconds", "io_read_bytes", "io_write_bytes"],
    "python": ["version", "implementation", "compiler"],
    "os": ["system", "release", "version", "machine", "hostname", "boot_time", "uptime_seconds"],
}
LEGACY_DISK = ["device", "mountpoint", "fstype", "total", "used", "free", "percent"]
LEGACY_NIC = ["name", "bytes_sent", "bytes_recv", "packets_sent", "packets_recv", "is_up", "speed", "addresses"]


def test_the_legacy_keys_keep_their_shape(fake_cgroup, fake_psutil):
    body = _collect().model_dump(mode="json")
    for key in LEGACY_TOP_LEVEL:
        assert key in body, key
    for block, keys in LEGACY_NESTED.items():
        missing = [key for key in keys if key not in body[block]]
        assert not missing, f"{block} lost {missing}"
    assert body["disks"] and all(key in body["disks"][0] for key in LEGACY_DISK)
    assert body["network"] and all(key in body["network"][0] for key in LEGACY_NIC)
    assert isinstance(body["cpu"]["percent"], float)
    assert isinstance(body["memory"]["total"], int)
    assert body["timestamp"].endswith("+00:00")
    assert set(body) - set(LEGACY_TOP_LEVEL) == {"host", "network_scope"}, "only additions"
    assert {"cpu_times", "cpu_window_seconds"} <= set(body["host"]), "the host block carries the raw cpu counters"


# --- role gate ------------------------------------------------------------------------------

def _client(groups: List[str], user_id: str) -> TestClient:
    from app.core.auth import AuthenticatedUser
    from app.core.rbac import get_current_user
    from app.routers.system import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id=user_id, email=f"{user_id}@example.test", groups=groups,
    )
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("groups,expected", [
    (["role_tenant"], 403),
    (["role_partner"], 200),
    (["role_admin"], 200),
])
def test_tenant_is_refused_partner_and_admin_are_served(fake_cgroup, fake_psutil, groups, expected):
    response = _client(groups, f"scoped-{groups[0]}").get("/system/resource-usage")
    assert response.status_code == expected, response.text[:300]
    if expected == 200:
        body = response.json()
        assert body["host"]["memory"]["total"] == HOST_TOTAL
        assert body["container"]["memory"]["limit"] == LIMIT
        assert body["network_scope"] == "container"
