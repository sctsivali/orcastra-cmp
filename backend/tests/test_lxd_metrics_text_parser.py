"""Reading /1.0/metrics: the families, labels and devices the instance dialog depends on.

The fixtures follow the shape a 5.21 daemon serves: a container's disk series name host block
devices (the pool's dm device and the disk under it, plus loop devices), a virtual machine's
name guest devices (the disk, its partitions and snap loop mounts).
"""

from app.services.lxd.metrics_text import InstanceSeries, honest_memory, parse_instance_series

CONTAINER = """
# HELP lxd_cpu_effective_total The total number of effective CPUs.
# TYPE lxd_cpu_effective_total gauge
lxd_cpu_effective_total{name="web",project="tenant-a",type="container"} 4
lxd_cpu_seconds_total{cpu="0",mode="user",name="web",project="tenant-a",type="container"} 120.5
lxd_disk_read_bytes_total{device="dm-0",name="web",project="tenant-a",type="container"} 5000
lxd_disk_read_bytes_total{device="nvme0n1",name="web",project="tenant-a",type="container"} 7000
lxd_disk_read_bytes_total{device="loop3",name="web",project="tenant-a",type="container"} 99
lxd_disk_written_bytes_total{device="nvme0n1",name="web",project="tenant-a",type="container"} 3000
lxd_memory_MemTotal_bytes{name="web",project="tenant-a",type="container"} 8589934592
lxd_memory_MemAvailable_bytes{name="web",project="tenant-a",type="container"} 3771674624
lxd_memory_MemFree_bytes{name="web",project="tenant-a",type="container"} 2579918848
lxd_memory_Cached_bytes{name="web",project="tenant-a",type="container"} 1191755776
lxd_memory_Shmem_bytes{name="web",project="tenant-a",type="container"} 209715200
lxd_go_goroutines 152
lxd_uptime_seconds 99999.1
"""

VM = """
lxd_cpu_effective_total{name="vm",project="tenant-a",type="virtual-machine"} 0
lxd_disk_read_bytes_total{device="sda",name="vm",project="tenant-a",type="virtual-machine"} 10000
lxd_disk_read_bytes_total{device="sda1",name="vm",project="tenant-a",type="virtual-machine"} 9000
lxd_disk_read_bytes_total{device="loop0",name="vm",project="tenant-a",type="virtual-machine"} 400
lxd_disk_written_bytes_total{device="sda",name="vm",project="tenant-a",type="virtual-machine"} 2500
lxd_memory_MemTotal_bytes{name="vm",project="tenant-a",type="virtual-machine"} 4078280704
lxd_memory_MemAvailable_bytes{name="vm",project="tenant-a",type="virtual-machine"} 3014754304
lxd_memory_MemFree_bytes{name="vm",project="tenant-a",type="virtual-machine"} 857030656
lxd_memory_Cached_bytes{name="vm",project="tenant-a",type="virtual-machine"} 2190761984
lxd_memory_Shmem_bytes{name="vm",project="tenant-a",type="virtual-machine"} 20131840
"""


def test_container_series_sum_whole_disks_only():
    series = parse_instance_series(CONTAINER, project="tenant-a")["web"]

    assert series.instance_type == "container"
    assert series.cpu_effective == 4
    assert series.disk_read == 7000, "dm-0 sits on nvme0n1 and loop3 is not a disk"
    assert series.disk_written == 3000
    assert series.disk_devices == ("nvme0n1",)


def test_vm_series_skip_partitions_and_snap_loops():
    series = parse_instance_series(VM, project="tenant-a")["vm"]

    assert series.instance_type == "virtual-machine"
    assert series.cpu_effective == 0
    assert series.disk_read == 10000
    assert series.disk_written == 2500
    assert series.disk_devices == ("sda",)


def test_other_projects_and_unlabelled_families_are_ignored():
    text = CONTAINER + 'lxd_memory_MemTotal_bytes{name="web",project="tenant-b",type="container"} 1\n'

    parsed = parse_instance_series(text, project="tenant-b")

    assert set(parsed) == {"web"}
    assert parsed["web"].mem_total == 1
    assert parsed["web"].disk_read is None


