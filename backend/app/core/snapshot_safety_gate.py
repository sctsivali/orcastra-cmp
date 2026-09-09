"""The safety copy a restore takes first, and the three ways it refuses.

A third refusal beside `snapshot_ownership_gate` and `snapshot_billing_gate`, in its own module for
the same reason those are: it decides whether an action may proceed and says why not, and the
restore handler is easier to read when that decision is not inlined in it.

Two defects this module exists to have fixed, both of which the restore path carried:

**The operation was never waited on.** The create's operation id was discarded, so only a
SYNCHRONOUS failure aborted anything. A daemon that accepted the POST and then failed the operation
was never observed, and the restore went ahead on top of a safety copy that did not exist. The
comment beside that code claimed the opposite. This is the exact "accepted is not done" failure the
snapshot surface was built to refuse, and it had it.

**It did not count against the cap.** `assert_capacity` was called only on the create path, so
restoring twice silently occupied two of the five operator slots and the next manual snapshot was
refused with a message about the operator's own snapshots. The lock key here is deliberately the one
the create path takes: a restore and a create contend for the same five slots and have the same
read-then-act window.
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

    Raises rather than returning a verdict, because every caller would raise on every refusal and a
    return value nobody can ignore is the same thing with an extra step.
    """
    if not wanted:
        return None

    name = snapshots_service.build_safety_snapshot_name(instance_name)

    # Held across the read and the create, so two concurrent restores cannot both see four slots.
    #
    # important: `rebuild_lock` registers a waiter and `release_lock` is what drops the entry once
    # the last one leaves. Taking the lock without the matching release leaves one dict entry per
    # (host, project, instance) alive for the life of the worker, which is the accumulation the
    # module's own docstring promises does not happen. Paired in a `finally` for the same reason
    # the create path does it: every refusal below raises out of this block.
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
                # The affordance is named rather than left to be inferred, the same shape
                # the two sibling gates use. Nothing has happened yet, so re-sending without
                # a safety copy is a real choice rather than a dare.
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

    # important: left in place on every refusal below. Deleting it would be a second call on a
    # failure path that can itself fail, and it carries its own `expires_at`, so LXD prunes it
    # unaided. The same reasoning the feature doc records for a stranded migration operation.
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
        # Nothing failed. The copy is still being written, and restoring over it is what LXD itself
        # answers 409 for, so refuse it here coherently: the operator's action is to wait and retry.
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
