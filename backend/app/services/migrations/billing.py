"""Billing-impact acknowledgement shared by acceptance and the mutation worker."""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import HTTPException

from app.core.snapshot_billing_gate import enforce_billing_impact
from app.services.lxd import snapshots as snapshots_service

from .contracts import MigrationConflict


BILLING_ACKNOWLEDGEMENT = "billing-impact-reviewed"
LEGACY_BILLING_ACKNOWLEDGEMENT = "billing-impact-acknowledged"


def billing_override_event(
    job_id: str, *, workload_count: int, reason: str, phase: str
) -> dict[str, Any]:
    material = f"{job_id}:audit:migration.billing_override".encode("utf-8")
    return {
        "event_id": f"mig_{hashlib.sha256(material).hexdigest()[:48]}",
        "channel": "audit",
        "event_type": "migration.billing_override",
        "phase": phase,
        "message": "Externally managed snapshot billing impact was acknowledged.",
        "extra": {
            "workload_count": workload_count,
            "reason": reason,
        },
    }


def required_acknowledgements(
    *,
    intent: str,
    strategy: str,
    external_cutover_required: bool,
) -> set[str]:
    required = {
        "copy-impact-reviewed" if intent == "copy" else "downtime-reviewed",
        BILLING_ACKNOWLEDGEMENT,
    }
    if intent == "move" and strategy != "member_relocation":
        required.add("source-retention-understood")
    if external_cutover_required:
        required.add("external-cutover-owned")
    return required


def validate_acknowledgements(
    *,
    intent: str,
    strategy: str,
    external_cutover_required: bool,
    acknowledgements: list[str],
) -> None:
    provided = set(acknowledgements)
    missing = required_acknowledgements(
        intent=intent,
        strategy=strategy,
        external_cutover_required=external_cutover_required,
    ) - provided
    if missing:
        raise MigrationConflict(
            "Required migration acknowledgements are missing: "
            + ", ".join(sorted(missing))
        )


def enforce_migration_billing_policy(
    source_client: Any,
    *,
    project: str,
    report: dict[str, Any],
    reason: str,
    acknowledgements: list[str],
) -> int:
    """Return the number of externally billed sources after fail-closed validation."""
    provided = set(acknowledgements)
    acknowledged = bool(
        provided
        & {BILLING_ACKNOWLEDGEMENT, LEGACY_BILLING_ACKNOWLEDGEMENT}
    )
    names: list[str] = []
    for resource in report.get("resources") or []:
        if resource.get("resource_type") != "instance":
            continue
        source = resource.get("source") or {}
        name = source.get("name") or resource.get("source_name")
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    if not names:
        raise MigrationConflict("Preflight did not return a complete source workload inventory.")

    impacted = 0
    for name in names:
        policy = snapshots_service.read_instance_policy(
            source_client,
            name,
            project,
        )
        if policy.get("externally_managed"):
            impacted += 1
        try:
            enforce_billing_impact(
                policy,
                name,
                acknowledged=acknowledged,
                reason=reason,
            )
        except HTTPException as exc:
            # The shared gate raises an HTTPException with schedule details for request-scoped
            # snapshot routes. Durable jobs keep those details out of worker errors and outbox.
            raise MigrationConflict(
                "Externally managed snapshot billing impact requires explicit acknowledgement."
            ) from exc
    return impacted
