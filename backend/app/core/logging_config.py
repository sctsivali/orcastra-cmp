"""
Logging Configuration Module
Provides structured JSON logging for Orcastra CMP.
"""

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Mapping
import traceback

from app.core.error_sanitizer import redact_query_secrets


class StructuredJsonFormatter(logging.Formatter):
    """
    JSON formatter for structured logging.
    """
    
    def __init__(self, service_name: str = "orcastra-backend", version: str = "0.0.0"):
        super().__init__()
        self.service_name = service_name
        self.version = version
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        # Import here to avoid circular imports
        from app.core.request_context import get_request_context
        
        # Check for specialized log types (access/audit logs)
        # These have pre-formatted data with log_type field for Fluent Bit routing
        if hasattr(record, "access_log_data"):
            # Access logs - output directly with log_type for routing
            log_entry = record.access_log_data.copy()
            log_entry["service"] = self.service_name
            log_entry["version"] = self.version
            return json.dumps(log_entry, default=str, ensure_ascii=False)
        
        if hasattr(record, "audit_log_data"):
            # Audit logs - output directly with log_type for routing
            log_entry = record.audit_log_data.copy()
            log_entry["service"] = self.service_name
            log_entry["version"] = self.version
            return json.dumps(log_entry, default=str, ensure_ascii=False)
        
        # Standard application logs
        log_entry: Dict[str, Any] = {
            "@timestamp": datetime.now(timezone.utc).isoformat(),
            "log_type": "app",  # Mark as app log for Fluent Bit routing
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service_name,
            "version": self.version,
        }
        
        # Add request context if available
        ctx = get_request_context()
        if ctx:
            log_entry["request_id"] = ctx.request_id
            if ctx.user_id:
                log_entry["user_id"] = ctx.user_id
        
        # Add source location for debug/error logs
        if record.levelno >= logging.WARNING:
            log_entry["source"] = {
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            }
        
        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]) if record.exc_info[1] else None,
                "traceback": traceback.format_exception(*record.exc_info) if record.exc_info[0] else None,
            }
        
        # Add any extra fields from the record
        if hasattr(record, "extra_fields"):
            log_entry.update(record.extra_fields)

        # Add dedup count if present (from LogDeduplicationFilter)
        if hasattr(record, "dedup_count") and record.dedup_count > 1:
            log_entry["dedup_count"] = record.dedup_count

        return json.dumps(log_entry, default=str, ensure_ascii=False)


class AccessLogFormatter(logging.Formatter):
    """
    Specialized formatter for HTTP access logs.
    Outputs in the exact format expected by orcastra-access-* index.
    """
    
    def __init__(self, service_name: str = "orcastra-backend", version: str = "0.0.0"):
        super().__init__()
        self.service_name = service_name
        self.version = version
    
    def format(self, record: logging.LogRecord) -> str:
        """Format access log record as JSON."""
        # Access logs should have access_log_data attribute
        if hasattr(record, "access_log_data"):
            log_entry = record.access_log_data
            log_entry["service"] = self.service_name
            log_entry["version"] = self.version
            return json.dumps(log_entry, default=str, ensure_ascii=False)
        
        # Fallback to standard format
        return json.dumps({
            "@timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "service": self.service_name,
            "version": self.version,
        }, default=str, ensure_ascii=False)


class AuditLogFormatter(logging.Formatter):
    """
    Specialized formatter for audit/activity logs.
    Outputs in the exact format expected by orcastra-audit-* index.
    """
    
    def __init__(self, service_name: str = "orcastra-backend", version: str = "0.0.0"):
        super().__init__()
        self.service_name = service_name
        self.version = version
    
    def format(self, record: logging.LogRecord) -> str:
        """Format audit log record as JSON."""
        # Audit logs should have audit_log_data attribute
        if hasattr(record, "audit_log_data"):
            log_entry = record.audit_log_data
            log_entry["service"] = self.service_name
            log_entry["version"] = self.version
            return json.dumps(log_entry, default=str, ensure_ascii=False)
        
        # Fallback to standard format
        return json.dumps({
            "@timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "service": self.service_name,
            "version": self.version,
        }, default=str, ensure_ascii=False)


