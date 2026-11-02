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
        
        # Authentication runs after this middleware. Seed no identity from an unverified token;
        # auth and RBAC add the verified principal to this context after validation succeeds.
        context = RequestContext(
            request_id=request_id,
            user_id=None,
            user_role=None,
            user_groups=[],
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
