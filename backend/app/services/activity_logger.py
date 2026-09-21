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

from app.core.request_context import (
    get_request_context,
    get_request_id,
    generate_event_id,
)
from app.core.config import get_settings
from app.services.activity_catalogue import (  # noqa: F401 - public re-exports
    ACTION_CATEGORIES,
    HIGH_SEVERITY_ACTIONS,
    EventCategory,
    EventSeverity,
)


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
        target_organization_resolution: Optional[str] = None,
        actor_user_id: Optional[str] = None,
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
        actor = ctx.get_actor() if ctx else {
            "user_id": None,
            "user_type": None,
            "role": None,
            "groups": [],
            "organizations": [],
            "session_id": None,
        }
        # Background workers do not retain request context across process restarts. Durable
        # jobs carry the authenticated requester explicitly so their terminal audit verdict is
        # still attributable instead of looking like an anonymous system mutation.
        if actor_user_id:
            actor = {**actor, "user_id": actor_user_id, "user_type": actor.get("user_type") or "user"}

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
            "actor": actor,  # user_type == role (kept for backward compat)
            "target": {
                "type": target_type,
                "id": target_id,
                "host": target_host,
                "project": target_project,
                # owning tenant org of the target resource (accountability: who was acted upon)
                "organization_id": target_organization_id,
                "organization_slug": target_organization_slug,
                # Explicit ambiguity/unbound state prevents the async task journal from
                # backfilling a shared cluster's first organization as though it owned the
                # target project. Legacy callers omit this and retain the historical fallback.
                "organization_resolution": target_organization_resolution,
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

        # The task journal keeps a copy of this record in Postgres for the bottom panel. The
        # stdout line above stays the audit of record, so the copy must never cost a request:
        # the sink never raises, and neither may reaching it.
        try:
            from app.services.task_journal.sink import get_journal_sink

            get_journal_sink().offer(
                dict(audit_log, actor_extra=dict(ctx.extra) if ctx else {})
            )
        except Exception:  # noqa: BLE001 - the journal is a copy; the stdout record is the audit
            pass

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
