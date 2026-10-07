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


def test_honest_memory_excludes_reclaimable_cache():
    vm = honest_memory(parse_instance_series(VM, project="tenant-a")["vm"])
    container = honest_memory(parse_instance_series(CONTAINER, project="tenant-a")["web"])

    assert vm == {
        "used": 4078280704 - 3014754304,
        "total": 4078280704,
        "available": 3014754304,
        "cache": 2190761984,
    }
    assert container["used"] == 8589934592 - 3771674624


def test_honest_memory_falls_back_when_available_does_not_exceed_free():
    series = InstanceSeries(mem_total=1000, mem_available=300, mem_free=300, mem_cached=200)

    assert honest_memory(series)["used"] == 500


def test_honest_memory_needs_a_total_and_available():
    assert honest_memory(InstanceSeries(mem_total=0, mem_available=1)) is None
    assert honest_memory(InstanceSeries(mem_total=10)) is None
