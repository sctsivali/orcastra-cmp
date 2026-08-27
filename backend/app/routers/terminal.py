"""
Terminal WebSocket Router.
Provides WebSocket endpoints for interactive terminal access to LXD instances.
"""

import asyncio
import json
import logging
from typing import Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException, Depends
import websockets
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State as WebSocketState

from app.services.terminal_service import get_terminal_service
from app.services.activity_logger import get_activity_logger
from app.services.session_tracker import get_session_tracker, SessionProtocol
from app.services.session_models import new_session_id
from app.core.dependencies import require_tenant, AuthenticatedUser, get_ws_user
from app.core.org_scope import org_scope_dependency, set_org_scope_for_ws

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/terminal",
    tags=["Terminal"],
)


@router.get(
    "/session",
    summary="Create terminal session",
    description="Create a new terminal session for an instance and get WebSocket connection info.",
    dependencies=[Depends(org_scope_dependency)],
)
async def create_terminal_session(
    instance: str = Query(..., description="Instance name"),
    host: str = Query(..., description="Host/cluster name"),
    project: str = Query("default", description="LXD project name"),
    width: int = Query(120, ge=10, le=500, description="Terminal width in columns"),
    height: int = Query(40, ge=5, le=200, description="Terminal height in rows"),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
):
    """
    Create a terminal session for an instance.
    Returns WebSocket URLs for connecting to the terminal.
    """
    # Break-glass gate: an admin reaching a tenant cluster they do not own must hold an
    # active grant (raises 403 break_glass_required). In-scope self-service / dev unaffected.
    from app.services.break_glass_service import enforce_sensitive_access
    from app.core.org_scope import resolve_cluster_org
    from app.core.database import async_session_maker
    async with async_session_maker() as _bg_db:
        _bg_grant = await enforce_sensitive_access(_rbac, host, _bg_db)
        _target_org_id, _target_org_slug = await resolve_cluster_org(host, _bg_db)
    _bg_grant_id = _bg_grant.id if _bg_grant else None

    terminal_service = get_terminal_service()

    session_info, error = await terminal_service.create_exec_session(
        instance_name=instance,
        host=host,
        project=project,
        width=width,
        height=height,
    )

    if error:
        # Log activity: terminal session creation failure
        activity_logger.log_activity(
            action="terminal.session.create",
            target_type="instance",
            target_id=instance,
            target_host=host,
            target_project=project,
            result="failure",
            error_message=error,
            target_organization_id=_target_org_id,
            target_organization_slug=_target_org_slug,
        )
        raise HTTPException(status_code=400, detail=error)

    # Log activity: terminal session creation success (HIGH SEVERITY - security audit)
    activity_logger.log_activity(
        action="terminal.session.create",
        target_type="instance",
        target_id=instance,
        target_host=host,
        target_project=project,
        result="success",
        details={"width": width, "height": height, "break_glass_grant_id": _bg_grant_id},
        target_organization_id=_target_org_id,
        target_organization_slug=_target_org_slug,
    )
    
    return {
        "success": True,
        "session": session_info,
    }


