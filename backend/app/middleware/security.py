"""
Application-level security middleware for request hardening.
"""

from __future__ import annotations

import logging
import base64
import json
import os
import time
from typing import Iterable, Optional

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



def _shared_allow(key: str, limit: int, window_seconds: int) -> Optional[bool]:
    """
    Count one hit against a counter shared by every worker.

    Returns True to allow, False to reject, and None when Redis cannot answer, which
    tells the caller to fall back rather than guess.

    important: fail open, never closed. A limiter that rejects when its store is
    unreachable turns a Redis outage into a total product outage, which is a far worse
    failure than briefly allowing more forced refreshes than intended.
    """
    if limit <= 0:
        return True
    try:
        from app.core.cache import get_cache_manager

        client = get_cache_manager().redis_client
        if client is None:
            return None
        bucket = f"orcastra:ratelimit:{key}"
        count = client.incr(bucket)
        if count == 1:
            client.expire(bucket, window_seconds)
        return int(count) <= limit
    except Exception as exc:  # noqa: BLE001 - any store failure must fall back, not block
        logger.warning("Shared rate limit unavailable, falling back to in-memory: %s", exc)
        return None


def _per_worker_share(limit: int) -> int:
    """
    Split a deployment-wide budget across the worker processes that will enforce it.

    important: the limiter holds its counters in memory, so each uvicorn worker enforces
    the configured number independently and the real ceiling is limit x WEB_CONCURRENCY.
    Measured on this deployment: a configured 12 let 44 of 70 requests through with four
    workers. Dividing here makes the configured number mean what it says. It is an
    approximation, since a client is not guaranteed an even spread across workers, and the
    honest fix is a shared counter in Redis.
    """
    if limit <= 0:
        return limit
    try:
        workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
    except ValueError:
        workers = 1
    return max(1, limit // max(1, workers))


def _wants_fresh(request: Request) -> bool:
    """True when the request asks to bypass the response cache."""
    value = request.query_params.get("fresh")
    return value is not None and value.lower() in {"1", "true", "yes", "on"}


def _rate_limit_subject(request: Request, client_ip: str) -> str:
    """
    Bucket identity for the bypass budget.

    note: the claim is read by decoding the payload segment directly, WITHOUT
    verifying the signature and without a JWT library, so the hermetic test suite
    keeps running on a host that has no crypto dependencies installed. That is safe for
    this purpose and only this purpose: the value picks a counter, it grants nothing.
    A forged token buys a fresh counter and then fails authentication downstream, so
    it cannot be used to read data. Real abuse comes from a valid token, which
    carries exactly one real subject.
    """
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer ":
        try:
            claims = header[7:].split(".")[1]
            padded = claims + "=" * (-len(claims) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
            subject = payload.get("sub") or payload.get("email")
            if subject:
                return f"sub:{subject}"
        except Exception:
            pass
    return f"ip:{client_ip}"

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

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
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

        limit_keys = self._rate_limit_keys(client_ip, request)
        now = time.time()
        window = self.settings.rate_limit_window_seconds
        for key, limit in limit_keys:
            # important: the bypass budget is meant to be a deployment-wide figure, and
            # the in-memory limiter cannot be one because each worker keeps its own
            # counters. Redis is already a hard dependency, so the shared counter lives
            # there and the configured number means what it says.
            if key.startswith("fresh:"):
                shared = _shared_allow(key, limit, window)
                if shared is not None:
                    if not shared:
                        return True
                    continue
                # Redis unavailable: fall back to the in-memory counter, divided so the
                # aggregate across workers still approximates the configured figure.
                limit = _per_worker_share(limit)
            if not self.limiter.allow(
                key=key,
                limit=limit,
                window_seconds=window,
                now=now,
            ):
                return True
        return False

    def _rate_limit_keys(self, client_ip: str, request: Request) -> Iterable[tuple[str, int]]:
        yield f"ip:{client_ip}", self.settings.rate_limit_requests

        subnet = get_client_subnet(client_ip)
        if subnet:
            yield f"subnet:{subnet}", self.settings.rate_limit_subnet_requests

        # important: a forced cache bypass costs a full fan-out to every cluster in
        # scope, so it gets its own much smaller budget. Keyed on the token subject
        # rather than the IP: the dashboard proxies every browser through one
        # container, so every operator shares a client IP and an IP-keyed budget
        # would let one person's refreshes throttle the whole tenancy.
        if _wants_fresh(request):
            yield f"fresh:{_rate_limit_subject(request, client_ip)}", self.settings.rate_limit_fresh_requests

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
