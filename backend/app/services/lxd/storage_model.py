"""Storage pool model: which pools a cluster can add up, and which it must count once.

A clustered host reports storage per member. Adding those up is wrong for a pool whose
backing store is shared, because every member reports the whole thing: a 400 GB Ceph pool
seen from three members would count as 1.2 TB.

Shared and local are told apart by driver only, as in `gpu_model.py`. The pool name cannot
do it (MicroCloud names the per-member ZFS pool `local` on every member), and neither can
`locations`: a storage pool is a cluster-wide object in LXD, so a ZFS pool defined on all
three members lists all three exactly as a Ceph pool does.

Unknown drivers are counted as local. If such a pool is in fact shared, that overstates
capacity, so every unknown driver is listed in `ClusterStorage.unknown_drivers`.
"""

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

# One backing store behind every member. Reading a pool on member A and again on member B
# returns the same bytes, so the cluster holds it once.
#
# `lvmcluster` is LVM over shared block storage, so it is shared despite the family name.
# `cephobject` is object storage and never carries a `space` block; listed for completeness.
SHARED_STORAGE_DRIVERS = frozenset({
    "ceph",         # RBD
    "cephfs",
    "cephobject",
    "lvmcluster",
    "powerflex",    # Dell PowerFlex, LXD 6.x
    "pure",         # Pure Storage, LXD 6.x
})

# Each member's own disks. The same pool name on two members is two different disks.
LOCAL_STORAGE_DRIVERS = frozenset({
    "dir",
    "btrfs",
    "lvm",
    "zfs",
})


@dataclass(frozen=True)
class PoolReading:
    """One pool's space as one member reported it."""

    member: str
    name: str
    driver: str
    space_total: int
    space_used: int

    @property
    def space_available(self) -> int:
        return self.space_total - self.space_used


@dataclass(frozen=True)
class ClusterStorage:
    """What a clustered host's pools add up to, both ways."""

    # Capacity that exists: a shared backing store counted once, local pools summed.
    total: int
    used: int
    # Every member's every pool added up, as a naive read reports it. Kept beside the real
    # figure, the way gpu_cards_total sits beside gpu_total.
    raw_total: int
    raw_used: int
    # One row per pool, shared pools collapsed, each carrying its scope and its members.
    pools: List[Dict[str, object]]
    # Drivers this module does not classify, counted as local and named so the gap shows.
    unknown_drivers: List[str]

    @property
    def available(self) -> int:
        return self.total - self.used


def is_shared_driver(driver: Optional[str]) -> bool:
    """Whether one backing store sits behind every member that reports this pool."""
    return (driver or "").strip().lower() in SHARED_STORAGE_DRIVERS


def classify_driver(driver: Optional[str]) -> str:
    """`shared`, `local`, or `unknown`. Unknown is counted as local (module docstring)."""
    normalised = (driver or "").strip().lower()
    if normalised in SHARED_STORAGE_DRIVERS:
        return "shared"
    if normalised in LOCAL_STORAGE_DRIVERS:
        return "local"
    return "unknown"


def summarise_cluster_storage(readings: Iterable[PoolReading]) -> ClusterStorage:
    """Fold per-member pool readings into what the cluster holds.

    A shared pool is identified by (name, driver) across members and contributes once. Members
    of a healthy cluster report the same store; when they disagree, one is mid-resync and sees
    less than is there. The largest total and the largest used are taken independently, so a
    stale member's usage figure cannot ride in on a fresh member's capacity.

    A local pool contributes once per member, and its row names the members it was read from.
    """
    shared: Dict[Tuple[str, str], PoolReading] = {}
    shared_used: Dict[Tuple[str, str], int] = {}
    shared_members: Dict[Tuple[str, str], List[str]] = {}
    local: Dict[Tuple[str, str], List[PoolReading]] = {}
    unknown: Set[str] = set()

    raw_total = raw_used = 0

    for reading in readings:
        raw_total += reading.space_total
        raw_used += reading.space_used
        kind = classify_driver(reading.driver)
        if kind == "unknown":
            unknown.add(reading.driver or "")
        key = (reading.name, (reading.driver or "").strip().lower())
        if kind == "shared":
            shared_members.setdefault(key, []).append(reading.member)
            best = shared.get(key)
            if best is None or reading.space_total > best.space_total:
                shared[key] = reading
            shared_used[key] = max(shared_used.get(key, 0), reading.space_used)
        else:
            local.setdefault(key, []).append(reading)

    pools: List[Dict[str, object]] = []
    total = used = 0

    for key, reading in shared.items():
        pool_used = min(shared_used[key], reading.space_total)
        total += reading.space_total
        used += pool_used
        pools.append({
            "name": reading.name,
            "driver": reading.driver,
            "scope": "shared",
            "members": sorted(shared_members[key]),
            "space_total": reading.space_total,
            "space_used": pool_used,
            "space_available": reading.space_total - pool_used,
        })

    for key, group in local.items():
        group_total = sum(r.space_total for r in group)
        group_used = sum(r.space_used for r in group)
        total += group_total
        used += group_used
        pools.append({
            "name": group[0].name,
            "driver": group[0].driver,
            "scope": "local",
            "members": sorted(r.member for r in group),
            "space_total": group_total,
            "space_used": group_used,
            "space_available": group_total - group_used,
        })

    pools.sort(key=lambda p: (str(p["scope"]), str(p["name"])))

    return ClusterStorage(
        total=total,
        used=used,
        raw_total=raw_total,
        raw_used=raw_used,
        pools=pools,
        unknown_drivers=sorted(d for d in unknown if d),
    )
