"""Who a snapshot belongs to, and what the console must be told before acting on one.

Independent of `snapshot_billing_gate.py`: this gate reads a snapshot NAME and asks whose
snapshot it is; that one reads `snapshots.schedule` on the SOURCE and asks whose bill the
instance is on. A row can be caught by either, both or neither, so the gates take separate
acknowledgements and share a reason. Refusals use the billing gate's machine-readable shape.

important: the acknowledgement here is `acknowledge_upstream_snapshot`. Sending
`acknowledge_billing_impact` at a `snap-` name must not open this gate.
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

    Returns the justification when an upstream override was accepted, for the caller to audit,
    and None when no override was needed; raises HTTPException(403) otherwise. The name is
    validated again here: the classifier falls back to MANUAL for any name it does not recognise,
    so a blank or hostile name would otherwise land in the weakest gate.
    """
    try:
        validated = assert_snapshot_name(snapshot_name)
    except SnapshotNameError as exc:
        # Raised as HTTP because this function is reachable from more than one router and a bare
        # SnapshotNameError there is a 500. The router validates first; both answer the same way,
        # so a caller cannot tell which one refused.
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
        # Taken by hand on the host: the operator's own, but the console cannot recreate it, so
        # the name has to be typed to guard against acting on the wrong row.
        # important: `manual_already_confirmed` is only for a caller that ALREADY took a typed
        # confirmation for this action (restore asks for the instance name).
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
