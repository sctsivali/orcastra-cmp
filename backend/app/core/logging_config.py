"""Structured JSON logging: app, access and audit records, routed by Fluent Bit on `log_type`."""

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
    """JSON formatter for app logs; access and audit records keep their pre-formatted fields."""
    
    def __init__(self, service_name: str = "orcastra-backend", version: str = "0.0.0"):
        super().__init__()
        self.service_name = service_name
        self.version = version
    
    def format(self, record: logging.LogRecord) -> str:
        # Import here to avoid circular imports
        from app.core.request_context import get_request_context
        
        # Access and audit records arrive pre-formatted, with the log_type Fluent Bit routes on.
        if hasattr(record, "access_log_data"):
            log_entry = record.access_log_data.copy()
            log_entry["service"] = self.service_name
            log_entry["version"] = self.version
            return json.dumps(log_entry, default=str, ensure_ascii=False)
        
        if hasattr(record, "audit_log_data"):
            log_entry = record.audit_log_data.copy()
            log_entry["service"] = self.service_name
            log_entry["version"] = self.version
            return json.dumps(log_entry, default=str, ensure_ascii=False)
        
        log_entry: Dict[str, Any] = {
            "@timestamp": datetime.now(timezone.utc).isoformat(),
            "log_type": "app",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service_name,
            "version": self.version,
        }
        
        ctx = get_request_context()
        if ctx:
            log_entry["request_id"] = ctx.request_id
            if ctx.user_id:
                log_entry["user_id"] = ctx.user_id
        
        if record.levelno >= logging.WARNING:
            log_entry["source"] = {
                "file": record.pathname,
                "line": record.lineno,
                "function": record.funcName,
            }
        
        if record.exc_info:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "message": str(record.exc_info[1]) if record.exc_info[1] else None,
                "traceback": traceback.format_exception(*record.exc_info) if record.exc_info[0] else None,
            }
        
        if hasattr(record, "extra_fields"):
            log_entry.update(record.extra_fields)

        # Set by LogDeduplicationFilter.
        if hasattr(record, "dedup_count") and record.dedup_count > 1:
            log_entry["dedup_count"] = record.dedup_count

        return json.dumps(log_entry, default=str, ensure_ascii=False)


class AccessLogFormatter(logging.Formatter):
    """HTTP access log formatter, in the shape the orcastra-access-* index expects."""
    
    def __init__(self, service_name: str = "orcastra-backend", version: str = "0.0.0"):
        super().__init__()
        self.service_name = service_name
        self.version = version
    
    def format(self, record: logging.LogRecord) -> str:
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
    """Audit log formatter, in the shape the orcastra-audit-* index expects."""
    
    def __init__(self, service_name: str = "orcastra-backend", version: str = "0.0.0"):
        super().__init__()
        self.service_name = service_name
        self.version = version
    
    def format(self, record: logging.LogRecord) -> str:
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
    """Suppresses repeats of an app log message within a time window.

    A message seen again (same logger, level and text) inside the window is dropped. The first
    occurrence after the window expires carries ``dedup_count``, the number seen in the window
    that ended. Access and audit records pass through; OpenSearch deduplicates them by event_id
    or request_id.
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
        if hasattr(record, "access_log_data") or hasattr(record, "audit_log_data"):
            return True

        key = (record.name, record.levelno, record.getMessage())
        now = time.time()

        with self._lock:
            if key in self._first_seen:
                elapsed = now - self._first_seen[key]
                if elapsed >= self._window:
                    count = self._counts.get(key, 1)
                    if count > 1 and not self._flushed.get(key, False):
                        record.dedup_count = count
                    self._counts[key] = 1
                    self._first_seen[key] = now
                    self._flushed[key] = True
                    return True

                self._counts[key] = self._counts.get(key, 0) + 1
                count = self._counts[key]

                if count == 1:
                    self._flushed[key] = False
                    return True
                elif count == 2:
                    return False
                else:
                    return False
            else:
                self._counts[key] = 1
                self._first_seen[key] = now
                self._flushed[key] = False
                return True

    def flush_pending(self) -> None:
        """Log a summary line for every run still suppressed. Call on shutdown."""
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
    """Configure root logging: one stdout handler, JSON by default, with the dedup filter."""
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
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
    
    try:
        dedup_window = float(os.environ.get("LOG_DEDUP_WINDOW_SECONDS", "60"))
    except (ValueError, TypeError):
        dedup_window = 60.0
    dedup_filter = LogDeduplicationFilter(window_seconds=dedup_window)
    stdout_handler.addFilter(dedup_filter)
    
    # Quieten noisy libraries.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    install_query_secret_filter()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


access_logger = logging.getLogger("orcastra.access")

audit_logger = logging.getLogger("orcastra.audit")


def get_access_logger() -> logging.Logger:
    return access_logger


def get_audit_logger() -> logging.Logger:
    return audit_logger
