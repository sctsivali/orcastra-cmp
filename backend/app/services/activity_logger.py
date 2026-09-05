"""
Activity Logger Service
Provides audit/activity logging for sensitive operations.

Activity logs are written to stdout in structured JSON format,
collected by Fluent Bit, and stored in OpenSearch orcastra-audit-* indices.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from enum import Enum

from app.core.request_context import (
    get_request_context,
    get_request_id,
    generate_event_id,
)
from app.core.config import get_settings


class EventCategory(str, Enum):
    """Event categories for audit logging."""
    IDENTITY = "identity"
    LXD_INFRA = "lxd_infra"
    STORAGE = "storage"
    NETWORK = "network"
    ADMIN = "admin"
    SETTINGS = "settings"
    VAULT = "vault"


class EventSeverity(str, Enum):
    """Event severity levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# Action to category mapping
ACTION_CATEGORIES = {
    # Identity
    "auth.login": EventCategory.IDENTITY,
    "auth.logout": EventCategory.IDENTITY,
    "auth.token.refresh": EventCategory.IDENTITY,
    "user.ssh_key.create": EventCategory.IDENTITY,
    "user.ssh_key.delete": EventCategory.IDENTITY,
    
    # LXD Infrastructure
    "cluster.register": EventCategory.LXD_INFRA,
    "cluster.delete": EventCategory.LXD_INFRA,
    "cluster.auth_check": EventCategory.LXD_INFRA,
    "instance.create": EventCategory.LXD_INFRA,
    "instance.start": EventCategory.LXD_INFRA,
    "instance.stop": EventCategory.LXD_INFRA,
    "instance.restart": EventCategory.LXD_INFRA,
    "instance.delete": EventCategory.LXD_INFRA,
    "instance.migrate": EventCategory.LXD_INFRA,
    "vm.create": EventCategory.LXD_INFRA,
    "vm.start": EventCategory.LXD_INFRA,
    "vm.stop": EventCategory.LXD_INFRA,
    "vm.restart": EventCategory.LXD_INFRA,
    "vm.delete": EventCategory.LXD_INFRA,
    "project.create": EventCategory.LXD_INFRA,
    "project.delete": EventCategory.LXD_INFRA,
    "profile.create": EventCategory.LXD_INFRA,
    "profile.update": EventCategory.LXD_INFRA,
    "profile.delete": EventCategory.LXD_INFRA,
    "image.copy": EventCategory.LXD_INFRA,
    "image.delete": EventCategory.LXD_INFRA,
    
    # Storage
    "storage.pool.create": EventCategory.STORAGE,
    "storage.pool.delete": EventCategory.STORAGE,
    "storage.volume.create": EventCategory.STORAGE,
    "storage.volume.delete": EventCategory.STORAGE,
    "storage.volume.resize": EventCategory.STORAGE,
    
    # Network
    "network.create": EventCategory.NETWORK,
    "network.delete": EventCategory.NETWORK,
    "network.acl.create": EventCategory.NETWORK,
    "network.acl.delete": EventCategory.NETWORK,
    
    # Admin
    "terminal.session.create": EventCategory.ADMIN,
    "console.session.create": EventCategory.ADMIN,
    # The key-authenticated surface. `integration.instance.exec` and `integration.instance.files`
    # are the API-key equivalents of the two session actions above: they reach inside a guest.
    # `integration.proxy.request` covers everything else that goes through the raw proxy, which
    # had no audit record at all before, only an app log line that the deduplication filter
    # collapsed.
    "integration.instance.exec": EventCategory.ADMIN,
    "integration.instance.files": EventCategory.ADMIN,
    # Seeing and driving a guest's graphical session. Three names rather than one so an alert can
    # tell an agent looking at a screen from an agent typing into it: they are different events to
    # whoever reviews them, and only one of them can change anything.
    "desktop_control.consent.set": EventCategory.ADMIN,
    "integration.instance.desktop_view": EventCategory.ADMIN,
    "integration.instance.desktop_input": EventCategory.ADMIN,
    "integration.instance.desktop_clipboard": EventCategory.ADMIN,
    "integration.proxy.request": EventCategory.ADMIN,
    # Credential lifecycle. The actor here is a human operator holding a JWT, not the key,
    # so these carry the operator in `actor` and the key only as an annotation in details.
    "integration.api_key.create": EventCategory.ADMIN,
    "integration.api_key.revoke": EventCategory.ADMIN,
    "integration.api_key.delete": EventCategory.ADMIN,
    # Add-ons. Installing one hands a third party a credential into an organization, so the
    # grant-shaped actions are high severity and the descriptive ones are not.
    "extension.register": EventCategory.SETTINGS,
    "extension.update": EventCategory.SETTINGS,
    "extension.delete": EventCategory.SETTINGS,
    "extension.install": EventCategory.ADMIN,
    "extension.install.rescope": EventCategory.ADMIN,
    "extension.install.suspend": EventCategory.ADMIN,
    "extension.install.resume": EventCategory.ADMIN,
    "extension.install.credential.rotate": EventCategory.ADMIN,
    "extension.install.settings": EventCategory.SETTINGS,
    "extension.install.uninstall": EventCategory.ADMIN,
    "extension.install.scope_narrowed": EventCategory.ADMIN,
    "extension.credential.orphan_revoked": EventCategory.ADMIN,
    "extension.webhook.subscribe": EventCategory.SETTINGS,
    "extension.webhook.unsubscribe": EventCategory.SETTINGS,
    "extension.webhook.secret.rotate": EventCategory.ADMIN,
    "extension.webhook.redeliver": EventCategory.ADMIN,
    "extension.webhook.suspended": EventCategory.ADMIN,
    # A destructive proxy call removes the same resource the dashboard's own endpoint would, so
    # it is named for the act rather than for the transport. Without these it landed under
    # `integration.proxy.request`, which is not high severity, and an alert on `image.delete`
    # could never see the API-key route.
    "integration.image.delete": EventCategory.LXD_INFRA,
    "integration.instance.delete": EventCategory.LXD_INFRA,
    "integration.network.delete": EventCategory.LXD_INFRA,
    "integration.project.delete": EventCategory.LXD_INFRA,
    "integration.profile.delete": EventCategory.LXD_INFRA,
    "integration.certificate.delete": EventCategory.LXD_INFRA,
    "integration.storage.pool.delete": EventCategory.LXD_INFRA,
    "integration.storage.volume.delete": EventCategory.LXD_INFRA,
    "admin.audit.view": EventCategory.ADMIN,
    
    # Settings
    "user.preference.update": EventCategory.SETTINGS,
    "tag.create": EventCategory.SETTINGS,
    "tag.assign": EventCategory.SETTINGS,
    "tag.remove": EventCategory.SETTINGS,
    
    # Vault
    "vault.secret.read": EventCategory.VAULT,
    "vault.secret.write": EventCategory.VAULT,
    "vault.secret.delete": EventCategory.VAULT,
    "vault.cert.generate": EventCategory.VAULT,
    "vault.cert.list": EventCategory.VAULT,
    "vault.kv.put": EventCategory.VAULT,
    "vault.kv.list": EventCategory.VAULT,
    "vault.health.check": EventCategory.VAULT,

    # LXD service-layer operations (distinct from router-layer actions)
    "lxd.instance.create": EventCategory.LXD_INFRA,
    "lxd.instance.start": EventCategory.LXD_INFRA,
    "lxd.instance.stop": EventCategory.LXD_INFRA,
    "lxd.instance.restart": EventCategory.LXD_INFRA,
    "lxd.instance.delete": EventCategory.LXD_INFRA,
    "lxd.project.create": EventCategory.LXD_INFRA,
    "lxd.project.update": EventCategory.LXD_INFRA,
    "lxd.project.delete": EventCategory.LXD_INFRA,
    "lxd.image.copy": EventCategory.LXD_INFRA,
    "lxd.image.delete": EventCategory.LXD_INFRA,
    "lxd.connection.establish": EventCategory.LXD_INFRA,
    "lxd.connection.remove": EventCategory.LXD_INFRA,
    "lxd.connection.reconnect": EventCategory.LXD_INFRA,
    # Cluster management
    "cluster.location.update": EventCategory.ADMIN,
}

