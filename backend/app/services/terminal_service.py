"""Starts the LXD exec operations behind interactive terminal sessions."""

import logging
import ssl
from typing import Optional, Dict, Any
from dataclasses import dataclass
from enum import IntEnum

from app.services.lxd_service import get_lxd_service
from app.services.lxd_cert_manager import LXDCertificateManager
from app.core.vault_client import get_vault_client
from app.core.config import get_settings
from app.services.lxd_ssl import create_lxd_ssl_context
from app.core.lxd_path import safe_segment
from app.services.lxd.instance_metrics_sections import guest_agent_status

logger = logging.getLogger(__name__)

_NO_AGENT_MESSAGE = "The VM agent is not running, so a shell cannot open. Use the Serial or Graphics console."


class ControlCode(IntEnum):
    """LXD WebSocket control codes for exec operations."""
    STDIN = 0
    STDOUT = 1
    STDERR = 2
    CONTROL = 3  # For window resize


@dataclass
class TerminalSession:
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
    """Opens interactive exec operations; terminal_proxy.py relays their sockets."""
    
    def __init__(self):
        self.settings = get_settings()
        self.active_sessions: Dict[str, TerminalSession] = {}
        self._lxd_service = None
        self._cert_manager = None
    
    @property
    def lxd_service(self):
        if self._lxd_service is None:
            self._lxd_service = get_lxd_service()
        return self._lxd_service
    
    @property
    def cert_manager(self) -> LXDCertificateManager:
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
        """The exec command: bash when the image has it, else /bin/sh; a VM gets a login shell."""
        if instance_type == "virtual-machine":
            # Minimal images (ex; Alpine) ship no bash, and an exec of a missing
            # shell exits at once, which the terminal sees as a reconnect loop.
            # /bin/sh is always present.
            return ["/bin/sh", "-c", "if command -v bash >/dev/null 2>&1; then exec bash --login; else exec sh; fi"]
        else:
            # Same fallback: minimal containers may only have /bin/sh.
            return ["/bin/sh", "-c", "if command -v bash >/dev/null 2>&1; then exec bash; else exec sh; fi"]
    
    def get_fallback_shell(self) -> list:
        """Fallback shell when detection fails."""
        return ["/bin/sh"]
    
    @staticmethod
    def _vm_agent_unreachable(client: Any, instance: Dict[str, Any], instance_name: str, project: str) -> bool:
        """Whether a VM's agent is known to be down. An unread state is not proof, so it says no."""
        instance_type = instance.get("type")
        if instance_type != "virtual-machine":
            return False
        try:
            response = client.api.instances[safe_segment(instance_name)].state.get(
                params={"project": project or "default"}
            )
            state = response.json().get("metadata") or {}
        except Exception as exc:  # noqa: BLE001 - this is only a better error, never a new failure
            logger.info("State read before exec on %s failed: %s", instance_name, type(exc).__name__)
            return False
        return guest_agent_status(instance_type, instance.get("expanded_config"), state) == "unreachable"

    def open_exec_operation(
        self,
        instance_name: str,
        host: str,
        project: str = "default",
        width: int = 120,
        height: int = 40,
        shell: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Start an interactive exec and return where its sockets are. Blocking.

        Raises SessionSetupError. The result carries the per-fd secrets, so it never leaves
        this process.
        """
        from app.services.lxd_ws import SessionSetupError, connection_material, lxd_client, operation_urls, read_instance

        instance = read_instance(host, project, instance_name)
        status = instance.get("status", "Unknown")
        if status != "Running":
            raise SessionSetupError(f"The instance is not running (status: {status})", retryable=False)
        if shell is None:
            shell = self.get_shell_command(instance.get("type", "container"))

        client = lxd_client(host)
        if client is None:
            raise SessionSetupError(f"Cannot reach cluster {host}", retryable=True)
        # LXD accepts an exec on a VM whose agent is down and only then drops its sockets,
        # so the reason has to be found before submitting it.
        if self._vm_agent_unreachable(client, instance, instance_name, project):
            raise SessionSetupError(_NO_AGENT_MESSAGE, retryable=False)
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
            "record-output": False,
        }
        response = client.api.instances[safe_segment(instance_name)].exec.post(
            json=exec_data, params={"project": project or "default"}
        )
        operation = response.json()
        if response.status_code >= 400 or operation.get("type") == "error":
            message = str(operation.get("error") or response.status_code)
            # An exec into a VM needs its lxd-agent; say so rather than the daemon's wording.
            if "agent" in message.lower():
                raise SessionSetupError(_NO_AGENT_MESSAGE, retryable=False)
            raise SessionSetupError(f"The cluster refused the shell: {message}", retryable=False)
        base, _ssl = connection_material(host)
        operation_id, data_url, control_url = operation_urls(base, operation)
        if not operation_id:
            raise SessionSetupError("The cluster did not return an exec operation", retryable=True)
        logger.info("Exec operation %s for %s@%s", operation_id, instance_name, host)
        return {
            "operation_id": operation_id,
            "instance_name": instance_name,
            "host": host,
            "project": project,
            "ws_url": data_url,
            "control_url": control_url,
            "shell": shell,
        }

    def get_ssl_context(self, host: str) -> ssl.SSLContext:
        """SSL context for a WebSocket connection to LXD, from the cluster's Vault certificate."""
        cert_data = self.cert_manager.get_cluster_certificate(host)
        
        return create_lxd_ssl_context(cert_data)
    

_terminal_service: Optional[TerminalService] = None


def get_terminal_service() -> TerminalService:
    global _terminal_service
    if _terminal_service is None:
        _terminal_service = TerminalService()
    return _terminal_service
