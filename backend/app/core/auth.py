"""
JWT Authentication module using JWKS (JSON Web Key Set) validation.

Validates access tokens from Authentik OIDC provider by fetching
the provider's public keys and verifying JWT signatures locally.
"""

import logging
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request, WebSocket, WebSocketException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings
from app.core.jwks import JWKSClient
from app.core.request_context import get_request_context

logger = logging.getLogger(__name__)

# reusable FastAPI security scheme
oauth2_scheme = HTTPBearer(auto_error=False)


# module-level singleton
_jwks_client = JWKSClient()


class AuthenticatedUser:
    """Represents a validated user extracted from a JWT token."""

    def __init__(
        self,
        user_id: str,
        email: Optional[str] = None,
        name: Optional[str] = None,
        groups: Optional[list[str]] = None,
        roles: Optional[list[str]] = None,
        raw_claims: Optional[dict] = None,
        session_exp: Optional[float] = None,
    ):
        self.user_id = user_id
        self.email = email
        self.name = name
        self.groups = groups or []
        self.roles = roles or []
        self.raw_claims = raw_claims or {}
        # The access token's expiry, epoch seconds. A long-lived socket outlives its token, and
        # this is when the session it was opened under ends.
        self.session_exp = session_exp

    def has_group(self, group: str) -> bool:
        return group in self.groups

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def __repr__(self) -> str:
        return f"AuthenticatedUser(id={self.user_id}, email={self.email})"


def validate_jwt(token: str, settings: Settings) -> AuthenticatedUser:
    """
    Validate a JWT access token against the Authentik JWKS.
    Returns an AuthenticatedUser on success, raises HTTPException on failure.
    """
    signing_key = _jwks_client.get_signing_key(token, settings)

    try:
        # build audience list - Authentik may issue tokens with client_id as audience
        audience = settings.authentik_audience or None

        # important: `verify_aud` used to be `bool(audience)`, so leaving AUTHENTIK_AUDIENCE
        # unset turned audience checking off silently. An issuer typically hosts several
        # applications, and without this check any RS256 token that issuer minted for any of them
        # is accepted here, with its `groups` claim driving RBAC. A user with a token for some
        # low-value app on the same Authentik becomes whatever their groups say they are.
        #
        # Same rule as the missing issuer: a server that asks for authentication and cannot
        # perform it properly refuses, rather than accepting a weaker proof without saying so.
        if not audience:
            logger.error(
                "AUTH_ENABLED=true but AUTHENTIK_AUDIENCE is not configured; refusing rather "
                "than accepting any token this issuer minted for any application"
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication is enabled but not fully configured on this server",
            )

        # important: no leeway made a host whose clock ran a few seconds ahead of Authentik's refuse
        # a token the dashboard had just been handed ("Token has expired", or iat in the future),
        # and a socket ticket minted with it came back 401. The skew is bounded in config (0..120).
        payload = jwt.decode(
            token,
            key=signing_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=settings.authentik_issuer,
            leeway=getattr(settings, "auth_jwt_leeway_seconds", 0),
            options={
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": True,
                "require": ["exp", "iss", "sub", "aud"],
            },
        )

        # extract user info from standard OIDC claims
        user_id = payload.get("email") or payload.get("sub", "")
        email = payload.get("email")
        name = payload.get("name") or payload.get("preferred_username")
        groups = payload.get("groups", [])
        roles = [g for g in groups if g.startswith("role_")]

        exp = payload.get("exp")
        return AuthenticatedUser(
            user_id=user_id,
            email=email,
            name=name,
            groups=groups,
            roles=roles,
            raw_claims=payload,
            session_exp=float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None,
        )

    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidAudienceError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token audience",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidIssuerError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token issuer",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError as e:
        logger.warning(f"JWT validation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(oauth2_scheme),
    settings: Settings = Depends(get_settings),
) -> Optional[AuthenticatedUser]:
    """
    FastAPI dependency that extracts and validates the current user from JWT.

    - If AUTH_ENABLED=true: requires valid Bearer token, raises 401 otherwise
    - If AUTH_ENABLED=false: returns None (dev mode / no auth)
    """
    if not settings.auth_enabled:
        return None

    if not settings.authentik_issuer:
        # important: this used to return None, and every RBAC dependency reads None as dev mode
        # and fabricates a full Admin. A single missing or misspelled AUTHENTIK_ISSUER therefore
        # turned an auth-enabled deployment into an open one for unauthenticated callers. A
        # deployment that asks for auth and cannot verify it must refuse, not wave people
        # through; 503 says the fault is the server's configuration, not the caller's token.
        logger.error(
            "AUTH_ENABLED=true but AUTHENTIK_ISSUER is not configured; refusing every request "
            "until it is set, rather than serving them unauthenticated"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is enabled but not configured on this server",
        )

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # The keys are refreshed here, asynchronously and never raising, so validate_jwt below makes
    # no network call and an IdP outage cannot block this worker's event loop.
    await _jwks_client.ensure_keys(credentials.credentials, settings)
    user = validate_jwt(credentials.credentials, settings)

    # attach user to request state for access by middleware/logging
    # user_role is populated later by RBAC dependency (_populate_request_state)
    request.state.user_id = user.user_id
    request.state.user_email = user.email
    request.state.user_groups = user.groups
    request.state.user_role = None
    request.state.user_type = None  # alias for user_role, kept for compat

    ctx = get_request_context()
    if ctx is not None:
        ctx.user_id = user.user_id
        ctx.user_groups = user.groups

    return user


async def get_ws_user(
    websocket: "WebSocket",
    settings: Settings = Depends(get_settings),
) -> Optional[AuthenticatedUser]:
    """
    WebSocket-compatible auth dependency.

    Extracts token from the ``token`` query parameter
    (WebSocket connections cannot use HTTP Authorization headers after upgrade).
    Returns ``None`` when auth is disabled; validates JWT otherwise.
    Does NOT close the socket for a bad token - callers decide how to handle ``None``.

    important: it DOES close the socket for a misconfigured server, and that asymmetry is the
    whole point. `None` means "no authentication is expected here", which every caller reads as
    permission to continue. Returning it for an auth-enabled server that cannot verify tokens
    made one unset variable open the sockets to anyone, and the guards could not catch it because
    they were gated on the same unset variable. See the matching branch in `get_current_user`.
    """

    if not settings.auth_enabled:
        return None

    if not settings.authentik_issuer:
        logger.error(
            "AUTH_ENABLED=true but AUTHENTIK_ISSUER is not configured; refusing every WebSocket "
            "until it is set, rather than serving them unauthenticated"
        )
        raise WebSocketException(
            code=status.WS_1011_INTERNAL_ERROR,
            reason="Authentication is enabled but not configured on this server",
        )

    token = websocket.query_params.get("token")
    if not token:
        logger.warning("WebSocket connection rejected: no token in query params")
        return None

    await _jwks_client.ensure_keys(token, settings)
    try:
        user = validate_jwt(token, settings)
        return user
    except HTTPException as exc:
        if exc.status_code >= 500:
            # important: a server that cannot verify the token is not a refused token. Returning
            # None closed the socket with 1008 (policy), which tells the client its credentials
            # were rejected; 1011 says the fault is on this side and a retry may succeed.
            logger.warning(f"WebSocket JWT validation could not run: {exc.detail}")
            raise WebSocketException(
                code=status.WS_1011_INTERNAL_ERROR,
                reason="The identity provider could not be reached; retry shortly",
            )
        logger.warning(f"WebSocket JWT validation failed: {exc.detail}")
        return None
