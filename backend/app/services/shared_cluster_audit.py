"""Fail-closed safety audit before a cluster gains another owner.

The ownership row is an authorization boundary. Once a physical cluster is shared, a host
device, raw LXD setting, or non-isolated project can cross organization boundaries. Every
binding path therefore serializes on the cluster, reads the complete daemon inventory, and
allows the transition only when that inventory satisfies the same value-free policy used by
instance and profile writes.

No raw config, device value, cloud-init body, workload/profile/project name, or organization
identity leaves this module. The public report contains the requested cluster id, counts, and
stable finding codes.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Mapping, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.models.db_models import InstanceOperationJob, OrganizationCluster
from app.services.instance_security_policy import (
    PolicyFinding,
    shared_payload_findings,
    shared_project_findings,
)

logger = logging.getLogger(__name__)

_READ_TIMEOUT = (3, 12)
_WALL_TIMEOUT_SECONDS = 50.0
_AUDIT_WORKERS = 4
_AUDIT_EXECUTOR = ThreadPoolExecutor(
    max_workers=_AUDIT_WORKERS,
    thread_name_prefix="shared_cluster_audit",
)
_cluster_read_locks_guard = threading.Lock()
_cluster_read_locks: dict[str, threading.Lock] = {}
_sqlite_locks_guard = threading.Lock()
_sqlite_locks: dict[tuple[int, str], asyncio.Lock] = {}


@dataclass(frozen=True)
class SharedClusterFindingSummary:
    """A value-free aggregate suitable for an API or exception response."""

    scope: str
    code: str
    field: str
    count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "code": self.code,
            "field": self.field,
            "count": self.count,
        }


@dataclass(frozen=True)
class SharedClusterAuditReport:
    """Sanitized audit verdict. Raw daemon records are deliberately not retained."""

    cluster_id: str
    safe: bool
    complete: bool
    projects_checked: int
    profiles_checked: int
    instances_checked: int
    findings: tuple[SharedClusterFindingSummary, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "safe": self.safe,
            "complete": self.complete,
            "resources_checked": {
                "projects": self.projects_checked,
                "profiles": self.profiles_checked,
                "instances": self.instances_checked,
            },
            "findings": [finding.as_dict() for finding in self.findings],
        }


class SharedClusterGuardError(RuntimeError):
    """A sanitized refusal raised before an ownership transition."""

    def __init__(
        self,
        code: str,
        message: str,
        report: SharedClusterAuditReport,
        *,
        status_code: int,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.report = report
        self.status_code = status_code

    def as_detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "audit": self.report.as_dict(),
        }


class _AuditUnavailable(RuntimeError):
    pass


def _cluster_lock(cluster_id: str) -> threading.Lock:
    with _cluster_read_locks_guard:
        return _cluster_read_locks.setdefault(cluster_id, threading.Lock())


def _sqlite_lock(cluster_id: str) -> asyncio.Lock:
    # SQLite is used only by focused tests. A lock is scoped to its event loop because asyncio
    # synchronization primitives cannot safely cross loops. PostgreSQL production uses a
    # transaction advisory lock below and is therefore serialized across workers and hosts.
    loop = asyncio.get_running_loop()
    key = (id(loop), cluster_id)
    with _sqlite_locks_guard:
        return _sqlite_locks.setdefault(key, asyncio.Lock())


def _dialect_name(db: AsyncSession) -> str:
    try:
        bind = db.get_bind()
        return str(bind.dialect.name).lower() if bind is not None else ""
    except (AttributeError, TypeError):
        return ""


def _advisory_key(cluster_id: str) -> int:
    digest = hashlib.sha256(f"orcastra:cluster-share:{cluster_id}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _cluster_log_token(cluster_id: str) -> str:
    return hashlib.sha256(cluster_id.encode()).hexdigest()[:12]


@asynccontextmanager
async def serialize_cluster_configuration(
    db: AsyncSession,
    cluster_id: str,
) -> AsyncIterator[None]:
    """Serialize ownership changes with ownership-sensitive daemon writes.

    PostgreSQL's transaction advisory lock remains held through the caller's commit or
    rollback. SQLite and light-weight fake sessions use an in-process lock intended only for
    tests; callers must keep the context open through their write or flush. Instance, profile,
    and project routes that grant exclusive-owner capabilities must use this same context from
    ownership policy resolution through the terminal daemon submission. That shared lock closes
    the race where a write is admitted as exclusive while another request shares the cluster.
    """

    if _dialect_name(db) == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _advisory_key(cluster_id)},
        )
        yield
        return

    lock = _sqlite_lock(cluster_id)
    async with lock:
        yield


@asynccontextmanager
async def serialize_cluster_share(
    db: AsyncSession,
    cluster_id: str,
) -> AsyncIterator[None]:
    """Compatibility name for ownership-transition callers."""

    async with serialize_cluster_configuration(db, cluster_id):
        yield


def _metadata(response: Any, collection: str) -> list[dict[str, Any]]:
    try:
        body = response.json()
    except Exception as exc:  # noqa: BLE001 - converted to a sanitized incomplete verdict
        raise _AuditUnavailable(f"Unreadable {collection} response") from exc
    raw = body.get("metadata") if isinstance(body, dict) else None
    if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
        raise _AuditUnavailable(f"Incomplete {collection} response")
    return raw


def _read_projects(client: Any) -> list[dict[str, Any]]:
    return _metadata(
        client.api.projects.get(
            params={"recursion": 1},
            timeout=_READ_TIMEOUT,
        ),
        "projects",
    )


def _read_across_projects(
    client: Any,
    collection: str,
    projects: list[str],
    *,
    deadline: float,
) -> list[dict[str, Any]]:
    from app.services.lxd.project_scope import supports_all_projects

    node = client.api
    for segment in collection.split("/"):
        node = node[segment]

    if supports_all_projects(client, collection):
        rows = _metadata(
            node.get(
                params={"recursion": 1, "all-projects": "true"},
                timeout=_READ_TIMEOUT,
            ),
            collection,
        )
        if not any(not row.get("project") for row in rows):
            return rows
        # A few older daemon builds advertise the extension but omit project attribution.
        # Never guess ownership; re-read each known project with an explicit query instead.

    # Compatibility fallback for older daemons. The wall deadline is checked between bounded
    # socket reads, so an unavailable project cannot create an unbounded fleet walk.
    rows: list[dict[str, Any]] = []
    for project in projects:
        if time.monotonic() >= deadline:
            raise _AuditUnavailable(f"Timed out reading {collection}")
        project_rows = _metadata(
            node.get(
                params={"recursion": 1, "project": project},
                timeout=_READ_TIMEOUT,
            ),
            collection,
        )
        for row in project_rows:
            row["project"] = project
            rows.append(row)
    return rows


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise _AuditUnavailable("Daemon resource shape is incomplete")
    return value


def _record_findings(
    counter: Counter[tuple[str, str, str]],
    scope: str,
    findings: tuple[PolicyFinding, ...],
) -> None:
    for finding in findings:
        field = finding.field
        if field.startswith("devices."):
            field = "devices"
        elif field.startswith("config.raw."):
            field = "config.raw.*"
        elif field.startswith("config.security."):
            field = "config.security.*"
        elif field.startswith("config.") and finding.code == "config_key_not_allowed":
            field = "config"
        counter[(scope, finding.code, field)] += 1


def _scan_cluster_sync(cluster_id: str, lxd_service: Any) -> SharedClusterAuditReport:
    lock = _cluster_lock(cluster_id)
    if not lock.acquire(blocking=False):
        raise _AuditUnavailable("An audit is already running for this cluster")
    try:
        client = lxd_service.connection_manager.get_client(cluster_id)
        if client is None:
            raise _AuditUnavailable("Cluster connection is unavailable")

        deadline = time.monotonic() + (_WALL_TIMEOUT_SECONDS - 2.0)
        projects = _read_projects(client)
        project_names = [
            str(row.get("name"))
            for row in projects
            if isinstance(row.get("name"), str) and row.get("name")
        ]
        if len(project_names) != len(projects):
            raise _AuditUnavailable("Project inventory is incomplete")

        profiles = _read_across_projects(
            client,
            "profiles",
            project_names,
            deadline=deadline,
        )
        instances = _read_across_projects(
            client,
            "instances",
            project_names,
            deadline=deadline,
        )

        findings: Counter[tuple[str, str, str]] = Counter()
        for row in projects:
            _record_findings(
                findings,
                "project",
                shared_project_findings(_mapping(row.get("config"))),
            )

        for row in profiles:
            _record_findings(
                findings,
                "profile",
                shared_payload_findings(
                    _mapping(row.get("config")),
                    _mapping(row.get("devices")),
                    existing_resource=True,
                ),
            )

        for row in instances:
            _record_findings(
                findings,
                "instance_local",
                shared_payload_findings(
                    _mapping(row.get("config")),
                    _mapping(row.get("devices")),
                    existing_resource=True,
                ),
            )
            _record_findings(
                findings,
                "instance_effective",
                shared_payload_findings(
                    _mapping(row.get("expanded_config")),
                    _mapping(row.get("expanded_devices")),
                    existing_resource=True,
                ),
            )

        summaries = tuple(
            SharedClusterFindingSummary(scope, code, field, count)
            for (scope, code, field), count in sorted(findings.items())
        )
        return SharedClusterAuditReport(
            cluster_id=cluster_id,
            safe=not summaries,
            complete=True,
            projects_checked=len(projects),
            profiles_checked=len(profiles),
            instances_checked=len(instances),
            findings=summaries,
        )
    finally:
        lock.release()


def _unavailable_report(cluster_id: str) -> SharedClusterAuditReport:
    return SharedClusterAuditReport(
        cluster_id=cluster_id,
        safe=False,
        complete=False,
        projects_checked=0,
        profiles_checked=0,
        instances_checked=0,
        findings=(
            SharedClusterFindingSummary(
                scope="audit",
                code="inventory_unavailable",
                field="daemon_inventory",
                count=1,
            ),
        ),
    )


def _active_write_report(cluster_id: str) -> SharedClusterAuditReport:
    return SharedClusterAuditReport(
        cluster_id=cluster_id,
        safe=False,
        complete=False,
        projects_checked=0,
        profiles_checked=0,
        instances_checked=0,
        findings=(
            SharedClusterFindingSummary(
                scope="audit",
                code="active_configuration_operation",
                field="instance_create",
                count=1,
            ),
        ),
    )


async def audit_shared_cluster(
    cluster_id: str,
    *,
    lxd_service: Optional[Any] = None,
) -> SharedClusterAuditReport:
    """Read and evaluate one full cluster without exposing its resource values."""

    if lxd_service is None:
        from app.services.lxd_service import get_lxd_service

        lxd_service = get_lxd_service()
    log_token = _cluster_log_token(cluster_id)
    try:
        return await run_blocking(
            _scan_cluster_sync,
            cluster_id,
            lxd_service,
            timeout=_WALL_TIMEOUT_SECONDS,
            label=f"shared-cluster audit [{log_token}]",
            executor=_AUDIT_EXECUTOR,
            max_in_flight=_AUDIT_WORKERS,
            concurrency_key=cluster_id,
            max_in_flight_per_key=1,
        )
    except (BlockingPoolFull, BlockingTimeout, _AuditUnavailable):
        logger.warning("Shared-cluster audit unavailable [%s]", log_token)
        return _unavailable_report(cluster_id)
    except Exception:  # noqa: BLE001 - raw daemon failures must not enter the response or logs
        logger.error("Shared-cluster audit failed [%s]", log_token)
        return _unavailable_report(cluster_id)


def _owner_identity(
    organization_id: Optional[int],
    registered_by: Optional[str],
    row_id: Optional[int] = None,
) -> str:
    if organization_id is not None:
        return f"organization:{organization_id}"
    # An unbound row is intentionally an owner identity, even if its historical record has no
    # registrant. The row id prevents two ambiguous records from collapsing into one identity.
    return f"unbound:{registered_by or f'row:{row_id or 0}'}"


async def assert_cluster_safe_to_share(
    db: AsyncSession,
    cluster_id: str,
    *,
    prospective_organization_id: Optional[int],
    prospective_registered_by: Optional[str] = None,
    force_audit: bool = False,
) -> Optional[SharedClusterAuditReport]:
    """Fail closed before adding or reconciling an additional owner identity.

    Callers must hold :func:`serialize_cluster_share` through the subsequent write. ``None``
    means the target is already present or this is the first exclusive owner, so no daemon I/O
    was needed. ``force_audit`` is used when an ambiguous NULL owner is converted to an org.
    """

    rows = (
        await db.execute(
            select(
                OrganizationCluster.id,
                OrganizationCluster.organization_id,
                OrganizationCluster.registered_by,
            ).where(OrganizationCluster.cluster_id == cluster_id)
        )
    ).all()
    existing = {
        _owner_identity(row[1], row[2], row[0])
        for row in rows
    }
    prospective = _owner_identity(
        prospective_organization_id,
        prospective_registered_by,
    )
    if not force_audit and (not existing or prospective in existing):
        return None

    # An accepted create may continue applying on the daemon after the API request releases its
    # advisory lock. Auditing the current inventory during that window could report safe and
    # then let an exclusive-owner payload land after sharing. Block without daemon I/O until
    # durable reconciliation has a terminal verdict, including for an ambiguous ``unknown``.
    active_create = (
        await db.execute(
            select(InstanceOperationJob.id)
            .where(
                InstanceOperationJob.cluster_id == cluster_id,
                InstanceOperationJob.kind == "create",
                InstanceOperationJob.status.in_(("pending", "running", "unknown")),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if active_create is not None:
        raise SharedClusterGuardError(
            "cluster_configuration_operation_active",
            (
                "A configuration operation is still active on this cluster. Retry sharing "
                "after its daemon state has been verified."
            ),
            _active_write_report(cluster_id),
            status_code=409,
        )

    report = await audit_shared_cluster(cluster_id)
    if not report.complete:
        raise SharedClusterGuardError(
            "cluster_share_audit_unavailable",
            "The cluster inventory could not be verified completely. No ownership was changed.",
            report,
            status_code=503,
        )
    if not report.safe:
        raise SharedClusterGuardError(
            "cluster_not_safe_to_share",
            "Unsafe or non-isolated resources must be remediated before this cluster can be shared.",
            report,
            status_code=409,
        )
    return report
