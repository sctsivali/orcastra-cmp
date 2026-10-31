"""Host kernel advisories across the fleet: the facts per member, judged on read.

Shared by `GET /security/kernel-advisories` and the system warnings feed so both report the
same verdicts from the same cache. Ubuntu's records come from the last reads the background
check stored (services/kernel_cve_feed/); nothing here reaches the internet.
"""

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.cache import get_cache_manager
from app.models.kernel_advisory_schemas import (
    AdvisoryScoreOut,
    HostKernelRow,
    KernelAdvisoryCounts,
    KernelAdvisoryFinding,
    KernelAdvisoryInfo,
    KernelAdvisoryResponse,
    KernelAdvisoryWarning,
    KernelCveCheck,
    KernelCveFeedStatus,
)
from app.services.kernel_advisory import HostKernelFacts, KernelAdvisory, assess, release_text, ubuntu_is_fresh
from app.services.kernel_advisory_data import ADVISORIES
from app.services.kernel_cve_feed import service as cve_feed
from app.services.kernel_cve_feed.record import UbuntuSnapshot
from app.services.lxd.fanout import complete_reasons
from app.services.lxd.host_kernels import ClusterKernels, MemberKernel, fleet_kernels

logger = logging.getLogger(__name__)

_STATE_ORDER = {"exposed": 0, "unverified": 1, "patched": 2}


def ubuntu_url(advisory_id: str) -> str:
    return f"https://ubuntu.com/security/{advisory_id}"


def cve_check(advisory: KernelAdvisory, check: Optional[cve_feed.CveCheck], now: datetime) -> KernelCveCheck:
    """What the reads of Ubuntu's record of one advisory left, as the API reports it."""
    snapshot = check.snapshot if check is not None else None
    record = snapshot.record if snapshot is not None else None
    return KernelCveCheck(
        id=advisory.id,
        source="ubuntu" if snapshot is not None else "built-in",
        checked_at=snapshot.checked_at if snapshot is not None else None,
        last_attempt_at=check.last_attempt_at if check is not None else None,
        stale=snapshot is not None and not ubuntu_is_fresh(snapshot, now),
        check_error=check.error if check is not None else None,
        ubuntu_priority=record.priority if record is not None else None,
        ubuntu_cvss_score=record.cvss_score if record is not None else None,
        ubuntu_updated_at=record.updated_at if record is not None else None,
        ubuntu_url=ubuntu_url(advisory.id),
        data_as_of=advisory.data_as_of,
    )


def _scores(advisory: KernelAdvisory, priority: Optional[str]) -> List[AdvisoryScoreOut]:
    scores = [AdvisoryScoreOut(**vars(score)) for score in advisory.scores]
    if priority:
        # Ubuntu's own priority replaces the one written into the data, so the two never disagree.
        ubuntu = next((score for score in scores if score.source == "Ubuntu"), None)
        if ubuntu is None:
            scores.append(AdvisoryScoreOut(source="Ubuntu", rating=priority.capitalize()))
        else:
            ubuntu.rating = priority.capitalize()
    return scores


def advisory_info(advisory: KernelAdvisory, check: KernelCveCheck) -> KernelAdvisoryInfo:
    return KernelAdvisoryInfo(
        id=advisory.id,
        name=advisory.name,
        summary=advisory.summary,
        scores=_scores(advisory, check.ubuntu_priority),
        reference_url=advisory.reference_url,
        data_as_of=advisory.data_as_of,
        introduced=release_text(advisory.introduced),
        fixed_upstream=[f"{a}.{b}.{c}" for (a, b), c in sorted(advisory.stable_fixes.items())],
        fixed_mainline=release_text(advisory.mainline_fixed),
        source=check.source,
        checked_at=check.checked_at,
        stale=check.stale,
        check_error=check.check_error,
        ubuntu_priority=check.ubuntu_priority,
        ubuntu_cvss_score=check.ubuntu_cvss_score,
        ubuntu_url=check.ubuntu_url,
    )


def _feed_checks() -> Dict[str, cve_feed.CveCheck]:
    try:
        return cve_feed.read_checks()
    except Exception as exc:  # noqa: BLE001 - the built-in data still judges every member
        logger.warning("Ubuntu's kernel CVE records could not be read from the store: %s", type(exc).__name__)
        return {}


def feed_status(checks: Dict[str, cve_feed.CveCheck], now: Optional[datetime] = None) -> KernelCveFeedStatus:
    moment = now or datetime.now(timezone.utc)
    return KernelCveFeedStatus(
        enabled=cve_feed.get_settings().kernel_cve_feed_enabled,
        mirror=cve_feed.mirror_configured(),
        advisories=[cve_check(advisory, checks.get(advisory.id), moment) for advisory in ADVISORIES],
    )


async def read_feed_status() -> KernelCveFeedStatus:
    """GET .../ubuntu-status: the stored reads only."""
    return feed_status(await asyncio.to_thread(_feed_checks))


