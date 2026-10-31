"""
Console Service - VGA/Graphics Console Handler.
Provides graphics console access to LXD VMs via WebSocket (SPICE protocol).

LXD/Incus VMs use QEMU with SPICE for graphics console access.
The console data is streamed via WebSocket from LXD API.
"""

import logging
from typing import Optional, Dict, Any, Tuple, List
from dataclasses import dataclass
from enum import Enum

from app.services.lxd_service import get_lxd_service
from app.services.lxd_cert_manager import LXDCertificateManager
from app.core.vault_client import get_vault_client
from app.core.config import get_settings
from app.core.lxd_path import safe_segment
from app.core.org_scope import project_is_visible
from app.core.tenant_visibility import filter_visible_lxd_references, tenant_acl_active
from app.services.lxd.project_scope import ALL_PROJECTS_READ_TIMEOUT, list_in_project, resolve_owner
from app.services.lxd.project_usage import parse_lxd_size

logger = logging.getLogger(__name__)


class GraphicsType(str, Enum):
    """Supported graphics types.
    
    LXD/Incus uses SPICE for VM graphics console.
    VNC is kept for future compatibility but SPICE is the default.
    """
    SPICE = "spice"  # Default for LXD/Incus VMs
    VNC = "vnc"      # Future/alternative
    NONE = "none"


class ConsoleType(str, Enum):
    """Console access types."""
    TEXT = "text"       # exec/shell console
    GRAPHICS = "graphics"  # VGA console


@dataclass
class ConsoleCapabilities:
    """Capabilities for an instance's console access."""
    instance_name: str
    instance_type: str  # container or virtual-machine
    host: str
    project: str
    text_console: bool = True  # always available for running instances
    graphics_console: bool = False
    graphics_type: GraphicsType = GraphicsType.NONE
    graphics_address: Optional[str] = None
    graphics_port: Optional[int] = None
    audio_enabled: bool = False
    usb_redirect: bool = False
    state: str = "Unknown"  # instance state (Running, Stopped, etc.)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "instance_name": self.instance_name,
            "instance_type": self.instance_type,
            "host": self.host,
            "project": self.project,
            "text_console": self.text_console,
            "graphics_console": self.graphics_console,
            "graphics_type": self.graphics_type.value,
            "graphics_address": self.graphics_address,
            "graphics_port": self.graphics_port,
            "audio_enabled": self.audio_enabled,
            "usb_redirect": self.usb_redirect,
            "state": self.state,
        }


@dataclass
class StartupOrderEntry:
    """Startup order configuration for an instance."""
    instance_name: str
    host: str
    project: str
    priority: int  # lower = starts first
    delay: int = 0  # seconds to wait before starting
    autostart: bool = True
    instance_type: str = "container"
    status: str = "Unknown"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "instance_name": self.instance_name,
            "host": self.host,
            "project": self.project,
            "priority": self.priority,
            "delay": self.delay,
            "autostart": self.autostart,
            "instance_type": self.instance_type,
            "status": self.status,
        }


@dataclass
class ISOInfo:
    """Information about an ISO file."""
    name: str
    pool: str
    volume: str
    path: str
    size: int = 0
    created_at: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "pool": self.pool,
            "volume": self.volume,
            "path": self.path,
            "size": self.size,
            "created_at": self.created_at,
        }


@dataclass
class AttachedISO:
    """Information about an attached ISO."""
    device_name: str
    source: str
    pool: Optional[str] = None
    boot_priority: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_name": self.device_name,
            "source": self.source,
            "pool": self.pool,
            "boot_priority": self.boot_priority,
        }


