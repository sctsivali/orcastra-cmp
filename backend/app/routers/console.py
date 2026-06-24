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
import json
import logging
from typing import Optional, List
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException, Depends
from pydantic import BaseModel, Field
import websockets
from websockets.exceptions import ConnectionClosed

from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser, get_ws_user_console
from app.core.org_scope import org_scope_dependency, set_org_scope_for_ws
from app.services.console_service import get_console_service
from app.services.activity_logger import get_activity_logger
from app.services.session_tracker import get_session_tracker
from app.services.spice_session import get_spice_session_manager, ConsoleSessionError
from app.core.database import async_session_maker
from app.models.db_models import SessionAuditLog

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
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """Get real-time session stats - scoped to the authenticated user's own session."""
    tracker = get_session_tracker()
    session = tracker.get(session_id)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    
    # enforce user-scoped access (users can only view their own sessions)
    if session.user_email and _rbac.email and session.user_email != _rbac.email:
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
    instance_name: Optional[str] = Query(None, description="Filter by instance name"),
    host: Optional[str] = Query(None, description="Filter by host node"),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """List active sessions scoped to the authenticated user (data minimization / GDPR)."""
    tracker = get_session_tracker()
    sessions = tracker.get_active_sessions(
        instance_name=instance_name,
        host=host,
        user_email=_rbac.email,
    )
    return [s.to_dict() for s in sessions]


