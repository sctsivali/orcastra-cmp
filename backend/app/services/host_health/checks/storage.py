"""Disk space: the host disk under Docker, and the certificate scratch space.

A full host disk stops Postgres writes, Fluent Bit's buffer and image uploads together; the
certificate scratch space (a tmpfs) full stops every cluster connection.
"""

from __future__ import annotations

import time
from typing import List

from app.services.host_health.context import CheckContext
from app.services.host_health.format import size
from app.services.host_health.models import CheckResult, HealthFact, fact

LABEL = "Disk Space"
HOST_WARN_PCT = 85.0
HOST_CRITICAL_PCT = 95.0
SCRATCH_CRITICAL_PCT = 95.0


def check(ctx: CheckContext) -> CheckResult:
    from app.services.lxd.cert_files import describe_usage
    from app.services.system_resources import host_filesystems

    started = time.monotonic()
    facts: List[HealthFact] = []
    status, severity, summary, remediation = "ok", "info", "There is room on the host disk and the certificate scratch space.", None

    filesystems = [fs for fs in host_filesystems() if fs.fstype not in ("tmpfs", "devtmpfs", "overlay")]
    worst = max(filesystems, key=lambda fs: fs.percent, default=None)
    for fs in sorted(filesystems, key=lambda fs: -fs.percent):
        facts.append(fact(f"Host Disk {fs.device}", f"{fs.percent:.1f}% used, {size(fs.free)} free"))
    if worst is not None and worst.percent >= HOST_CRITICAL_PCT:
        status, severity = "down", "critical"
        summary = f"The host disk is {worst.percent:.0f}% full. Database writes and log buffering will fail."
        remediation = "Free space on the host (old images: `docker image prune`, rotated logs) or grow the disk."
    elif worst is not None and worst.percent >= HOST_WARN_PCT:
        status, severity = "degraded", "warning"
        summary = f"The host disk is {worst.percent:.0f}% full."
        remediation = "Free space on the host before it fills: old Docker images and build cache are the usual cause."
    if worst is None:
        facts.append(fact("Host Disk", "not readable from the backend container"))

    scratch = describe_usage()
    if scratch.get("available"):
        percent = float(scratch.get("percent_used") or 0)
        facts.append(fact(
            "Certificate Scratch Space",
            f"{percent:.1f}% used of {size(float(scratch.get('total_mib') or 0) * 1024 * 1024)}",
        ))
        if percent >= SCRATCH_CRITICAL_PCT:
            status, severity = "down", "critical"
            summary = "The certificate scratch space is full. New cluster connections will fail."
            remediation = "Restart the backend to clear it, then look for a leak of temporary certificate files."
        elif scratch.get("degraded") and status == "ok":
            status, severity = "degraded", "warning"
            summary = f"The certificate scratch space is {percent:.0f}% full."
            remediation = "Restart the backend at a quiet moment; temporary certificate files are not being cleaned up."
    else:
        facts.append(fact("Certificate Scratch Space", "not readable"))
    return CheckResult(id="storage", label=LABEL, status=status, severity=severity, summary=summary, facts=facts,
                       remediation=remediation, checked_at=ctx.now, latency_ms=int((time.monotonic() - started) * 1000))
