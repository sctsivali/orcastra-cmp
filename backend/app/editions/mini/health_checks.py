"""Host Health in Mini: "Certificate sign-in" replaces the identity provider check and "Audit log"
replaces the logging pipeline check, under the same ids and in the same places.
"""

from __future__ import annotations

from typing import List

from app.editions.mini.housekeeping import STATE
from app.services.host_health.context import CheckContext
from app.services.host_health.models import CheckResult, HealthFact, fact

IDENTITY_LABEL = "Certificate Sign-In"
AUDIT_LABEL = "Audit Log"


def _waiting(check_id: str, label: str, ctx: CheckContext) -> CheckResult:
    return CheckResult(id=check_id, label=label, status="unknown", severity="warning", checked_at=ctx.now,
                       summary="The first figures are still being collected; check again in a minute.")


def identity_check(ctx: CheckContext) -> CheckResult:
    if STATE.get("collected_at") is None:
        return _waiting("identity", IDENTITY_LABEL, ctx)
    admins, expiring = STATE.get("usable_admins", 0), STATE.get("expiring_operators", 0)
    facts: List[HealthFact] = [
        fact("Administrators Who Can Sign In", admins),
        fact("Active Operators", STATE.get("active_operators", 0)),
        fact("Certificates Expiring Soon", expiring),
        fact("Bootstrap Token", "set" if STATE.get("bootstrap_token_set") else "cleared"),
    ]
    status, severity, summary, remediation = "ok", "info", "Operators sign in with their certificates.", None
    advisories: List[str] = []
    if admins == 0:
        status, severity = "down", "critical"
        summary = "No Administrator can sign in: every Administrator certificate is revoked or expired."
        remediation = ("On the host, run the recovery command in the operations guide to issue a new "
                       "Administrator certificate.")
    elif admins == 1:
        advisories.append("Only one Administrator can sign in. Issue a second Administrator certificate so a lost "
                          "device does not lock everyone out.")
    if STATE.get("bootstrap_token_set") and admins > 0:
        if status == "ok":
            status, severity = "degraded", "warning"
            summary = "The bootstrap token is still set although an Administrator exists."
        remediation = remediation or "Clear BOOTSTRAP_ADMIN_TOKEN in the deployment's .env and restart the backend."
    if expiring and status == "ok":
        advisories.append(f"{expiring} operator certificate(s) expire within the warning window. "
                          "Operators can renew from their user menu; Administrators can reissue on the Operators page.")
    return CheckResult(id="identity", label=IDENTITY_LABEL, status=status, severity=severity, summary=summary,
                       facts=facts, advisories=advisories, remediation=remediation, checked_at=ctx.now)


def audit_check(ctx: CheckContext) -> CheckResult:
    if STATE.get("collected_at") is None:
        return _waiting("logging", AUDIT_LABEL, ctx)
    if not STATE.get("audit_enabled"):
        return CheckResult(id="logging", label=AUDIT_LABEL, status="not_configured", severity="warning",
                           checked_at=ctx.now, summary="The audit log is turned off (AUDIT_DB_ENABLED=false).")
    dropped, failed = STATE.get("audit_dropped", 0), STATE.get("audit_failed", 0)
    retention = STATE.get("retention_days", 0)
    facts = [
        fact("Writer", "running" if STATE.get("audit_writer_running") else "stopped"),
        fact("Dropped Events", dropped),
        fact("Failed Writes", failed),
        fact("Retention", f"{retention} days" if retention else "keep every row"),
    ]
    status, severity, summary, remediation = "ok", "info", "Audit events are written to the hash-chained log.", None
    if not STATE.get("audit_writer_running") or failed:
        status, severity = "degraded", "warning"
        summary = "Some audit events could not be written to the database."
        remediation = "Check the database's free space and the backend log for the failed writes."
    if dropped:
        status, severity = "degraded", "critical"
        summary = "The audit queue overflowed and dropped events."
        remediation = "The backend is writing audit events faster than the database accepts them; check database load."
    return CheckResult(id="logging", label=AUDIT_LABEL, status=status, severity=severity, summary=summary,
                       facts=facts, remediation=remediation, checked_at=ctx.now)
