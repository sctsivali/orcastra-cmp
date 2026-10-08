"""Whether a container's disk I/O can be measured, decided by its root pool's storage driver.

important: a container's disk counters come from its cgroup, and the cgroup sees only the I/O
issued on the container's behalf. A driver that writes from its own kernel threads never charges
the container: on ZFS a container writing 40 MB a second read 0. A measured zero is worse than no
number, so a driver joins this set only after a live probe shows the counters following real
writes and direct reads, and the product's rate agreeing with the workload.

note: empty for now. ZFS does not charge the cgroup. The lab's one btrfs pool was unavailable on
2026-10-08 (LXD refused to create a volume on it), so the probe could not run there, and Ceph is
untested by decision.
"""

from typing import FrozenSet, Optional

CONTAINER_DISK_IO_DRIVERS: FrozenSet[str] = frozenset()


def container_disk_io_measured(driver: Optional[str]) -> bool:
    return driver in CONTAINER_DISK_IO_DRIVERS
