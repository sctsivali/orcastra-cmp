"""What a scheduled snapshot is called, in one place.

Split from the snapshot service rather than added to it: that module is already past this project's
500-line rule, and this vocabulary belongs with the scheduler that is its only consumer.

The name is the load-bearing part of the whole design, in two directions. It is how retention finds
this schedule's snapshots and only this schedule's, and it is how a retried slot is refused by the
daemon instead of taking a second copy.
"""

from datetime import datetime
from typing import Optional

from app.services.lxd.snapshots import OPS_PREFIX, build_operator_snapshot_name


def scheduled_snapshot_prefix(schedule_id: int) -> str:
    """What every snapshot this schedule made is called, and nothing else is.

    important: retention deletes by this prefix, so it has to be unambiguous in both directions,
    and the trailing dash is what makes it so. Without it `ops-sch1-` also matches schedule 12's
    `ops-sch12-nightly-...`, and a schedule would prune a sibling's restore points.

    Matching on the operator's LABEL instead would be worse again: label `db` prefixes label
    `db-nightly`, and both prefix an operator's hand-taken `ops-db-...`, which is exactly the
    snapshot the ownership gate exists to protect.
    """
    return f"{OPS_PREFIX}sch{int(schedule_id)}-"


def build_scheduled_snapshot_name(
    schedule_id: int, label: str, moment: Optional[datetime] = None
) -> str:
    """The name for one slot of one schedule.

    `moment` is the DUE SLOT, never `now`. That is the entire at-most-once property: a worker that
    died after LXD created the snapshot but before its row was updated retries the same slot,
    composes the same name, and the daemon refuses it as already existing rather than taking a
    second copy of a moment that has already passed.

    Composed through `build_operator_snapshot_name` so the 63-character bound, the slug rules and
    the `ops-` prefix are honoured by construction rather than by a second implementation that
    would drift.
    """
    return build_operator_snapshot_name(f"sch{int(schedule_id)}-{label}", moment)
