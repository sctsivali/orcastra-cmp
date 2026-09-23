"""
JWT Authentication module using JWKS (JSON Web Key Set) validation.

Validates access tokens from Authentik OIDC provider by fetching
the provider's public keys and verifying JWT signatures locally.
"""

import asyncio
import logging
import time
from typing import Optional

import httpx
import jwt
from fastapi import Depends, HTTPException, Request, WebSocket, WebSocketException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# reusable FastAPI security scheme
oauth2_scheme = HTTPBearer(auto_error=False)


class JWKSClient:
    """
    Caches JWKS (public keys) from the Authentik OIDC provider.
    Keys are refreshed after `cache_ttl` seconds or on key-not-found.
    Uses async httpx for non-blocking HTTP calls.
    """

    def __init__(self) -> None:
        self._jwks: dict = {}
        self._jwks_uri: str = ""
        self._last_fetched: float = 0.0
        self._cache_ttl: int = 3600  # default 1h
        self._lock = asyncio.Lock()
        self._warmed_up: bool = False

    def _discover_jwks_uri_sync(self, issuer: str) -> str:
        """Fetch JWKS URI synchronously (fallback for non-async contexts)."""
        import requests as _requests
        discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
        try:
            resp = _requests.get(discovery_url, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            jwks_uri = data.get("jwks_uri", "")
            if not jwks_uri:
                raise ValueError("jwks_uri not found in OIDC discovery document")
            logger.info(f"Discovered JWKS URI: {jwks_uri}")
            return jwks_uri
        except Exception as e:
            logger.error(f"Failed to fetch OIDC discovery from {discovery_url}: {e}")
            raise

    async def _discover_jwks_uri_async(self, issuer: str) -> str:
        """Fetch JWKS URI asynchronously using httpx."""
        discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(discovery_url)
                resp.raise_for_status()
                data = resp.json()
            jwks_uri = data.get("jwks_uri", "")
            if not jwks_uri:
                raise ValueError("jwks_uri not found in OIDC discovery document")
            logger.info(f"Discovered JWKS URI: {jwks_uri}")
            return jwks_uri
        except Exception as e:
            logger.error(f"Failed to fetch OIDC discovery from {discovery_url}: {e}")
            raise

    async def _fetch_jwks_async(self, jwks_uri: str) -> dict:
        """Fetch JWKS asynchronously using httpx."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(jwks_uri)
                resp.raise_for_status()
                jwks = resp.json()
            logger.info(f"Fetched {len(jwks.get('keys', []))} keys from JWKS endpoint")
            return jwks
        except Exception as e:
            logger.error(f"Failed to fetch JWKS from {jwks_uri}: {e}")
            raise

    def _fetch_jwks_sync(self, jwks_uri: str) -> dict:
        """Fetch JWKS synchronously (fallback)."""
        import requests as _requests
        try:
            resp = _requests.get(jwks_uri, timeout=5)
            resp.raise_for_status()
            jwks = resp.json()
            logger.info(f"Fetched {len(jwks.get('keys', []))} keys from JWKS endpoint")
            return jwks
        except Exception as e:
            logger.error(f"Failed to fetch JWKS from {jwks_uri}: {e}")
            raise

    async def warm_up(self, settings: Settings) -> None:
        """Pre-warm JWKS cache at startup to avoid first-request latency."""
        if not settings.authentik_issuer:
            return
        try:
            async with self._lock:
                self._cache_ttl = settings.auth_jwks_cache_ttl
                self._jwks_uri = await self._discover_jwks_uri_async(settings.authentik_issuer)
                self._jwks = await self._fetch_jwks_async(self._jwks_uri)
                self._last_fetched = time.time()
                self._warmed_up = True
            logger.info("JWKS cache pre-warmed successfully")
        except Exception as e:
            logger.warning(f"JWKS pre-warm failed (will retry on first request): {e}")

    def get_signing_key(self, token: str, settings: Settings) -> jwt.algorithms.RSAAlgorithm:
        """
        Get the signing key for a given JWT token.
        Fetches/caches JWKS and matches by 'kid' header.
        Uses sync calls as fallback if not pre-warmed.
        """
        self._cache_ttl = settings.auth_jwks_cache_ttl

        # discover jwks_uri if not yet known
        if not self._jwks_uri:
            self._jwks_uri = self._discover_jwks_uri_sync(settings.authentik_issuer)

        # refresh cache if expired
        now = time.time()
        if not self._jwks or (now - self._last_fetched) > self._cache_ttl:
            self._jwks = self._fetch_jwks_sync(self._jwks_uri)
            self._last_fetched = now

        # decode header to get kid
        try:
            unverified_header = jwt.get_unverified_header(token)
        except jwt.exceptions.DecodeError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token header: {e}",
            )

        kid = unverified_header.get("kid")
        if not kid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token header missing 'kid' claim",
            )

        # find matching key
        for key_data in self._jwks.get("keys", []):
            if key_data.get("kid") == kid:
                return jwt.algorithms.RSAAlgorithm.from_jwk(key_data)

        # key not found - try refreshing JWKS once (key rotation scenario)
        logger.warning(f"Key '{kid}' not found in cached JWKS, refreshing...")
        self._jwks = self._fetch_jwks_sync(self._jwks_uri)
        self._last_fetched = time.time()

        for key_data in self._jwks.get("keys", []):
            if key_data.get("kid") == kid:
                return jwt.algorithms.RSAAlgorithm.from_jwk(key_data)

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Unable to find signing key '{kid}' in JWKS",
        )


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
    ):
        self.user_id = user_id
        self.email = email
        self.name = name
        self.groups = groups or []
        self.roles = roles or []
        self.raw_claims = raw_claims or {}

    def has_group(self, group: str) -> bool:
        return group in self.groups

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def __repr__(self) -> str:
        return f"AuthenticatedUser(id={self.user_id}, email={self.email})"


def _auth_is_configured(settings: Settings) -> bool:
    return bool(settings.authentik_issuer) and bool(settings.authentik_audience)


def _require_auth_configured(settings: Settings) -> None:
    """Refuse with 503 when the server cannot tell whose token it is looking at.

    important: without an audience any token the issuer signed for any other application
    verifies here, so a missing AUTHENTIK_AUDIENCE is as much a misconfiguration as a
    missing issuer. The fault is the server's, so 503 rather than a 401 that blames the
    caller's token.
    """
    if not _auth_is_configured(settings):
        logger.error(
            "AUTH_ENABLED=true but AUTHENTIK_ISSUER or AUTHENTIK_AUDIENCE is not configured. "
            "Refusing the request rather than verifying tokens for any audience"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is enabled but not fully configured on this server",
        )


def validate_jwt(token: str, settings: Settings) -> AuthenticatedUser:
    """
    Validate a JWT access token against the Authentik JWKS.
    Returns an AuthenticatedUser on success, raises HTTPException on failure.
    """
    _require_auth_configured(settings)
    signing_key = _jwks_client.get_signing_key(token, settings)

    try:
        payload = jwt.decode(
            token,
            key=signing_key,
            algorithms=["RS256"],
            audience=settings.authentik_audience,
            issuer=settings.authentik_issuer,
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

        return AuthenticatedUser(
            user_id=user_id,
            email=email,
            name=name,
            groups=groups,
            roles=roles,
            raw_claims=payload,
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

    if not settings.authentik_issuer or not settings.authentik_audience:
        # important: this used to return None, and every RBAC dependency reads None as dev mode
        # and fabricates a full Admin. A single missing or misspelled AUTHENTIK_ISSUER therefore
        # turned an auth-enabled deployment into an open one for unauthenticated callers. A
        # deployment that asks for auth and cannot verify it must refuse, not wave people
        # through. 503 says the fault is the server's configuration, not the caller's token.
        # The audience is required for the same reason: without it a token minted for any
        # other application on the same issuer verifies here.
        logger.error(
            "AUTH_ENABLED=true but AUTHENTIK_ISSUER or AUTHENTIK_AUDIENCE is not configured. "
            "Refusing every request until both are set, rather than serving them unverified"
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

    user = validate_jwt(credentials.credentials, settings)

    # attach user to request state for access by middleware/logging
    # user_role is populated later by RBAC dependency (_populate_request_state)
    request.state.user_id = user.user_id
    request.state.user_email = user.email
    request.state.user_groups = user.groups
    request.state.user_role = None
    request.state.user_type = None  # alias for user_role, kept for compat

    return user


async def get_optional_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(oauth2_scheme),
    settings: Settings = Depends(get_settings),
) -> Optional[AuthenticatedUser]:
    """
    Like get_current_user but never raises 401.
    Returns None if no valid token is present.
    Useful for endpoints that work for both authenticated and anonymous users.
    """
    if not settings.auth_enabled or not settings.authentik_issuer:
        return None

    if not credentials:
        return None

    try:
        user = validate_jwt(credentials.credentials, settings)
        request.state.user_id = user.user_id
        request.state.user_email = user.email
        request.state.user_groups = user.groups
        return user
    except HTTPException:
        return None


async def get_ws_user(
    websocket: "WebSocket",
    settings: Settings = Depends(get_settings),
) -> Optional[AuthenticatedUser]:
    """
    WebSocket-compatible auth dependency.

    Extracts token from the ``token`` query parameter
    (WebSocket connections cannot use HTTP Authorization headers after upgrade).
    Returns ``None`` when auth is disabled, validates the JWT otherwise, and returns ``None``
    for a missing or invalid token so the caller closes the socket with 1008.

    important: a server with auth enabled and no issuer used to return ``None`` here, and
    every socket guard was gated on the issuer too, so the terminal, console, events and
    monitoring sockets served anyone. A server missing the issuer or the audience now
    refuses the handshake with 1011, which names the server's configuration as the fault,
    the same way get_current_user answers 503.
    """

    if not settings.auth_enabled:
        return None

    if not _auth_is_configured(settings):
        logger.error(
            "AUTH_ENABLED=true but AUTHENTIK_ISSUER or AUTHENTIK_AUDIENCE is not configured. "
            "Refusing every WebSocket until both are set, rather than serving them unverified"
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
        user = validate_jwt(token, settings)
        return user
    except HTTPException as exc:
        logger.warning(f"WebSocket JWT validation failed: {exc.detail}")
        return None
