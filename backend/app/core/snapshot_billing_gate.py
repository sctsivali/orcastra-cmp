"""Refusing an action on a source whose snapshots someone else is paying for.

The upstream product meters a snapshot schedule by the storage its SOURCE occupies, not by
counting snapshots, so anything this console does on such an instance can pause a schedule a
customer pays for. The default is refusal; proceeding takes an acknowledgement and a reason, and
is audited.
"""

import logging
from typing import Any, Dict, Optional

from fastapi import HTTPException

from app.services.activity_logger import get_activity_logger

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()


def enforce_billing_impact(
    policy: Dict[str, Any],
    instance_name: str,
    *,
    acknowledged: bool,
    reason: Optional[str],
) -> Optional[str]:
    """Refuse an action on an instance whose snapshots are managed and billed elsewhere.

    As in `enforce_sensitive_access` (break_glass_service), the 403 carries a machine-readable
    body naming what is in the way and which fields to send. The override covers one request:
    a one-shot action needs no expiry. Returns None when the snapshots are not externally managed,
    otherwise the caller's justification for the handler to audit.
    """
    if not policy.get("externally_managed"):
        return None

    justification = (reason or "").strip()
    if not acknowledged or not justification:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "billing_impact_acknowledgement_required",
                "message": (
                    "Snapshots on this instance are managed and billed elsewhere. Acting here "
                    "changes the storage that schedule is measured on and can pause it."
                ),
                "instance": instance_name,
                "schedule": policy.get("schedule"),
                "expiry": policy.get("expiry"),
                "acknowledge_field": "acknowledge_billing_impact",
                "reason_field": "reason",
            },
        )
    return justification


def audit_billing_override(
    action: str,
    instance_name: str,
    host: str,
    project: str,
    policy: Dict[str, Any],
    justification: str,
    requester: str,
) -> None:
    """A separate high severity record for a forced action.

    Written beside the ordinary record, not instead of it, so a forced action can be alerted on
    separately, as break-glass audits a grant apart from the session it opens.
    """
    activity_logger.log_activity(
        action="instance.snapshot.billing_override",
        target_type="instance_snapshot",
        target_id=instance_name,
        target_host=host,
        target_project=project,
        result="success",
        details={
            "overridden_action": action,
            "reason": justification,
            "external_schedule": policy.get("schedule"),
            "external_expiry": policy.get("expiry"),
            "requester": requester,
        },
    )
