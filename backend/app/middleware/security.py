"""
Application-level security middleware for request hardening.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

from pydantic import TypeAdapter, ValidationError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import configured_worker_count
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


def _shared_consume(key: str, limit: int, window_seconds: int) -> Optional[Tuple[int, int]]:
    """Spend one hit and report the count and the seconds left in the window.

    The same operation `_shared_allow` performs, keeping the number it throws away. A caller that
    wants to tell the client where it stands needs the count; a caller that only wants a yes or no
    keeps using `_shared_allow`, whose contract is unchanged.

    None means the store could not answer, which is the fail-open case: a caller must not invent a
    remaining budget it does not know.
    """
    if limit <= 0:
        return None
    try:
        from app.core.cache import get_cache_manager

        client = get_cache_manager().redis_client
        if client is None:
            return None
        bucket = f"orcastra:ratelimit:{key}"
        count = int(client.incr(bucket))
        if count == 1:
            client.expire(bucket, window_seconds)
        ttl = client.ttl(bucket)
        if ttl is not None and ttl == -1:
            # note: an expire lost after its incr (a blip between the two calls) left the counter with
            # none, which would hold its budget spent for good. Give it its window again.
            client.expire(bucket, window_seconds)
        # Redis answers -1 for a key with no expiry and -2 for one that just vanished. Neither is
        # a window this caller can report, so both fall back to the configured length.
        reset = window_seconds if ttl is None or ttl < 0 else int(ttl)
        return count, reset
    except Exception as exc:  # noqa: BLE001 - any store failure must fall back, not block
        logger.warning("Shared rate limit unavailable, falling back to in-memory: %s", exc)
        return None


def _shared_retry_after(key: str, window_seconds: int) -> int:
    """Seconds left on the shared counter's window, the window itself when Redis cannot say."""
    try:
        from app.core.cache import get_cache_manager

        client = get_cache_manager().redis_client
        ttl = int(client.ttl(f"orcastra:ratelimit:{key}")) if client is not None else -1
        return ttl if ttl > 0 else window_seconds
    except Exception:  # noqa: BLE001 - a header hint must never fail the response
        return window_seconds


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
    return max(1, limit // configured_worker_count())


_FRESH_FLAG = TypeAdapter(bool)


def _wants_fresh(request: Request) -> bool:
    """True when the request asks to bypass the response cache.

    important: this asks the same parser FastAPI will use on `fresh: bool = False`,
    rather than a list of spellings. A hand-written set said {1, true, yes, on} while
    pydantic also coerces t, T, y and Y, so `?fresh=t` bypassed the cache and yielded no
    `fresh:` bucket at all, escaping the per-operator budget and leaving only the general
    limit. Any divergence between the two is a hole of exactly that shape.
    """
    value = request.query_params.get("fresh")
    if value is None:
        return False
    try:
        return _FRESH_FLAG.validate_python(value)
    except ValidationError:
        return False


def _rate_limit_subject(request: Request, client_ip: str) -> str:
    """
    Bucket identity for the bypass budget.

    The budget must be per operator rather than per address: the dashboard proxies every browser
    through one container, so an IP-keyed budget would let one person's refreshes throttle the
    whole tenancy.

    important: keyed on a digest of the PRESENTED TOKEN, not on the `sub` claim inside it. This
    middleware runs before authentication, so any claim it reads is unverified, attacker-supplied
    text, and the counter is spent here whether or not the request later authenticates. Keying on
    the claim therefore let anyone who knew a victim's email exhaust that operator's forced-
    refresh budget from unauthenticated requests. Reaching a victim's bucket now requires the
    victim's token, and holding that already means being them.

    The trade is deliberate and small: a token rotation starts a fresh counter, which loosens the
    budget briefly. The budget exists to bound cluster fan-out cost, and that stays bounded per
    token; what it must not do is let a stranger spend someone else's.
    """
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer ":
        token = header[7:].strip()
        if token:
            return "tok:" + hashlib.sha256(token.encode()).hexdigest()[:32]
    return f"ip:{client_ip}"


def consume_api_key_budget(key_id: str) -> bool:
    """Spend one request against an API key's own budget. True when it may proceed.

    important: this is called from `validate_api_key` AFTER the secret verifies, deliberately
    not from the middleware. An API key id is a public identifier, not a credential: it is
    returned to any partner listing organizations (`IntegrationOrgResponse.api_key_id`), it is
    written to logs, and it travels in the header of every outbound callback. Counting in the
    middleware would count unauthenticated requests too, so anyone holding a victim's id could
    present it with a garbage secret and burn that integration's whole minute. That is a
    targeted denial of service against a named integration, which is worse than the shared-budget
    problem this bucket exists to solve. Unauthenticated traffic stays on the per-IP bucket,
    which is where it belongs.

    Fails open when the shared store cannot answer, like every other limiter here: the per-IP
    bucket still applies, and a Redis outage must not throttle every integration at once.
    """
    from app.core.config import get_settings

    settings = get_settings()
    if not settings.rate_limit_enabled:
        return True
    limit = settings.rate_limit_api_key_requests
    if limit <= 0:
        return True
    allowed = _shared_allow(f"apikey:{key_id}", limit, settings.rate_limit_window_seconds)
    return True if allowed is None else allowed


def consume_api_key_budget_state(key_id: str) -> Tuple[bool, Optional[Dict[str, int]]]:
    """Spend one request and report where the caller now stands.

    Returns `(allowed, state)`. `state` carries `limit`, `remaining` and `reset`, or is None when
    the limiter is off or its store could not answer. A caller emitting headers must omit them
    rather than guess: an integration told it has budget it does not have is worse served than one
    told nothing.

    This exists because a caller had no way to learn its limit. The refusal carried a hardcoded
    `Retry-After: 30` and no ceiling, while the documentation says deployments retune the number,
    so an integrator was asked to guess something the server knew.
    """
    from app.core.config import get_settings

    settings = get_settings()
    if not settings.rate_limit_enabled:
        return True, None
    limit = settings.rate_limit_api_key_requests
    if limit <= 0:
        return True, None

    window = settings.rate_limit_window_seconds
    spent = _shared_consume(f"apikey:{key_id}", limit, window)
    if spent is None:
        # Fails open, like every other limiter here.
        return True, None

    count, reset = spent
    return count <= limit, {
        "limit": limit,
        "remaining": max(0, limit - count),
        "reset": reset,
    }
@dataclass(frozen=True)
class RateLimitHit:
    """Which budget a request exhausted and when the client may come back."""

    scope: str
    limit: int
    retry_after: int


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

    def retry_after(self, key: str, window_seconds: int, now: float | None = None) -> int:
        """Seconds until the oldest hit in the window ages out, never below one.

        The window is sliding, so the client is admitted again the moment its oldest hit is
        older than the window, which is what this reports. A constant here told a client
        throttled one second before its window cleared to wait half a minute.
        """
        current_time = now if now is not None else time.time()
        bucket = self.storage.get(key, [])
        if not bucket:
            return 1
        remainder = window_seconds - (current_time - min(bucket))
        return max(1, math.ceil(remainder))

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

        hit = self._rate_limit_exceeded(request, client_ip)
        if hit is not None:
            logger.warning(
                "Rate limit exceeded",
                extra={
                    "client_ip": client_ip,
                    "client_subnet": get_client_subnet(client_ip),
                    "path": request.url.path,
                    "rate_limit_scope": hit.scope,
                    "rate_limit_limit": hit.limit,
                    "retry_after": hit.retry_after,
                },
            )
            return self._rate_limit_response(request, hit)

        return await call_next(request)

    def _rate_limit_exceeded(self, request: Request, client_ip: str) -> Optional[RateLimitHit]:
        """The budget this request exhausted, or None when it is admitted.

        note: the ip and subnet buckets are enforced per worker at the full configured value.
        A counter shared across workers would turn a deployment that sized RATE_LIMIT_REQUESTS
        for one browser into that same number for the whole VM. The documented ceiling is
        therefore the configured value per pinned client, up to WEB_CONCURRENCY times it in
        aggregate; only the fresh bucket is deployment-wide.
        """
        if not self.settings.rate_limit_enabled or request.method == "OPTIONS":
            return None

        limit_keys = self._rate_limit_keys(client_ip, request)
        now = time.time()
        window = self.settings.rate_limit_window_seconds
        for key, limit in limit_keys:
            scope = key.split(":", 1)[0]
            # important: the bypass budget is meant to be a deployment-wide figure, and
            # the in-memory limiter cannot be one because each worker keeps its own
            # counters. Redis is already a hard dependency, so the shared counter lives
            # there and the configured number means what it says. The per-key budget uses
            # the same store through consume_api_key_budget, but is spent after
            # authentication rather than here.
            if key.startswith("fresh:"):
                shared = _shared_allow(key, limit, window)
                if shared is not None:
                    if not shared:
                        return RateLimitHit(scope, limit, _shared_retry_after(key, window))
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
                return RateLimitHit(scope, limit, self.limiter.retry_after(key, window, now))
        return None

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

    def _rate_limit_response(self, request: Request, hit: RateLimitHit) -> Response:
        response = JSONResponse(
            status_code=429,
            content={"detail": f"Rate limit exceeded. Try again in {hit.retry_after} seconds."},
            headers={
                "Retry-After": str(hit.retry_after),
                "X-RateLimit-Limit": str(hit.limit),
                "X-RateLimit-Scope": hit.scope,
            },
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