# High severity actions
HIGH_SEVERITY_ACTIONS = {
    "cluster.register",
    "cluster.delete",
    "instance.delete",
    "vm.delete",
    "project.delete",
    "storage.pool.delete",
    "storage.volume.delete",
    "image.delete",
    "network.delete",
    "terminal.session.create",
    "console.session.create",
    "integration.instance.exec",
    "integration.instance.files",
    "desktop_control.consent.set",
    "integration.instance.desktop_view",
    "integration.instance.desktop_input",
    "integration.instance.desktop_clipboard",
    "integration.api_key.create",
    "integration.api_key.revoke",
    "integration.api_key.delete",
    "extension.install",
    "extension.install.rescope",
    "extension.install.resume",
    "extension.install.credential.rotate",
    "extension.credential.orphan_revoked",
    "extension.webhook.secret.rotate",
    "extension.webhook.redeliver",
    "integration.image.delete",
    "integration.instance.delete",
    "integration.network.delete",
    "integration.project.delete",
    "integration.profile.delete",
    "integration.certificate.delete",
    "integration.storage.pool.delete",
    "integration.storage.volume.delete",
    "break_glass.grant.create",
    "break_glass.grant.revoke",
    "user.ssh_key.create",
    "user.ssh_key.delete",
    "vault.cert.generate",
    "vault.secret.write",
    "vault.secret.delete",
    # Service-layer high severity
    "lxd.instance.delete",
    "lxd.project.delete",
    "lxd.connection.establish",
    "lxd.connection.remove",
    "lxd.connection.reconnect",
}