class ConsoleService:
    """
    Service for managing console access to LXD instances.
    Handles VGA/graphics consoles, startup order, and ISO management.
    """
    
    def __init__(self):
        self.settings = get_settings()
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
    
    def get_instance_capabilities(
        self,
        instance_name: str,
        host: str,
        project: str = "default"
    ) -> Tuple[Optional[ConsoleCapabilities], Optional[str]]:
        """
        Detect console capabilities for an instance.
        
        Returns:
            Tuple of (capabilities, error_message)
        """
        from app.services.lxd_ws import SessionSetupError, read_instance

        try:
            try:
                instance_data = read_instance(host, project, instance_name)
            except SessionSetupError as e:
                return None, str(e)

            instance_type = instance_data.get("type", "container")
            instance_status = instance_data.get("status", "Unknown")
            devices = instance_data.get("devices", {})
            
            # Initialize capabilities with instance state
            caps = ConsoleCapabilities(
                instance_name=instance_name,
                instance_type=instance_type,
                host=host,
                project=project,
                state=instance_status,
            )
            
            # Check for graphics devices (VMs only)
            if instance_type == "virtual-machine":
                for device_name, device_config in devices.items():
                    device_type = device_config.get("type", "")
                    
                    if device_type == "gpu":
                        # GPU passthrough device
                        gputype = device_config.get("gputype", "")
                        if gputype in ("physical", "mdev", "sriov"):
                            # Physical GPU passthrough - SPICE still used for console
                            caps.graphics_console = True
                            caps.graphics_type = GraphicsType.SPICE
                    
                    # Check for explicit graphics device
                    if device_name in ("graphics", "display", "vnc", "spice"):
                        caps.graphics_console = True
                        # LXD/Incus always uses SPICE for VGA console
                        caps.graphics_type = GraphicsType.SPICE
                        caps.audio_enabled = device_config.get("audio", "false").lower() == "true"
                        caps.usb_redirect = device_config.get("usb", "false").lower() == "true"
                
                # VMs always have VGA console available via LXD console API
                # LXD/Incus uses SPICE protocol for all VM graphics consoles
                caps.graphics_console = True
                if caps.graphics_type == GraphicsType.NONE:
                    caps.graphics_type = GraphicsType.SPICE  # LXD/Incus default
            
            return caps, None
            
        except Exception as e:
            logger.error(f"Error getting capabilities for {instance_name}: {e}")
            return None, str(e)
    
    def open_console_operation(
        self,
        instance_name: str,
        host: str,
        project: str = "default",
        console_type: str = "vga",
        width: int = 0,
        height: int = 0,
    ) -> Dict[str, Any]:
        """Start one LXD console operation and return where its sockets are. Blocking.

        ``vga`` is the SPICE display of a VM; ``console`` is the serial console of a VM or a
        container. Raises SessionSetupError. The result carries the per-fd secrets, so it never
        leaves this process.
        """
        from app.services.lxd_ws import SessionSetupError, connection_material, lxd_client, operation_urls, read_instance

        instance = read_instance(host, project, instance_name)
        instance_type = instance.get("type", "container")
        status = instance.get("status", "Unknown")
        if console_type == "vga" and instance_type != "virtual-machine":
            raise SessionSetupError("The graphical console is only available for virtual machines", retryable=False)
        if status != "Running":
            raise SessionSetupError(f"The instance is not running (status: {status})", retryable=False)

        client = lxd_client(host)
        if client is None:
            raise SessionSetupError(f"Cannot reach cluster {host}", retryable=True)
        # note: LXD-UI sends 0x0 for vga so QEMU keeps its own mode; a serial console takes the
        # terminal's size so the guest's tty wraps where the browser does.
        body = {"type": console_type, "width": max(0, int(width)), "height": max(0, int(height))}
        response = client.api.instances[safe_segment(instance_name)].console.post(
            json=body, params={"project": project or "default"}
        )
        operation = response.json()
        if response.status_code >= 400 or operation.get("type") == "error":
            raise SessionSetupError(
                f"The cluster refused the console: {operation.get('error') or response.status_code}",
                retryable=False,
            )
        base, _ssl = connection_material(host)
        operation_id, data_url, control_url = operation_urls(base, operation)
        if not operation_id:
            raise SessionSetupError("The cluster did not return a console operation", retryable=True)
        logger.info("Console operation %s (%s) for %s@%s", operation_id, console_type, instance_name, host)
        return {
            "operation_id": operation_id,
            "instance_name": instance_name,
            "host": host,
            "project": project,
            "console_type": console_type,
            "ws_url": data_url,
            "control_url": control_url,
        }

    def get_startup_order(
        self,
        host: str,
        project: str = "default"
    ) -> Tuple[List[StartupOrderEntry], Optional[str]]:
        """
        Get startup order for all instances on a host/project.
        """
        try:
            if not self.lxd_service.ensure_connection(host):
                return [], f"Cannot connect to host: {host}"
            
            client = self.lxd_service.get_client_with_reconnect(host)
            if not client:
                return [], f"No connection to host: {host}"
            
            entries = []
            
            try:
                response = client.api.instances.get(
                    params={"project": project, "recursion": 1}
                )
                instances_data = response.json().get("metadata", [])
                
                for inst in instances_data:
                    config = inst.get("config", {})
                    
                    # Extract boot.autostart settings
                    autostart = config.get("boot.autostart", "false").lower() == "true"
                    priority_str = config.get("boot.autostart.priority", "0")
                    delay_str = config.get("boot.autostart.delay", "0")
                    
                    try:
                        priority = int(priority_str)
                    except ValueError:
                        priority = 0
                    
                    try:
                        delay = int(delay_str)
                    except ValueError:
                        delay = 0
                    
                    entry = StartupOrderEntry(
                        instance_name=inst.get("name", ""),
                        host=host,
                        project=project,
                        priority=priority,
                        delay=delay,
                        autostart=autostart,
                        instance_type=inst.get("type", "container"),
                        status=inst.get("status", "Unknown"),
                    )
                    entries.append(entry)
                
                # Sort by priority (lower first), then by name
                entries.sort(key=lambda x: (x.priority, x.instance_name))
                
            except Exception as e:
                logger.error(f"Error listing instances: {e}")
                return [], str(e)
            
            return entries, None
            
        except Exception as e:
            logger.error(f"Error getting startup order: {e}")
            return [], str(e)
    
    def update_startup_order(
        self,
        host: str,
        project: str,
        updates: List[Dict[str, Any]]
    ) -> Tuple[bool, Optional[str]]:
        """
        Update startup order for multiple instances.
        """
        try:
            if not self.lxd_service.ensure_connection(host):
                return False, f"Cannot connect to host: {host}"
            
            client = self.lxd_service.get_client_with_reconnect(host)
            if not client:
                return False, f"No connection to host: {host}"
            
            errors = []
            
            for update in updates:
                instance_name = update.get("instance_name")
                if not instance_name:
                    continue
                
                try:
                    # Get current instance config
                    if project and project != "default":
                        response = client.api.instances[safe_segment(instance_name)].get(
                            params={"project": project}
                        )
                        instance_data = response.json().get("metadata", {})
                    else:
                        instance = client.instances.get(safe_segment(instance_name))
                        instance_data = {
                            "config": dict(instance.config),
                            "devices": dict(instance.devices),
                            "profiles": list(instance.profiles),
                        }
                    
                    # Update config
                    config = dict(instance_data.get("config", {}))
                    
                    if "priority" in update:
                        config["boot.autostart.priority"] = str(update["priority"])
                    
                    if "delay" in update:
                        config["boot.autostart.delay"] = str(update["delay"])
                    
                    if "autostart" in update:
                        config["boot.autostart"] = "true" if update["autostart"] else "false"
                    
                    # Apply update
                    patch_data = {"config": config}
                    
                    if project and project != "default":
                        client.api.instances[safe_segment(instance_name)].patch(
                            json=patch_data,
                            params={"project": project}
                        )
                    else:
                        instance.config = config
                        instance.save(wait=True)
                    
                    logger.info(f"Updated startup order for {instance_name}")
                    
                except Exception as e:
                    logger.error(f"Failed to update {instance_name}: {str(e)}")
                    errors.append(f"Could not update {instance_name}: {str(e)}")
            
            if errors:
                return False, "; ".join(errors)
            
            return True, None
            
        except Exception as e:
            logger.error(f"Error updating startup order: {e}")
            return False, str(e)
    
    def get_available_isos(
        self,
        host: str,
        project: str = "default",
    ) -> Tuple[List[ISOInfo], Optional[str]]:
        """
        Discover available ISO files from all storage pools.
        """
        try:
            if not self.lxd_service.ensure_connection(host):
                return [], f"Cannot connect to host: {host}"
            
            client = self.lxd_service.get_client_with_reconnect(host)
            if not client:
                return [], f"No connection to host: {host}"
            owner = resolve_owner(client, "storage-volumes", project)
            if not project_is_visible(host, owner):
                return [], "Access denied to the ISO storage project"
            
            isos = []
            
            try:
                # Get all storage pools
                pools_response = client.api.storage_pools.get(timeout=ALL_PROJECTS_READ_TIMEOUT)
                pools = pools_response.json().get("metadata", [])
                
                for pool_url in pools:
                    pool_name = pool_url.split("/")[-1]
                    
                    try:
                        # Get volumes of type "custom" (where ISOs are stored)
                        # or type "iso" if available
                        volumes = list_in_project(
                            client, "storage-volumes", owner,
                            path=f"storage-pools/{safe_segment(pool_name)}/volumes",
                        )
                        
                        for vol in volumes:
                            vol_name = vol.get("name", "")
                            vol_type = vol.get("type", "")
                            content_type = vol.get("content_type", "")
                            config = vol.get("config", {})
                            
                            # Check if it's an ISO volume
                            # ISOs can be stored as custom volumes with iso content type
                            # or have .iso extension in the name
                            is_iso = (
                                content_type == "iso" or 
                                vol_name.lower().endswith(".iso") or
                                config.get("content_type") == "iso"
                            )
                            
                            if is_iso or vol_type == "iso":
                                visible_usage = filter_visible_lxd_references(host, vol.get("used_by") or [])
                                if tenant_acl_active() and not visible_usage:
                                    continue
                                size_str = config.get("size", "0")
                                try:
                                    # Parse size (could be like "1GiB", "500MiB", etc)
                                    size = parse_lxd_size(size_str) or 0
                                except (TypeError, ValueError, OverflowError):
                                    size = 0
                                
                                iso_info = ISOInfo(
                                    name=vol_name,
                                    pool=pool_name,
                                    volume=vol_name,
                                    path=f"{pool_name}/{vol_name}",
                                    size=size,
                                    created_at=vol.get("created_at"),
                                )
                                isos.append(iso_info)
                        
                        # Also check for block volumes that might be ISOs
                        # Some setups store ISOs as block devices
                        
                    except Exception as e:
                        logger.warning(f"Error listing volumes in pool {pool_name}: {e}")
                        return [], "ISO discovery was incomplete. Retry the request."
                
            except Exception as e:
                logger.error(f"Error listing storage pools: {e}")
                return [], str(e)
            
            return isos, None
            
        except Exception as e:
            logger.error(f"Error getting ISOs: {e}")
            return [], str(e)
    
    def get_instance_iso(
        self,
        instance_name: str,
        host: str,
        project: str = "default"
    ) -> Tuple[Optional[AttachedISO], Optional[str]]:
        """
        Get currently attached ISO for an instance.
        """
        try:
            if not self.lxd_service.ensure_connection(host):
                return None, f"Cannot connect to host: {host}"
            
            client = self.lxd_service.get_client_with_reconnect(host)
            if not client:
                return None, f"No connection to host: {host}"
            
            try:
                if project and project != "default":
                    response = client.api.instances[safe_segment(instance_name)].get(
                        params={"project": project}
                    )
                    instance_data = response.json().get("metadata", {})
                    devices = instance_data.get("devices", {})
                else:
                    instance = client.instances.get(safe_segment(instance_name))
                    devices = instance.devices
                
                # Look for disk devices with cdrom/iso source
                for device_name, device_config in devices.items():
                    device_type = device_config.get("type", "")
                    
                    if device_type == "disk":
                        source = device_config.get("source", "")
                        
                        # Check if it's an ISO (by source path or special markers)
                        if (
                            source.lower().endswith(".iso") or
                            device_config.get("boot.priority") is not None or
                            device_name in ("cdrom", "iso", "install-media")
                        ):
                            # LXD: "A higher value indicates a higher boot
                            # precedence." An unset boot.priority therefore means
                            # no precedence at all (lowest), not the highest --
                            # reporting 99 here would read as "boots first".
                            boot_priority_str = device_config.get("boot.priority", "0")
                            try:
                                boot_priority = int(boot_priority_str)
                            except (TypeError, ValueError):
                                boot_priority = 0
                            
                            attached = AttachedISO(
                                device_name=device_name,
                                source=source,
                                pool=device_config.get("pool"),
                                boot_priority=boot_priority,
                            )
                            return attached, None
                
                return None, None  # No ISO attached
                
            except Exception as e:
                return None, f"Instance not found: {instance_name} - {str(e)}"
            
        except Exception as e:
            logger.error(f"Error getting ISO for {instance_name}: {e}")
            return None, str(e)
    
    def attach_iso(
        self,
        instance_name: str,
        host: str,
        project: str,
        pool: str,
        volume: str,
        device_name: str = "cdrom",
        boot_priority: int = 0,
    ) -> Tuple[bool, Optional[str]]:
        """
        Attach an ISO to a VM instance.
        
        Note: This typically requires the VM to be stopped or restarted.
        """
        try:
            if not self.lxd_service.ensure_connection(host):
                return False, f"Cannot connect to host: {host}"
            
            client = self.lxd_service.get_client_with_reconnect(host)
            if not client:
                return False, f"No connection to host: {host}"
            
            try:
                # Get current instance
                if project and project != "default":
                    response = client.api.instances[safe_segment(instance_name)].get(
                        params={"project": project}
                    )
                    instance_data = response.json().get("metadata", {})
                    devices = dict(instance_data.get("devices", {}))
                    instance_status = instance_data.get("status", "Unknown")
                else:
                    instance = client.instances.get(safe_segment(instance_name))
                    devices = dict(instance.devices)
                    instance_status = instance.status
                
                # Check if VM is running (warn but allow)
                needs_restart = instance_status == "Running"
                
                # Add/update cdrom device
                devices[device_name] = {
                    "type": "disk",
                    "source": volume,
                    "pool": pool,
                    "boot.priority": str(boot_priority),
                }
                
                # Apply changes
                patch_data = {"devices": devices}
                
                if project and project != "default":
                    client.api.instances[safe_segment(instance_name)].patch(
                        json=patch_data,
                        params={"project": project}
                    )
                else:
                    instance.devices = devices
                    instance.save(wait=True)
                
                msg = "ISO attached successfully"
                if needs_restart:
                    msg += " (restart required for changes to take effect)"
                
                logger.info(f"Attached ISO {volume} to {instance_name}")
                return True, msg
                
            except Exception as e:
                return False, f"Could not attach ISO: {str(e)}"
            
        except Exception as e:
            logger.error(f"Error attaching ISO: {e}")
            return False, str(e)
    
    def eject_iso(
        self,
        instance_name: str,
        host: str,
        project: str,
        device_name: str = "cdrom",
    ) -> Tuple[bool, Optional[str]]:
        """
        Eject (remove) an ISO from a VM instance.
        """
        try:
            if not self.lxd_service.ensure_connection(host):
                return False, f"Cannot connect to host: {host}"
            
            client = self.lxd_service.get_client_with_reconnect(host)
            if not client:
                return False, f"No connection to host: {host}"
            
            try:
                if project and project != "default":
                    response = client.api.instances[safe_segment(instance_name)].get(
                        params={"project": project}
                    )
                    instance_data = response.json().get("metadata", {})
                    devices = dict(instance_data.get("devices", {}))
                else:
                    instance = client.instances.get(safe_segment(instance_name))
                    devices = dict(instance.devices)
                
                if device_name not in devices:
                    return False, f"Device {device_name} not found"
                
                # Remove the device
                del devices[device_name]
                
                # Apply changes
                patch_data = {"devices": devices}
                
                if project and project != "default":
                    client.api.instances[safe_segment(instance_name)].patch(
                        json=patch_data,
                        params={"project": project}
                    )
                else:
                    instance.devices = devices
                    instance.save(wait=True)
                
                logger.info(f"Ejected ISO from {instance_name} (device: {device_name})")
                return True, "ISO ejected successfully"
                
            except Exception as e:
                return False, f"Could not eject ISO: {str(e)}"
            
        except Exception as e:
            logger.error(f"Error ejecting ISO: {e}")
            return False, str(e)


# Singleton instance
_console_service: Optional[ConsoleService] = None


def get_console_service() -> ConsoleService:
    """Get or create console service singleton."""
    global _console_service
    if _console_service is None:
        _console_service = ConsoleService()
    return _console_service
