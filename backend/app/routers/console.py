"""
Console Router - VGA/Graphics Console API.
Provides endpoints for graphics console access, startup order, and ISO management.

LXD/Incus uses SPICE protocol for VM graphics console access.
The console data is streamed as SPICE protocol via WebSocket.

Endpoints:
- GET  /console/{instance}/capabilities - Get console capabilities
- GET  /console/{instance}/session - Create console session
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
from typing import Optional, List
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException, Depends, Response
from pydantic import BaseModel, Field
import websockets
from websockets import State as WebSocketState
from websockets.exceptions import ConnectionClosed

from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser, get_ws_user
from app.core.org_scope import org_scope_dependency, set_org_scope_for_ws, verify_cluster_access
from app.core.session_scope import audit_scope_clause, visible_session_rows
from app.core.instance_access import (
    can_view_instance,
    operation_targets,
    verify_instance_view_access,
    verify_project_access,
)
from app.services.console_service import get_console_service, public_session_info
from app.services.activity_logger import get_activity_logger
from app.services.session_tracker import get_session_tracker, SessionProtocol
from app.services.session_models import new_session_id
from app.services.session_registry import SESSION_REGISTRY_DEGRADED_HEADER
from app.core.database import async_session_maker
from app.models.db_models import SessionAuditLog
from app.core.lxd_names import safe_segment
from app.services.lxd.utils import read_running_operations

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

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


class ConsoleSessionResponse(BaseModel):
    """Response for console session creation."""
    success: bool
    session: Optional[dict] = None
    error: Optional[str] = None


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
    host: str = Field(..., description="Host node")
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
    host: Optional[str] = Query(None, description="Filter by host node"),
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
    if scope == "org":
        sessions = visible_session_rows(sessions)
    if degraded:
        # the shared view is unavailable, so this is one worker's slice of the truth.
        # Say so rather than let the UI present a short list as the whole picture.
        response.headers[SESSION_REGISTRY_DEGRADED_HEADER] = "1"
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
    limit: int = Query(100, ge=1, le=1000, description="Max results to return"),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """
    Query session audit logs.

    scope=self (default): the caller's own sessions only (data minimization).
    scope=org: every session on clusters in the caller's organization scope, including
    a platform admin's break-glass console/terminal access - the tenant-visible
    accountability trail. Isolation is the caller's own get_user_allowed_clusters set,
    so no other organization's sessions leak.
    """
    from sqlalchemy import select, desc
    from app.core.org_scope import get_current_allowed_clusters

    async with async_session_maker() as db:
        query = select(SessionAuditLog).order_by(desc(SessionAuditLog.created_at))

        if scope == "org":
            # important: the request-scoped set, which org_scope_dependency has already
            # narrowed by the tenant ACL. This previously called get_user_allowed_clusters
            # directly, which returns the org-derived clusters WITHOUT that narrowing, so a
            # tenant scoped off a cluster by ACL could still read its audit trail. Nothing
            # in the UI sent scope=org before, so it was unreachable; the Sessions page now
            # does. Same set verify_cluster_access enforces, computed once.
            allowed = get_current_allowed_clusters()
            if allowed is not None:
                # the caller's own clusters, and on a shared one only the projects in its bound
                query = query.where(audit_scope_clause(SessionAuditLog, allowed))
            # admin (allowed is None): no cluster filter
        else:
            # default: own sessions only (data minimization / GDPR)
            query = query.where(SessionAuditLog.user_email == _rbac.email)

        if instance_name:
            query = query.where(SessionAuditLog.instance_name == instance_name)
        if event_type:
            query = query.where(SessionAuditLog.event_type == event_type)

        query = query.limit(limit)
        result = await db.execute(query)
        logs = result.scalars().all()
        if scope == "org":
            logs = visible_session_rows(logs)

        return [
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


@router.get(
    "/{instance_name}/capabilities",
    response_model=ConsoleCapabilitiesResponse,
    summary="Get console capabilities",
    description="Get available console types and features for an instance.",
    dependencies=[Depends(org_scope_dependency)],
)
async def get_capabilities(
    instance_name: str,
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """Get console capabilities for an instance."""
    verify_cluster_access(host)
    project = verify_instance_view_access(host, project, instance_name)
    console_service = get_console_service()
    
    caps, error = await console_service.get_instance_capabilities(
        instance_name=instance_name,
        host=host,
        project=project,
    )
    
    if error:
        raise HTTPException(status_code=400, detail=error)
    
    return ConsoleCapabilitiesResponse(**caps.to_dict())


@router.get(
    "/{instance_name}/session",
    response_model=ConsoleSessionResponse,
    summary="Create console session",
    description="Create a new graphics console session for a VM.",
    dependencies=[Depends(org_scope_dependency)],
)
async def create_console_session(
    instance_name: str,
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
    console_type: str = Query("vga", description="Console type: vga or console"),
    width: int = Query(1024, ge=640, le=4096, description="Console width"),
    height: int = Query(768, ge=480, le=2160, description="Console height"),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """Create a graphics console session."""
    # Per-project enforcement (flag-gated so the deploy is a no-op until flipped): today console has
    # no cluster/ACL check at all beyond break-glass. verify_cluster_access + verify_tenant_permission
    # are no-ops for admin/partner (allowed=None / acl=None) and enforce for tenants.
    from app.core.org_scope import verify_cluster_access, verify_tenant_permission
    from app.core.config import get_settings
    # important: cluster membership is the organisation boundary and is enforced unconditionally,
    # as it is on every other router. Only the per-project ACL stays behind the flag, which is
    # off by default until every active project carries a policy.
    verify_cluster_access(host)
    # important: the project bound and the tenant view check are unconditional. With the flag
    # off they are the only things keeping a Partner on a shared cluster out of another
    # organization's console, and a tenant out of an instance its list never shows it.
    project = verify_instance_view_access(host, project, instance_name)
    if get_settings().tenant_perm_enforcement_enabled:
        verify_tenant_permission(host, project, instance_name, "console")
    # Break-glass gate: an admin reaching a tenant cluster they do not own must hold an
    # active grant (raises 403 break_glass_required otherwise). In-scope self-service and
    # local dev are unaffected.
    from app.services.break_glass_service import enforce_sensitive_access
    from app.core.org_scope import resolve_cluster_org
    async with async_session_maker() as _bg_db:
        _bg_grant = await enforce_sensitive_access(_rbac, host, _bg_db)
        _target_org_id, _target_org_slug = await resolve_cluster_org(host, _bg_db)
    _bg_grant_id = _bg_grant.id if _bg_grant else None

    console_service = get_console_service()

    session_info, error = await console_service.create_console_session(
        instance_name=instance_name,
        host=host,
        project=project,
        console_type=console_type,
        width=width,
        height=height,
    )

    if error:
        # Log activity: console session creation failure (HIGH SEVERITY - security audit)
        activity_logger.log_activity(
            action="console.session.create",
            target_type="instance",
            target_id=instance_name,
            target_host=host,
            target_project=project,
            result="failure",
            error_message=error,
            target_organization_id=_target_org_id,
            target_organization_slug=_target_org_slug,
        )
        return ConsoleSessionResponse(success=False, error=error)

    # Log activity: console session creation success (HIGH SEVERITY - security audit)
    activity_logger.log_activity(
        action="console.session.create",
        target_type="instance",
        target_id=instance_name,
        target_host=host,
        target_project=project,
        result="success",
        details={"console_type": console_type, "width": width, "height": height, "break_glass_grant_id": _bg_grant_id},
        target_organization_id=_target_org_id,
        target_organization_slug=_target_org_slug,
    )
    
    return ConsoleSessionResponse(success=True, session=public_session_info(session_info))


@router.get(
    "/startup-order",
    response_model=StartupOrderResponse,
    summary="Get startup order",
    description="Get startup order for all instances on a host/project.",
    dependencies=[Depends(org_scope_dependency)],
)
async def get_startup_order(
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """Get startup order for instances."""
    verify_cluster_access(host)
    project = verify_project_access(host, project)
    console_service = get_console_service()
    
    entries, error = await console_service.get_startup_order(
        host=host,
        project=project,
    )
    
    if error:
        return StartupOrderResponse(success=False, error=error)
    
    # note: a tenant sees the boot order of the instances it may view, not the whole project
    return StartupOrderResponse(
        success=True,
        entries=[
            StartupOrderEntry(**e.to_dict()) for e in entries
            if can_view_instance(host, project, e.instance_name)
        ]
    )


@router.put(
    "/startup-order",
    response_model=ActionResponse,
    summary="Update startup order",
    description="Update startup order for multiple instances.",
    dependencies=[Depends(org_scope_dependency)],
)
async def update_startup_order(
    request: StartupOrderUpdateRequest,
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Update startup order for instances."""
    verify_cluster_access(host)
    project = verify_project_access(host, project)
    console_service = get_console_service()
    
    updates = [u.model_dump(exclude_none=True) for u in request.updates]
    
    success, error = await console_service.update_startup_order(
        host=host,
        project=project,
        updates=updates,
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
    host: str = Query(..., description="Target host node"),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """List available ISO files."""
    verify_cluster_access(host)
    console_service = get_console_service()
    
    isos, error = await console_service.get_available_isos(host=host)
    
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
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """Get attached ISO for an instance."""
    verify_cluster_access(host)
    project = verify_instance_view_access(host, project, instance_name)
    console_service = get_console_service()
    
    attached, error = await console_service.get_instance_iso(
        instance_name=instance_name,
        host=host,
        project=project,
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
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Attach an ISO to an instance."""
    verify_cluster_access(host)
    project = verify_project_access(host, project)
    console_service = get_console_service()
    
    success, message = await console_service.attach_iso(
        instance_name=instance_name,
        host=host,
        project=project,
        pool=request.pool,
        volume=request.volume,
        device_name=request.device_name,
        boot_priority=request.boot_priority,
    )
    
    if not success:
        return ActionResponse(success=False, error=message)
    
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
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
    device_name: str = Query("cdrom", description="Device name to eject"),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Eject an ISO from an instance."""
    verify_cluster_access(host)
    project = verify_project_access(host, project)
    console_service = get_console_service()
    
    success, message = await console_service.eject_iso(
        instance_name=instance_name,
        host=host,
        project=project,
        device_name=device_name,
    )
    
    if not success:
        return ActionResponse(success=False, error=message)
    
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
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """
    Check for active console operations for an instance.
    
    Returns list of active console sessions that might conflict.
    Useful for warning users before opening a new console.
    """
    verify_cluster_access(host)
    project = verify_instance_view_access(host, project, instance_name)
    from app.services.lxd_service import get_lxd_service
    
    lxd_service = get_lxd_service()
    active_sessions: List[ActiveConsoleSession] = []
    
    try:
        client = lxd_service.connection_manager.get_client(host)
        if not client:
            return ActiveConsoleCheckResponse(has_active_session=False, sessions=[])
        
        # Get running operations
        response = await asyncio.to_thread(read_running_operations, client)
        if response.status_code == 200:
            operations_data = response.json().get('metadata', {})
            running_ops = operations_data.get('running', [])
            
            for op in running_ops:
                # important: the operations list spans every project on the host, so an
                # operation counts only when it names this instance in this project
                if (project, instance_name) not in operation_targets(op):
                    continue
                description = op.get('description', '')
                op_class = op.get('class', '')
                
                # Check if this is a console operation for our instance
                # Console operations typically have "Console" or "exec" in description
                # and are of class "websocket" or "task"
                is_console_op = (
                    ('console' in description.lower() or 
                     'exec' in description.lower() or
                     'Executing command' in description) and
                    instance_name in description
                )
                
                # Also check resources for instance match
                resources = op.get('resources', {})
                instances = resources.get('instances', [])
                instance_match = any(instance_name in inst for inst in instances)
                
                if is_console_op or (op_class == 'websocket' and instance_match):
                    active_sessions.append(ActiveConsoleSession(
                        operation_id=op.get('id', ''),
                        instance_name=instance_name,
                        description=description,
                        created_at=op.get('created_at', ''),
                        may_cancel=op.get('may_cancel', True),
                    ))
        
        return ActiveConsoleCheckResponse(
            has_active_session=len(active_sessions) > 0,
            sessions=active_sessions
        )
        
    except Exception as e:
        logger.error(f"Error checking active sessions: {e}")
        return ActiveConsoleCheckResponse(has_active_session=False, sessions=[])


@router.post(
    "/{instance_name}/kill-all-sessions",
    response_model=ActionResponse,
    summary="Kill all active console sessions",
    description="Kill all active console operations for an instance. Used when closing console tab.",
    dependencies=[Depends(org_scope_dependency)],
)
async def kill_all_sessions(
    instance_name: str,
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Kill all active console sessions for an instance."""
    verify_cluster_access(host)
    project = verify_project_access(host, project)
    from app.services.lxd_service import get_lxd_service
    
    lxd_service = get_lxd_service()
    killed_count = 0
    
    try:
        client = lxd_service.connection_manager.get_client(host)
        if not client:
            return ActionResponse(success=False, error=f"Cannot connect to host: {host}")
        
        # Get running operations
        response = await asyncio.to_thread(read_running_operations, client)
        if response.status_code == 200:
            operations_data = response.json().get('metadata', {})
            running_ops = operations_data.get('running', [])
            
            for op in running_ops:
                # important: a same-named instance in another organization's project on a
                # shared cluster is not this instance, and its console must survive this call
                if (project, instance_name) not in operation_targets(op):
                    continue
                _description = op.get('description', '')
                op_class = op.get('class', '')
                resources = op.get('resources', {})
                instances = resources.get('instances', [])
                
                # Check if this is a console operation for our instance
                instance_match = any(instance_name in inst for inst in instances)
                is_console_op = op_class == 'websocket' and instance_match
                
                if is_console_op:
                    op_id = op.get('id', '')
                    try:
                        # Try to delete/cancel the operation
                        client.api.operations[safe_segment(op_id)].delete()
                        killed_count += 1
                        logger.info(f"Killed console operation {op_id} for {instance_name}@{host}")
                    except Exception as e:
                        logger.warning(f"Failed to kill operation {op_id}: {e}")
        
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
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Kill an active console session by cancelling the operation."""
    verify_cluster_access(host)
    project = verify_project_access(host, project)
    from app.services.lxd_service import get_lxd_service
    
    lxd_service = get_lxd_service()
    
    try:
        client = lxd_service.connection_manager.get_client(host)
        if not client:
            return ActionResponse(success=False, error=f"Cannot connect to host: {host}")

        # important: an operation id alone named any operation on the host, so a caller could
        # cancel another organization's session. It must name this instance in this project.
        try:
            op_response = await asyncio.to_thread(client.api.operations[safe_segment(operation_id)].get)
            operation = op_response.json().get("metadata") or {}
        except Exception as exc:
            # note: only a 404 means the session is gone. A timeout or a 5xx says nothing about
            # it, and without the read the ownership check below cannot run, so nothing is killed.
            if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                return ActionResponse(success=True, message=f"Session {operation_id[:8]}... already ended")
            logger.warning(f"Could not read operation {operation_id} on {host}: {exc}")
            return ActionResponse(success=False, error="Could not read the session, so it was not terminated")
        if (project, instance_name) not in operation_targets(operation):
            return ActionResponse(success=False, error="Session not found for this instance")
        
        # For websocket operations, use direct API DELETE
        # operation.cancel() doesn't work for websocket class operations
        try:
            response = client.api.operations[safe_segment(operation_id)].delete()
            if response.status_code in (200, 202):
                logger.info(f"Deleted console operation {operation_id} for {instance_name}@{host}")
                return ActionResponse(success=True, message=f"Session {operation_id[:8]}... terminated")
        except Exception as del_err:
            logger.debug(f"DELETE request returned: {del_err}")
        
        # Check if the operation still exists
        await asyncio.sleep(0.3)  # Small delay for operation cleanup
        try:
            client.operations.get(safe_segment(operation_id))
            # Operation still exists
            return ActionResponse(success=False, error="Failed to terminate session - operation still exists")
        except Exception:
            # Operation no longer exists - success!
            return ActionResponse(success=True, message=f"Session {operation_id[:8]}... terminated")
        
    except Exception as e:
        logger.error(f"Error killing session: {e}")
        return ActionResponse(success=False, error=str(e))


# ============ WebSocket Endpoint ============

class ConsoleProxy:
    """
    Manages bidirectional WebSocket proxy between browser and LXD VGA console.
    
    Architecture:
    Browser <--WS--> FastAPI <--WS (control)--> LXD
                             <--WS (data)----> LXD (SPICE protocol)
    
    LXD VGA console requires TWO WebSocket connections:
    1. Control socket - for lifecycle management (must connect first)
    2. Data socket - for SPICE protocol data (triggers SPICE socket creation)
    
    When data socket connects, LXD creates a SPICE connection to QEMU.
    Data is then mirrored bidirectionally between WebSocket and SPICE socket.
    
    IMPORTANT: SPICE HTML5 client expects raw binary SPICE protocol immediately.
    We cannot send JSON handshake messages as SPICE client will interpret them
    as corrupted SPICE data.
    """
    
    def __init__(
        self,
        browser_ws: WebSocket,
        instance_name: str,
        host: str,
        project: str,
        console_type: str = "vga",
        user_id: str = "anonymous",
        user_email: str = "",
        user_role: str = "unknown",
        target_organization_id: Optional[int] = None,
        target_org_slug: Optional[str] = None,
        break_glass_grant_id: Optional[int] = None,
    ):
        self.browser_ws = browser_ws
        self.instance_name = instance_name
        self.host = host
        self.project = project
        self.console_type = console_type
        self.user_id = user_id
        self.user_email = user_email
        self.user_role = user_role
        self.target_organization_id = target_organization_id
        self.target_org_slug = target_org_slug
        self.break_glass_grant_id = break_glass_grant_id

        self.lxd_data_ws = None  # Data socket for SPICE data
        self.lxd_control_ws = None  # Control socket for lifecycle
        self._shutdown = asyncio.Event()
        self._session = None  # SessionInfo from tracker
    
    async def _send_error(self, message: str):
        """Send error message to browser as JSON before closing.
        
        SPICE HTML5 client will see this as corrupted data and report an error,
        but we prefix with a special marker so frontend can detect it.
        """
        try:
            # send as text for clear error detection
            # prefix with marker so frontend can parse it
            error_payload = f"__CONSOLE_ERROR__:{message}"
            await self.browser_ws.send_text(error_payload)
        except Exception:
            pass
    
    async def run(self):
        """Main entry point - runs the console proxy."""
        console_service = get_console_service()
        tracker = get_session_tracker()
        
        try:
            # Create console session with LXD
            # Default dimensions - resize happens via SPICE agent
            session_info, error = await console_service.create_console_session(
                instance_name=self.instance_name,
                host=self.host,
                project=self.project,
                console_type=self.console_type,
                width=1024,
                height=768,
            )
            
            if error:
                logger.error(f"Console session error: {error}")
                await self._send_error(error)
                return
            
            # Register session for tracking. See new_session_id for why the empty case
            # matters now that the id is a key shared across workers.
            session_id = new_session_id(session_info.get("operation_id"), "console")
            if session_id != session_info.get("operation_id"):
                logger.warning(
                    f"LXD returned no operation id for {self.instance_name}@{self.host}; "
                    f"tracking under {session_id}"
                )
            self._session = tracker.register(
                session_id=session_id,
                instance_name=self.instance_name,
                host=self.host,
                project=self.project,
                protocol=SessionProtocol.GRAPHICS,
                user_id=self.user_id,
                user_email=self.user_email,
                user_role=self.user_role,
                target_organization_id=self.target_organization_id,
                target_org_slug=self.target_org_slug,
                break_glass_grant_id=self.break_glass_grant_id,
            )

            ssl_context = console_service.get_ssl_context(self.host)
            
            # LXD VGA console requires BOTH control and data sockets
            # Control socket must be connected for the operation to work
            try:
                # 1. Connect to CONTROL socket first (required by LXD)
                control_url = session_info.get("control_url", "")
                if control_url:
                    self.lxd_control_ws = await asyncio.wait_for(
                        websockets.connect(
                            control_url,
                            ssl=ssl_context,
                            close_timeout=10,
                            ping_interval=None,
                            ping_timeout=None,
                        ),
                        timeout=10.0
                    )
                    logger.info(f"Connected to LXD control socket: {self.instance_name}")
                
                # 2. Connect to DATA socket (this triggers SPICE socket creation)
                self.lxd_data_ws = await asyncio.wait_for(
                    websockets.connect(
                        session_info["ws_url"],
                        ssl=ssl_context,
                        close_timeout=10,
                        ping_interval=None,  # LXD doesn't support ping
                        ping_timeout=None,
                        max_size=10 * 1024 * 1024,
                        compression=None,  # No compression for binary data
                    ),
                    timeout=15.0
                )
                logger.info(f"Connected to LXD data socket: {self.instance_name}")
                
                # Mark session as connected after both sockets are ready.
                # via the tracker, not the SessionInfo: a console registers as
                # CONNECTING and only lands here after two LXD connects with 10s and
                # 15s timeouts, so this edge has to reach the other workers now rather
                # than at the next heartbeat.
                if self._session:
                    get_session_tracker().mark_connected(self._session.session_id)
                
                # Note: SPICE doesn't send initial data - it waits for client handshake
                # We immediately start the proxy to avoid any timeout issues
                
            except asyncio.TimeoutError:
                error_msg = f"Timeout connecting to LXD console for {self.instance_name}"
                logger.error(error_msg)
                await self._send_error(error_msg)
                return
            except Exception as e:
                error_msg = f"Failed to connect to LXD console: {e}"
                logger.error(error_msg)
                await self._send_error(error_msg)
                return
            
            # Run bidirectional proxy - pure binary, no JSON
            await self._run_proxy()
            
        except WebSocketDisconnect:
            logger.info(f"Console WebSocket disconnected: {self.instance_name}")
        except Exception as e:
            logger.error(f"Console proxy error: {e}")
        finally:
            await self._cleanup()
    
    async def _run_proxy(self):
        """Run bidirectional binary data forwarding."""
        
        async def browser_to_lxd():
            """Forward browser input to LXD data socket."""
            first_message = True
            try:
                while not self._shutdown.is_set():
                    try:
                        # Receive any type of message from browser
                        message = await asyncio.wait_for(
                            self.browser_ws.receive(),
                            timeout=0.5
                        )
                        
                        if message["type"] == "websocket.disconnect":
                            self._shutdown.set()
                            break
                        
                        # Log first message from browser (SPICE handshake)
                        if first_message:
                            first_message = False
                            if "bytes" in message and message["bytes"]:
                                logger.debug(f"Browser SPICE handshake ({len(message['bytes'])} bytes)")
                        
                        # Forward bytes to LXD data socket
                        has_bytes = "bytes" in message and message["bytes"]
                        is_open = self.lxd_data_ws and self.lxd_data_ws.state == WebSocketState.OPEN
                        
                        if has_bytes:
                            if is_open:
                                try:
                                    await asyncio.wait_for(
                                        self.lxd_data_ws.send(message["bytes"]),
                                        timeout=5.0
                                    )
                                    # Track upload bytes
                                    if self._session:
                                        self._session.add_upload_bytes(len(message["bytes"]))
                                except asyncio.TimeoutError:
                                    logger.error("TIMEOUT sending to LXD data socket")
                                    self._shutdown.set()
                                    break
                                except Exception as send_err:
                                    logger.error(f"Failed to forward to LXD: {type(send_err).__name__}: {send_err}")
                                    self._shutdown.set()
                                    break
                            else:
                                logger.warning(f"LXD data socket not open (state={self.lxd_data_ws.state if self.lxd_data_ws else 'None'})")
                        elif "text" in message and message["text"]:
                            # Convert text to bytes if needed
                            if self.lxd_data_ws and self.lxd_data_ws.state == WebSocketState.OPEN:
                                encoded = message["text"].encode('utf-8')
                                await self.lxd_data_ws.send(encoded)
                                # Track upload bytes
                                if self._session:
                                    self._session.add_upload_bytes(len(encoded))
                                logger.info(f"FORWARDED {len(message['text'])} text chars to LXD data socket")
                                
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        if "disconnect" not in str(e).lower():
                            logger.debug(f"Browser to LXD: {e}")
                        self._shutdown.set()
                        break
            except Exception as e:
                logger.debug(f"Browser to LXD loop error: {e}")
                self._shutdown.set()
        
        async def lxd_to_browser():
            """Forward LXD data socket output to browser."""
            first_message = True
            try:
                while not self._shutdown.is_set():
                    try:
                        data = await asyncio.wait_for(
                            self.lxd_data_ws.recv(),
                            timeout=0.5
                        )
                        
                        # Log first SPICE response
                        if first_message:
                            first_message = False
                            if isinstance(data, bytes):
                                logger.debug(f"LXD SPICE response ({len(data)} bytes)")
                        
                        # Send as binary to browser + track download bytes
                        if isinstance(data, bytes):
                            await self.browser_ws.send_bytes(data)
                            if self._session:
                                self._session.add_download_bytes(len(data))
                        else:
                            encoded = data.encode('utf-8')
                            await self.browser_ws.send_bytes(encoded)
                            if self._session:
                                self._session.add_download_bytes(len(encoded))
                            
                    except asyncio.TimeoutError:
                        continue
                    except ConnectionClosed:
                        logger.info(f"LXD connection closed: {self.instance_name}")
                        self._shutdown.set()
                        break
                    except Exception as e:
                        logger.debug(f"LXD to browser: {e}")
                        self._shutdown.set()
                        break
            except Exception as e:
                logger.debug(f"LXD to browser loop error: {e}")
                self._shutdown.set()
        
        async def consume_control():
            """Consume control socket messages (keeps connection alive)."""
            try:
                while not self._shutdown.is_set():
                    if self.lxd_control_ws:
                        try:
                            await asyncio.wait_for(
                                self.lxd_control_ws.recv(),
                                timeout=0.5
                            )
                        except asyncio.TimeoutError:
                            continue
                        except ConnectionClosed:
                            logger.info(f"Control socket closed: {self.instance_name}")
                            self._shutdown.set()
                            break
                        except Exception:
                            pass
                    else:
                        await asyncio.sleep(0.5)
            except Exception:
                pass
        
        # Run all tasks concurrently
        await asyncio.gather(
            browser_to_lxd(),
            lxd_to_browser(),
            consume_control(),
            return_exceptions=True
        )
    
    async def _cleanup(self):
        """Clean up connections."""
        self._shutdown.set()
        
        # Unregister session from tracker
        if self._session:
            tracker = get_session_tracker()
            tracker.unregister(self._session.session_id)
        
        if self.lxd_data_ws:
            try:
                await self.lxd_data_ws.close()
            except:
                pass
        
        if self.lxd_control_ws:
            try:
                await self.lxd_control_ws.close()
            except:
                pass


@router.websocket("/{instance_name}/ws")
async def console_websocket(
    websocket: WebSocket,
    instance_name: str,
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
    console_type: str = Query("vga", description="Console type: vga or console"),
    user: Optional[AuthenticatedUser] = Depends(get_ws_user),
):
    """
    WebSocket endpoint for graphics console.
    
    LXD/Incus streams SPICE protocol data via WebSocket for VGA consoles.
    The client should use spice-html5 or similar SPICE client to render.
    
    IMPORTANT: spice-html5 requires 'binary' subprotocol to be accepted.
    Auth token is passed as ``token`` query parameter.
    """
    # Validate auth when enabled
    from app.core.config import get_settings as _gs
    _settings = _gs()
    if _settings.auth_enabled and user is None:
        await websocket.close(code=1008, reason="Authentication required")
        return

    # set org scope for WebSocket (context variable)
    await set_org_scope_for_ws(user)

    # important: the REST console path checks this and the WebSocket did not, so the same
    # session opened over a socket skipped the organisation boundary entirely. Closing rather
    # than raising, since HTTPException is meaningless once the socket is being negotiated.
    try:
        from app.core.org_scope import verify_cluster_access, verify_tenant_permission
        from app.core.config import get_settings

        verify_cluster_access(host)
        project = verify_instance_view_access(host, project, instance_name)
        if get_settings().tenant_perm_enforcement_enabled:
            verify_tenant_permission(host, project, instance_name, "console")
    except HTTPException as exc:
        await websocket.close(code=1008, reason="forbidden")
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
        from fastapi import HTTPException as _HTTPException
        async with async_session_maker() as _bg_db:
            try:
                _grant = await enforce_sensitive_access(user, host, _bg_db)
            except _HTTPException:
                await websocket.close(code=1008, reason="break_glass_required")
                logger.warning(f"Console WS blocked (break_glass_required): {instance_name}@{host} user={user.email}")
                return
            _bg_grant_id = _grant.id if _grant else None
            _target_org_id, _target_org_slug = await resolve_cluster_org(host, _bg_db)

    # Accept with 'binary' subprotocol - required by spice-html5
    # spice-html5 uses: new WebSocket(uri, 'binary')
    await websocket.accept(subprotocol="binary")

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
    )
    
    # A break-glass session must not outlive the grant that authorized it: the gate
    # above runs once, at connect. The watchdog closes the socket when the grant
    # expires or is revoked mid-session.
    _bg_watch = None
    if _bg_grant_id:
        from app.services.break_glass_service import watch_grant_while_open
        _bg_watch = asyncio.create_task(watch_grant_while_open(_bg_grant_id, websocket))
    try:
        await proxy.run()
    finally:
        if _bg_watch:
            _bg_watch.cancel()
    
    logger.info(f"Console WebSocket closed: {instance_name}@{host}")
