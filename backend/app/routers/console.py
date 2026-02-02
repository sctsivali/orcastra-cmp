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
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException
from pydantic import BaseModel, Field
import websockets
from websockets import State as WebSocketState
from websockets.exceptions import ConnectionClosed

from app.services.console_service import get_console_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/console", tags=["Console"])


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


# ============ Endpoints ============

@router.get(
    "/{instance_name}/capabilities",
    response_model=ConsoleCapabilitiesResponse,
    summary="Get console capabilities",
    description="Get available console types and features for an instance.",
)
async def get_capabilities(
    instance_name: str,
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
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
)
async def create_console_session(
    instance_name: str,
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
    console_type: str = Query("vga", description="Console type: vga or console"),
    width: int = Query(1024, ge=640, le=4096, description="Console width"),
    height: int = Query(768, ge=480, le=2160, description="Console height"),
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
        return ConsoleSessionResponse(success=False, error=error)
    
    return ConsoleSessionResponse(success=True, session=session_info)


@router.get(
    "/startup-order",
    response_model=StartupOrderResponse,
    summary="Get startup order",
    description="Get startup order for all instances on a host/project.",
)
async def get_startup_order(
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
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
)
async def update_startup_order(
    request: StartupOrderUpdateRequest,
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
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
)
async def list_isos(
    host: str = Query(..., description="Target host node"),
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
)
async def get_attached_iso(
    instance_name: str,
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
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
)
async def attach_iso(
    instance_name: str,
    request: AttachISORequest,
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
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
)
async def eject_iso(
    instance_name: str,
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
    device_name: str = Query("cdrom", description="Device name to eject"),
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
)
async def check_active_sessions(
    instance_name: str,
    host: str = Query(..., description="Target host node"),
    project: str = Query("default", description="LXD project name"),
):
    """
    Check for active console operations for an instance.
    
    Returns list of active console sessions that might conflict.
    Useful for warning users before opening a new console.
    """
    from app.services.lxd_service import get_lxd_service
    
    lxd_service = get_lxd_service()
    active_sessions: List[ActiveConsoleSession] = []
    
    try:
        client = lxd_service.connection_manager.get_client(host)
        if not client:
            return ActiveConsoleCheckResponse(has_active_session=False, sessions=[])
        
        # Get running operations
        response = client.api.operations.get(params={'recursion': '1'}, timeout=(5, 30))
        if response.status_code == 200:
            operations_data = response.json().get('metadata', {})
            running_ops = operations_data.get('running', [])
            
            for op in running_ops:
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
)
async def kill_all_sessions(
    instance_name: str,
    host: str = Query(..., description="Target host node"),
):
    """Kill all active console sessions for an instance."""
    from app.services.lxd_service import get_lxd_service
    
    lxd_service = get_lxd_service()
    killed_count = 0
    
    try:
        client = lxd_service.connection_manager.get_client(host)
        if not client:
            return ActionResponse(success=False, error=f"Cannot connect to host: {host}")
        
        # Get running operations
        response = client.api.operations.get(params={'recursion': '1'}, timeout=(5, 30))
        if response.status_code == 200:
            operations_data = response.json().get('metadata', {})
            running_ops = operations_data.get('running', [])
            
            for op in running_ops:
                description = op.get('description', '')
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
                        client.api.operations[op_id].delete()
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
)
async def kill_active_session(
    instance_name: str,
    operation_id: str,
    host: str = Query(..., description="Target host node"),
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
    ):
        self.browser_ws = browser_ws
        self.instance_name = instance_name
        self.host = host
        self.project = project
        self.console_type = console_type
        
        self.lxd_data_ws = None  # Data socket for SPICE data
        self.lxd_control_ws = None  # Control socket for lifecycle
        self._shutdown = asyncio.Event()
    
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
                                except asyncio.TimeoutError:
                                    logger.error(f"TIMEOUT sending to LXD data socket")
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
                                await self.lxd_data_ws.send(message["text"].encode('utf-8'))
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
                        
                        # Send as binary to browser
                        if isinstance(data, bytes):
                            await self.browser_ws.send_bytes(data)
                        else:
                            await self.browser_ws.send_bytes(data.encode('utf-8'))
                            
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
):
    """
    WebSocket endpoint for graphics console.
    
    LXD/Incus streams SPICE protocol data via WebSocket for VGA consoles.
    The client should use spice-html5 or similar SPICE client to render.
    
    IMPORTANT: spice-html5 requires 'binary' subprotocol to be accepted.
    """
    # Accept with 'binary' subprotocol - required by spice-html5
    # spice-html5 uses: new WebSocket(uri, 'binary')
    await websocket.accept(subprotocol="binary")
    
    logger.info(f"Console WebSocket connected: {instance_name}@{host}")
    
    proxy = ConsoleProxy(
        browser_ws=websocket,
        instance_name=instance_name,
        host=host,
        project=project,
        console_type=console_type,
    )
    
    await proxy.run()
    
    logger.info(f"Console WebSocket closed: {instance_name}@{host}")
