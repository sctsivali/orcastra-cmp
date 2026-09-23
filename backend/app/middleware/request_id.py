"""
Request ID Middleware
Generates and propagates unique request IDs for end-to-end tracing.
"""

import uuid
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import get_settings
from app.core.request_context import RequestContext, set_request_context
from app.core.request_security import resolve_client_ip


# Header name for request ID propagation
REQUEST_ID_HEADER = "X-Request-ID"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """
    Middleware to generate and propagate request IDs.
    
    - Generates a new UUID if X-Request-ID header is not present
    - Propagates existing request ID if header is present
    - Stores request ID in context for access by other components
    - Adds X-Request-ID header to response
    """
    
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Get or generate request ID
        request_id = request.headers.get(REQUEST_ID_HEADER)
        if not request_id:
            request_id = str(uuid.uuid4())
        
        # important: no identity is seeded here. This runs before routing, so nothing has
        # verified the bearer token yet, and a claim decoded from it let anyone write any user
        # and role into the access log, the app log and the audit actor. The verified principal
        # is written into this same context by get_current_user and the RBAC dependencies.
        context = RequestContext(
            request_id=request_id,
            user_id=None,
            user_role=None,
            user_groups=[],
            client_ip=self._get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            origin=request.headers.get("origin"),
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
