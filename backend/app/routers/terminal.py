"""
Terminal WebSocket Router.
Provides WebSocket endpoints for interactive terminal access to LXD instances.
"""

import asyncio
import json
import logging
from typing import Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException
import websockets
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State as WebSocketState

from app.services.terminal_service import get_terminal_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/terminal", tags=["Terminal"])


@router.get(
    "/session",
    summary="Create terminal session",
    description="Create a new terminal session for an instance and get WebSocket connection info.",
)
async def create_terminal_session(
    instance: str = Query(..., description="Instance name"),
    host: str = Query(..., description="Host/cluster name"),
    project: str = Query("default", description="LXD project name"),
    width: int = Query(120, ge=10, le=500, description="Terminal width in columns"),
    height: int = Query(40, ge=5, le=200, description="Terminal height in rows"),
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
        raise HTTPException(status_code=400, detail=error)
    
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
    ):
        self.browser_ws = browser_ws
        self.instance_name = instance_name
        self.host = host
        self.project = project
        
        self.data_ws: Optional[websockets.WebSocketClientProtocol] = None
        self.control_ws: Optional[websockets.WebSocketClientProtocol] = None
        self._shutdown = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
    
    async def run(self):
        """Main entry point - runs the terminal proxy."""
        terminal_service = get_terminal_service()
        
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
            
            # Send session info
            await self.browser_ws.send_json({
                "type": "session",
                "session_id": session_info["operation_id"],
                "instance": self.instance_name,
                "host": self.host,
            })
            
            # Connect to LXD WebSockets
            ssl_context = terminal_service.get_ssl_context(self.host)
            
            try:
                # Connect both WebSockets concurrently
                # Use reasonable ping intervals - WebSocket library handles keepalive
                self.data_ws, self.control_ws = await asyncio.wait_for(
                    asyncio.gather(
                        websockets.connect(
                            session_info["ws_url"],
                            ssl=ssl_context,
                            close_timeout=10,
                            ping_interval=20,  # Ping every 20s
                            ping_timeout=30,   # Allow 30s for pong
                            max_size=10 * 1024 * 1024,
                        ),
                        websockets.connect(
                            session_info["control_url"],
                            ssl=ssl_context,
                            close_timeout=10,
                            ping_interval=20,
                            ping_timeout=30,
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
        tasks = [
            asyncio.create_task(self._forward_browser_to_lxd(), name="browser_to_lxd"),
            asyncio.create_task(self._forward_lxd_to_browser(), name="lxd_to_browser"),
            asyncio.create_task(self._monitor_control(), name="control_monitor"),
            asyncio.create_task(self._keepalive(), name="keepalive"),
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
            
            # Wait for cancellation to complete
            await asyncio.gather(*tasks, return_exceptions=True)
    
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
                    await self.data_ws.send(input_data.encode('utf-8'))
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
                else:
                    text = message
                
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
    
    async def _keepalive(self):
        """Send periodic pings to browser to keep connection alive."""
        try:
            while not self._shutdown.is_set():
                await asyncio.sleep(30)  # Ping every 30 seconds
                
                if self._shutdown.is_set():
                    break
                
                try:
                    await self.browser_ws.send_json({"type": "ping"})
                except Exception:
                    self._shutdown.set()
                    break
                    
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Keepalive error: {e}")
            self._shutdown.set()
            raise
    
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
):
    """
    WebSocket endpoint for terminal connection.
    Acts as a proxy between browser and LXD instance exec.
    """
    await websocket.accept()
    logger.info(f"Terminal WebSocket connected: {instance_name}@{host}")
    
    proxy = TerminalProxy(
        browser_ws=websocket,
        instance_name=instance_name,
        host=host,
        project=project,
    )
    
    await proxy.run()


@router.get(
    "/health",
    summary="Terminal service health check",
)
async def terminal_health():
    """Check if terminal service is available."""
    return {"status": "ok", "service": "terminal"}
