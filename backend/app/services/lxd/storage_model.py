"""Storage pool model: which pools a cluster can add up, and which it must count once.

A clustered host reports storage per member. Adding those up is wrong for any pool whose
backing store is shared, because every member reports the whole thing: a 400 GB Ceph pool seen
from three members is 1.2 TB of capacity that does not exist. Measured against the three-member
QA fixture, a naive sum reports 1450 GB where the cluster holds 1050 GB.

Two ways to tell shared from local were tried and rejected before this one.

The pool NAME cannot do it. MicroCloud names the per-member ZFS pool `local` on every member,
so name-equality marks three separate disks as one.

`locations` cannot do it either. A storage pool is a cluster-wide object in LXD, so a ZFS pool
defined on all three members lists all three in `locations` exactly as a Ceph pool does. The
field says where the pool is defined, not where its bytes live.

What is left is the driver, which is also what `gpu_model.py` settled on for the same kind of
question, and its comment is worth repeating here: classify by driver, and only by driver.

important: unknown drivers are treated as LOCAL. That is the direction that fails safe for an
operator making a capacity decision: a shared pool misread as local overstates what they have,
while a local pool misread as shared understates it. Understating loses a sale; overstating
loses an instance at 3am. A driver this does not know is named in the response so the gap is
visible rather than silently absorbed.
"""

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

# One backing store behind every member. Reading a pool on member A and again on member B
# returns the same bytes, so the cluster holds it once.
#
# `lvmcluster` is LVM over shared block storage, which is shared despite the family name.
# `cephobject` is object storage rather than instance storage and never carries a `space`
# block, but it is listed so a future reader does not have to wonder whether it was forgotten.
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
    # Every member's every pool added up, which is what a naive read reports. Kept so no
    # consumer loses a figure it had, the way gpu_cards_total sits beside gpu_total.
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
    """`shared`, `local`, or `unknown`. Unknown is counted as local; see the module note."""
    normalised = (driver or "").strip().lower()
    if normalised in SHARED_STORAGE_DRIVERS:
        return "shared"
    if normalised in LOCAL_STORAGE_DRIVERS:
        return "local"
    return "unknown"


def summarise_cluster_storage(readings: Iterable[PoolReading]) -> ClusterStorage:
    """Fold per-member pool readings into what the cluster actually holds.

    A shared pool is identified by (name, driver) across members and contributes once. Members
    of a healthy cluster report the same store, so any of them would do; when they disagree one
    of them is mid-resync and sees less than is there. The largest total and the largest used
    are therefore taken INDEPENDENTLY, because both are the more complete view of the same
    store, and pinning both to whichever member happened to win on size would let a stale
    member's usage figure ride in on a fresh member's capacity.

    A local pool contributes once per member, and its row names the members it was read from,
    because "local 600 GB" with no member list is three numbers pretending to be one.
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
