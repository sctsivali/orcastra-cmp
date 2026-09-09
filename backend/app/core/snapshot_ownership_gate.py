"""Who a snapshot belongs to, and what the console must be told before acting on one.

A sibling of `snapshot_billing_gate.py`, and the two are deliberately independent. This one reads
a NAME and asks whose snapshot it is. That one reads `snapshots.schedule` on the SOURCE and asks
whose bill the instance is on. A row can be caught by both, by either, or by neither, so they
carry separate acknowledgements and a shared reason.

important: the acknowledgement here is NOT `acknowledge_billing_impact`. Reusing that field would
have turned a live QA spec from proving a refusal into performing an upstream restore on a real
instance while still reporting green, because that spec sends the billing acknowledgement at a
`snap-` name precisely to prove it does not open this door.

The refusals are machine-readable, the same shape the billing gate publishes, so the console can
grow the affordance a refusal names instead of dead-ending on a sentence.
"""

import logging
from typing import Optional

from fastapi import HTTPException

from app.services.lxd.snapshots import (
    RESERVED_PREFIXES,
    SnapshotNameError,
    SnapshotOrigin,
    assert_snapshot_name,
    classify_snapshot_origin,
)

logger = logging.getLogger(__name__)


def enforce_snapshot_ownership(
    snapshot_name: str,
    instance_name: str,
    action: str,
    *,
    confirm_snapshot: Optional[str] = None,
    manual_already_confirmed: bool = False,
    acknowledged_upstream: bool = False,
    reason: Optional[str] = None,
) -> Optional[str]:
    """Authorize acting on a snapshot this console may not have created.

    Returns the justification when an upstream override was accepted, so the caller can audit it,
    and None when the class needed no override. Raises HTTPException(403) otherwise.

    important: the name is re-validated here rather than trusted from the caller. The classifier
    is total and falls back to MANUAL, which is the most permissive class, so a blank or hostile
    name arriving at this function directly would land in the weakest gate. The regex costs
    nothing on a 63-character string and makes that unreachable by construction rather than by a
    comment asking the next caller to remember.
    """
    try:
        validated = assert_snapshot_name(snapshot_name)
    except SnapshotNameError as exc:
        # Raised as HTTP rather than let through, because this function is reachable from more
        # than one router and a bare SnapshotNameError there is a 500. The router validates first
        # and this is the second line; both answer the same way so a caller cannot tell which
        # one refused, which is the point.
        raise HTTPException(
            status_code=403,
            detail={
                "error": "snapshot_name_invalid",
                "message": str(exc),
                "field": "snapshot_name",
            },
        ) from exc
    origin = classify_snapshot_origin(validated)

    if origin is SnapshotOrigin.OPERATOR:
        return None

    if origin is SnapshotOrigin.MANUAL:
        # Taken by hand on the host. It is the operator's own, so the console acts on it. What it
        # is not is something this console can recreate, and on a fleet listing the thing an
        # operator gets wrong is which row they clicked, so the name has to be typed.
        #
        # important: `manual_already_confirmed` is for a caller that ALREADY took a typed
        # confirmation for this action. Restore makes the operator type the instance name before
        # it reaches here, and asking for a second typed string in one dialog is what produces
        # copy-paste without reading. It is a named parameter rather than the caller quietly
        # passing the snapshot name back to itself, because that reads as a bypass and would be
        # copied as one.
        if manual_already_confirmed:
            return None
        if (confirm_snapshot or "").strip() != validated:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "snapshot_confirmation_required",
                    "message": (
                        f"'{validated}' was taken on the host rather than from this console. "
                        "Nothing here will make another one. Type its name to confirm."
                    ),
                    "instance": instance_name,
                    "snapshot": validated,
                    "origin": origin.value,
                    "action": action,
                    "confirm_field": "confirm_snapshot",
                    "confirm_value": validated,
                },
            )
        return None

    justification = (reason or "").strip()
    if not acknowledged_upstream or not justification:
        reserved = next((p for p in RESERVED_PREFIXES if validated.startswith(p)), "")
        raise HTTPException(
            status_code=403,
            detail={
                "error": "upstream_snapshot_acknowledgement_required",
                "message": (
                    f"'{reserved}' is the naming the upstream snapshot product uses. Acting on "
                    f"'{validated}' here touches a restore point that product owns and a customer "
                    "may be paying to keep."
                ),
                "instance": instance_name,
                "snapshot": validated,
                "origin": origin.value,
                "action": action,
                "reserved_prefix": reserved,
                "acknowledge_field": "acknowledge_upstream_snapshot",
                "reason_field": "reason",
            },
        )
    return justification
