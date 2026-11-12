"""Request hardening: security headers, scanner-probe blocking and rate limits."""

from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Tuple

from pydantic import TypeAdapter, ValidationError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

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
    """Count one hit against a counter shared by every worker.

    True allows, False rejects, and None means Redis could not answer and the caller falls back.

    important: fail open. A limiter that rejects when its store is unreachable turns a Redis
    outage into a full product outage.
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
        allowed = int(count) <= limit
        if not allowed:
            # An expire lost after its incr (ex; a timeout between the two, or the Redis guard
            # opening) leaves a counter with no expiry, which refuses its subject for good once
            # past the limit. Repaired only on a refusal, in its own try so the answer stands.
            try:
                if client.ttl(bucket) == -1:
                    client.expire(bucket, window_seconds)
            except Exception:  # noqa: BLE001 - the repair is best effort
                pass
        return allowed
    except Exception as exc:  # noqa: BLE001 - any store failure must fall back, not block
        logger.warning("Shared rate limit unavailable, falling back to in-memory: %s", exc)
        return None


def _shared_consume(key: str, limit: int, window_seconds: int) -> Optional[Tuple[int, int]]:
    """Spend one hit and report the count and the seconds left in the window.

    The same operation as `_shared_allow`, for callers that tell the client where it stands. None
    means the store could not answer (fail open); the caller must not invent a remaining budget.
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
            # An expire lost after its incr leaves the counter with no expiry, which would hold its
            # budget spent for good. Give it its window again.
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
    """Split a deployment-wide budget across the worker processes that enforce it.

    important: the in-memory limiter counts per uvicorn worker, so without the split the real
    ceiling is limit x WEB_CONCURRENCY. The split is approximate because a client's requests are
    not spread evenly across workers; the shared Redis counter is the exact path.
    """
    if limit <= 0:
        return limit
    return max(1, limit // configured_worker_count())


# The frontend's /api/v1 proxy sets this on a request it authenticated from the session cookie, and
# drops it from anything a caller sends (frontend/src/lib/api-proxy-auth.ts applyProxyAuth).
PROXY_SESSION_HEADER = "x-orcastra-proxy-auth"

_FRESH_FLAG = TypeAdapter(bool)


def _wants_fresh(request: Request) -> bool:
    """True when the request asks to bypass the response cache.

    important: use the same pydantic coercion FastAPI applies to `fresh: bool = False`. A
    hand-written list of spellings misses values pydantic accepts (ex; `t`, `Y`), and such a
    request would bypass the cache without being charged to the per-operator `fresh:` budget.
    """
    value = request.query_params.get("fresh")
    if value is None:
        return False
    try:
        return _FRESH_FLAG.validate_python(value)
    except ValidationError:
        return False


def _rate_limit_subject(request: Request, client_ip: str) -> str:
    """Bucket identity for the bypass budget.

    The budget is per operator: the dashboard proxies every browser through one container, so an
    IP-keyed budget would let one person's refreshes throttle the whole tenancy.

    important: key on a digest of the presented token, never on a claim inside it. This middleware
    runs before authentication, so a claim is unverified text, and keying on `sub` would let anyone
    who knows a victim's email spend that operator's budget from unauthenticated requests. A token
    rotation starts a fresh counter; the fan-out cost stays bounded per token.
    """
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer ":
        token = header[7:].strip()
        if token:
            return "tok:" + hashlib.sha256(token.encode()).hexdigest()[:32]
    return f"ip:{client_ip}"


def consume_api_key_budget(key_id: str) -> bool:
    """Spend one request against an API key's own budget. True when it may proceed.

    important: spend it only after the key's secret verifies, never in the middleware. The key id
    is public (`IntegrationOrgResponse.api_key_id`, logs, outbound callback headers), so counting
    unauthenticated requests would let anyone holding an id exhaust that integration's budget with
    a wrong secret. Unauthenticated traffic stays on the per-IP bucket.

    Fails open when the shared store cannot answer; the per-IP bucket still applies.
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
    the limiter is off or its store could not answer; a caller emitting rate-limit headers then
    omits them instead of guessing.
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

        The window slides, so the client is admitted again as soon as its oldest hit is older than
        the window.
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

        The ip and subnet buckets are enforced per worker at the full configured value: the
        ceiling is that value per pinned client, up to WEB_CONCURRENCY times it in aggregate. A
        shared counter would turn a RATE_LIMIT_REQUESTS sized for one browser into the limit for
        the whole host. Only the fresh bucket is deployment-wide.
        """
        if not self.settings.rate_limit_enabled or request.method == "OPTIONS":
            return None

        limit_keys = self._rate_limit_keys(client_ip, request)
        now = time.time()
        window = self.settings.rate_limit_window_seconds
        for key, limit in limit_keys:
            scope = key.split(":", 1)[0]
            # The bypass budget is deployment-wide, which the per-worker in-memory limiter
            # cannot enforce, so it uses the shared Redis counter. The per-key budget uses the
            # same store through consume_api_key_budget, spent after authentication.
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

    def _proxied_session(self, request: Request) -> Optional[str]:
        """The token bucket of a request the dashboard's proxy authenticated from a session, or None.

        Every operator's page calls arrive from the frontend container's one address, so an
        address-keyed budget is shared by everyone signed in to the deployment. The proxy's mark is
        believed only from a trusted proxy address, the trust `x-orcastra-client-ip` already gets.
        important: never the bearer alone. A cookieless caller's own Authorization passes through
        the proxy unmarked, and keying on it would let random tokens open a bucket per request.
        """
        if request.headers.get(PROXY_SESSION_HEADER) != "session":
            return None
        if not resolve_client_ip(request, self.settings.trusted_proxy_cidrs).trusted_proxy:
            return None
        subject = _rate_limit_subject(request, "")
        return subject if subject.startswith("tok:") else None

    def _rate_limit_keys(self, client_ip: str, request: Request) -> Iterable[tuple[str, int]]:
        session = self._proxied_session(request)
        if session is not None:
            # No subnet bucket: it would be the frontend container's, shared again by everyone.
            yield f"user:{session}", self.settings.rate_limit_requests
        else:
            yield f"ip:{client_ip}", self.settings.rate_limit_requests

            subnet = get_client_subnet(client_ip)
            if subnet:
                yield f"subnet:{subnet}", self.settings.rate_limit_subnet_requests

        # important: a forced cache bypass costs a full fan-out to every cluster in
        # scope, so it gets its own much smaller budget, keyed per operator token
        # (see _rate_limit_subject).
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


# A terminal socket, or a console socket (one per SPICE channel). The feeds are not counted here.
_INTERACTIVE_SOCKET_PATH = re.compile(r"/(?:terminal/ws/[^/]+|console/[^/]+/ws)$")


class InteractiveSocketLimitMiddleware:
    """A per-client-IP budget for terminal and console WebSocket upgrades.

    important: RequestSecurityMiddleware covers HTTP only. uvicorn holds a refused terminal or
    console socket (accepted, then closed by core/ws_close.py) until the client answers the close,
    inside --limit-concurrency, so one client could fill those slots with sockets that never
    authenticate. Upgrades are counted per client IP before routing and any ticket lookup.

    Over budget the upgrade is refused without accepting: HTTP 429 with Retry-After where the
    server offers the denial-response extension (uvicorn does), otherwise a close before accept
    (HTTP 403). Each worker enforces its share of the deployment-wide figure.
    """

    def __init__(self, app: ASGIApp, settings, limiter: WindowedRateLimiter | None = None) -> None:
        self.app = app
        self.settings = settings
        self.limiter = limiter or WindowedRateLimiter()
        # At most one warning per client per window.
        self._warned = WindowedRateLimiter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "websocket" or not _INTERACTIVE_SOCKET_PATH.search(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        hit = self._exceeded(scope)
        if hit is None:
            await self.app(scope, receive, send)
            return
        await self._refuse(scope, receive, send, hit)

    def _exceeded(self, scope: Scope) -> Optional[RateLimitHit]:
        configured = self.settings.ws_upgrade_rate_limit
        if not self.settings.rate_limit_enabled or configured <= 0:
            return None
        client_ip = resolve_client_ip(
            HTTPConnection(scope),
            self.settings.trusted_proxy_cidrs,
            self.settings.allow_private_forwarded_ips,
        ).client_ip
        key = f"ws:{client_ip}"
        limit = _per_worker_share(configured)
        window = self.settings.rate_limit_window_seconds
        now = time.time()
        if self.limiter.allow(key=key, limit=limit, window_seconds=window, now=now):
            return None
        retry_after = self.limiter.retry_after(key, window, now)
        if self._warned.allow(key=key, limit=1, window_seconds=window, now=now):
            logger.warning(
                "Interactive socket upgrades over budget; refusing them until the window clears",
                extra={
                    "client_ip": client_ip,
                    "path": scope.get("path", ""),
                    "rate_limit_scope": "ws",
                    "rate_limit_limit": limit,
                    "retry_after": retry_after,
                },
            )
        return RateLimitHit("ws", limit, retry_after)

    async def _refuse(self, scope: Scope, receive: Receive, send: Send, hit: RateLimitHit) -> None:
        message = await receive()
        if message.get("type") != "websocket.connect":
            return
        if "websocket.http.response" in (scope.get("extensions") or {}):
            body = f"Too many live connections. Try again in {hit.retry_after} seconds.".encode("utf-8")
            await send({
                "type": "websocket.http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"retry-after", str(hit.retry_after).encode("ascii")),
                    (b"x-ratelimit-scope", b"ws"),
                ],
            })
            await send({"type": "websocket.http.response.body", "body": body})
            return
        await send({"type": "websocket.close", "code": 1013, "reason": "rate_limited"})
