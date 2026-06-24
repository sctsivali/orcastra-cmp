"""
Terminal Service - LXD Exec WebSocket Handler.
Provides interactive terminal access to LXD instances via WebSocket.
"""

import asyncio
import json
import logging
import ssl
import threading
import time
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


class _TerminalBuildError(Exception):
    """Known, user-facing failure while creating an exec session.

    Carries a message safe to surface to the client (e.g. "Instance must be
    running"); raised from the synchronous build path and mapped back to the
    (None, message) result by create_exec_session.
    """


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

    # important: terminal connect runs the SSL fetch from worker threads (offloaded
    # so it never blocks the event loop). An ssl.SSLContext is thread-safe and
    # reusable, so cache one per host briefly to skip the Vault round-trip on the
    # warm path. Mirrors console_service - the lock keeps the cache consistent under
    # concurrent connects.
    _SSL_CACHE_TTL = 60.0

    def __init__(self):
        self.settings = get_settings()
        self.active_sessions: Dict[str, TerminalSession] = {}
        self._lxd_service = None
        self._cert_manager = None
        self._ssl_cache: Dict[str, Tuple[ssl.SSLContext, float]] = {}
        self._ssl_lock = threading.Lock()
    
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
            # VMs typically have a proper login environment
            return ["/bin/bash", "--login"]
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
        """Create an exec session for interactive terminal access.

        The pylxd/Vault calls are synchronous and would block the event loop, so the
        whole blocking body runs in a worker thread (matching console_service and the
        LXD service layer). On a single uvicorn worker this is what keeps one terminal
        connect from stalling every other request. asyncio.to_thread copies the
        contextvars, so the org-scope cluster ACL is still enforced inside the thread.
        """
        try:
            session_info = await asyncio.to_thread(
                self._build_exec_session,
                instance_name, host, project, width, height, shell,
            )
            return session_info, None
        except _TerminalBuildError as e:
            # Known, user-facing failure (not running, not found, exec rejected).
            return None, str(e)
        except Exception as e:
            logger.error(f"Error creating exec session: {e}")
            return None, str(e)

    def _build_exec_session(
        self,
        instance_name: str,
        host: str,
        project: str,
        width: int,
        height: int,
        shell: Optional[list],
    ) -> Dict[str, Any]:
        """Synchronous body of create_exec_session - runs in a worker thread.

        Raises _TerminalBuildError with a user-facing message on known failures.
        """
        if not self.lxd_service.ensure_connection(host):
            raise _TerminalBuildError(f"Cannot connect to host: {host}")

        client = self.lxd_service.get_client_with_reconnect(host)
        if not client:
            raise _TerminalBuildError(f"No connection to host: {host}")

        # Resolve instance + type. pylxd's instances.get() can't pass a project, so
        # non-default projects go through the raw API.
        try:
            if project and project != "default":
                response = client.api.instances[instance_name].get(params={"project": project})
                instance_data = response.json().get("metadata", {})
                instance_status = instance_data.get("status", "Unknown")
                instance_type = instance_data.get("type", "container")
            else:
                instance = client.instances.get(instance_name)
                instance_status = instance.status
                instance_type = instance.type
        except Exception as e:
            raise _TerminalBuildError(f"Instance not found: {instance_name} - {str(e)}")

        if instance_status != "Running":
            raise _TerminalBuildError(f"Instance must be running. Current status: {instance_status}")

        # Determine shell command
        if shell is None:
            shell = self.get_shell_command(instance_type)

        # Get cluster endpoint
        cluster_info = self.cert_manager.get_cluster_info(host)
        endpoint = cluster_info.get("metadata", {}).get("endpoint", "")
        # Remove trailing slash to avoid double slashes in URL
        ws_endpoint = endpoint.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")

        # Create exec operation via LXD API (raw call for the interactive WebSocket).
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

        if project and project != "default":
            response = client.api.instances[instance_name].exec.post(
                json=exec_data,
                params={"project": project}
            )
        else:
            response = client.api.instances[instance_name].exec.post(json=exec_data)

        operation = response.json()
        if operation.get("type") == "error":
            error_msg = operation.get("error", "Unknown error")
            raise _TerminalBuildError(f"Exec failed: {error_msg}")

        metadata = operation.get("metadata", {})
        operation_id = metadata.get("id", "")
        ws_metadata = metadata.get("metadata", {})

        # control = lifecycle signals; "0" = stdin/stdout/stderr combined for interactive.
        fds = ws_metadata.get("fds", {})
        control_fd = fds.get("control", "")
        stdio_fd = fds.get("0", "")

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
        return session_info

    def get_ssl_context(self, host: str) -> ssl.SSLContext:
        """SSL context for the upstream LXD WebSocket, cached per host.

        The Vault cert fetch + context build is blocking, so callers offload this via
        asyncio.to_thread; the 60s cache means the warm path skips Vault entirely.
        The context is built outside the lock (a rare cold-cache double-fetch is
        harmless - an ssl.SSLContext is reusable) so a slow Vault round-trip never
        serializes other hosts' connects.
        """
        now = time.monotonic()
        with self._ssl_lock:
            cached = self._ssl_cache.get(host)
            if cached and (now - cached[1]) < self._SSL_CACHE_TTL:
                return cached[0]

        cert_data = self.cert_manager.get_cluster_certificate(host)
        ctx = create_lxd_ssl_context(cert_data)
        with self._ssl_lock:
            self._ssl_cache[host] = (ctx, time.monotonic())
        return ctx
    
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
