"""What a scheduled snapshot is called, in one place.

The name is how retention finds this schedule's snapshots and only this schedule's, and how the
daemon refuses a retried slot instead of taking a second copy.
"""

from datetime import datetime
from typing import Optional

from app.services.lxd.snapshots import OPS_PREFIX, build_operator_snapshot_name


def scheduled_snapshot_prefix(schedule_id: int) -> str:
    """The prefix of every snapshot this schedule made, and of no other snapshot.

    important: retention deletes by this prefix. The trailing dash keeps `ops-sch1` from matching
    schedule 12's `ops-sch12-nightly-...`, which would prune a sibling's restore points. Matching
    on the operator's label would be worse: label `db` prefixes `db-nightly`, and both prefix an
    operator's hand-taken `ops-db-...`, the snapshot the ownership gate protects.
    """
    return f"{OPS_PREFIX}sch{int(schedule_id)}-"


def build_scheduled_snapshot_name(
    schedule_id: int, label: str, moment: Optional[datetime] = None
) -> str:
    """The name for one slot of one schedule.

    important: `moment` is the due slot, never `now`. That gives at-most-once: a worker that died
    after LXD created the snapshot but before its row was updated retries the same slot, composes
    the same name, and the daemon refuses it as already existing.

    Composed through `build_operator_snapshot_name`, so the 63-character bound, the slug rules and
    the `ops-` prefix come from one implementation.
    """
    return build_operator_snapshot_name(f"sch{int(schedule_id)}-{label}", moment)
