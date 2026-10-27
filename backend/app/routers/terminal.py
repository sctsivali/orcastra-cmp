"""
Terminal WebSocket Router.
Provides WebSocket endpoints for interactive terminal access to LXD instances.
"""

import asyncio
import json
import logging
from typing import Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, Depends
import websockets
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State as WebSocketState

from app.services.terminal_service import get_terminal_service
from app.services.activity_logger import get_activity_logger
from app.services.session_tracker import get_session_tracker, SessionProtocol
from app.services.session_models import new_session_id
from app.core.dependencies import AuthenticatedUser
from app.core.instance_access import verify_instance_access
from app.core.ws_auth import ws_user
from app.services.ws_session_guard import instance_session_check, reauthorize_while_open
from app.core.org_scope import set_org_scope_for_ws
from app.services.migration_fence import (
    MigrationFenceConflict,
    assert_instance_not_migration_fenced,
    guard_instance_mutation,
)

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/terminal",
    tags=["Terminal"],
)


# note: `GET /terminal/session` used to live here. It created a real LXD exec operation and
# returned `session_info` verbatim, which carries `ws_url` and `control_url` complete with the
# operation's `?secret=` file-descriptor tokens and the cluster endpoint. Anyone holding that
# response could talk to LXD directly: outside this proxy, outside session tracking, outside the
# kill paths, and outside `watch_grant_while_open`, which is what revokes a break-glass session
# mid-flight. Measured before removing it: zero calls in seven days and no caller anywhere in
# `frontend/src`. The app uses the WebSocket below, which mints its own session internally and
# never discloses the secret. Removed rather than redacted, because without the secret the
# endpoint answers nothing anyone wanted.


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
            
            try:
                # The short critical section ends after the exec operation is submitted and
                # registered. It never spans the interactive WebSocket session.
                async with guard_instance_mutation(
                    cluster_id=self.host,
                    project=self.project,
                    instance_name=self.instance_name,
                ):
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

                    session_id = new_session_id(
                        session_info.get("operation_id"),
                        "terminal",
                    )
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
                    # Publish cross-worker tracking before admission is released.
                    tracker.mark_connected(session_id)
            except MigrationFenceConflict as exc:
                await self._send_error(exc.detail)
                return
            
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
    user: Optional[AuthenticatedUser] = Depends(ws_user("terminal")),
):
    """
    WebSocket endpoint for terminal connection.
    Acts as a proxy between browser and LXD instance exec.
    Opened with a ticket from POST /ws-tickets; re-authorized every minute while open and
    closed with 4403 once access ends.
    """
    # Validate auth when enabled
    from app.core.config import get_settings as _gs
    _settings = _gs()
    # important: gated on auth_enabled ALONE. This used to also require
    # `_settings.authentik_issuer`, which is the variable whose absence produces the
    # unauthenticated `user`, so the guard switched itself off in exactly the situation it
    # existed to catch. ws_user now refuses a misconfigured server itself.
    if _settings.auth_enabled and user is None:
        await websocket.close(code=1008, reason="Authentication required")
        return

    # set org scope for WebSocket (context variable)
    await set_org_scope_for_ws(user, interactive=True)

    # important: the console WebSocket added this and the terminal one never had it, so the
    # organisation boundary here rested entirely on the break-glass gate below and on a flag whose
    # default was off. verify_cluster_access appeared seventeen times in console.py and not
    # once here, which is why this file was blanket-exempt from the cluster-scope ratchet; that
    # exemption is gone now, so the ratchet enforces this call. Closing rather than raising,
    # since HTTPException is meaningless once the socket is being negotiated.
    from fastapi import HTTPException as _HTTPExc
    try:
        # The project bound, the tenant's view grant and, behind the rollout flag, its console
        # grant: the one instance gate the re-authorization below repeats every minute.
        project = verify_instance_access(host, project, instance_name, "console")
    except _HTTPExc as exc:
        await websocket.close(code=1008, reason="forbidden")
        logger.warning(
            "Terminal WS blocked (%s): %s@%s user=%s",
            exc.detail, instance_name, host, getattr(user, "email", "?"),
        )
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

    try:
        await assert_instance_not_migration_fenced(
            cluster_id=host,
            project=project,
            instance_name=instance_name,
        )
    except MigrationFenceConflict:
        await websocket.close(code=1008, reason="Instance fenced by Migration Center")
        return

    await websocket.accept()
    user_id = user.user_id if user else "anonymous"
    logger.info(f"Terminal WebSocket connected: {instance_name}@{host} user={user_id}")

    # important: this is the only emitter of terminal.session.create now, and until this line
    # there was none on the socket at all. The removed REST endpoint carried it, so a client
    # that connected straight to the WebSocket, which is what the app itself does, opened a root
    # shell in a guest and left nothing in the activity index. Session tracking recorded it; the
    # index a human reviews did not.
    activity_logger.log_activity(
        action="terminal.session.create",
        target_type="instance",
        target_id=instance_name,
        target_host=host,
        target_project=project,
        result="success",
        details={"transport": "websocket", "break_glass_grant_id": _bg_grant_id},
        target_organization_id=_target_org_id,
        target_organization_slug=_target_org_slug,
    )

    # Extract user details for session tracking
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
    reauthorize = asyncio.create_task(reauthorize_while_open(
        websocket, user, instance_session_check(host, project, instance_name), label=f"terminal {instance_name}@{host}",
    ))
    try:
        await proxy.run()
    finally:
        reauthorize.cancel()
        if _bg_watch:
            _bg_watch.cancel()


@router.get(
    "/health",
    summary="Terminal service health check",
)
async def terminal_health():
    """Check if terminal service is available."""
    return {"status": "ok", "service": "terminal"}
