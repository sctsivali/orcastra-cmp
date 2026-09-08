"""Refusing an action on a source whose snapshots someone else is paying for.

The upstream product meters a snapshot schedule by the storage its SOURCE occupies, not by
counting snapshots, so anything this console does on such an instance moves a number that can
pause a schedule a customer pays for. That is a service interruption rather than a cosmetic
surprise, which is why the default here is refusal and proceeding is a deliberate, attributable
act.

Shaped after `enforce_sensitive_access` in break_glass_service: refuse with a machine readable body
naming what is in the way and what to send, rather than a bare 403. Unlike that one the override is
a single request rather than a time boxed grant, because a grant carries an expiry and a session
watchdog and a snapshot is one short action with no session to watch.
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
    """Refuse an action on a source whose snapshots someone else is paying for, unless it is owned.

    The upstream product meters a schedule by the storage its source occupies, not by counting
    snapshots, so anything this console does here moves a number that can pause a schedule a
    customer is paying for. That is a service interruption, not a cosmetic surprise, so the default
    is refusal and proceeding is a deliberate, attributable act.

    Modelled on `enforce_sensitive_access` in break_glass_service: a machine readable body naming
    what is in the way and what to send, rather than a bare 403. Unlike that one this override is a
    single request rather than a time boxed grant, because the grant carries an expiry and a
    session watchdog that a one shot action has no use for.

    Returns the justification when the caller has acknowledged, so the handler can audit it.
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

    Written beside the ordinary one rather than replacing it. Leaving the routine actions at their
    own severity means a forced action is separately alertable instead of being one row among
    hundreds, which is the same reason break-glass audits a grant apart from the session it opens.
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
