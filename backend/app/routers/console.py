"""
Console Router - VGA/Graphics Console API.
Provides endpoints for graphics console access, startup order, and ISO management.

LXD/Incus uses SPICE protocol for VM graphics console access.
The console data is streamed as SPICE protocol via WebSocket.

Endpoints:
- GET  /console/{instance}/capabilities - Get console capabilities
- WS   /console/{instance}/ws - WebSocket for graphics console (SPICE)
- GET  /console/startup-order - Get startup order for instances
- PUT  /console/startup-order - Update startup order
- GET  /console/{instance}/iso - Get attached ISO
- POST /console/{instance}/iso/attach - Attach ISO
- POST /console/{instance}/iso/eject - Eject ISO
- GET  /console/isos - List available ISOs
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Callable, Dict, List, Optional, TypeVar
from urllib.parse import parse_qs, unquote, urlsplit
from fastapi import APIRouter, WebSocket, Query, HTTPException, Depends, Response
from pydantic import BaseModel, Field

from app.core.cache import get_cache_manager
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.core import ws_close
from app.core.ws_auth import refusal_of, ws_user
from app.services.ws_session_guard import instance_session_check, reauthorize_while_open
from app.core.config import get_settings
from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.org_scope import (
    check_tenant_permission,
    org_scope_dependency,
    project_is_visible,
    set_org_scope_for_ws,
    verify_cluster_access,
    verify_project_access,
    verify_tenant_permission,
)
from app.services.console_service import get_console_service
from app.services.console_proxy import ConsoleProxy
from app.services.instance_power_watch import wait_for_power_down
from app.services.activity_logger import get_activity_logger
from app.services.session_tracker import get_session_tracker
from app.services.session_registry import SESSION_REGISTRY_DEGRADED_HEADER
from app.core.database import async_session_maker
from app.models.db_models import SessionAuditLog
from app.core.lxd_path import safe_segment
from app.core.lxd_names import LXD_NAME_PATTERN
from app.services.migration_fence import (
    MigrationFenceConflict,
    assert_instance_not_migration_fenced,
)

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

_ConsoleResult = TypeVar("_ConsoleResult")
_console_operation_executor = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="console_operation",
)
_CONSOLE_OPERATION_MAX_IN_FLIGHT = 8
_CONSOLE_OPERATION_TIMEOUT_SECONDS = 35.0


async def _console_io(call: Callable[[], _ConsoleResult], *, label: str) -> _ConsoleResult:
    """Run one synchronous pylxd/requests operation without parking the API event loop."""
    return await run_blocking(
        call,
        timeout=_CONSOLE_OPERATION_TIMEOUT_SECONDS,
        label=label,
        executor=_console_operation_executor,
        max_in_flight=_CONSOLE_OPERATION_MAX_IN_FLIGHT,
    )


async def _console_call(call: Callable[[], _ConsoleResult], *, label: str, write: bool = False) -> _ConsoleResult:
    """``_console_io`` for a request handler: a saturated pool or a slow cluster is a 503/504.

    note: these service calls used to be awaited inline although nothing in them awaits, so each
    one parked the whole event loop for its LXD round trips (every console, terminal and feed on
    the worker froze with it). A write that outlives its budget keeps running; the 504 says so,
    and the instance caches are cleared when it really finishes.
    """
    try:
        return await _console_io(call, label=label)
    except BlockingPoolFull as exc:
        raise HTTPException(status_code=503, detail="The console service is busy. Retry shortly.") from exc
    except BlockingTimeout as exc:
        if write:
            exc.when_finished(get_cache_manager().invalidate_instance_cache)
            raise HTTPException(
                status_code=504,
                detail="The cluster is still applying the change. Check again in a moment.",
            ) from exc
        raise HTTPException(status_code=504, detail="The cluster did not answer in time.") from exc


def _operation_targets_instance(
    operation: object,
    *,
    instance_name: str,
    project: str,
) -> bool:
    """Return whether one websocket operation belongs to exactly this instance and project.

    LXD reports operation resources as URLs such as
    ``/1.0/instances/web-01?project=customer-a``. Instance names are only unique inside a
    project, so substring matching either the description or the URL can cancel a neighbour's
    console. A missing project is LXD's default project; a non-default target must be named by
    the operation metadata or resource URL and therefore fails closed when ownership is unknown.
    """
    if not isinstance(operation, dict) or operation.get("class") != "websocket":
        return False

    resources = operation.get("resources")
    if not isinstance(resources, dict):
        return False

    operation_project = operation.get("project")
    for key in ("instances", "containers"):
        references = resources.get(key)
        if not isinstance(references, list):
            continue
        for raw_reference in references:
            parsed = urlsplit(str(raw_reference))
            segments = [unquote(part) for part in parsed.path.split("/") if part]
            if len(segments) >= 3 and segments[-2] in {"instances", "containers"}:
                resource_name = segments[-1]
            elif len(segments) == 1:
                # Some older daemons return only the resource name in this field.
                resource_name = segments[0]
            else:
                continue
            if resource_name != instance_name:
                continue

            query_project = parse_qs(parsed.query, keep_blank_values=False).get("project", [])
            resource_project = query_project[-1] if query_project else None
            if resource_project and operation_project and resource_project != operation_project:
                continue
            effective_project = resource_project or operation_project or "default"
            if effective_project == project:
                return True
    return False


def _is_console_operation(operation: object) -> bool:
    """Whether a websocket operation is a console (VGA or serial) rather than an exec shell.

    LXD describes a console operation as "Showing console" and an exec as "Executing command".
    The conflict check and kill-all are about consoles: QEMU's SPICE server takes one client at a
    time, while any number of shells coexist, and killing those ended other people's terminals.
    """
    if not isinstance(operation, dict):
        return False
    return "console" in str(operation.get("description") or "").lower()


def _operation_metadata(response: object) -> Optional[dict]:
    """Read one operation metadata object without accepting malformed daemon responses."""
    if getattr(response, "status_code", None) != 200:
        return None
    try:
        payload = response.json()
    except (AttributeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("metadata")
    return metadata if isinstance(metadata, dict) else None


def _operation_lookup_says_gone(exc: Exception) -> bool:
    """Return true only when LXD explicitly reports that an operation is absent."""
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 404 or type(exc).__name__ == "NotFound"


def _session_audit_org_conditions() -> List[object]:
    """Build SQL boundaries for the organization-wide session audit view.

    The returned expressions are shared verbatim by the count and page statements. Filtering
    after pagination would leak the pre-filter total and make short pages appear before the end
    of the result set.
    """
    from sqlalchemy import and_, false, or_
    from app.core.org_scope import (
        get_current_allowed_clusters,
        get_current_visible_projects,
        get_tenant_acl,
    )

    conditions: List[object] = []
    allowed = get_current_allowed_clusters()
    if allowed is not None:
        conditions.append(SessionAuditLog.host.in_(allowed or {"__none__"}))

    visible_projects = get_current_visible_projects()
    if visible_projects:
        restricted_hosts = tuple(visible_projects)
        project_clauses = [SessionAuditLog.host.not_in(restricted_hosts)]
        project_clauses.extend(
            and_(
                SessionAuditLog.host == host,
                SessionAuditLog.project.in_(projects or {"__none__"}),
            )
            for host, projects in visible_projects.items()
        )
        conditions.append(or_(*project_clauses))

    acl = get_tenant_acl()
    if acl is not None:
        instance_clauses = []
        for host, rules in acl.items():
            for (project, instance), permissions in rules.items():
                if "view" not in permissions:
                    continue
                parts = [SessionAuditLog.host == host]
                if project != "*":
                    parts.append(SessionAuditLog.project == project)
                if instance != "*":
                    parts.append(SessionAuditLog.instance_name == instance)
                instance_clauses.append(and_(*parts))
        conditions.append(or_(*instance_clauses) if instance_clauses else false())

    return conditions


def _verify_instance_access(
    host: str,
    project: Optional[str],
    instance_name: str,
    permission: str = "view",
) -> str:
    """Authorize a console action on ONE instance, not merely on its cluster.

    important: `verify_cluster_access` answers "may this caller reach this cluster", which for a
    tenant scoped to a single instance is true of every instance their neighbours run on it. The
    per-instance console reads were gated on that alone, so a tenant could ask whether another
    tenant's instance exists, whether it supports VGA, what ISO is attached and where it came
    from, and who is connected to it. None of that is theirs.

    Behind the same flag as the rest of the tenant ACL, because the grants it reads were pushed by
    an upstream sync long before anything enforced them and turning them all on at once is a
    deliberate act, not a side effect of a bug fix.

    important: `project` is used AS GIVEN. The helper used to normalise it with `or "default"`,
    which reintroduced the very split it exists to close: an empty `?project=` authorized
    `default` here while the service saw a falsy value and searched every project. The caller
    normalises once and passes the same value to the action, so there is exactly one project in
    play per request.
    """
    scoped_project = verify_project_access(host, project)
    # Viewing the instance is the invariant beneath every console action. Operation-specific
    # grants retain their rollout flag, but disabling it must never make an unviewable instance
    # observable or addressable.
    verify_tenant_permission(host, scoped_project, instance_name, "view")
    if permission != "view" and get_settings().tenant_perm_enforcement_enabled:
        verify_tenant_permission(host, scoped_project, instance_name, permission)
    return scoped_project


router = APIRouter(
    prefix="/console",
    tags=["Console"],
)


# ============ Request/Response Models ============

class ConsoleCapabilitiesResponse(BaseModel):
    """Response for console capabilities."""
    instance_name: str
    instance_type: str
    host: str
    project: str
    text_console: bool
    graphics_console: bool
    graphics_type: str
    graphics_address: Optional[str] = None
    graphics_port: Optional[int] = None
    audio_enabled: bool = False
    usb_redirect: bool = False
    state: str = "Unknown"  # instance state (Running, Stopped, etc.)


class ActiveConsoleSession(BaseModel):
    """Information about an active console session."""
    operation_id: str
    instance_name: str
    description: str
    created_at: str
    may_cancel: bool = True


class ActiveConsoleCheckResponse(BaseModel):
    """Response for checking active console sessions."""
    has_active_session: bool
    sessions: List[ActiveConsoleSession] = []


class StartupOrderEntry(BaseModel):
    """Single entry in startup order."""
    instance_name: str
    host: str
    project: str
    priority: int
    delay: int = 0
    autostart: bool = True
    instance_type: str = "container"
    status: str = "Unknown"


class StartupOrderResponse(BaseModel):
    """Response for startup order."""
    success: bool
    entries: List[StartupOrderEntry] = []
    error: Optional[str] = None


class StartupOrderUpdate(BaseModel):
    """Single startup order update."""
    instance_name: str
    priority: Optional[int] = None
    delay: Optional[int] = None
    autostart: Optional[bool] = None


class StartupOrderUpdateRequest(BaseModel):
    """Request to update startup order."""
    updates: List[StartupOrderUpdate]


class ISOInfo(BaseModel):
    """Information about an available ISO."""
    name: str
    pool: str
    volume: str
    path: str
    size: int = 0
    created_at: Optional[str] = None


class ISOListResponse(BaseModel):
    """Response for ISO list."""
    success: bool
    isos: List[ISOInfo] = []
    error: Optional[str] = None


class AttachedISOResponse(BaseModel):
    """Response for attached ISO."""
    success: bool
    attached: bool = False
    device_name: Optional[str] = None
    source: Optional[str] = None
    pool: Optional[str] = None
    boot_priority: int = 0
    error: Optional[str] = None


class AttachISORequest(BaseModel):
    """Request to attach ISO."""
    pool: str
    volume: str
    device_name: str = "cdrom"
    boot_priority: int = 0


class ActionResponse(BaseModel):
    """Generic action response."""
    success: bool
    message: Optional[str] = None
    error: Optional[str] = None


class SessionBandwidth(BaseModel):
    """Bandwidth stats for a session."""
    download_bytes: int = Field(0, description="Total bytes received by user")
    upload_bytes: int = Field(0, description="Total bytes sent by user")
    download_rate_bps: float = Field(0.0, description="Current download rate in bytes/second")
    upload_rate_bps: float = Field(0.0, description="Current upload rate in bytes/second")


class SessionUser(BaseModel):
    """User info for a session."""
    id: str = Field("anonymous", description="User ID")
    email: str = Field("", description="User email")
    role: str = Field("unknown", description="User access role")


class ConsoleSessionStats(BaseModel):
    """Real-time statistics for an active console/terminal session."""
    session_id: str = Field(..., description="Unique session identifier")
    instance_name: str = Field(..., description="Instance name")
    host: str = Field(..., description="Cluster")
    project: str = Field("default", description="LXD project")
    protocol: str = Field(..., description="Protocol type (websocket-terminal / spice-graphics)")
    started_at: float = Field(..., description="Session start time (Unix timestamp)")
    duration_seconds: float = Field(0.0, description="Session duration in seconds")
    user: SessionUser = Field(default_factory=SessionUser, description="User info")
    bandwidth: SessionBandwidth = Field(default_factory=SessionBandwidth, description="Bandwidth stats")
    connection_status: str = Field("connected", description="Connection status")
    target_org_slug: Optional[str] = Field(None, description="Organization owning the target cluster")
    is_break_glass: bool = Field(False, description="Session opened under a break-glass grant")


# ============ Endpoints ============

@router.get(
    "/session/{session_id}/stats",
    response_model=ConsoleSessionStats,
    summary="Get console/terminal session stats",
    description="Get real-time bandwidth, duration, and user info for an active session.",
    dependencies=[Depends(org_scope_dependency)],
)
async def get_session_stats(
    session_id: str,
    response: Response,
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """Get real-time session stats - scoped to the authenticated user's own session."""
    # important: a caller we cannot identify cannot be scoped, so refuse rather than
    # guess. The email claim is not required by validate_jwt, so this is reachable.
    if not _rbac.email:
        raise HTTPException(status_code=403, detail="Token carries no email claim")

    tracker = get_session_tracker()
    # across all workers, not just whichever one answered this request: the socket is
    # pinned to one process and this poll is not, which is why it used to 404 at random.
    session, degraded = await tracker.resolve(session_id)
    if degraded:
        response.headers[SESSION_REGISTRY_DEGRADED_HEADER] = "1"

    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    # enforce user-scoped access (users can only view their own sessions)
    if session.user_email and session.user_email != _rbac.email:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    data = session.to_dict()
    return ConsoleSessionStats(
        session_id=data["session_id"],
        instance_name=data["instance_name"],
        host=data["host"],
        project=data["project"],
        protocol=data["protocol"],
        started_at=data["started_at"],
        duration_seconds=data["duration_seconds"],
        target_org_slug=data["target_org_slug"],
        is_break_glass=data["is_break_glass"],
        user=SessionUser(**data["user"]),
        bandwidth=SessionBandwidth(**data["bandwidth"]),
        connection_status=data["connection_status"],
    )