class LogDeduplicationFilter(logging.Filter):
    """
    In-memory log deduplication filter for app logs.

    Identical log messages (same logger + level + message) within a
    configurable time window are suppressed and replaced with a single
    entry carrying a ``count`` field when the window expires or a new
    unique message arrives.

    Only applies to **app logs** - access and audit logs pass through
    unmodified (they have their own dedup via event_id / request_id
    at the OpenSearch level).

    Thread-safe via a lock protecting the internal state.
    """

    def __init__(self, window_seconds: float = 60.0, name: str = ""):
        super().__init__(name)
        self._window = window_seconds
        self._lock = threading.Lock()
        # key = (logger_name, levelno, message)
        self._counts: Dict[tuple, int] = {}
        self._first_seen: Dict[tuple, float] = {}
        self._flushed: Dict[tuple, bool] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        # never deduplicate access or audit logs
        if hasattr(record, "access_log_data") or hasattr(record, "audit_log_data"):
            return True

        key = (record.name, record.levelno, record.getMessage())
        now = time.time()

        with self._lock:
            # check if window expired for this key - flush summary
            if key in self._first_seen:
                elapsed = now - self._first_seen[key]
                if elapsed >= self._window:
                    # window expired: emit summary if count > 1
                    count = self._counts.get(key, 1)
                    if count > 1 and not self._flushed.get(key, False):
                        record.dedup_count = count
                    # reset tracking
                    self._counts[key] = 1
                    self._first_seen[key] = now
                    self._flushed[key] = True
                    return True

                # within window
                self._counts[key] = self._counts.get(key, 0) + 1
                count = self._counts[key]

                if count == 1:
                    # first occurrence - let it through
                    self._flushed[key] = False
                    return True
                elif count == 2:
                    # second occurrence - suppress but flag first will need summary
                    return False
                else:
                    # 3+ occurrences - suppress
                    return False
            else:
                # brand-new message
                self._counts[key] = 1
                self._first_seen[key] = now
                self._flushed[key] = False
                return True

    def flush_pending(self) -> None:
        """Force-flush any pending dedup summaries (call on shutdown)."""
        with self._lock:
            for key, count in list(self._counts.items()):
                if count > 1 and not self._flushed.get(key, False):
                    logger_name, levelno, message = key
                    _logger = logging.getLogger(logger_name)
                    summary = f"[repeated {count}x in {self._window}s] {message}"
                    _logger.log(levelno, summary)
                    self._flushed[key] = True
            self._counts.clear()
            self._first_seen.clear()
            self._flushed.clear()


class QuerySecretFilter(logging.Filter):
    """Keep credential query values out of Uvicorn HTTP and WebSocket logs.

    Uvicorn logs the WebSocket handshake before application authentication. The socket URL
    carries a bearer token, so both the message and string arguments need redaction before
    any handler formats the record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_query_secrets(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_query_secrets(value) if isinstance(value, str) else value
                for value in record.args
            )
        elif isinstance(record.args, Mapping):
            record.args = {
                key: redact_query_secrets(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        return True


QUERY_SECRET_LOGGERS = ("uvicorn.error", "uvicorn.access")


def install_query_secret_filter() -> None:
    """Attach the filter once even when logging setup runs again."""
    for name in QUERY_SECRET_LOGGERS:
        target = logging.getLogger(name)
        if not any(isinstance(existing, QuerySecretFilter) for existing in target.filters):
            target.addFilter(QuerySecretFilter())


def setup_logging(
    service_name: str = "orcastra-backend",
    version: str = "0.0.0",
    log_level: str = "INFO",
    json_logs: bool = True,
) -> None:
    """
    Configure application logging with structured JSON output.
    """
    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    
    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Create stdout handler
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.DEBUG)
    
    if json_logs:
        stdout_handler.setFormatter(StructuredJsonFormatter(service_name, version))
    else:
        # Human-readable format for local development
        stdout_handler.setFormatter(logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - [%(request_id)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            defaults={"request_id": "no-request"}
        ))
    
    root_logger.addHandler(stdout_handler)
    
    # Add dedup filter for app logs (NOT access/audit - those pass through)
    try:
        dedup_window = float(os.environ.get("LOG_DEDUP_WINDOW_SECONDS", "60"))
    except (ValueError, TypeError):
        dedup_window = 60.0
    dedup_filter = LogDeduplicationFilter(window_seconds=dedup_window)
    stdout_handler.addFilter(dedup_filter)
    
    # Set specific log levels for noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    install_query_secret_filter()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


# Access log logger - separate logger for access logs
access_logger = logging.getLogger("orcastra.access")

# Audit log logger - separate logger for audit logs  
audit_logger = logging.getLogger("orcastra.audit")


def get_access_logger() -> logging.Logger:
    """Get the access log logger."""
    return access_logger


def get_audit_logger() -> logging.Logger:
    """Get the audit log logger."""
    return audit_logger
