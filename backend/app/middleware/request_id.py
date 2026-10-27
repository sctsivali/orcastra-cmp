"""
Request ID Middleware
Generates and propagates unique request IDs for end-to-end tracing.
"""

import re
import uuid
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import get_settings
from app.core.request_context import RequestContext, set_request_context
from app.core.request_security import resolve_client_ip


# Header name for request ID propagation
REQUEST_ID_HEADER = "X-Request-ID"

# important: the request ID is always minted here. It becomes the access log's OpenSearch `_id`
# (Fluent Bit `Id_Key request_id`, an upsert), so a caller-chosen ID let a client overwrite the
# access record of an earlier request, its own included, and shape the bulk metadata line. The
# caller's header is kept for correlation as `client_request_id`, and only when it is a plain token.
_CLIENT_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


def client_request_id(value: str | None) -> str | None:
    """The caller's X-Request-ID when it is a plain token, else None."""
    return value if value and _CLIENT_REQUEST_ID.fullmatch(value) else None


class RequestIdMiddleware(BaseHTTPMiddleware):
    """
    Middleware to generate and propagate request IDs.
    
    - Generates a new UUID for every request (see the note above on why a caller's is not used)
    - Keeps a caller's plain-token X-Request-ID as request.state.client_request_id
    - Stores request ID in context for access by other components
    - Adds X-Request-ID header to response
    """
    
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = str(uuid.uuid4())
        request.state.client_request_id = client_request_id(request.headers.get(REQUEST_ID_HEADER))
        
        # Extract user info from headers/session (if available)
        user_id = self._extract_user_id(request)
        user_role = self._extract_user_role(request)
        user_groups = self._extract_user_groups(request)
        
        # Create request context (user_type is an alias for user_role)
        context = RequestContext(
            request_id=request_id,
            user_id=user_id,
            user_role=user_role,
            user_groups=user_groups,
            client_ip=self._get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            origin=request.headers.get("origin"),
            method=request.method,
            path=request.url.path,
        )
        
        # Set context for this request
        set_request_context(context)
        
        # Store in request state for easy access
        request.state.request_id = request_id
        request.state.request_context = context
        
        # Process request
        response = await call_next(request)
        
        # Add request ID to response headers
        response.headers[REQUEST_ID_HEADER] = request_id
        # What the response cache served (core/swr.py): the frontend reads it to ask again once
        # a background read has replaced a stale body, and operators read it in the browser.
        cache_state = context.extra.get("cache")
        if cache_state:
            response.headers["X-Orcastra-Cache"] = cache_state
            if context.extra.get("cache_age_s") is not None:
                response.headers["X-Orcastra-Cache-Age"] = str(int(context.extra["cache_age_s"]))
        
        return response
    
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
    
    def _extract_user_id(self, request: Request) -> str | None:
        """Extract user ID from request (from JWT claims or auth middleware)."""
        # Check if already set by auth dependency (preferred path)
        if hasattr(request.state, "user_id") and request.state.user_id:
            return request.state.user_id
        
        # Fallback: lightweight JWT decode for early context (no verification)
        claims = self._peek_jwt_claims(request)
        if claims:
            return claims.get("email") or claims.get("sub")
        
        return None
    
    def _extract_user_role(self, request: Request) -> str | None:
        """Extract user role from request."""
        if hasattr(request.state, "user_role") and request.state.user_role:
            return request.state.user_role
        # Fallback: resolve from JWT groups
        return self._resolve_role_from_jwt(request)
    
    def _extract_user_groups(self, request: Request) -> list:
        """Extract user groups from request."""
        if hasattr(request.state, "user_groups") and request.state.user_groups:
            return request.state.user_groups
        # Fallback: extract from JWT claims
        claims = self._peek_jwt_claims(request)
        if claims:
            return claims.get("groups", [])
        return []
    
    def _peek_jwt_claims(self, request: Request) -> dict | None:
        """Lightweight unverified JWT decode for early context extraction.
        Actual verification happens in the auth dependency."""
        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return None
        try:
            import jwt as pyjwt
            token = auth_header[7:]
            return pyjwt.decode(token, options={"verify_signature": False})
        except Exception:
            return None
    
    def _resolve_role_from_jwt(self, request: Request) -> str | None:
        """Resolve the highest role from JWT groups (lightweight, no auth check).
        Mirrors the RBAC AUTHENTIK_GROUP_MAP logic for early context."""
        claims = self._peek_jwt_claims(request)
        if not claims:
            return None
        groups = claims.get("groups", [])
        if not groups:
            return None
        # lightweight role resolution matching AUTHENTIK_GROUP_MAP precedence
        _GROUP_ROLE = {
            "role_admin": "admin", "admin": "admin", "admins": "admin",
            "authentik admins": "admin", "orcastra-admins": "admin",
            "role_partner": "partner", "partner": "partner",
            "orcastra-partners": "partner",
            "role_tenant": "tenant", "tenant": "tenant",
            "orcastra-tenants": "tenant",
        }
        _HIERARCHY = {"tenant": 0, "partner": 1, "admin": 2}
        best = None
        for g in groups:
            role = _GROUP_ROLE.get(g.lower().strip())
            if role and (best is None or _HIERARCHY.get(role, 0) > _HIERARCHY.get(best, 0)):
                best = role
        return best
