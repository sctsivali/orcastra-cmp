"""
Access Log Middleware
Logs all HTTP requests with structured fields for OpenSearch ingestion.
"""

import time
import logging
from datetime import datetime, timezone

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import get_settings
from app.core.request_context import get_request_context
from app.core.request_security import resolve_client_ip
from app.core.logging_config import get_access_logger
from app.core.error_sanitizer import is_sensitive_param as _is_sensitive_param, sanitize_error


# Paths to exclude from access logging (health checks, metrics, etc.)
EXCLUDED_PATHS = {
    "/health",
    "/metrics",
    "/favicon.ico",
}



class AccessLogMiddleware(BaseHTTPMiddleware):
    """
    Middleware to log HTTP access in structured JSON format.
    
    Logs include:
    - Request ID (for correlation)
    - HTTP method, path, query params
    - Response status code
    - Request/response latency
    - User info (if authenticated)
    - Client IP, user agent, origin
    """
    
    def __init__(self, app, service_name: str = "orcastra-backend", version: str = "0.0.0"):
        super().__init__(app)
        self.service_name = service_name
        self.version = version
        self.logger = get_access_logger()
    
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Skip excluded paths
        if request.url.path in EXCLUDED_PATHS:
            return await call_next(request)
        
        # Record start time
        start_time = time.perf_counter()
        
        # Get request size
        request_size = 0
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                request_size = int(content_length)
            except ValueError:
                pass
        
        # Process request
        response = None
        error_message = None
        try:
            response = await call_next(request)
        except Exception as e:
            # note: sanitised on the way into the log, not just on the way to the client. This
            # value lands in OpenSearch, and an unhandled exception's text carries the same
            # control-plane URLs and paths the response handler redacts.
            error_message = sanitize_error(str(e))
            raise
        finally:
            # Calculate latency
            latency_ms = (time.perf_counter() - start_time) * 1000
            
            # Get response info
            status_code = response.status_code if response else 500
            response_size = 0
            if response:
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        response_size = int(content_length)
                    except ValueError:
                        pass
            
            # Get request context
            ctx = get_request_context()
            
            # Build access log entry
            access_log = self._build_access_log(
                request=request,
                status_code=status_code,
                latency_ms=latency_ms,
                request_size=request_size,
                response_size=response_size,
                error_message=error_message,
                ctx=ctx,
            )
            
            # Log the access entry
            self._log_access(access_log, status_code)
        
        return response
    
    def _build_access_log(
        self,
        request: Request,
        status_code: int,
        latency_ms: float,
        request_size: int,
        response_size: int,
        error_message: str | None,
        ctx,
    ) -> dict:
        """Build structured access log entry."""
        # Extract query params (sanitized)
        #
        # important: substring match on a lowercased name, not `name in dict`. The exact-match
        # version missed `access_token`, `refresh_token`, `id_token`, `client_secret`, `apikey`,
        # `api-key`, `code`, `signature` and every capitalisation, while the WebSocket token
        # parameter happened to be spelled `token` and so looked covered. This is the shape
        # `activity_logger._sanitize_details` already uses; two divergent redaction policies in
        # one application is the durable defect, so they now agree.
        query_params = {
            name: ("[REDACTED]" if _is_sensitive_param(name) else value)
            for name, value in request.query_params.items()
        }
        
        # Build user info
        user_info = None
        if ctx and ctx.user_id:
            user_info = {
                "id": ctx.user_id,
                "type": ctx.user_role,
                "role": ctx.user_role,
                "groups": ctx.user_groups,
                "organizations": ctx.user_orgs,
            }
        
        client_ip = ctx.client_ip if ctx else self._get_client_ip(request)

        # Build client info
        client_info = {
            "ip": client_ip,
            "user_agent": request.headers.get("user-agent"),
            "origin": request.headers.get("origin"),
            "proxy_ip": getattr(request.state, "client_proxy_ip", None),
            "ip_source": getattr(request.state, "client_ip_source", "direct"),
            "proxy_trusted": getattr(request.state, "client_proxy_trusted", False),
        }
        
        return {
            "@timestamp": datetime.now(timezone.utc).isoformat(),
            "log_type": "access",
            "request_id": ctx.request_id if ctx else request.state.request_id if hasattr(request.state, "request_id") else None,
            "service": self.service_name,
            "version": self.version,
            "method": request.method,
            "path": request.url.path,
            "query_params": query_params if query_params else None,
            "status_code": status_code,
            "latency_ms": round(latency_ms, 2),
            "request_size": request_size,
            "response_size": response_size,
            "user": user_info,
            "client": client_info,
            "error": error_message,
        }
    
    def _log_access(self, access_log: dict, status_code: int) -> None:
        """Log the access entry with appropriate level."""
        # Create log record with custom attribute
        record = logging.LogRecord(
            name="orcastra.access",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=f"{access_log['method']} {access_log['path']} - {status_code}",
            args=(),
            exc_info=None,
        )
        record.access_log_data = access_log
        
        # Log with appropriate level based on status code
        if status_code >= 500:
            self.logger.error(
                f"{access_log['method']} {access_log['path']} - {status_code}",
                extra={"access_log_data": access_log}
            )
        elif status_code >= 400:
            self.logger.warning(
                f"{access_log['method']} {access_log['path']} - {status_code}",
                extra={"access_log_data": access_log}
            )
        else:
            self.logger.info(
                f"{access_log['method']} {access_log['path']} - {status_code}",
                extra={"access_log_data": access_log}
            )
    
    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP, considering proxies."""
        if hasattr(request.state, "client_ip") and request.state.client_ip:
            return request.state.client_ip
        settings = get_settings()
        resolution = resolve_client_ip(
            request,
            settings.trusted_proxy_cidrs,
            settings.allow_private_forwarded_ips,
        )
        request.state.client_ip = resolution.client_ip
        request.state.client_ip_source = resolution.source
        request.state.client_proxy_ip = resolution.direct_ip
        request.state.client_proxy_trusted = resolution.trusted_proxy
        return resolution.client_ip