class TerminalProxy:
    """
    Manages bidirectional WebSocket proxy between browser and LXD.
    
    Architecture:
    Browser <--WS--> FastAPI <--WS--> LXD (data + control)
    """
    
    def __init__(
        self,
        browser_ws: WebSocket,
        instance_name: str,
        host: str,
        project: str,
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
        self.user_id = user_id
        self.user_email = user_email
        self.user_role = user_role
        self.target_organization_id = target_organization_id
        self.target_org_slug = target_org_slug
        self.break_glass_grant_id = break_glass_grant_id

        self.data_ws: Optional[websockets.WebSocketClientProtocol] = None
        self.control_ws: Optional[websockets.WebSocketClientProtocol] = None
        self._shutdown = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._session = None  # SessionInfo from tracker
    
    async def run(self):
        """Main entry point - runs the terminal proxy."""
        terminal_service = get_terminal_service()
        tracker = get_session_tracker()
        
        try:
            # Get initial terminal size from client (with timeout)
            try:
                init_msg = await asyncio.wait_for(
                    self.browser_ws.receive_json(), 
                    timeout=5.0
                )
                width = init_msg.get("width", 120)
                height = init_msg.get("height", 40)
            except (asyncio.TimeoutError, Exception):
                width, height = 120, 40
            
            # Create exec session
            session_info, error = await terminal_service.create_exec_session(
                instance_name=self.instance_name,
                host=self.host,
                project=self.project,
                width=width,
                height=height,
            )
            
            if error:
                await self._send_error(error)
                return
            
            # Register session for tracking. See new_session_id for why the empty case
            # matters now that the id is a key shared across workers.
            session_id = new_session_id(session_info.get("operation_id"), "terminal")
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
                protocol=SessionProtocol.TERMINAL,
                user_id=self.user_id,
                user_email=self.user_email,
                user_role=self.user_role,
                target_organization_id=self.target_organization_id,
                target_org_slug=self.target_org_slug,
                break_glass_grant_id=self.break_glass_grant_id,
            )
            # via the tracker, not the SessionInfo: this is the edge that has to reach
            # the other workers immediately rather than at the next heartbeat.
            tracker.mark_connected(session_id)
            
            # Send session info (include session_id for frontend tracking)
            await self.browser_ws.send_json({
                "type": "session",
                "session_id": session_id,
                "instance": self.instance_name,
                "host": self.host,
            })
            
            # Connect to LXD WebSockets
            ssl_context = terminal_service.get_ssl_context(self.host)
            
            try:
                # Connect both WebSockets concurrently
                # websockets library handles keepalive ping/pong at protocol level
                self.data_ws, self.control_ws = await asyncio.wait_for(
                    asyncio.gather(
                        websockets.connect(
                            session_info["ws_url"],
                            ssl=ssl_context,
                            close_timeout=10,
                            ping_interval=60,
                            ping_timeout=120,
                            max_size=10 * 1024 * 1024,
                        ),
                        websockets.connect(
                            session_info["control_url"],
                            ssl=ssl_context,
                            close_timeout=10,
                            ping_interval=60,
                            ping_timeout=120,
                        ),
                    ),
                    timeout=15.0
                )
                logger.info(f"Connected to LXD terminal: {self.instance_name}")
            except asyncio.TimeoutError:
                await self._send_error("Timeout connecting to LXD")
                return
            except Exception as e:
                await self._send_error(f"Failed to connect to LXD: {e}")
                return
            
            # Send ready signal
            await self.browser_ws.send_json({"type": "ready"})
            
            # Run all tasks - use manual task management for Python 3.10 compatibility
            await self._run_tasks()
                        
        except WebSocketDisconnect:
            logger.info(f"Browser disconnected: {self.instance_name}")
        except Exception as e:
            logger.error(f"Terminal proxy error: {e}")
            try:
                await self._send_error(str(e))
            except Exception:
                pass
        finally:
            await self._cleanup()
    
    async def _run_tasks(self):
        """Run all proxy tasks concurrently with proper cancellation."""
        # keepalive removed - websockets library handles ping/pong at protocol level
        tasks = [
            asyncio.create_task(self._forward_browser_to_lxd(), name="browser_to_lxd"),
            asyncio.create_task(self._forward_lxd_to_browser(), name="lxd_to_browser"),
            asyncio.create_task(self._monitor_control(), name="control_monitor"),
        ]
        
        try:
            # Wait for first task to complete (or fail)
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Check if any completed task raised an exception
            for task in done:
                try:
                    task.result()
                except Exception as e:
                    if not isinstance(e, (asyncio.CancelledError, WebSocketDisconnect, ConnectionClosed)):
                        logger.error(f"Task {task.get_name()} failed: {e}")
                        
        finally:
            # Cancel all remaining tasks
            self._shutdown.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            
            # Wait for cancellation with a hard timeout to prevent zombies
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning("Task cancellation timed out for %s", self.instance_name)
    
    async def _forward_browser_to_lxd(self):
        """Forward input from browser to LXD data WebSocket."""
        try:
            while not self._shutdown.is_set():
                # Use wait_for with timeout to allow checking shutdown
                try:
                    data = await asyncio.wait_for(
                        self.browser_ws.receive(),
                        timeout=0.5  # Short timeout for responsiveness
                    )
                except asyncio.TimeoutError:
                    continue
                
                if data["type"] == "websocket.disconnect":
                    self._shutdown.set()
                    break
                
                if "text" in data:
                    await self._handle_browser_message(data["text"])
                elif "bytes" in data:
                    # Binary data - forward directly
                    if self.data_ws and self.data_ws.state == WebSocketState.OPEN:
                        await self.data_ws.send(data["bytes"])
                        
        except WebSocketDisconnect:
            self._shutdown.set()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Error forwarding browser->LXD: {e}")
            self._shutdown.set()
            raise
    
    async def _handle_browser_message(self, text: str):
        """Parse and handle JSON message from browser."""
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            return
        
        msg_type = msg.get("type", "")
        
        if msg_type == "input":
            # Terminal input - forward immediately
            input_data = msg.get("data", "")
            if input_data and self.data_ws and self.data_ws.state == WebSocketState.OPEN:
                try:
                    encoded = input_data.encode('utf-8')
                    await self.data_ws.send(encoded)
                    # Track upload bytes
                    if self._session:
                        self._session.add_upload_bytes(len(encoded))
                except Exception as e:
                    logger.warning(f"Failed to send input to LXD: {e}")
                    self._shutdown.set()
                
        elif msg_type == "resize":
            # Window resize - send to control WebSocket
            width = msg.get("width", 120)
            height = msg.get("height", 40)
            if self.control_ws and self.control_ws.state == WebSocketState.OPEN:
                resize_cmd = json.dumps({
                    "command": "window-resize",
                    "args": {
                        "width": str(width),
                        "height": str(height),
                    }
                })
                try:
                    await self.control_ws.send(resize_cmd)
                except Exception as e:
                    logger.warning(f"Resize failed: {e}")
                    
        elif msg_type == "ping":
            # Browser ping - respond immediately
            await self.browser_ws.send_json({"type": "pong"})
            
        elif msg_type == "pong":
            # Browser responded to our keepalive ping
            pass
    
    async def _forward_lxd_to_browser(self):
        """Forward output from LXD to browser."""
        try:
            while not self._shutdown.is_set():
                try:
                    # Use recv with timeout instead of async for
                    # This prevents blocking and allows shutdown checks
                    message = await asyncio.wait_for(
                        self.data_ws.recv(),
                        timeout=1.0  # Check shutdown every second
                    )
                except asyncio.TimeoutError:
                    # No data - that's fine, just continue checking
                    continue
                
                if self._shutdown.is_set():
                    break
                
                # Forward immediately - no buffering
                if isinstance(message, bytes):
                    text = message.decode('utf-8', errors='replace')
                    byte_count = len(message)
                else:
                    text = message
                    byte_count = len(message.encode('utf-8', errors='replace'))
                
                # Track download bytes
                if self._session:
                    self._session.add_download_bytes(byte_count)
                
                try:
                    await self.browser_ws.send_json({
                        "type": "output",
                        "data": text
                    })
                except Exception as e:
                    logger.warning(f"Error sending to browser: {e}")
                    self._shutdown.set()
                    break
                    
            logger.info(f"LXD data WebSocket ended: {self.instance_name}")
            
        except ConnectionClosed as e:
            logger.info(f"LXD connection closed: {self.instance_name} - {e.code}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Error forwarding LXD->browser: {e}")
            raise
        finally:
            self._shutdown.set()
            try:
                await self.browser_ws.send_json({"type": "closed"})
            except Exception:
                pass
    
    async def _monitor_control(self):
        """Monitor control WebSocket for signals."""
        try:
            while not self._shutdown.is_set():
                try:
                    # Use recv with timeout instead of async for
                    message = await asyncio.wait_for(
                        self.control_ws.recv(),
                        timeout=1.0
                    )
                    logger.debug(f"Control message: {message}")
                except asyncio.TimeoutError:
                    continue
                
        except ConnectionClosed as e:
            logger.info(f"LXD control closed: {self.instance_name} - {e.code}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Control WebSocket error: {e}")
            raise
        finally:
            self._shutdown.set()
    
    async def _send_error(self, message: str):
        """Send error message to browser and close."""
        try:
            await self.browser_ws.send_json({"type": "error", "message": message})
            await self.browser_ws.close(code=1008, reason=message)
        except Exception:
            pass
    
    async def _cleanup(self):
        """Clean up all WebSocket connections."""
        self._shutdown.set()
        
        # Unregister session from tracker
        if self._session:
            tracker = get_session_tracker()
            tracker.unregister(self._session.session_id)
        
        # Close LXD WebSockets - use state check for websockets v16+
        for ws in [self.data_ws, self.control_ws]:
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass
        
        logger.info(f"Terminal session ended: {self.instance_name}@{self.host}")


