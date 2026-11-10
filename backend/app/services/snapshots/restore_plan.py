"""Whether a restore can run on the instance's storage, decided before anything is written.

ZFS rolls an instance back only to its newest snapshot. LXD fails the restore of an older one
("cannot be restored due to subsequent snapshot(s)") unless `zfs.remove_snapshots` deletes every
later snapshot. The safety copy a restore takes first is always later than the target, so on ZFS a
restore with a safety copy would be refused by the daemon after the copy was written. Upstream
documents the same restriction for Ceph, but only ZFS has been verified, so only ZFS is listed.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.lxd_path import safe_segment
from app.core.lxd_segment import lxd_segment

ROLLBACK_TO_NEWEST_ONLY = frozenset({"zfs"})

_MOMENT = re.compile(
    r"^(?P<base>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(?P<frac>\d+))?(?P<zone>Z|[+-]\d\d:\d\d)$"
)


@dataclass(frozen=True)
class RestorePlan:
    storage_driver: Optional[str]
    safety_snapshot_supported: bool
    restorable: bool
    newer_snapshots: List[str] = field(default_factory=list)
    reason: Optional[str] = None


def _moment(value: Any) -> Optional[datetime]:
    """LXD's RFC 3339 time, which carries nanoseconds that `fromisoformat` does not accept."""
    if not isinstance(value, str):
        return None
    match = _MOMENT.match(value.strip())
    if not match:
        return None
    frac = (match.group("frac") or "")[:6]
    zone = match.group("zone")
    text = match.group("base") + (f".{frac.ljust(6, '0')}" if frac else "") + ("+00:00" if zone == "Z" else zone)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def read_root_pool_driver(client: Any, instance_name: str, project: str) -> Optional[str]:
    """The storage driver behind the instance's root disk, or None when it cannot be named."""
    response = client.api.instances[lxd_segment(instance_name, what="Instance name")].get(
        params={"project": project}, timeout=(3, 15)
    )
    metadata = response.json().get("metadata") or {}
    devices = metadata.get("expanded_devices") or {}
    root = next(
        (
            device
            for device in devices.values()
            if isinstance(device, dict) and device.get("type") == "disk" and device.get("path") == "/"
        ),
        None,
    )
    pool = (root or {}).get("pool")
    if not pool:
        return None
    pool_response = client.api.storage_pools[safe_segment(pool)].get(timeout=(3, 15))
    driver = (pool_response.json().get("metadata") or {}).get("driver")
    return str(driver) if driver else None


def plan_restore(
    storage_driver: Optional[str],
    snapshots: List[Dict[str, Any]],
    target: str,
    *,
    name_driver: bool = True,
) -> RestorePlan:
    """Decide from the driver and the instance's snapshots, in LXD's listing order.

    `name_driver` False is a Tenant's plan: the same decision, with the driver withheld and the
    reason worded without it, because the pool's driver describes the provider's storage.
    """
    shown_driver = storage_driver if name_driver else None
    storage = "This instance is on ZFS storage, which" if name_driver else "This instance's storage"
    if storage_driver not in ROLLBACK_TO_NEWEST_ONLY:
        return RestorePlan(storage_driver=shown_driver, safety_snapshot_supported=True, restorable=True)

    names = [str(row.get("name")) for row in snapshots]
    position = names.index(target) if target in names else -1
    target_at = _moment(snapshots[position].get("created_at")) if position >= 0 else None
    newer: List[str] = []
    for index, row in enumerate(snapshots):
        if index == position:
            continue
        row_at = _moment(row.get("created_at"))
        # By time when both are known, by listing order otherwise. LXD lists an instance's
        # snapshots oldest first, which is also the order ZFS counts "subsequent" in.
        later = row_at > target_at if (row_at and target_at) else index > position
        if later:
            newer.append(str(row.get("name")))

    if newer:
        listed = ", ".join(newer[:5]) + (f" and {len(newer) - 5} more" if len(newer) > 5 else "")
        reason = (
            f"{storage} can only roll back to its newest snapshot. "
            f"{target} has {len(newer)} newer snapshot{'s' if len(newer) != 1 else ''} ({listed}). "
            f"Delete {'them' if len(newer) != 1 else 'it'} first to restore {target}."
        )
        return RestorePlan(
            storage_driver=shown_driver,
            safety_snapshot_supported=False,
            restorable=False,
            newer_snapshots=newer,
            reason=reason,
        )
    return RestorePlan(
        storage_driver=shown_driver,
        safety_snapshot_supported=False,
        restorable=True,
        reason=(
            f"{storage} can only roll back to its newest snapshot, so no safety snapshot can be "
            "kept through the restore."
        ),
    )


# LXD 5.21.5 and later re-check project policy on a snapshot restore (the CVE-2026-9640 fix) and
# refuse one that changes any `volatile.*` key, which most restores do, whenever the project sets
# limits. On 5.21.5 and 5.21.8 an instance's first restore passes and every later one is refused
# until the project's limits are cleared. The dashboard cannot change that, so the refusal is
# explained instead of passed on as a daemon failure.
_VOLATILE_REFUSAL = re.compile(r'Changing "volatile\.[^"]+" on \S+ ".+" in project ".+" is forbidden')


def project_limits_refusal(lxd_error: str, project: str) -> Optional[Dict[str, str]]:
    """The coded refusal for LXD's volatile-key restore check, or None for any other failure."""
    if not _VOLATILE_REFUSAL.search(lxd_error or ""):
        return None
    return {
        "error": "restore_refused_by_project_limits",
        "message": (
            f"LXD refused this restore because project {project} sets resource limits, and this "
            "LXD version refuses most restores in such a project. To restore, clear the "
            "project's limits, restore, then set them again."
        ),
        "lxd_error": lxd_error,
    }