@router.get(
    "/sessions/audit",
    summary="Query session audit logs",
    description="Get historical session audit logs for compliance and analytics.",
    dependencies=[Depends(org_scope_dependency)],
)
async def query_session_audit_logs(
    instance_name: Optional[str] = Query(None, description="Filter by instance name"),
    user_email: Optional[str] = Query(None, description="Filter by user email"),
    event_type: Optional[str] = Query(None, description="Filter by event type (connect/disconnect)"),
    limit: int = Query(100, ge=1, le=1000, description="Max results to return"),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """Query session audit logs - scoped to the authenticated user's own sessions."""
    from sqlalchemy import select, desc

    async with async_session_maker() as db:
        query = select(SessionAuditLog).order_by(desc(SessionAuditLog.created_at))

        # always scope to own sessions (data minimization / GDPR)
        query = query.where(SessionAuditLog.user_email == _rbac.email)

        if instance_name:
            query = query.where(SessionAuditLog.instance_name == instance_name)
        if event_type:
            query = query.where(SessionAuditLog.event_type == event_type)

        query = query.limit(limit)
        result = await db.execute(query)
        logs = result.scalars().all()

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
            error_message=error
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
        details={"console_type": console_type, "width": width, "height": height}
    )
    
    return ConsoleSessionResponse(success=True, session=session_info)


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
    console_service = get_console_service()
    
    entries, error = await console_service.get_startup_order(
        host=host,
        project=project,
    )
    
    if error:
        return StartupOrderResponse(success=False, error=error)
    
    return StartupOrderResponse(
        success=True,
        entries=[StartupOrderEntry(**e.to_dict()) for e in entries]
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
    from app.services.lxd_service import get_lxd_service
    from datetime import datetime, timezone

    lxd_service = get_lxd_service()
    active_sessions: List[ActiveConsoleSession] = []
    seen_ops: set = set()

    # Authoritative source: live sessions tracked in-process. The tracker knows the
    # instance/host of every session including SPICE graphics, which the LXD-operation
    # heuristic below cannot match (a "Showing console" op carries no instance in its
    # description and an empty resources.instances, so graphics sessions were invisible
    # to the conflict check). Only CONNECTED/CONNECTING sessions are returned.
    try:
        for s in get_session_tracker().get_active_sessions(instance_name=instance_name, host=host):
            op_id = s.session_id
            seen_ops.add(op_id)
            active_sessions.append(ActiveConsoleSession(
                operation_id=op_id,
                instance_name=s.instance_name,
                description=f"{s.protocol.value} ({s.user_email or s.user_id})",
                created_at=datetime.fromtimestamp(s.started_at, tz=timezone.utc).isoformat(),
                may_cancel=True,
            ))
    except Exception as e:
        logger.debug(f"Tracker active-session lookup failed: {e}")

    # Supplementary: scan LXD operations for anything not tracked in-process (e.g.
    # an orphan from a crash, or a session from before a restart).
    try:
        client = lxd_service.connection_manager.get_client(host)
        if client:
            response = client.api.operations.get(params={'recursion': '1'}, timeout=(5, 30))
            if response.status_code == 200:
                running_ops = response.json().get('metadata', {}).get('running', [])
                for op in running_ops:
                    op_id = op.get('id', '')
                    if op_id in seen_ops:
                        continue
                    description = op.get('description', '')
                    op_class = op.get('class', '')
                    is_console_op = (
                        ('console' in description.lower() or
                         'exec' in description.lower() or
                         'Executing command' in description) and
                        instance_name in description
                    )
                    resources = op.get('resources', {})
                    instance_match = any(instance_name in inst for inst in resources.get('instances', []))
                    if is_console_op or (op_class == 'websocket' and instance_match):
                        seen_ops.add(op_id)
                        active_sessions.append(ActiveConsoleSession(
                            operation_id=op_id,
                            instance_name=instance_name,
                            description=description,
                            created_at=op.get('created_at', ''),
                            may_cancel=op.get('may_cancel', True),
                        ))
    except Exception as e:
        logger.error(f"Error checking active sessions: {e}")

    return ActiveConsoleCheckResponse(
        has_active_session=len(active_sessions) > 0,
        sessions=active_sessions,
    )


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
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Kill all active console sessions for an instance."""
    from app.services.lxd_service import get_lxd_service
    
    lxd_service = get_lxd_service()
    killed_count = 0
    to_cancel: set = set()

    # Tracked sessions for this instance (authoritative; includes SPICE graphics,
    # whose "Showing console" op the resources heuristic below cannot match).
    try:
        for s in get_session_tracker().get_active_sessions(instance_name=instance_name, host=host):
            if s.session_id:
                to_cancel.add(s.session_id)
    except Exception as e:
        logger.debug(f"Tracker lookup for kill-all failed: {e}")

    try:
        client = lxd_service.connection_manager.get_client(host)
        if not client:
            return ActionResponse(success=False, error=f"Cannot connect to host: {host}")

        # Add LXD websocket operations whose resources match this instance.
        response = client.api.operations.get(params={'recursion': '1'}, timeout=(5, 30))
        if response.status_code == 200:
            running_ops = response.json().get('metadata', {}).get('running', [])
            for op in running_ops:
                resources = op.get('resources', {})
                instance_match = any(instance_name in inst for inst in resources.get('instances', []))
                if op.get('class') == 'websocket' and instance_match:
                    to_cancel.add(op.get('id', ''))

        for op_id in to_cancel:
            if not op_id:
                continue
            try:
                client.api.operations[op_id].delete()
                killed_count += 1
                logger.info(f"Killed console operation {op_id} for {instance_name}@{host}")
            except Exception as e:
                logger.warning(f"Failed to kill operation {op_id}: {e}")

        return ActionResponse(
            success=True,
            message=f"Killed {killed_count} console session(s) for {instance_name}",
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
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Kill an active console session by cancelling the operation."""
    from app.services.lxd_service import get_lxd_service
    
    lxd_service = get_lxd_service()
    
    try:
        client = lxd_service.connection_manager.get_client(host)
        if not client:
            return ActionResponse(success=False, error=f"Cannot connect to host: {host}")
        
        # For websocket operations, use direct API DELETE
        # operation.cancel() doesn't work for websocket class operations
        try:
            response = client.api.operations[operation_id].delete()
            if response.status_code in (200, 202):
                logger.info(f"Deleted console operation {operation_id} for {instance_name}@{host}")
                return ActionResponse(success=True, message=f"Session {operation_id[:8]}... terminated")
        except Exception as del_err:
            logger.debug(f"DELETE request returned: {del_err}")
        
        # Check if the operation still exists
        await asyncio.sleep(0.3)  # Small delay for operation cleanup
        try:
            client.operations.get(operation_id)
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
        session_key: str = "",
    ):
        self.browser_ws = browser_ws
        self.instance_name = instance_name
        self.host = host
        self.project = project
        self.console_type = console_type
        self.user_id = user_id
        self.user_email = user_email
        self.user_role = user_role
        # Shared-operation key: every channel of one SPICE console carries the same
        # ticket, so all four map to one LXD console operation via the manager.
        self.session_key = session_key
        self._acquired = False

        self.lxd_data_ws = None  # This channel's own data socket for SPICE data
        self._shutdown = asyncio.Event()
        self._session = None  # shared SessionInfo from tracker (one per console)
    
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
        """Main entry point - runs the console proxy for ONE SPICE channel.

        All channels of a console share ONE LXD console operation (created once by
        the SpiceSessionManager, keyed by the ticket). This channel opens only its
        own data socket to that shared operation; the single control socket and the
        operation lifecycle are owned by the manager.
        """
        manager = get_spice_session_manager()

        try:
            # Acquire (or create-once) the shared operation for this console.
            try:
                session = await manager.acquire(
                    self.session_key,
                    instance_name=self.instance_name,
                    host=self.host,
                    project=self.project,
                    console_type=self.console_type,
                    user_id=self.user_id,
                    user_email=self.user_email,
                    user_role=self.user_role,
                )
            except ConsoleSessionError as e:
                logger.error(f"Console session error: {e}")
                await self._send_error(str(e))
                return

            self._acquired = True
            # Share the one tracker entry: every channel adds its bytes here.
            self._session = session.tracked

            # Connect ONLY this channel's data socket to the shared operation
            # (LXD allows multiple connections to one operation's data-fd secret).
            try:
                self.lxd_data_ws = await asyncio.wait_for(
                    websockets.connect(
                        session.data_url,
                        ssl=session.ssl_context,
                        close_timeout=10,
                        ping_interval=None,  # LXD doesn't support ping
                        ping_timeout=None,
                        max_size=10 * 1024 * 1024,
                        compression=None,  # No compression for binary data
                    ),
                    timeout=15.0
                )
                logger.info(f"Connected to LXD data socket: {self.instance_name}")

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
        """Run bidirectional binary forwarding with proper cancellation.

        Each direction is a plain awaiting loop with no per-frame timeout poll.
        Shutdown is driven by cancelling the sibling tasks as soon as any one
        finishes (FIRST_COMPLETED), so a client disconnect or an LXD close tears
        the session down immediately instead of after a 0.5s poll, and we avoid
        allocating a TimerHandle per frame on the hot path. Mirrors the terminal
        proxy (terminal.py:_run_tasks).
        """
        tasks = [
            asyncio.create_task(self._forward_browser_to_lxd(), name="browser_to_lxd"),
            asyncio.create_task(self._forward_lxd_to_browser(), name="lxd_to_browser"),
            asyncio.create_task(self._revocation_watchdog(), name="revocation_watchdog"),
        ]
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    task.result()
                except Exception as e:
                    if not isinstance(e, (asyncio.CancelledError, WebSocketDisconnect, ConnectionClosed)):
                        logger.error(f"Console task {task.get_name()} failed: {e}")
        finally:
            self._shutdown.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning("Console task cancellation timed out for %s", self.instance_name)

    async def _forward_browser_to_lxd(self):
        """Forward browser input to the LXD data socket as opaque SPICE binary.

        High-frequency path: no decode, no JSON, no per-frame timer. SPICE is
        pure binary; the text branch is a defensive fallback only. A closed LXD
        socket surfaces as ConnectionClosed and tears the session down.
        """
        try:
            while True:
                message = await self.browser_ws.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                data = message.get("bytes")
                if data:
                    await self.lxd_data_ws.send(data)
                    if self._session:
                        self._session.add_upload_bytes(len(data))
                elif message.get("text"):
                    encoded = message["text"].encode("utf-8")
                    await self.lxd_data_ws.send(encoded)
                    if self._session:
                        self._session.add_upload_bytes(len(encoded))
        except (WebSocketDisconnect, ConnectionClosed):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"Console browser->LXD ended: {e}")
        finally:
            self._shutdown.set()

    async def _forward_lxd_to_browser(self):
        """Forward LXD data socket output to the browser as opaque SPICE binary.

        Iterates the websockets connection directly (idiomatic, no per-recv
        timeout). A slow browser applies natural backpressure through
        ``send_bytes``; SPICE has its own flow control, so we never drop frames.
        """
        try:
            async for message in self.lxd_data_ws:
                if isinstance(message, (bytes, bytearray)):
                    payload = message
                else:
                    payload = message.encode("utf-8")
                await self.browser_ws.send_bytes(payload)
                if self._session:
                    self._session.add_download_bytes(len(payload))
        except (ConnectionClosed, WebSocketDisconnect):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"Console LXD->browser ended: {e}")
        finally:
            self._shutdown.set()

    async def _revocation_watchdog(self):
        """Re-check identity revocation for the life of the session so a revoked or
        role-changed cert loses an open SPICE console within ~60s. The connect-time check
        alone cannot cover a long-lived socket."""
        from app.core.config import get_settings
        applies = (
            get_settings().auth_mode == "client-cert"
            and self.user_id
            and self.user_id != "anonymous"
        )
        if not applies:
            # idle until the session ends; the other tasks drive shutdown
            await self._shutdown.wait()
            return
        from app.core.rbac_cache import get_rbac_cache
        cache = get_rbac_cache()
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                pass
            if self._shutdown.is_set():
                return
            try:
                if cache.is_identity_session_revoked(self.user_id):
                    logger.warning(
                        "Console session revoked mid-stream: user=%s instance=%s",
                        self.user_id, self.instance_name,
                    )
                    self._shutdown.set()
                    try:
                        await self.browser_ws.close(code=1008, reason="Session revoked")
                    except Exception:
                        pass
                    return
            except Exception:
                pass

    async def _cleanup(self):
        """Clean up this channel's data socket and release the shared operation.

        The tracker entry and the control socket belong to the shared operation,
        so they are torn down by the manager when the LAST channel releases - not
        here per channel.
        """
        self._shutdown.set()

        if self.lxd_data_ws:
            try:
                await self.lxd_data_ws.close()
            except:
                pass

        if self._acquired:
            try:
                await get_spice_session_manager().release(self.session_key)
            except Exception:
                pass


