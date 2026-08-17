"""
Terminal Service - LXD Exec WebSocket Handler.
Provides interactive terminal access to LXD instances via WebSocket.
"""

import json
import logging
import ssl
import websockets
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass
from enum import IntEnum

from app.services.lxd_service import get_lxd_service
from app.services.lxd_cert_manager import LXDCertificateManager
from app.core.vault_client import get_vault_client
from app.core.config import get_settings
from app.services.lxd_ssl import create_lxd_ssl_context

logger = logging.getLogger(__name__)


class ControlCode(IntEnum):
    """LXD WebSocket control codes for exec operations."""
    STDIN = 0
    STDOUT = 1
    STDERR = 2
    CONTROL = 3  # For window resize


@dataclass
class TerminalSession:
    """Represents an active terminal session."""
    session_id: str
    instance_name: str
    host: str
    project: str
    ws_url: str
    control_url: str
    secret: str
    control_secret: str
    connected: bool = False


class TerminalService:
    """
    Service for managing terminal connections to LXD instances.
    Handles LXD exec API integration with WebSocket forwarding.
    """
    
    def __init__(self):
        self.settings = get_settings()
        self.active_sessions: Dict[str, TerminalSession] = {}
        self._lxd_service = None
        self._cert_manager = None
    
    @property
    def lxd_service(self):
        """Lazy load LXD service."""
        if self._lxd_service is None:
            self._lxd_service = get_lxd_service()
        return self._lxd_service
    
    @property
    def cert_manager(self) -> LXDCertificateManager:
        """Lazy load certificate manager."""
        if self._cert_manager is None:
            vault_client = get_vault_client(
                self.settings.vault_addr,
                self.settings.vault_token
            )
            self._cert_manager = LXDCertificateManager(
                vault_client=vault_client,
                clusters_path=self.settings.vault_clusters_path
            )
        return self._cert_manager
    
    def get_shell_command(self, instance_type: str = "container") -> list:
        """
        Get appropriate shell command for instance type.
        For VMs with lxd-agent, use login shell.
        For containers, use smart shell detection that works on any distro.
        """
        if instance_type == "virtual-machine":
            # /bin/sh is guaranteed on all distros; try bash (login) for nicer UX
            # and fall back to sh. Hardcoding bash broke minimal VM images (e.g.
            # Alpine) that ship no bash -> exec exits instantly -> the terminal
            # disconnect/reconnect loop.
            return ["/bin/sh", "-c", "if command -v bash >/dev/null 2>&1; then exec bash --login; else exec sh; fi"]
        else:
            # important: /bin/sh is guaranteed on all Linux distros (Alpine, BusyBox, Ubuntu, etc.)
            # This tries bash first for better UX (tab completion, history), falls back to sh.
            # Fixes immediate disconnect on minimal containers that only have /bin/sh.
            return ["/bin/sh", "-c", "if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi"]
    
    def get_fallback_shell(self) -> list:
        """Fallback shell when smart detection fails."""
        return ["/bin/sh"]
    
    async def create_exec_session(
        self,
        instance_name: str,
        host: str,
        project: str = "default",
        width: int = 120,
        height: int = 40,
        shell: Optional[list] = None
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        Create an exec session for interactive terminal access.
        """
        try:
            # Ensure connection to the host
            if not self.lxd_service.ensure_connection(host):
                return None, f"Cannot connect to host: {host}"
            
            # Get client
            client = self.lxd_service.get_client_with_reconnect(host)
            if not client:
                return None, f"No connection to host: {host}"
            
            # Get instance
            try:
                # For non-default projects, use raw API to get instance
                # pylxd's instances.get() doesn't support project parameter directly
                if project and project != "default":
                    # Use API directly with project parameter
                    response = client.api.instances[instance_name].get(params={"project": project})
                    instance_data = response.json().get("metadata", {})
                    instance_status = instance_data.get("status", "Unknown")
                    instance_type = instance_data.get("type", "container")
                    
                    # Check status
                    if instance_status != "Running":
                        return None, f"Instance must be running. Current status: {instance_status}"
                else:
                    instance = client.instances.get(instance_name)
                    instance_status = instance.status
                    instance_type = instance.type
                    
                    if instance_status != "Running":
                        return None, f"Instance must be running. Current status: {instance_status}"
            except Exception as e:
                return None, f"Instance not found: {instance_name} - {str(e)}"
            
            # Determine shell command
            if shell is None:
                shell = self.get_shell_command(instance_type if project and project != "default" else instance.type)
            
            # Get cluster endpoint
            cluster_info = self.cert_manager.get_cluster_info(host)
            endpoint = cluster_info.get("metadata", {}).get("endpoint", "")
            
            # Parse endpoint for WebSocket URL
            # Remove trailing slash to avoid double slashes in URL
            ws_endpoint = endpoint.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
            
            # Create exec operation via LXD API
            # Using raw API call for exec with WebSocket
            exec_data = {
                "command": shell,
                "environment": {
                    "TERM": "xterm-256color",
                    "HOME": "/root",
                    "USER": "root",
                    "SHELL": shell[0],
                    "LANG": "C.UTF-8",
                },
                "wait-for-websocket": True,
                "interactive": True,
                "width": width,
                "height": height,
                "record-output": False,  # Don't record, pure interactive
            }
            
            # Execute via pylxd raw API (include project parameter for non-default projects)
            if project and project != "default":
                response = client.api.instances[instance_name].exec.post(
                    json=exec_data,
                    params={"project": project}
                )
            else:
                response = client.api.instances[instance_name].exec.post(json=exec_data)
            
            # Get operation details
            operation = response.json()
            if operation.get("type") == "error":
                error_msg = operation.get("error", "Unknown error")
                return None, f"Exec failed: {error_msg}"
            
            # Extract WebSocket metadata
            metadata = operation.get("metadata", {})
            operation_id = metadata.get("id", "")
            ws_metadata = metadata.get("metadata", {})
            
            # Get secrets for WebSocket connections
            fds = ws_metadata.get("fds", {})
            control_fd = fds.get("control", "")
            stdio_fd = fds.get("0", "")  # stdin/stdout/stderr combined for interactive
            
            # Build WebSocket URLs
            ws_base = f"{ws_endpoint}/1.0/operations/{operation_id}/websocket"
            
            session_info = {
                "operation_id": operation_id,
                "instance_name": instance_name,
                "host": host,
                "project": project,
                "ws_url": f"{ws_base}?secret={stdio_fd}",
                "control_url": f"{ws_base}?secret={control_fd}",
                "endpoint": endpoint,
                "shell": shell,
            }
            
            logger.info(f"Created exec session for {instance_name}@{host}: {operation_id}")
            return session_info, None
            
        except Exception as e:
            logger.error(f"Error creating exec session: {e}")
            return None, str(e)
    
    def get_ssl_context(self, host: str) -> ssl.SSLContext:
        """
        Create SSL context for WebSocket connection to LXD.
        Uses certificates from Vault.
        
        Note: Temp files are kept for the process lifetime to avoid SSL errors
        during long-running connections.
        """
        # Get certificates from Vault
        cert_data = self.cert_manager.get_cluster_certificate(host)
        
        return create_lxd_ssl_context(cert_data)
    
    async def resize_terminal(
        self,
        control_url: str,
        host: str,
        width: int,
        height: int
    ) -> bool:
        """
        Send resize signal to terminal via control WebSocket.
        """
        try:
            ssl_context = self.get_ssl_context(host)
            
            async with websockets.connect(
                control_url,
                ssl=ssl_context,
                close_timeout=5
            ) as ws:
                resize_msg = json.dumps({
                    "command": "window-resize",
                    "args": {"width": width, "height": height}
                })
                await ws.send(resize_msg)
                return True
        except Exception as e:
            logger.error(f"Error resizing terminal: {e}")
            return False


# Singleton instance
_terminal_service: Optional[TerminalService] = None


def get_terminal_service() -> TerminalService:
    """Get or create terminal service singleton."""
    global _terminal_service
    if _terminal_service is None:
        _terminal_service = TerminalService()
    return _terminal_service
