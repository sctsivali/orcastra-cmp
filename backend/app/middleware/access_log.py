"""Access log middleware: one structured record per HTTP request, sent to OpenSearch."""

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


# Probes and the favicon are left out of the access log.
EXCLUDED_PATHS = {
    "/health",
    "/metrics",
    "/favicon.ico",
}



class AccessLogMiddleware(BaseHTTPMiddleware):
    """Logs each request as structured JSON: request ID, method, path, redacted query params,
    status, latency, sizes, the verified user and the client."""
    
    def __init__(self, app, service_name: str = "orcastra-backend", version: str = "0.0.0"):
        super().__init__(app)
        self.service_name = service_name
        self.version = version
        self.logger = get_access_logger()
    
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path in EXCLUDED_PATHS:
            return await call_next(request)
        
        start_time = time.perf_counter()
        
        request_size = 0
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                request_size = int(content_length)
            except ValueError:
                pass
        
        response = None
        error_message = None
        try:
            response = await call_next(request)
        except Exception as e:
            # Sanitised for the log as well: this lands in OpenSearch, and an unhandled exception's
            # text carries the same control-plane URLs and paths the response handler redacts.
            error_message = sanitize_error(str(e))
            raise
        finally:
            latency_ms = (time.perf_counter() - start_time) * 1000
            
            status_code = response.status_code if response else 500
            response_size = 0
            if response:
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        response_size = int(content_length)
                    except ValueError:
                        pass
            
            ctx = get_request_context()
            
            access_log = self._build_access_log(
                request=request,
                status_code=status_code,
                latency_ms=latency_ms,
                request_size=request_size,
                response_size=response_size,
                error_message=error_message,
                ctx=ctx,
            )
            
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
        """Build the structured access log entry."""
        # important: match substrings of the lowercased name, the shape
        # `activity_logger._sanitize_details` uses, so `access_token`, `client_secret`, `api-key`,
        # `code`, `signature` and any capitalisation are redacted. An exact match misses them.
        query_params = {
            name: ("[REDACTED]" if _is_sensitive_param(name) else value)
            for name, value in request.query_params.items()
        }
        
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
            "client_request_id": getattr(request.state, "client_request_id", None),
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
            # Response cache outcome (core/swr.py); absent on endpoints that do not use it.
            "cache": ctx.extra.get("cache") if ctx else None,
            "cache_age_s": ctx.extra.get("cache_age_s") if ctx else None,
        }
    
    def _log_access(self, access_log: dict, status_code: int) -> None:
        """Log the access entry with appropriate level."""
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
        
        # 5xx logs as error, 4xx as warning, the rest as info.
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
        """The client IP, honouring forwarded headers only from trusted proxies."""
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