@router.websocket("/{instance_name}/ws")
async def console_websocket(
    websocket: WebSocket,
    instance_name: str,
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
    console_type: str = Query("vga", description="Console type: vga or console"),
    # important: multi-use ticket - SPICE opens one WebSocket per channel, all sharing
    # the same ticket; a single-use redeem would 403 every channel after the first.
    user: Optional[AuthenticatedUser] = Depends(get_ws_user_console),
):
    """
    WebSocket endpoint for graphics console.
    
    LXD/Incus streams SPICE protocol data via WebSocket for VGA consoles.
    The client should use spice-html5 or similar SPICE client to render.
    
    IMPORTANT: spice-html5 requires 'binary' subprotocol to be accepted.
    Auth is a single-use ``ticket`` query parameter (see POST /terminal/ticket).
    """
    # Validate auth when enabled (fail-closed, independent of auth backend).
    # NOTE: do NOT gate on authentik_issuer - that bypassed auth entirely in client-cert mode.
    from app.core.config import get_settings as _gs
    _settings = _gs()
    if _settings.auth_enabled and user is None:
        await websocket.close(code=1008, reason="Authentication required")
        return

    # set org scope for WebSocket (context variable)
    await set_org_scope_for_ws(user)

    # important: enforce the per-instance tenant ACL BEFORE accepting / connecting upstream.
    # Resolve the instance's REAL project server-side (never trust the client project param),
    # then check the ACL. Admin/partner have no ACL (no-op); a tenant outside the instance
    # ACL is rejected. This stops cross-instance SPICE console on an otherwise-allowed cluster.
    from app.services.lxd_service import get_lxd_service
    from app.core.org_scope import verify_tenant_permission
    try:
        _vm = await get_lxd_service().get_vm(instance_name, host)
    except Exception:
        _vm = None
    if _vm is None:
        await websocket.close(code=1008, reason="Instance not found or not accessible")
        return
    real_project = (
        getattr(_vm, "project", None)
        or (_vm.config.get("project") if getattr(_vm, "config", None) else None)
        or "default"
    )
    try:
        verify_tenant_permission(host, real_project, instance_name, "view")
    except HTTPException:
        logger.warning(
            "Console ACL denied: user=%s instance=%s@%s project=%s",
            user.user_id if user else "anonymous", instance_name, host, real_project,
        )
        await websocket.close(code=1008, reason="Access denied for this instance")
        return

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

    # Shared-operation key: all four SPICE channels of one console carry the same
    # ticket, so they collapse to ONE LXD console operation. Fall back to a stable
    # per-user/instance key when auth (and thus the ticket) is disabled.
    ticket = websocket.query_params.get("ticket")
    session_key = ticket or f"{instance_name}|{host}|{real_project}|{user_id}"

    proxy = ConsoleProxy(
        browser_ws=websocket,
        instance_name=instance_name,
        host=host,
        project=real_project,
        console_type=console_type,
        user_id=user_id,
        user_email=user_email,
        user_role=user_role,
        session_key=session_key,
    )
    
    await proxy.run()
    
    logger.info(f"Console WebSocket closed: {instance_name}@{host}")
