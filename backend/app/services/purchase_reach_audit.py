"""Host reach in what a buyer can reach, checked when a purchase skips the whole-cluster audit.

A purchase on a cluster that the provisioning key's party already holds adds no holder outside that
party, so the whole-cluster share audit (shared_cluster_audit) is not run; every stock cluster
fails it. The buyer still gets console, as root, inside the purchased project's instances, and an
OrcaHub project leaves features.profiles off, so those instances inherit the `default` project's
default profile. This reads exactly that: the purchased project's instances, local and expanded,
and the default profile in force there, and refuses host reach (core.limited_key_writes.host_reach:
privileged, raw.*, host disks, passthrough and proxy devices) and NICs on a host interface. The
seller's other profiles and projects are not the buyer's and are not read. Anything the seller's own
key adds later is the seller's to answer for, as it is today.

Like the whole-cluster audit, nothing here leaves the module but counts and stable codes.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Mapping, Optional

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.limited_key_writes import host_reach
from app.services.shared_cluster_audit import (
    _AUDIT_EXECUTOR,
    _AUDIT_WORKERS,
    _READ_TIMEOUT,
    SharedClusterAuditReport,
    SharedClusterFindingSummary,
    SharedClusterGuardError,
    _AuditUnavailable,
    _cluster_log_token,
    _mapping,
    _metadata,
    _read_projects,
    _unavailable_report,
)

logger = logging.getLogger(__name__)

# Three bounded reads of one project; well inside the request a purchase arrives on.
_WALL_TIMEOUT_SECONDS = 30.0
HOST_REACH = "host_reach_not_allowed"
# NICs that put a guest on a host interface rather than a network LXD manages. `bridged` is left
# out: a stock profile names lxdbr0 as its parent, and telling a managed bridge from a host bridge
# would need another read.
_HOST_NIC_TYPES = frozenset({"physical", "macvlan", "ipvlan", "sriov", "routed"})


def _profiles_in_force(client: Any, project: Optional[Mapping[str, Any]], name: str) -> str:
    """The project whose profiles an instance in `name` uses: its own only with features.profiles on."""
    config = _mapping(project.get("config")) if project is not None else {}
    return name if str(config.get("features.profiles") or "").strip().lower() == "true" else "default"


def _host_nic(devices: Mapping[str, Any]) -> bool:
    return any(
        isinstance(device, Mapping)
        and str(device.get("type") or "").lower() == "nic"
        and str(device.get("nictype") or "").lower() in _HOST_NIC_TYPES
        for device in devices.values()
    )


def _record(counter: Counter, scope: str, config: Any, devices: Any) -> None:
    devices = _mapping(devices or {})
    reached = host_reach({"config": _mapping(config or {}), "devices": devices})
    if not reached and _host_nic(devices):
        reached = "devices"
    if reached:
        # Only the kind: a key, device name or path is a value the report must not carry.
        counter[(scope, HOST_REACH, "config" if reached.startswith("config") else "devices")] += 1


def _scan_purchase_sync(cluster_id: str, project_name: str, lxd_service: Any) -> SharedClusterAuditReport:
    client = lxd_service.connection_manager.get_client(cluster_id)
    if client is None:
        raise _AuditUnavailable("Cluster connection is unavailable")
    projects = _read_projects(client)
    target = next((row for row in projects if isinstance(row, dict) and row.get("name") == project_name), None)
    profile_project = _profiles_in_force(client, target, project_name)
    profiles = _metadata(
        client.api.profiles.get(params={"recursion": 1, "project": profile_project}, timeout=_READ_TIMEOUT),
        "profiles",
    )
    defaults = [row for row in profiles if row.get("name") == "default"]
    # A project that does not exist yet has no instance; what it will inherit is still read above.
    instances = [] if target is None else _metadata(
        client.api.instances.get(params={"recursion": 1, "project": project_name}, timeout=_READ_TIMEOUT),
        "instances",
    )

    findings: Counter = Counter()
    for row in defaults:
        _record(findings, "profile", row.get("config"), row.get("devices"))
    for row in instances:
        _record(findings, "instance_local", row.get("config"), row.get("devices"))
        _record(findings, "instance_effective", row.get("expanded_config"), row.get("expanded_devices"))
    summaries = tuple(
        SharedClusterFindingSummary(scope, code, field, count)
        for (scope, code, field), count in sorted(findings.items())
    )
    return SharedClusterAuditReport(
        cluster_id=cluster_id,
        safe=not summaries,
        complete=True,
        projects_checked=0 if target is None else 1,
        profiles_checked=len(defaults),
        instances_checked=len(instances),
        findings=summaries,
    )


async def audit_purchased_project(
    cluster_id: str,
    project_name: str,
    *,
    lxd_service: Optional[Any] = None,
) -> SharedClusterAuditReport:
    """Read and judge what a purchase on `project_name` hands the buyer, value free."""
    if lxd_service is None:
        from app.services.lxd_service import get_lxd_service

        lxd_service = get_lxd_service()
    log_token = _cluster_log_token(cluster_id)
    try:
        return await run_blocking(
            _scan_purchase_sync,
            cluster_id,
            project_name,
            lxd_service,
            timeout=_WALL_TIMEOUT_SECONDS,
            label=f"purchase reach audit [{log_token}]",
            executor=_AUDIT_EXECUTOR,
            max_in_flight=_AUDIT_WORKERS,
            concurrency_key=f"purchase:{cluster_id}",
            max_in_flight_per_key=1,
        )
    except (BlockingPoolFull, BlockingTimeout, _AuditUnavailable):
        logger.warning("Purchase reach audit unavailable [%s]", log_token)
        return _unavailable_report(cluster_id)
    except Exception:  # noqa: BLE001 - raw daemon failures must not enter the response or logs
        logger.error("Purchase reach audit failed [%s]", log_token)
        return _unavailable_report(cluster_id)


async def assert_purchase_reach_safe(cluster_id: str, project_name: str) -> SharedClusterAuditReport:
    """Refuse a purchase whose project, or the default profile it inherits, reaches the host.

    The codes are the whole-cluster audit's, which OrcaHub already reads.
    """
    report = await audit_purchased_project(cluster_id, project_name)
    if not report.complete:
        raise SharedClusterGuardError(
            "cluster_share_audit_unavailable",
            "The purchased project could not be verified. No ownership was changed.",
            report,
            status_code=503,
        )
    if not report.safe:
        raise SharedClusterGuardError(
            "cluster_not_safe_to_share",
            (
                "The purchased project, or the default profile it inherits, reaches the host "
                "(privileged, raw settings, host disks or passthrough devices). Remediate it before "
                "this purchase can be provisioned."
            ),
            report,
            status_code=409,
        )
    return report