def test_label_order_escapes_timestamps_and_the_incus_prefix():
    text = (
        'incus_memory_MemTotal_bytes{type="container",project="p",name="a\\"b"} 42 1700000000000\n'
        'lxd_memory_MemFree_bytes{project="p",name="a\\"b"} 7\n'
    )

    parsed = parse_instance_series(text, project="p")

    assert parsed['a"b'].mem_total == 42
    assert parsed['a"b'].mem_free == 7


def test_malformed_and_non_finite_lines_are_skipped_not_fatal():
    text = (
        'lxd_memory_MemTotal_bytes{name="a",project="p"} notanumber\n'
        'lxd_memory_MemFree_bytes{name="a",project="p"} NaN\n'
        'lxd_memory_Cached_bytes{name="a",project="p"} +Inf\n'
        'lxd_memory_MemAvailable_bytes{name="a",project="p"} 5\r\n'
        "lxd_memory_MemTotal_bytes{broken\n"
    )

    parsed = parse_instance_series(text, project="p")

    assert parsed["a"].mem_available == 5
    assert parsed["a"].mem_total is None
    assert parsed["a"].mem_free is None


def test_series_round_trip_through_the_cache_shape():
    series = parse_instance_series(CONTAINER, project="tenant-a")["web"]

    assert InstanceSeries.from_json(series.to_json()) == series


def test_vm_memory_is_the_guest_kernels_own_reading():
    vm = honest_memory(parse_instance_series(VM, project="tenant-a")["vm"])

    assert vm == {
        "used": 4078280704 - 3014754304,
        "total": 4078280704,
        "available": 3014754304,
        "cache": 2190761984 - 20131840,
    }


def test_container_shared_memory_counts_as_used_not_available():
    """LXD 5.21 sets a container's MemAvailable to MemFree plus every file page, tmpfs included."""
    container = honest_memory(parse_instance_series(CONTAINER, project="tenant-a")["web"])

    usage = 8589934592 - 2579918848
    reclaimable = 1191755776 - 209715200
    assert container["used"] == usage - reclaimable
    assert container["cache"] == reclaimable
    assert container["available"] == 8589934592 - container["used"]


def test_a_container_holding_only_shmem_is_not_read_as_idle():
    """The live case: 200 MiB in /dev/shm of a 1 GiB container read as 2 MiB used."""
    mib = 1024 * 1024
    series = InstanceSeries(
        instance_type="container",
        mem_total=1024 * mib,
        mem_free=819 * mib,
        mem_available=1022 * mib,
        mem_cached=203 * mib,
        mem_shmem=200 * mib,
    )

    assert honest_memory(series)["used"] == 202 * mib


def test_honest_memory_needs_a_total_and_its_inputs():
    assert honest_memory(InstanceSeries(mem_total=0, mem_available=1)) is None
    assert honest_memory(InstanceSeries(mem_total=10)) is None
    assert honest_memory(InstanceSeries(instance_type="container", mem_total=10, mem_available=5)) is None


def test_a_hostile_line_parses_in_linear_time():
    """A run of name characters with no `=\"` made the old label regex quadratic."""
    import time

    body = "lxd_cpu_effective_total{" + "a" * 1_000_000 + "} 1\n"
    body += 'lxd_memory_MemTotal_bytes{name="ok",project="p"} 5\n'
    started = time.monotonic()

    parsed = parse_instance_series(body, project="p")

    assert time.monotonic() - started < 1.0
    assert parsed["ok"].mem_total == 5


def test_long_labels_with_many_pairs_still_parse():
    labels = ",".join(f'k{n}="v{n}"' for n in range(100)) + ',name="a",project="p"'
    parsed = parse_instance_series(f"lxd_memory_MemFree_bytes{{{labels}}} 3\n", project="p")

    assert parsed["a"].mem_free == 3