@router.websocket("/ws/{instance_name}")
async def terminal_websocket(
    websocket: WebSocket,
    instance_name: str,
    host: str = Query(...),
    project: str = Query("default"),
    user: Optional[AuthenticatedUser] = Depends(get_ws_user),
):
    """
    WebSocket endpoint for terminal connection.
    Acts as a proxy between browser and LXD instance exec.
    Auth token is passed as ``token`` query parameter.
    """
    # Validate auth when enabled
    from app.core.config import get_settings as _gs
    _settings = _gs()
    if _settings.auth_enabled and _settings.authentik_issuer and user is None:
        await websocket.close(code=1008, reason="Authentication required")
        return

    # set org scope for WebSocket (context variable)
    await set_org_scope_for_ws(user)

    # Per-project enforcement (flag-gated). No-op for admin/partner (acl is None); for a tenant a
    # missing/insufficient 'console' rule closes the socket with 1008 rather than raising.
    if _settings.tenant_perm_enforcement_enabled and user is not None:
        from app.core.org_scope import verify_tenant_permission
        from fastapi import HTTPException as _HTTPExc
        try:
            verify_tenant_permission(host, project, instance_name, "console")
        except _HTTPExc:
            await websocket.close(code=1008, reason="permission_denied")
            logger.warning(f"Terminal WS blocked (no console permission): {instance_name}@{host} user={user.email}")
            return

    # Break-glass gate (defense in depth). An admin reaching a tenant cluster they do not
    # own must hold an active grant; otherwise reject. Dev/no-user skips inside enforce.
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
                logger.warning(f"Terminal WS blocked (break_glass_required): {instance_name}@{host} user={user.email}")
                return
            _bg_grant_id = _grant.id if _grant else None
            _target_org_id, _target_org_slug = await resolve_cluster_org(host, _bg_db)

    await websocket.accept()
    user_id = user.user_id if user else "anonymous"
    logger.info(f"Terminal WebSocket connected: {instance_name}@{host} user={user_id}")

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

    proxy = TerminalProxy(
        browser_ws=websocket,
        instance_name=instance_name,
        host=host,
        project=project,
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


@router.get(
    "/health",
    summary="Terminal service health check",
)
async def terminal_health():
    """Check if terminal service is available."""
    return {"status": "ok", "service": "terminal"}