@router.get(
    "/sessions/active",
    summary="List all active tracked sessions",
    description="Get all active console/terminal sessions being tracked.",
    dependencies=[Depends(org_scope_dependency)],
)
async def list_active_tracked_sessions(
    response: Response,
    instance_name: Optional[str] = Query(None, description="Filter by instance name"),
    host: Optional[str] = Query(None, description="Filter by cluster"),
    scope: str = Query("self", description="self = your own sessions (default); org = every session on your organization's clusters"),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """
    List active console/terminal sessions.

    scope=self (default): the caller's own sessions only (data minimization).
    scope=org: every live session on clusters in the caller's scope, including a
    platform admin's break-glass access. Isolation is the caller's own
    get_user_allowed_clusters set, mirroring /sessions/audit exactly so there is one
    authorization model here rather than two that can drift apart.
    """
    # important: a caller with no email cannot be scoped to "their own" sessions. This
    # used to pass None straight into a filter that read None as "no filter" and handed
    # back every session on the worker, across tenants. Refuse instead.
    if not _rbac.email:
        raise HTTPException(status_code=403, detail="Token carries no email claim")

    if host:
        verify_cluster_access(host)

    allowed_clusters = None
    user_email = _rbac.email
    if scope == "org":
        # important: the REQUEST-scoped set, not a fresh get_user_allowed_clusters(). That
        # function returns only the org-derived clusters; org_scope_dependency then narrows
        # it by the tenant ACL and stores the result in the context var, which is the set
        # verify_cluster_access actually enforces. Calling the function directly skips the
        # ACL layer, and a live QA run caught exactly that: qa-tenant is a member of the org
        # owning nuc2 but is narrowed off it by ACL, and saw an admin's break-glass session
        # on a cluster it cannot reach. Read the enforced set, never recompute a second one.
        from app.core.org_scope import get_current_allowed_clusters
        allowed = get_current_allowed_clusters()
        # admin -> None -> unrestricted. An empty set means the caller owns nothing, which
        # must match nothing rather than falling open to everything.
        allowed_clusters = None if allowed is None else (allowed or {"__none__"})
        user_email = None

    tracker = get_session_tracker()
    sessions, degraded = await tracker.list_active(
        instance_name=instance_name,
        host=host,
        user_email=user_email,
        allowed_clusters=allowed_clusters,
    )
    if degraded:
        # the shared view is unavailable, so this is one worker's slice of the truth.
        # Say so rather than let the UI present a short list as the whole picture.
        response.headers[SESSION_REGISTRY_DEGRADED_HEADER] = "1"
    if scope == "org":
        # Cluster membership alone is too broad on a cluster shared by organizations. The
        # session row carries its concrete project and instance, so apply the same two gates
        # as the instance detail route before serializing operation ids or actor metadata.
        sessions = [
            session
            for session in sessions
            if project_is_visible(session.host, session.project)
            and check_tenant_permission(
                session.host,
                session.project,
                session.instance_name,
                "view",
            )
        ]
    return [s.to_dict() for s in sessions]


@router.get(
    "/sessions/audit",
    summary="Query session audit logs",
    description="Get historical session audit logs for compliance and analytics.",
    dependencies=[Depends(org_scope_dependency)],
)
async def query_session_audit_logs(
    instance_name: Optional[str] = Query(None, description="Filter by instance name"),
    event_type: Optional[str] = Query(None, description="Filter by event type (connect/disconnect)"),
    scope: str = Query("self", description="self = own sessions (default); org = all sessions on your organization's clusters (incl. admin break-glass)"),
    limit: int = Query(100, ge=1, le=1000, description="Page size"),
    offset: int = Query(0, ge=0, description="Rows to skip, for paging"),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """
    Query session audit logs, one page at a time.

    scope=self (default): the caller's own sessions only (data minimization).
    scope=org: every session on clusters in the caller's organization scope, including
    a platform admin's break-glass console/terminal access - the tenant-visible
    accountability trail. Cluster, Partner project, and Tenant instance boundaries are
    applied to both the count and page query, so no other organization's sessions leak.

    important: `limit` used to truncate with no way past it. The trail held 660 rows while the
    page asked for 500, so the oldest 160 were unreachable and the pager in the UI was paging a
    set it had already lost the tail of. An audit trail that silently drops its oldest entries
    is the one kind of list where truncation is not a display choice. `total` is counted over
    the same filters as the page, so a caller can tell a short last page from a lost one.
    """
    from sqlalchemy import select, desc, func

    if not _rbac.email:
        raise HTTPException(status_code=403, detail="Token carries no email claim")

    async with async_session_maker() as db:
        # Built as a list so the count and the page cannot drift onto different filters, which
        # would report a total the caller can never page to.
        conditions = []

        if scope == "org":
            conditions.extend(_session_audit_org_conditions())
        else:
            # default: own sessions only (data minimization / GDPR)
            conditions.append(SessionAuditLog.user_email == _rbac.email)

        if instance_name:
            conditions.append(SessionAuditLog.instance_name == instance_name)
        if event_type:
            conditions.append(SessionAuditLog.event_type == event_type)

        total = (
            await db.execute(
                select(func.count()).select_from(SessionAuditLog).where(*conditions)
            )
        ).scalar_one()

        query = (
            select(SessionAuditLog)
            .where(*conditions)
            .order_by(desc(SessionAuditLog.created_at))
            .offset(offset)
            .limit(limit)
        )
        result = await db.execute(query)
        logs = result.scalars().all()

        rows = [
            {
                "id": log.id,
                "session_id": log.session_id,
                "event_type": log.event_type,
                "instance_name": log.instance_name,
                "host": log.host,
                "project": log.project,
                "protocol": log.protocol,
                "user_id": log.user_id,
                "user_email": log.user_email,
                "user_role": log.user_role,
                "duration_seconds": log.duration_seconds,
                "download_bytes": log.download_bytes,
                "upload_bytes": log.upload_bytes,
                "target_organization_id": log.target_organization_id,
                "target_org_slug": log.target_org_slug,
                "break_glass_grant_id": log.break_glass_grant_id,
                "is_break_glass": log.break_glass_grant_id is not None,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            }
            for log in logs
        ]

        return {
            "logs": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        }


@router.get(
    "/{instance_name}/capabilities",
    response_model=ConsoleCapabilitiesResponse,
    summary="Get console capabilities",
    description="Get available console types and features for an instance.",
    dependencies=[Depends(org_scope_dependency)],
)
async def get_capabilities(
    instance_name: str,
    host: str = Query(..., description="Target cluster"),
    project: str = Query("default", description="LXD project name"),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """Get console capabilities for an instance."""
    project = _verify_instance_access(host, project, instance_name, "view")
    console_service = get_console_service()
    
    caps, error = await _console_call(
        partial(
            console_service.get_instance_capabilities,
            instance_name=instance_name,
            host=host,
            project=project,
        ),
        label=f"console.capabilities:{host}",
    )
    
    if error:
        raise HTTPException(status_code=400, detail=error)
    
    return ConsoleCapabilitiesResponse(**caps.to_dict())


# note: `GET /console/{name}/session` used to live here. It created a real VGA operation and, since
# its secrets could not be handed out, returned only the operation id, so nothing could ever
# connect to what it made: every call left an orphaned operation on the cluster. No caller existed
# (frontend, MCP or integrations). The console runs on the WebSocket below, which creates its
# own operation and never discloses it. Removed for the same reason as its terminal twin.


@router.get(
    "/startup-order",
    response_model=StartupOrderResponse,
    summary="Get startup order",
    description="Get startup order for all instances in a cluster and project.",
    dependencies=[Depends(org_scope_dependency)],
)
async def get_startup_order(
    host: str = Query(..., description="Target cluster"),
    project: str = Query("default", description="LXD project name"),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """Get startup order for instances."""
    project = verify_project_access(host, project)
    console_service = get_console_service()
    
    entries, error = await _console_call(
        partial(
            console_service.get_startup_order,
            host=host,
            project=project,
        ),
        label=f"console.startup_order.read:{host}",
    )
    
    if error:
        return StartupOrderResponse(success=False, error=error)

    # important: this is a PROJECT-wide listing, and the cluster check above says nothing about
    # which instances in it are the caller's. On a cluster where several tenants share a project
    # it returned the full roster: every neighbour's instance name, its boot order and its delay.
    # Admin and Partner carry no ACL, so this is a no-op for an operator.
    # important: narrowed by the permission the caller actually holds, not by "has any rule here".
    # The helper this replaces ignores the permissions on a rule entirely, so a tenant holding only
    # `console` on one instance was still shown every instance they hold any rule for. Asking the
    # same question the per-instance routes ask is what makes the two agree. Admin and Partner
    # carry no ACL and check_tenant_permission answers True for them, so an operator read is
    # unfiltered.
    scoped = project
    rows = [StartupOrderEntry(**e.to_dict()) for e in entries]
    seen: Dict[str, bool] = {}
    visible = []
    for r in rows:
        if r.instance_name not in seen:
            seen[r.instance_name] = check_tenant_permission(host, scoped, r.instance_name, "view")
        if seen[r.instance_name]:
            visible.append(r)

    return StartupOrderResponse(success=True, entries=visible)


@router.put(
    "/startup-order",
    response_model=ActionResponse,
    summary="Update startup order",
    description="Update startup order for multiple instances.",
    dependencies=[Depends(org_scope_dependency)],
)
async def update_startup_order(
    request: StartupOrderUpdateRequest,
    host: str = Query(..., description="Target cluster"),
    project: str = Query("default", description="LXD project name"),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Update startup order for instances."""
    project = verify_project_access(host, project)
    for update in request.updates:
        try:
            await assert_instance_not_migration_fenced(
                cluster_id=host,
                project=project,
                instance_name=update.instance_name,
            )
        except MigrationFenceConflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
    console_service = get_console_service()
    
    updates = [u.model_dump(exclude_none=True) for u in request.updates]
    
    success, error = await _console_call(
        partial(
            console_service.update_startup_order,
            host=host,
            project=project,
            updates=updates,
        ),
        label=f"console.startup_order.write:{host}",
        write=True,
    )
    
    if not success:
        return ActionResponse(success=False, error=error)
    
    return ActionResponse(success=True, message="Startup order updated successfully")


@router.get(
    "/isos",
    response_model=ISOListResponse,
    summary="List available ISOs",
    description="Discover available ISO files from storage pools.",
    dependencies=[Depends(org_scope_dependency)],
)
async def list_isos(
    host: str = Query(..., pattern=LXD_NAME_PATTERN, description="Target cluster"),
    project: str = Query("default", pattern=LXD_NAME_PATTERN, description="Hypervisor project name"),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """List available ISO files."""
    project = verify_project_access(host, project)
    console_service = get_console_service()
    
    try:
        isos, error = await _console_io(
            partial(console_service.get_available_isos, host=host, project=project),
            label="console.iso.discovery",
        )
    except (BlockingTimeout, BlockingPoolFull) as exc:
        raise HTTPException(status_code=503, detail="ISO discovery is temporarily unavailable. Retry later.") from exc
    
    if error:
        return ISOListResponse(success=False, error=error)
    
    return ISOListResponse(
        success=True,
        isos=[ISOInfo(**iso.to_dict()) for iso in isos]
    )


@router.get(
    "/{instance_name}/iso",
    response_model=AttachedISOResponse,
    summary="Get attached ISO",
    description="Get currently attached ISO for an instance.",
    dependencies=[Depends(org_scope_dependency)],
)
async def get_attached_iso(
    instance_name: str,
    host: str = Query(..., description="Target cluster"),
    project: str = Query("default", description="LXD project name"),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """Get attached ISO for an instance."""
    project = _verify_instance_access(host, project, instance_name, "view")
    console_service = get_console_service()
    
    attached, error = await _console_call(
        partial(
            console_service.get_instance_iso,
            instance_name=instance_name,
            host=host,
            project=project,
        ),
        label=f"console.iso.read:{host}",
    )
    
    if error:
        return AttachedISOResponse(success=False, error=error)
    
    if attached:
        return AttachedISOResponse(
            success=True,
            attached=True,
            device_name=attached.device_name,
            source=attached.source,
            pool=attached.pool,
            boot_priority=attached.boot_priority,
        )
    
    return AttachedISOResponse(success=True, attached=False)


@router.post(
    "/{instance_name}/iso/attach",
    response_model=ActionResponse,
    summary="Attach ISO",
    description="Attach an ISO to a VM instance.",
    dependencies=[Depends(org_scope_dependency)],
)
async def attach_iso(
    instance_name: str,
    request: AttachISORequest,
    host: str = Query(..., description="Target cluster"),
    project: str = Query("default", description="LXD project name"),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Attach an ISO to an instance."""
    project = _verify_instance_access(host, project, instance_name, "operate")
    try:
        await assert_instance_not_migration_fenced(
            cluster_id=host,
            project=project,
            instance_name=instance_name,
        )
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    console_service = get_console_service()
    
    success, message = await _console_call(
        partial(
            console_service.attach_iso,
            instance_name=instance_name,
            host=host,
            project=project,
            pool=request.pool,
            volume=request.volume,
            device_name=request.device_name,
            boot_priority=request.boot_priority,
        ),
        label=f"console.iso.attach:{host}",
        write=True,
    )
    
    if not success:
        return ActionResponse(success=False, error=message)
    
    # the ISO volume's used_by and the instance's devices both changed; the ISO page reads the
    # former before letting an operator delete the image
    get_cache_manager().invalidate_instance_cache()
    return ActionResponse(success=True, message=message)


@router.post(
    "/{instance_name}/iso/eject",
    response_model=ActionResponse,
    summary="Eject ISO",
    description="Eject (remove) an ISO from a VM instance.",
    dependencies=[Depends(org_scope_dependency)],
)
async def eject_iso(
    instance_name: str,
    host: str = Query(..., description="Target cluster"),
    project: str = Query("default", description="LXD project name"),
    device_name: str = Query("cdrom", description="Device name to eject"),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Eject an ISO from an instance."""
    project = _verify_instance_access(host, project, instance_name, "operate")
    try:
        await assert_instance_not_migration_fenced(
            cluster_id=host,
            project=project,
            instance_name=instance_name,
        )
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    console_service = get_console_service()
    
    success, message = await _console_call(
        partial(
            console_service.eject_iso,
            instance_name=instance_name,
            host=host,
            project=project,
            device_name=device_name,
        ),
        label=f"console.iso.eject:{host}",
        write=True,
    )
    
    if not success:
        return ActionResponse(success=False, error=message)
    
    get_cache_manager().invalidate_instance_cache()
    return ActionResponse(success=True, message=message)


# ============ Active Session Detection ============

@router.get(
    "/{instance_name}/active-sessions",
    response_model=ActiveConsoleCheckResponse,
    summary="Check active console sessions",
    description="Check if there are active console operations for this instance.",
    dependencies=[Depends(org_scope_dependency)],
)
async def check_active_sessions(
    instance_name: str,
    host: str = Query(..., description="Target cluster"),
    project: str = Query("default", description="LXD project name"),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """
    Check for active console operations for an instance.
    
    Returns list of active console sessions that might conflict.
    Useful for warning users before opening a new console.
    """
    project = _verify_instance_access(host, project, instance_name, "view")
    from app.services.lxd_service import get_lxd_service
    
    lxd_service = get_lxd_service()
    active_sessions: List[ActiveConsoleSession] = []
    
    try:
        client = await _console_io(
            partial(lxd_service.connection_manager.get_client, host),
            label=f"console.operations.connect:{host}",
        )
        if not client:
            raise HTTPException(status_code=503, detail="The cluster did not answer")
        
        # Get running operations
        response = await _console_io(
            partial(
                client.api.operations.get,
                params={"recursion": "1", "project": project},
                timeout=(5, 30),
            ),
            label=f"console.operations.list:{host}",
        )
        if response.status_code == 200:
            operations_data = response.json().get('metadata', {})
            running_ops = operations_data.get('running', [])
            
            for op in running_ops:
                if _operation_targets_instance(
                    op,
                    instance_name=instance_name,
                    project=project,
                ) and _is_console_operation(op):
                    active_sessions.append(ActiveConsoleSession(
                        operation_id=op.get('id', ''),
                        instance_name=instance_name,
                        description=op.get('description', ''),
                        created_at=op.get('created_at', ''),
                        may_cancel=op.get('may_cancel', True),
                    ))
        
        return ActiveConsoleCheckResponse(
            has_active_session=len(active_sessions) > 0,
            sessions=active_sessions
        )
        
    except HTTPException:
        raise
    except (BlockingTimeout, BlockingPoolFull) as exc:
        logger.warning("Console session lookup temporarily unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Console session lookup is temporarily unavailable",
        )
    except Exception as exc:
        logger.error("Error checking active sessions: %s", exc)
        raise HTTPException(status_code=503, detail="Could not read console sessions")


@router.post(
    "/{instance_name}/kill-all-sessions",
    response_model=ActionResponse,
    summary="Kill all active console sessions",
    description="Kill the console operations (VGA and serial) of an instance; exec shells are left alone.",
    dependencies=[Depends(org_scope_dependency)],
)
async def kill_all_sessions(
    instance_name: str,
    host: str = Query(..., description="Target cluster"),
    project: str = Query("default", description="LXD project name"),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Kill all active console sessions for an instance."""
    project = verify_project_access(host, project)
    from app.services.lxd_service import get_lxd_service
    
    lxd_service = get_lxd_service()
    killed_count = 0
    
    try:
        client = await _console_io(
            partial(lxd_service.connection_manager.get_client, host),
            label=f"console.operations.connect:{host}",
        )
        if not client:
            return ActionResponse(
                success=False,
                error=f"Cannot connect to cluster '{host}'",
            )
        
        # Get running operations
        response = await _console_io(
            partial(
                client.api.operations.get,
                params={"recursion": "1", "project": project},
                timeout=(5, 30),
            ),
            label=f"console.operations.list:{host}",
        )
        failed_count = 0
        if response.status_code == 200:
            operations_data = response.json().get('metadata', {})
            running_ops = operations_data.get('running', [])
            
            for op in running_ops:
                if _operation_targets_instance(
                    op,
                    instance_name=instance_name,
                    project=project,
                ) and _is_console_operation(op):
                    op_id = op.get('id', '')
                    if not op_id:
                        continue
                    try:
                        # Try to delete/cancel the operation
                        deleted = await _console_io(
                            partial(
                                client.api.operations[safe_segment(op_id)].delete,
                                params={"project": project},
                                timeout=(5, 30),
                            ),
                            label=f"console.operations.cancel:{host}",
                        )
                        if deleted.status_code in (200, 202):
                            killed_count += 1
                            logger.info(
                                f"Killed console operation {op_id} for {instance_name}@{host}"
                            )
                        else:
                            failed_count += 1
                            logger.warning(
                                "Console operation %s refused cancellation with HTTP %s",
                                op_id,
                                deleted.status_code,
                            )
                    except Exception as e:
                        failed_count += 1
                        logger.warning(f"Failed to kill operation {op_id}: {e}")

        if response.status_code != 200:
            return ActionResponse(
                success=False,
                error=f"Could not list console sessions (HTTP {response.status_code})",
            )
        if failed_count:
            return ActionResponse(
                success=False,
                error=(
                    f"Killed {killed_count} console session(s), but {failed_count} "
                    "session(s) could not be terminated"
                ),
            )
        return ActionResponse(
            success=True, 
            message=f"Killed {killed_count} console session(s) for {instance_name}"
        )
        
    except Exception as e:
        logger.error(f"Error killing all sessions: {e}")
        return ActionResponse(success=False, error=str(e))


@router.delete(
    "/{instance_name}/active-sessions/{operation_id}",
    response_model=ActionResponse,
    summary="Kill active console session",
    description="Cancel an active console operation.",
    dependencies=[Depends(org_scope_dependency)],
)
async def kill_active_session(
    instance_name: str,
    operation_id: str,
    host: str = Query(..., description="Target cluster"),
    project: str = Query("default", description="LXD project name"),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Kill an active console session by cancelling the operation."""
    project = verify_project_access(host, project)
    from app.services.lxd_service import get_lxd_service
    
    lxd_service = get_lxd_service()
    
    try:
        client = await _console_io(
            partial(lxd_service.connection_manager.get_client, host),
            label=f"console.operations.connect:{host}",
        )
        if not client:
            return ActionResponse(
                success=False,
                error=f"Cannot connect to cluster '{host}'",
            )
        
        # An operation id is opaque and cluster-wide. Read and verify its metadata before
        # cancellation so a caller cannot pair an allowed instance name with another project's
        # operation id. The DELETE repeats the project scope as defence in depth.
        endpoint = client.api.operations[safe_segment(operation_id)]
        metadata = _operation_metadata(
            await _console_io(
                partial(
                    endpoint.get,
                    params={"project": project},
                    timeout=(5, 30),
                ),
                label=f"console.operations.get:{host}",
            )
        )
        metadata_id = metadata.get("id") if metadata else None
        if (
            not metadata
            or (metadata_id and metadata_id != operation_id)
            or not _operation_targets_instance(
                metadata,
                instance_name=instance_name,
                project=project,
            )
        ):
            raise HTTPException(status_code=404, detail="Console session not found")

        # For websocket operations, use direct API DELETE. operation.cancel() does not work
        # for websocket-class operations.
        try:
            response = await _console_io(
                partial(
                    endpoint.delete,
                    params={"project": project},
                    timeout=(5, 30),
                ),
                label=f"console.operations.cancel:{host}",
            )
            if response.status_code in (200, 202):
                logger.info(f"Deleted console operation {operation_id} for {instance_name}@{host}")
                return ActionResponse(success=True, message=f"Session {operation_id[:8]}... terminated")
        except Exception as del_err:
            logger.debug(f"DELETE request returned: {del_err}")
        
        # Check if the operation still exists
        await asyncio.sleep(0.3)  # Small delay for operation cleanup
        try:
            remaining = await _console_io(
                partial(
                    endpoint.get,
                    params={"project": project},
                    timeout=(5, 30),
                ),
                label=f"console.operations.verify:{host}",
            )
            if remaining.status_code != 404:
                return ActionResponse(
                    success=False,
                    error="Failed to terminate session - operation still exists",
                )
            return ActionResponse(success=True, message=f"Session {operation_id[:8]}... terminated")
        except Exception as verify_err:
            if _operation_lookup_says_gone(verify_err):
                return ActionResponse(
                    success=True,
                    message=f"Session {operation_id[:8]}... terminated",
                )
            # A timeout or broken connection proves nothing about the operation's state. Do not
            # tell the operator it was terminated when the verification itself never answered.
            logger.warning(
                "Could not verify console operation %s after cancellation: %s",
                operation_id,
                verify_err,
            )
            return ActionResponse(
                success=False,
                error="Could not verify whether the session was terminated",
            )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error killing session: {e}")
        return ActionResponse(success=False, error=str(e))


# ============ WebSocket Endpoint ============

@router.websocket("/{instance_name}/ws")
async def console_websocket(
    websocket: WebSocket,
    instance_name: str,
    host: str = Query(..., description="Target cluster"),
    project: str = Query("default", description="LXD project name"),
    console_type: str = Query("vga", description="Console type: vga or console"),
    user: Optional[AuthenticatedUser] = Depends(ws_user("console")),
):
    """
    WebSocket for an instance console: the SPICE display (``vga``) or the serial console (``console``).

    A SPICE display opens one socket per channel, each with its own ticket from POST /ws-tickets;
    the tickets of one display share a group, and its channels share one LXD operation. Binary
    frames carry the protocol; a serial console also takes JSON text frames for resize and ACKs.
    Re-authorized every minute while open.
    """
    # Validate auth when enabled
    from app.core.config import get_settings as _gs
    _settings = _gs()
    # important: gated on auth_enabled ALONE. This used to also require
    # `_settings.authentik_issuer`, which is the variable whose absence produces the
    # unauthenticated `user`, so the guard switched itself off in exactly the situation it
    # existed to catch. ws_user now refuses a misconfigured server itself.
    if _settings.auth_enabled and user is None:
        # note: every refusal below accepts first (core/ws_close.py). A close before accept is an
        # HTTP 403 to the browser, which reports 1006 with no reason and never recovers.
        await ws_close.refuse(websocket, *refusal_of(websocket))
        return
    # After the caller is known: an anonymous client learns nothing here it could not from 4401.
    # A ticket names its console type, so a ticket for another type was already refused above.
    if console_type not in ("vga", "console"):
        await ws_close.refuse(websocket, ws_close.POLICY, "unknown_console_type")
        return

    # set org scope for WebSocket (context variable)
    await set_org_scope_for_ws(user, interactive=True)

    # important: the REST console path checks this and the WebSocket did not, so the same
    # session opened over a socket skipped the organisation boundary entirely. Refusing rather
    # than raising, since HTTPException is meaningless once the socket is being negotiated.
    try:
        project = _verify_instance_access(host, project, instance_name, "console")
    except HTTPException as exc:
        await ws_close.refuse(websocket, ws_close.FORBIDDEN, "forbidden")
        logger.warning(
            "Console WS blocked (%s): %s@%s user=%s",
            exc.detail, instance_name, host, getattr(user, "email", "?"),
        )
        return

    # Break-glass gate (defense in depth - a client can hit the WS directly). An admin
    # reaching a tenant cluster they do not own must hold an active grant; otherwise close.
    # Dev mode (no user / auth disabled) skips the gate inside enforce_sensitive_access.
    _bg_grant_id: Optional[int] = None
    _target_org_id: Optional[int] = None
    _target_org_slug: Optional[str] = None
    if user is not None:
        from app.services.break_glass_service import enforce_sensitive_access
        from app.core.org_scope import resolve_cluster_org
        from app.core.database import async_session_maker
        async with async_session_maker() as _bg_db:
            try:
                _grant = await enforce_sensitive_access(user, host, _bg_db)
            except HTTPException:
                await ws_close.refuse(websocket, ws_close.BREAK_GLASS, "break_glass_required")
                logger.warning(f"Console WS blocked (break_glass_required): {instance_name}@{host} user={user.email}")
                return
            _bg_grant_id = _grant.id if _grant else None
            _target_org_id, _target_org_slug = await resolve_cluster_org(host, _bg_db)

    try:
        await assert_instance_not_migration_fenced(
            cluster_id=host,
            project=project,
            instance_name=instance_name,
        )
    except MigrationFenceConflict:
        await ws_close.refuse(websocket, ws_close.FENCED, "migration_fenced")
        return

    # spice-html5 opens its sockets with the 'binary' subprotocol and fails one that does not
    # echo it; a serial client asks for none, and answering one it did not offer fails it too.
    offered = websocket.scope.get("subprotocols") or []
    await websocket.accept(subprotocol="binary" if "binary" in offered else None)

    # Extract user details for session tracking
    user_id = user.user_id if user else "anonymous"
    user_email = user.email if user else ""
    # resolve via RBAC to get canonical role name (admin/partner/tenant)
    if user:
        from app.core.rbac import resolve_roles, get_highest_role
        _roles = resolve_roles(user)
        user_role = get_highest_role(_roles).value
    else:
        user_role = "unknown"
    logger.info(f"Console WebSocket connected: {instance_name}@{host} user={user_id}")

    proxy = ConsoleProxy(
        browser_ws=websocket,
        instance_name=instance_name,
        host=host,
        project=project,
        console_type=console_type,
        user_id=user_id,
        user_email=user_email,
        user_role=user_role,
        target_organization_id=_target_org_id,
        target_org_slug=_target_org_slug,
        break_glass_grant_id=_bg_grant_id,
        group=getattr(websocket.state, "ws_ticket_group", None),
        share_operation=_settings.console_shared_spice_operation,
        watch_power=wait_for_power_down,
    )
    
    # A break-glass session must not outlive the grant that authorized it: the gate
    # above runs once, at connect. The watchdog closes the socket when the grant
    # expires or is revoked mid-session.
    _bg_watch = None
    if _bg_grant_id:
        from app.services.break_glass_service import watch_grant_while_open
        _bg_watch = asyncio.create_task(watch_grant_while_open(_bg_grant_id, websocket))
    check = instance_session_check(host, project, instance_name)
    reauthorize = asyncio.create_task(reauthorize_while_open(websocket, user, check, label=f"console {instance_name}@{host}"))
    try:
        await proxy.run()
    finally:
        reauthorize.cancel()
        if _bg_watch:
            _bg_watch.cancel()
    
    logger.info(f"Console WebSocket closed: {instance_name}@{host}")