def _collected(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


Snapshots = Dict[str, Optional[UbuntuSnapshot]]


def _row(
    cluster: ClusterKernels, member: MemberKernel, display_name: str, today: date, snapshots: Snapshots, now: datetime
) -> HostKernelRow:
    facts = HostKernelFacts(member.kernel_version, member.os_name, member.os_version, tuple(member.instance_types))
    findings = []
    for advisory in ADVISORIES:
        finding = assess(advisory, facts, today=today, ubuntu=snapshots.get(advisory.id), now=now)
        reason = finding.reason
        if member.error and member.kernel_version is None:
            reason = f"This member was not read ({member.error}), so its kernel is unknown."
        findings.append(KernelAdvisoryFinding(**{**vars(finding), "reason": reason}))
    return HostKernelRow(
        cluster_id=cluster.cluster_id,
        display_name=display_name,
        member=member.member,
        clustered=cluster.clustered,
        members_listed=cluster.members_listed,
        kernel_version=member.kernel_version,
        os_name=member.os_name,
        os_version=member.os_version,
        vm_capable=facts.vm_capable,
        read_error=member.error,
        collected_at=_collected(cluster.collected_at),
        findings=findings,
    )


def _row_or_unverified(
    cluster: ClusterKernels, member: MemberKernel, display_name: str, today: date, snapshots: Snapshots, now: datetime
) -> HostKernelRow:
    """One member's row; a member whose facts cannot be judged reads as unverified, never as an error."""
    try:
        return _row(cluster, member, display_name, today, snapshots, now)
    except Exception as exc:  # noqa: BLE001 - one host must not fail the fleet's report
        logger.warning("Kernel facts of %s/%s could not be judged: %s", cluster.cluster_id, member.member, type(exc).__name__)
        return HostKernelRow(
            cluster_id=cluster.cluster_id,
            display_name=display_name,
            member=str(member.member)[:128],
            clustered=cluster.clustered,
            members_listed=cluster.members_listed,
            read_error=f"error: facts could not be judged ({type(exc).__name__})",
            findings=[
                KernelAdvisoryFinding(
                    advisory_id=advisory.id, state="unverified", severity="warning",
                    reason="This member's kernel facts could not be judged, so its kernel is unknown.",
                    check_command="uname -r", reference_url=advisory.reference_url,
                )
                for advisory in ADVISORIES
            ],
        )


def _display_names() -> Dict[str, str]:
    from app.core.node_metadata import get_node_metadata_store

    try:
        metadata = get_node_metadata_store().get_all_node_metadata()
    except Exception as exc:  # noqa: BLE001 - the cluster id is a correct, if plainer, label
        logger.debug("Node metadata unreadable: %s", exc)
        return {}
    return {name: str(meta.get("display_name") or name) for name, meta in metadata.items()}


async def build_kernel_report(
    lxd_service: Any, *, host: Optional[str], fresh: bool, wall: float, today: Optional[date] = None
) -> KernelAdvisoryResponse:
    """Every member of every cluster in scope, judged against every advisory."""
    cluster_ids: List[str] = [host] if host else await asyncio.to_thread(lxd_service.get_node_names)
    fleet = await fleet_kernels(lxd_service, cluster_ids, get_cache_manager(), fresh=fresh, wall=wall)
    names = await asyncio.to_thread(_display_names)
    checks = await asyncio.to_thread(_feed_checks)
    now = datetime.now(timezone.utc)
    day = today or now.date()
    snapshots: Snapshots = {cve_id: check.snapshot for cve_id, check in checks.items()}
    status = feed_status(checks, now)

    hosts = [
        _row_or_unverified(cluster, member, names.get(cluster.cluster_id, cluster.cluster_id), day, snapshots, now)
        for cluster in fleet.clusters
        for member in cluster.members
    ]
    hosts.sort(key=lambda r: (min(_STATE_ORDER[f.state] for f in r.findings), r.display_name, r.member))
    counts = KernelAdvisoryCounts()
    for row in hosts:
        worst = min((f.state for f in row.findings), key=_STATE_ORDER.__getitem__)
        setattr(counts, worst, getattr(counts, worst) + 1)
    return KernelAdvisoryResponse(
        advisories=[advisory_info(a, c) for a, c in zip(ADVISORIES, status.advisories)],
        hosts=hosts,
        counts=counts,
        unreachable_nodes=fleet.unreachable_nodes,
        node_errors=complete_reasons(fleet.unreachable_nodes, fleet.node_errors),
        ubuntu_check_enabled=status.enabled,
    )


def kernel_warning_rows(report: KernelAdvisoryResponse) -> List[KernelAdvisoryWarning]:
    """The exposed and unverified findings, one warning row each."""
    infos = {info.id: info for info in report.advisories}
    rows: List[KernelAdvisoryWarning] = []
    for host in report.hosts:
        for finding in host.findings:
            if finding.state == "patched":
                continue
            rows.append(KernelAdvisoryWarning(
                cluster_id=host.cluster_id,
                display_name=host.display_name,
                member=host.member,
                warning_type="kernel_exposed" if finding.state == "exposed" else "kernel_unverified",
                advisory_id=finding.advisory_id,
                state=finding.state,
                severity=finding.severity,
                message=finding.reason,
                kernel_version=host.kernel_version,
                os_name=host.os_name,
                os_version=host.os_version,
                check_command=finding.check_command,
                reference_url=finding.reference_url,
                data_as_of=infos[finding.advisory_id].data_as_of,
                fixed_in=finding.fixed_in,
                source=finding.source,
                checked_at=finding.checked_at,
                ubuntu_priority=infos[finding.advisory_id].ubuntu_priority,
                ubuntu_url=infos[finding.advisory_id].ubuntu_url,
            ))
    return rows


def kernel_severity_totals(rows: List[KernelAdvisoryWarning]) -> Tuple[int, int, int]:
    critical = sum(1 for r in rows if r.severity == "critical")
    warning = sum(1 for r in rows if r.severity == "warning")
    return critical, warning, len(rows) - critical - warning
