"""
Terminal WebSocket Router.
Provides WebSocket endpoints for interactive terminal access to LXD instances.
"""

import asyncio
import json
import logging
from typing import Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException, Depends
from pydantic import BaseModel, Field
import websockets
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State as WebSocketState

from app.services.terminal_service import get_terminal_service
from app.services.activity_logger import get_activity_logger
from app.services.session_tracker import get_session_tracker, SessionProtocol
from app.core.dependencies import require_tenant, AuthenticatedUser, get_ws_user
from app.core.org_scope import org_scope_dependency, set_org_scope_for_ws, verify_cluster_access
from app.core.ws_ticket import create_ticket

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/terminal",
    tags=["Terminal"],
)


class WsTicketRequest(BaseModel):
    """Request body for minting a single-use WebSocket auth ticket."""
    instance: str = Field(..., description="Instance name")
    host: str = Field(..., description="Host/cluster name")
    project: str = Field("default", description="LXD project name")
    console_type: Optional[str] = Field(None, description="Console type for graphics (ex; 'vga')")


@router.post(
    "/ticket",
    summary="Mint a single-use WebSocket ticket",
    description=(
        "Exchange the caller's bearer token for an opaque, short-lived, single-use "
        "ticket bound to a specific instance. Used by both the terminal and console "
        "WebSockets so the JWT never appears in the WebSocket URL."
    ),
    dependencies=[Depends(org_scope_dependency)],
)
async def create_ws_ticket(
    payload: WsTicketRequest,
    user: AuthenticatedUser = Depends(require_tenant()),
):
    """Mint a WebSocket auth ticket for the authenticated caller."""
    verify_cluster_access(payload.host)
    ticket, expires_in = create_ticket(
        user,
        instance=payload.instance,
        host=payload.host,
        project=payload.project,
        console_type=payload.console_type,
    )
    return {"ticket": ticket, "expires_in": expires_in}


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
            error_message=error
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
        details={"width": width, "height": height}
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

    # Output flow-control watermarks (bytes). The browser ACKs output once xterm has
    # parsed it; this proxy pauses reading from LXD when outstanding (sent-but-unacked)
    # bytes cross HIGH_WATER, and resumes once ACKs drain it below LOW_WATER. Bounds the
    # browser's in-memory write buffer under a flood (cat bigfile / yes) without touching
    # interactive latency - watermarks are generous, so normal output never throttles.
    HIGH_WATER = 2 * 1024 * 1024
    LOW_WATER = 512 * 1024

    def __init__(
        self,
        browser_ws: WebSocket,
        instance_name: str,
        host: str,
        project: str,
        user_id: str = "anonymous",
        user_email: str = "",
        user_role: str = "unknown",
    ):
        self.browser_ws = browser_ws
        self.instance_name = instance_name
        self.host = host
        self.project = project
        self.user_id = user_id
        self.user_email = user_email
        self.user_role = user_role
        
        self.data_ws: Optional[websockets.WebSocketClientProtocol] = None
        self.control_ws: Optional[websockets.WebSocketClientProtocol] = None
        self._shutdown = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._session = None  # SessionInfo from tracker

        # Output flow control: outstanding (sent-but-unacked) byte count, plus a gate
        # the LXD->browser forwarder waits on when that count is above HIGH_WATER.
        # Starts open so normal output flows without ever blocking.
        self._unacked = 0
        self._resume = asyncio.Event()
        self._resume.set()
    
    def _trigger_shutdown(self):
        """Signal shutdown and release the flow-control gate.

        Releasing _resume is what guarantees a paused LXD->browser forwarder (waiting
        for ACKs that will never come once the client is gone) wakes up, sees the
        shutdown flag, and exits cleanly instead of hanging until task cancellation.
        """
        self._shutdown.set()
        self._resume.set()

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
            
            # Register session for tracking
            session_id = session_info["operation_id"]
            self._session = tracker.register(
                session_id=session_id,
                instance_name=self.instance_name,
                host=self.host,
                project=self.project,
                protocol=SessionProtocol.TERMINAL,
                user_id=self.user_id,
                user_email=self.user_email,
                user_role=self.user_role,
            )
            self._session.mark_connected()
            
            # Send session info (include session_id for frontend tracking)
            await self.browser_ws.send_json({
                "type": "session",
                "session_id": session_id,
                "instance": self.instance_name,
                "host": self.host,
            })
            
            # Connect to LXD WebSockets. get_ssl_context does a (cached) Vault fetch +
            # context build, both blocking - offload so it never stalls the event loop.
            ssl_context = await asyncio.to_thread(terminal_service.get_ssl_context, self.host)
            
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
            asyncio.create_task(self._revocation_watchdog(), name="revocation_watchdog"),
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
            self._trigger_shutdown()
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

    async def _revocation_watchdog(self):
        """Re-check identity revocation for the life of the session so a revoked or
        role-changed cert loses an open root shell within ~60s. The connect-time check
        alone cannot cover a long-lived socket."""
        from app.core.config import get_settings
        applies = (
            get_settings().auth_mode == "client-cert"
            and self.user_id
            and self.user_id != "anonymous"
        )
        if not applies:
            # idle until the session ends; must NOT complete first and trigger teardown
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
                        "Terminal session revoked mid-stream: user=%s instance=%s",
                        self.user_id, self.instance_name,
                    )
                    self._trigger_shutdown()
                    try:
                        await self.browser_ws.close(code=1008, reason="Session revoked")
                    except Exception:
                        pass
                    return
            except Exception:
                pass

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
                    self._trigger_shutdown()
                    break
                
                if "text" in data:
                    await self._handle_browser_message(data["text"])
                elif "bytes" in data:
                    # Binary frame = raw terminal input (keystrokes/paste). Forward straight
                    # to the LXD data socket - no decode, no JSON. This is the high-frequency
                    # path; control messages (resize/ping) still arrive as text JSON above.
                    raw = data["bytes"]
                    if raw and self.data_ws and self.data_ws.state == WebSocketState.OPEN:
                        await self.data_ws.send(raw)
                        if self._session:
                            self._session.add_upload_bytes(len(raw))
                        
        except WebSocketDisconnect:
            self._trigger_shutdown()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Error forwarding browser->LXD: {e}")
            self._trigger_shutdown()
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
                    self._trigger_shutdown()
                
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
                    
        elif msg_type == "ack":
            # Output flow control: the browser reports bytes it has finished parsing
            # into xterm. Lower the outstanding count and reopen the gate once we are
            # back under the low-water mark so the forwarder resumes pushing output.
            acked = int(msg.get("bytes", 0) or 0)
            if acked > 0:
                self._unacked = max(0, self._unacked - acked)
                if self._unacked < self.LOW_WATER:
                    self._resume.set()

        elif msg_type == "ping":
            # Browser ping - respond immediately
            await self.browser_ws.send_json({"type": "pong"})

        elif msg_type == "pong":
            # Browser responded to our keepalive ping
            pass
    
    async def _forward_lxd_to_browser(self):
        """Forward raw PTY output from LXD to the browser as binary frames.

        # important: forward bytes verbatim (no per-frame decode). xterm.js keeps a
        # streaming UTF-8 decoder, so it correctly stitches a multibyte codepoint that
        # LXD split across two frames. Decoding here with errors='replace' would corrupt
        # that codepoint to U+FFFD before the client ever saw it. Binary framing also
        # drops the json.dumps/JSON.parse tax on the hot path. Shutdown is handled by
        # _run_tasks() cancelling this task, so no per-recv timeout poll is needed.
        """
        try:
            async for message in self.data_ws:
                if isinstance(message, bytes):
                    payload = message
                else:
                    # LXD's exec data socket is binary; this is a defensive fallback.
                    payload = message.encode("utf-8")

                # Flow control: if the browser is behind (outstanding output above the
                # high-water mark), wait for its ACKs to drain us below the low-water
                # mark before pushing more. This is the only thing that bounds the
                # browser's xterm write buffer - TCP backpressure bounds this socket but
                # not the unbounded in-process buffer downstream of it. The gate is also
                # released on shutdown, so a paused forwarder never hangs past teardown.
                if self._unacked >= self.HIGH_WATER and not self._shutdown.is_set():
                    self._resume.clear()
                    if self._unacked >= self.HIGH_WATER:
                        await self._resume.wait()
                if self._shutdown.is_set():
                    break

                if self._session:
                    self._session.add_download_bytes(len(payload))

                try:
                    await self.browser_ws.send_bytes(payload)
                    self._unacked += len(payload)
                except Exception as e:
                    logger.warning(f"Error sending to browser: {e}")
                    self._trigger_shutdown()
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
            self._trigger_shutdown()
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
            self._trigger_shutdown()
    
    async def _send_error(self, message: str):
        """Send error message to browser and close."""
        try:
            await self.browser_ws.send_json({"type": "error", "message": message})
            await self.browser_ws.close(code=1008, reason=message)
        except Exception:
            pass
    
    async def _cleanup(self):
        """Clean up all WebSocket connections."""
        self._trigger_shutdown()
        
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
    # ACL is rejected. This stops cross-instance exec/console on an otherwise-allowed cluster.
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
            "Terminal ACL denied: user=%s instance=%s@%s project=%s",
            user.user_id if user else "anonymous", instance_name, host, real_project,
        )
        await websocket.close(code=1008, reason="Access denied for this instance")
        return

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
        project=real_project,
        user_id=user_id,
        user_email=user_email,
        user_role=user_role,
    )

    await proxy.run()


@router.get(
    "/health",
    summary="Terminal service health check",
)
async def terminal_health():
    """Check if terminal service is available."""
    return {"status": "ok", "service": "terminal"}