class ActivityLogger:
    """
    Activity/Audit logger for Orcastra.
    
    Logs sensitive operations in structured JSON format.
    """
    
    def __init__(self, service_name: str = "orcastra-backend", version: str = "0.0.0"):
        self.service_name = service_name
        self.version = version
        self.logger = logging.getLogger("orcastra.audit")
    
    def log_activity(
        self,
        action: str,
        target_type: str,
        target_id: str,
        target_host: Optional[str] = None,
        target_project: Optional[str] = None,
        result: str = "success",
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        duration_ms: Optional[float] = None,
        before_state: Optional[Dict[str, Any]] = None,
        after_state: Optional[Dict[str, Any]] = None,
        target_organization_id: Optional[int] = None,
        target_organization_slug: Optional[str] = None,
    ) -> str:
        """
        Log an activity/audit event.
        """
        # Generate event ID
        event_id = generate_event_id()
        
        # Get request context
        ctx = get_request_context()
        request_id = ctx.request_id if ctx else get_request_id()
        
        # Determine category and severity
        category = ACTION_CATEGORIES.get(action, EventCategory.LXD_INFRA)
        severity = EventSeverity.HIGH if action in HIGH_SEVERITY_ACTIONS else EventSeverity.MEDIUM
        if result == "failure":
            severity = EventSeverity.HIGH
        
        # Build audit log entry
        audit_log = {
            "@timestamp": datetime.now(timezone.utc).isoformat(),
            "log_type": "audit",
            "event_id": event_id,
            "request_id": request_id,
            "service": self.service_name,
            "version": self.version,
            "action": action,
            "category": category.value,
            "severity": severity.value,
            "actor": ctx.get_actor() if ctx else {
                "user_id": None,
                "user_type": None,
                "role": None,
                "groups": [],
                "organizations": [],
                "session_id": None,
            },  # user_type == role (kept for backward compat)
            "target": {
                "type": target_type,
                "id": target_id,
                "host": target_host,
                "project": target_project,
                # owning tenant org of the target resource (accountability: who was acted upon)
                "organization_id": target_organization_id,
                "organization_slug": target_organization_slug,
            },
            "result": result,
            "error_code": error_code,
            "error_message": error_message,
            "details": self._sanitize_details(details) if details else None,
            "metadata": {
                "duration_ms": duration_ms,
                "before": before_state,
                "after": after_state,
            } if any([duration_ms, before_state, after_state]) else None,
        }
        
        # Log with appropriate level
        log_level = logging.WARNING if result == "failure" else logging.INFO
        self.logger.log(
            log_level,
            f"AUDIT: {action} on {target_type}/{target_id} - {result}",
            extra={"audit_log_data": audit_log}
        )
        
        return event_id
    
    def _sanitize_details(self, details: Dict[str, Any]) -> Dict[str, Any]:
        """Sanitize details to remove sensitive information."""
        sensitive_keys = {
            "password", "secret", "token", "api_key", "private_key",
            "client_secret", "trust_password", "credential"
        }
        # important: substring matching against `api_key` also catches `api_key_id`, which is the
        # PUBLIC half of the credential: it is returned in create and validate responses and
        # rendered on the key card. Redacting it removed the only actor an API-key action has,
        # so a record of what a key did could not say which key. The secret never travels in
        # details at all; it is verified and discarded in validate_api_key.
        public_keys = {"api_key_id"}

        sanitized = {}
        for key, value in details.items():
            key_lower = key.lower()
            # note: the allowlist exempts a name from REDACTION, not from the recursion below.
            # Placing it first would have let a dict under an allowlisted name pass through
            # unsanitised, and the allowlist is meant to say "this identifier is public", not
            # "stop looking".
            public = key_lower in public_keys
            if isinstance(value, dict):
                sanitized[key] = self._sanitize_details(value)
            elif public:
                sanitized[key] = value
            elif any(sensitive in key_lower for sensitive in sensitive_keys):
                sanitized[key] = "[REDACTED]"
            elif isinstance(value, dict):
                sanitized[key] = self._sanitize_details(value)
            else:
                sanitized[key] = value
        
        return sanitized
    
    def log_with_timing(
        self,
        action: str,
        target_type: str,
        target_id: str,
        **kwargs
    ):
        """
        Context manager for logging activities with automatic timing.
        """
        return ActivityLogContext(self, action, target_type, target_id, **kwargs)


