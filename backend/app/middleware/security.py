"""
Application-level security middleware for request hardening.
"""

from __future__ import annotations

import hmac
import logging
import time
from typing import Iterable

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.request_context import get_request_context
from app.core.request_security import (
    SecurityDecision,
    get_client_subnet,
    inspect_request_target,
    resolve_client_ip,
)

logger = logging.getLogger(__name__)


def apply_security_headers(response: Response) -> Response:
    """Attach hardened response headers for API responses."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-XSS-Protection", "1; mode=block")
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


class WindowedRateLimiter:
    """Small in-memory sliding-window limiter with bounded storage."""

    def __init__(self, max_buckets: int = 10_000):
        self.max_buckets = max_buckets
        self.storage: dict[str, list[float]] = {}

    def allow(self, key: str, limit: int, window_seconds: int, now: float | None = None) -> bool:
        if limit <= 0:
            return True

        current_time = now if now is not None else time.time()
        bucket = self.storage.get(key, [])
        bucket = [hit_time for hit_time in bucket if current_time - hit_time < window_seconds]

        if len(bucket) >= limit:
            self.storage[key] = bucket
            return False

        bucket.append(current_time)
        self._set_bucket(key, bucket)
        return True

    def _set_bucket(self, key: str, bucket: list[float]) -> None:
        if key not in self.storage and len(self.storage) >= self.max_buckets:
            oldest_key = min(
                self.storage,
                key=lambda item: self.storage[item][-1] if self.storage[item] else 0,
            )
            del self.storage[oldest_key]
        self.storage[key] = bucket


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Ensure all responses include baseline browser security headers."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        return apply_security_headers(response)


class RequestSecurityMiddleware(BaseHTTPMiddleware):
    """Block common scanner probes and throttle IP plus subnet buckets."""

    def __init__(self, app, settings, limiter: WindowedRateLimiter | None = None):
        super().__init__(app)
        self.settings = settings
        self.limiter = limiter or WindowedRateLimiter()

    def _strip_untrusted_cert_headers(self, request: Request) -> None:
        """Remove forwarded X-SSL-* cert headers unless the proxy secret matches."""
        expected = self.settings.auth_proxy_secret or ""
        provided = request.headers.get("x-auth-proxy-secret", "")
        trusted = bool(expected) and hmac.compare_digest(provided, expected)
        if trusted:
            return
        raw = request.scope.get("headers")
        if not raw:
            return
        request.scope["headers"] = [
            (k, v) for (k, v) in raw if not k.lower().startswith(b"x-ssl-client-")
        ]

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Defense in depth: in client-cert mode, never let X-SSL-* cert headers reach the
        # app unless the request carries the matching proxy secret. extract_client_cert
        # also checks the secret, but stripping here protects any other reader too.
        if self.settings.auth_mode == "client-cert":
            self._strip_untrusted_cert_headers(request)

        resolution = resolve_client_ip(
            request,
            self.settings.trusted_proxy_cidrs,
            self.settings.allow_private_forwarded_ips,
        )
        client_ip = resolution.client_ip
        request.state.client_ip = client_ip
        request.state.client_ip_source = resolution.source
        request.state.client_proxy_ip = resolution.direct_ip
        request.state.client_proxy_trusted = resolution.trusted_proxy

        ctx = get_request_context()
        if ctx:
            ctx.client_ip = client_ip
            ctx.extra["client_ip_source"] = resolution.source
            ctx.extra["client_proxy_ip"] = resolution.direct_ip
            ctx.extra["client_proxy_trusted"] = resolution.trusted_proxy

        if self.settings.security_probe_block_enabled:
            decision = inspect_request_target(
                method=request.method,
                path=request.url.path,
                query=request.url.query,
            )
            if decision:
                self._log_security_event(request, client_ip, decision.reason)
                return self._blocked_response(request, decision)

        if self._rate_limit_exceeded(request, client_ip):
            logger.warning(
                "Rate limit exceeded",
                extra={
                    "client_ip": client_ip,
                    "client_subnet": get_client_subnet(client_ip),
                    "path": request.url.path,
                },
            )
            return self._rate_limit_response(request)

        return await call_next(request)

    def _rate_limit_exceeded(self, request: Request, client_ip: str) -> bool:
        if not self.settings.rate_limit_enabled or request.method == "OPTIONS":
            return False

        limit_keys = self._rate_limit_keys(client_ip)
        now = time.time()
        for key, limit in limit_keys:
            if not self.limiter.allow(
                key=key,
                limit=limit,
                window_seconds=self.settings.rate_limit_window_seconds,
                now=now,
            ):
                return True
        return False

    def _rate_limit_keys(self, client_ip: str) -> Iterable[tuple[str, int]]:
        yield f"ip:{client_ip}", self.settings.rate_limit_requests

        subnet = get_client_subnet(client_ip)
        if subnet:
            yield f"subnet:{subnet}", self.settings.rate_limit_subnet_requests

    def _blocked_response(self, request: Request, decision: SecurityDecision) -> Response:
        headers = {"X-Orcastra-Security-Action": decision.reason}
        if decision.status_code == 405:
            headers["Allow"] = "GET, HEAD, OPTIONS"

        response = JSONResponse(
            status_code=decision.status_code,
            content={"detail": "Not found" if decision.status_code == 404 else "Method not allowed"},
            headers=headers,
        )
        self._attach_cors_if_allowed(request, response)
        return apply_security_headers(response)

    def _rate_limit_response(self, request: Request) -> Response:
        response = JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Please try again later."},
            headers={"Retry-After": "30"},
        )
        self._attach_cors_if_allowed(request, response)
        return apply_security_headers(response)

    def _attach_cors_if_allowed(self, request: Request, response: Response) -> None:
        origin = request.headers.get("origin")
        if not origin:
            return

        allowed_origins = self.settings.get_cors_origins()
        if allowed_origins == ["*"] or origin in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"

    def _log_security_event(self, request: Request, client_ip: str, reason: str) -> None:
        logger.warning(
            "Blocked suspicious request",
            extra={
                "security_reason": reason,
                "client_ip": client_ip,
                "client_subnet": get_client_subnet(client_ip),
                "method": request.method,
                "path": request.url.path,
                "user_agent": request.headers.get("user-agent"),
            },
        )
