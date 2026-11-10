"""The safety copy a restore takes first, and the three ways it refuses.

Beside `snapshot_ownership_gate` and `snapshot_billing_gate`, this decides whether a restore may
proceed and says why not, which keeps that decision out of the restore handler. The create
operation is waited on, so a daemon that accepts the POST and then fails the operation stops the
restore instead of letting it run without a safety copy. The copy counts against the operator
snapshot cap under the create path's lock key, so a restore and a create contend for one slot set.
"""

import logging
from typing import Any, Callable, Dict, List, Optional

from fastapi import HTTPException, status

from app.core.single_flight import rebuild_lock, release_lock
from app.services.lxd import snapshots as snapshots_service

logger = logging.getLogger(__name__)


def _operator_names(rows: List[Dict[str, Any]]) -> List[str]:
    return [
        str(row.get("name"))
        for row in rows
        if snapshots_service.is_operator_snapshot(str(row.get("name") or ""))
    ]


async def take_safety_snapshot(
    client: Any,
    *,
    instance_name: str,
    host: str,
    project: str,
    wanted: bool,
    existing: List[Dict[str, Any]],
    blocking: Callable[..., Any],
    settle: Callable[..., Any],
    audit: Callable[..., None],
) -> Optional[str]:
    """Take the pre-restore copy, or refuse the restore. Returns its name, or None if not wanted.

    Raises HTTPException on every refusal instead of returning a verdict, since every caller would
    raise on it anyway.
    """
    if not wanted:
        return None

    name = snapshots_service.build_safety_snapshot_name(instance_name)

    # Held across the read and the create, so two concurrent restores cannot both take the last
    # free slot.
    # important: every `rebuild_lock` needs its `release_lock` (in `finally`, since every refusal
    # below raises), or one entry per (host, project, instance) stays alive for the worker's life.
    lock_key = f"snapshot-create:{host}/{project}/{instance_name}"
    lock = rebuild_lock(lock_key)
    try:
        async with lock:
            try:
                await blocking(
                    snapshots_service.assert_capacity,
                    client,
                    instance_name,
                    project,
                    label="snapshots.safety.capacity",
                )
            except snapshots_service.SnapshotLimitError:
                held = _operator_names(existing)
                audit(
                    result="denied",
                    error_message="safety_snapshot_capacity_exhausted",
                    details={"operator_snapshots": held},
                )
                # The way forward is named in the body, the same shape the two sibling gates
                # use. Nothing has happened yet, so re-sending without a safety copy is the
                # operator's informed choice.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "safety_snapshot_capacity_exhausted",
                        "message": (
                            f"{instance_name} already holds {len(held)} operator snapshots, so the "
                            "safety copy this restore takes first would not fit. Delete one, or "
                            "restore without a safety copy."
                        ),
                        "instance": instance_name,
                        "operator_snapshots": held,
                        "maximum": snapshots_service.MAX_OPS_SNAPSHOTS_PER_INSTANCE,
                        "proceed_field": "safety_snapshot",
                    },
                )

            operation = await blocking(
                snapshots_service.create_instance_snapshot,
                client,
                instance_name,
                project,
                name,
                stateful=False,
                expires_at=snapshots_service.default_expiry(),
                label="snapshots.safety",
            )
            outcome = await settle(client, operation, project)
    finally:
        release_lock(lock_key)

    # The safety copy is left in place on every refusal below. Deleting it would be a
    # second call on a failure path that can itself fail, and it carries its own `expires_at`, so
    # LXD prunes it unaided.
    if outcome.get("failed"):
        audit(
            result="failure",
            error_message=outcome.get("err"),
            details={"safety_snapshot": name, "safety_failed": True},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"The safety copy {name} did not complete, so nothing was restored and "
                f"{instance_name} is unchanged. "
                f"{outcome.get('err') or 'The cluster reported no reason.'}"
            ),
        )

    if not outcome.get("settled"):
        # Nothing failed, but the copy is still being written and LXD answers 409 to a restore over
        # it. Refuse here with the same status: the operator waits and retries.
        audit(
            result="denied",
            error_message="safety_snapshot_unsettled",
            details={"safety_snapshot": name},
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"The safety copy {name} is still being written, so nothing was restored. Try "
                "again once it has finished, or restore without a safety copy."
            ),
        )

    return name