class ActivityLogContext:
    """Context manager for activity logging with timing."""
    
    def __init__(
        self,
        logger: ActivityLogger,
        action: str,
        target_type: str,
        target_id: str,
        **kwargs
    ):
        self.logger = logger
        self.action = action
        self.target_type = target_type
        self.target_id = target_id
        self.kwargs = kwargs
        self.start_time: float = 0
        self.details: Dict[str, Any] = {}
        self.result = "success"
        self.error_code: Optional[str] = None
        self.error_message: Optional[str] = None
    
    def __enter__(self):
        self.start_time = time.perf_counter()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = (time.perf_counter() - self.start_time) * 1000
        
        if exc_type is not None:
            self.result = "failure"
            self.error_message = str(exc_val)
            self.error_code = exc_type.__name__
        
        self.logger.log_activity(
            action=self.action,
            target_type=self.target_type,
            target_id=self.target_id,
            result=self.result,
            error_code=self.error_code,
            error_message=self.error_message,
            details=self.details if self.details else None,
            duration_ms=round(duration_ms, 2),
            **self.kwargs
        )
        
        # Don't suppress exceptions
        return False
    
    def set_details(self, details: Dict[str, Any]) -> None:
        """Set additional details for the log."""
        self.details.update(details)
    
    def set_result(self, result: str, error_code: str = None, error_message: str = None) -> None:
        """Manually set the result."""
        self.result = result
        self.error_code = error_code
        self.error_message = error_message


# Singleton instance
_activity_logger: Optional[ActivityLogger] = None


def get_activity_logger() -> ActivityLogger:
    """Get or create the activity logger singleton."""
    global _activity_logger
    
    if _activity_logger is None:
        settings = get_settings()
        _activity_logger = ActivityLogger(
            service_name="orcastra-backend",
            version=settings.app_version or "0.0.0"
        )
    
    return _activity_logger
