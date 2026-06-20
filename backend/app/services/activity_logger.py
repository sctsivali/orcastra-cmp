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
    "network.delete",
    "terminal.session.create",
    "console.session.create",
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

# High-frequency, system-initiated read/list operations. These are operational telemetry,
# not security-significant events, so they are NOT persisted to the Postgres audit table
# when they SUCCEED (they still go to stdout for the full-version OpenSearch sink). A
# FAILURE of any of these IS persisted - it is an anomaly worth keeping.
AUDIT_DB_SKIP_ON_SUCCESS = {
    "vault.kv.list",
    "vault.kv.get",
    "vault.secret.read",
    "vault.cert.list",
    "vault.health.check",
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
    
    def _build_audit_log(
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
    ) -> tuple[str, Dict[str, Any]]:
        """Build the structured audit event (shared by log_activity + log_security_event)."""
        event_id = generate_event_id()

        ctx = get_request_context()
        request_id = ctx.request_id if ctx else get_request_id()

        category = ACTION_CATEGORIES.get(action, EventCategory.LXD_INFRA)
        severity = EventSeverity.HIGH if action in HIGH_SEVERITY_ACTIONS else EventSeverity.MEDIUM
        if result == "failure":
            severity = EventSeverity.HIGH

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
            },
            "result": result,
            "error_code": error_code,
            "error_message": error_message,
            "source_ip": getattr(ctx, "client_ip", None) if ctx else None,
            "details": self._sanitize_details(details) if details else None,
            "metadata": {
                "duration_ms": duration_ms,
                "before": before_state,
                "after": after_state,
            } if any([duration_ms, before_state, after_state]) else None,
        }
        return event_id, audit_log

    def _emit_stdout(self, audit_log: Dict[str, Any]) -> None:
        log_level = logging.WARNING if audit_log.get("result") == "failure" else logging.INFO
        tgt = audit_log.get("target", {})
        self.logger.log(
            log_level,
            f"AUDIT: {audit_log.get('action')} on {tgt.get('type')}/{tgt.get('id')} - {audit_log.get('result')}",
            extra={"audit_log_data": audit_log},
        )

    @staticmethod
    def _should_persist_db(action: str, result: str) -> bool:
        """Mini deployment: persist to Postgres unless it is a routine successful
        system read/list (kept security-focused). Failures are always persisted."""
        return get_settings().audit_db_enabled and (
            result != "success" or action not in AUDIT_DB_SKIP_ON_SUCCESS
        )

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
    ) -> str:
        """Log an activity/audit event (non-blocking DB persistence)."""
        event_id, audit_log = self._build_audit_log(
            action, target_type, target_id, target_host, target_project, result,
            error_code, error_message, details, duration_ms, before_state, after_state,
        )
        self._emit_stdout(audit_log)

        # Mini deployment: also persist to the hash-chained Postgres audit_log table.
        # Non-blocking - the request path never waits on the DB write.
        try:
            if self._should_persist_db(action, result):
                from app.services.audit_writer import get_audit_writer
                get_audit_writer().enqueue(audit_log)
        except Exception:
            pass

        return event_id

    async def log_security_event(
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
    ) -> str:
        """Durable, fail-CLOSED audit for security-critical events (auth, identity
        issue/revoke/role-change, key ops). In mini mode the DB write is awaited and a
        failure RAISES, so the operation cannot complete without an audit record. In full
        mode (audit DB off) this behaves like log_activity (stdout only, non-blocking)."""
        event_id, audit_log = self._build_audit_log(
            action, target_type, target_id, target_host, target_project, result,
            error_code, error_message, details, duration_ms, before_state, after_state,
        )
        self._emit_stdout(audit_log)

        if get_settings().audit_db_enabled:
            from app.services.audit_writer import get_audit_writer
            await get_audit_writer().write_now(audit_log)  # raises on failure -> fail closed

        return event_id
    
    def _sanitize_details(self, details: Dict[str, Any]) -> Dict[str, Any]:
        """Sanitize details to remove sensitive information."""
        sensitive_keys = {
            "password", "secret", "token", "api_key", "private_key",
            "client_secret", "trust_password", "credential"
        }
        
        sanitized = {}
        for key, value in details.items():
            key_lower = key.lower()
            if any(sensitive in key_lower for sensitive in sensitive_keys):
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
