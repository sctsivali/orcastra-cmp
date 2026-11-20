"""JWT authentication against the Authentik JWKS.

Access tokens are verified locally with the provider's public keys.
"""

import logging
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request, WebSocket, WebSocketException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.requests import HTTPConnection

from app.core.config import Settings, get_settings
from app.core.jwks import JWKSClient
from app.core.request_context import get_request_context

logger = logging.getLogger(__name__)

oauth2_scheme = HTTPBearer(auto_error=False)


_jwks_client = JWKSClient()


class AuthenticatedUser:
    """A user built from the claims of a validated JWT."""

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
    """Validate a JWT access token against the Authentik JWKS.

    Returns an AuthenticatedUser on success and raises HTTPException on failure.
    """
    signing_key = _jwks_client.get_signing_key(token, settings)

    try:
        audience = settings.authentik_audience or None

        # important: refuse when AUTHENTIK_AUDIENCE is unset. An issuer usually hosts several
        # applications; without the audience check any RS256 token it minted for any of them
        # passes here, and its `groups` claim drives RBAC. Same rule as a missing issuer: a
        # server that asks for authentication and cannot perform it fully refuses.
        if not audience:
            logger.error(
                "AUTH_ENABLED=true but AUTHENTIK_AUDIENCE is not configured; refusing rather "
                "than accepting any token this issuer minted for any application"
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication is enabled but not fully configured on this server",
            )

        # The leeway lets a host whose clock is a few seconds off Authentik's accept a token it
        # was just handed (`exp`, `iat`). Bounded in config to 0..120 seconds.
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

        # Standard OIDC claims.
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


def _edition_verifier():
    from app.editions import current_edition

    return current_edition().token_verifier


def token_verification_configured(settings: Settings) -> bool:
    """Whether this server can verify an access token: the edition's verifier, else the IdP issuer."""
    verifier = _edition_verifier()
    if verifier is not None:
        return verifier.configured(settings)
    return bool(settings.authentik_issuer)


async def verify_access_token(token: str, settings: Settings, connection: HTTPConnection) -> AuthenticatedUser:
    """The caller behind a bearer token, or an HTTPException.

    The identity provider's keys are refreshed first, asynchronously and never raising, so
    validate_jwt makes no network call and an IdP outage cannot block this worker's event loop.
    """
    verifier = _edition_verifier()
    if verifier is not None:
        user = await verifier.verify(token, settings, connection)
        # important: every caller reads a missing principal as "no authentication expected".
        if not isinstance(user, AuthenticatedUser):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication is enabled but not configured on this server",
            )
        return user
    await _jwks_client.ensure_keys(token, settings)
    return validate_jwt(token, settings)


async def admit_socket(user: AuthenticatedUser, websocket: WebSocket) -> Optional[str]:
    """None to admit a socket opened with a redeemed ticket, else the edition's refusal reason."""
    verifier = _edition_verifier()
    if verifier is None:
        return None
    return await verifier.admit_socket(user, websocket)


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(oauth2_scheme),
    settings: Settings = Depends(get_settings),
) -> Optional[AuthenticatedUser]:
    """FastAPI dependency that validates the Bearer token and returns the current user.

    With AUTH_ENABLED=false it returns None (dev mode). Otherwise a missing or invalid token is a
    401, and a server without AUTHENTIK_ISSUER answers 503.
    """
    if not settings.auth_enabled:
        return None

    if not token_verification_configured(settings):
        # important: never return None here; every RBAC dependency reads None as dev mode and
        # grants a full Admin. A deployment that asks for auth and cannot verify it refuses, and
        # 503 puts the fault on the server's configuration.
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

    user = await verify_access_token(credentials.credentials, settings, request)

    # For middleware and logging. The RBAC dependency fills user_role later
    # (_populate_request_state).
    request.state.user_id = user.user_id
    request.state.user_email = user.email
    request.state.user_groups = user.groups
    request.state.user_role = None
    request.state.user_type = None  # Alias for user_role.

    ctx = get_request_context()
    if ctx is not None:
        ctx.user_id = user.user_id
        ctx.user_groups = user.groups

    return user


async def get_ws_user(
    websocket: "WebSocket",
    settings: Settings = Depends(get_settings),
) -> Optional[AuthenticatedUser]:
    """WebSocket auth dependency; reads the token from the ``token`` query parameter.

    A WebSocket cannot send an Authorization header after the upgrade. Returns ``None`` when auth
    is disabled or the token is missing or invalid, and leaves the socket open for the caller to
    decide. With auth enabled a caller must refuse on ``None`` (``settings.auth_enabled and user is
    None``).

    important: a server that cannot verify tokens raises instead of returning ``None``, so a caller
    gated on anything other than ``auth_enabled`` still cannot let an unverified socket through.
    See the matching branch in `get_current_user`.
    """

    if not settings.auth_enabled:
        return None

    if not token_verification_configured(settings):
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

    try:
        return await verify_access_token(token, settings, websocket)
    except HTTPException as exc:
        if exc.status_code >= 500:
            # A server that could not verify the token closes with 1011: the fault is on this
            # side and a retry may succeed. 1008 would tell the client its credentials were refused.
            logger.warning(f"WebSocket JWT validation could not run: {exc.detail}")
            raise WebSocketException(
                code=status.WS_1011_INTERNAL_ERROR,
                reason="The identity provider could not be reached; retry shortly",
            )
        logger.warning(f"WebSocket JWT validation failed: {exc.detail}")
        return None
