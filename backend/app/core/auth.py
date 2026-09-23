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
from app.core.request_context import get_request_context

logger = logging.getLogger(__name__)

# reusable FastAPI security scheme
oauth2_scheme = HTTPBearer(auto_error=False)


class JWKSClient:
    """
    Caches JWKS (public keys) from the Authentik OIDC provider.

    important: every fetch happens in ensure_key_for, which is async, and get_signing_key is a
    pure lookup. A token with an unknown kid used to trigger a synchronous refetch on the event
    loop for every request, so random kids stalled the worker and hammered the provider. An
    unknown kid now forces at most one refetch per FORCED_REFETCH_INTERVAL per worker, and a
    failed refresh is not retried inside FAILED_REFRESH_BACKOFF. Keys stay in use while a TTL
    refresh fails.
    """

    FORCED_REFETCH_INTERVAL = 30.0
    FAILED_REFRESH_BACKOFF = 5.0

    def __init__(self) -> None:
        self._jwks: dict = {}
        self._jwks_uri: str = ""
        self._cache_ttl: int = 3600  # default 1h
        self._lock = asyncio.Lock()
        self._warmed_up: bool = False
        # note: monotonic, and an attribute so tests can move time without touching the loop's clock
        self._clock = time.monotonic
        self._last_fetched: float = float("-inf")
        self._last_forced_refetch: float = float("-inf")
        self._last_failed_refresh: float = float("-inf")

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

    async def warm_up(self, settings: Settings) -> None:
        """Pre-warm JWKS cache at startup to avoid first-request latency."""
        if not settings.authentik_issuer:
            return
        try:
            async with self._lock:
                self._cache_ttl = settings.auth_jwks_cache_ttl
                self._jwks_uri = await self._discover_jwks_uri_async(settings.authentik_issuer)
                self._jwks = await self._fetch_jwks_async(self._jwks_uri)
                self._last_fetched = self._clock()
                self._warmed_up = True
            logger.info("JWKS cache pre-warmed successfully")
        except Exception as e:
            logger.warning(f"JWKS pre-warm failed (will retry on first request): {e}")

    def _find_key(self, kid: object) -> Optional[dict]:
        for key_data in self._jwks.get("keys", []):
            if key_data.get("kid") == kid:
                return key_data
        return None

    def _is_stale(self, ttl: float) -> bool:
        return not self._jwks.get("keys") or (self._clock() - self._last_fetched) > ttl

    def _satisfied(self, kid: object, ttl: float) -> bool:
        return self._find_key(kid) is not None and not self._is_stale(ttl)

    async def ensure_key_for(self, token: str, settings: Settings) -> None:
        """Refresh the cache when it cannot answer for this token's kid. Never raises.

        A stale or empty cache is refreshed unless the last refresh failed within the backoff.
        A fresh cache missing the kid is refetched at most once per FORCED_REFETCH_INTERVAL,
        whoever asks. Whatever happens, get_signing_key decides the answer afterwards.
        """
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            return
        if not kid:
            return
        ttl = settings.auth_jwks_cache_ttl
        self._cache_ttl = ttl
        if self._satisfied(kid, ttl):
            return
        async with self._lock:
            if self._satisfied(kid, ttl):
                return
            now = self._clock()
            if self._is_stale(ttl):
                if now - self._last_failed_refresh < self.FAILED_REFRESH_BACKOFF:
                    return
            else:
                if now - self._last_forced_refetch < self.FORCED_REFETCH_INTERVAL:
                    return
                self._last_forced_refetch = now
                # note: the kid is attacker-chosen text, so it stays out of the log line
                logger.warning("Token signing key not found in cached JWKS, refreshing")
            try:
                if not self._jwks_uri:
                    self._jwks_uri = await self._discover_jwks_uri_async(settings.authentik_issuer)
                self._jwks = await self._fetch_jwks_async(self._jwks_uri)
                self._last_fetched = self._clock()
            except Exception:  # noqa: BLE001 - the lookup that follows answers 401 or 503
                self._last_failed_refresh = self._clock()

    def get_signing_key(self, token: str) -> jwt.algorithms.RSAAlgorithm:
        """Look up the key for this token's kid in the cache. Never touches the network."""
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

        if not self._jwks.get("keys"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Token signing keys are not available yet, try again shortly",
            )

        key_data = self._find_key(kid)
        if key_data is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unable to find the token's signing key",
            )
        return jwt.algorithms.RSAAlgorithm.from_jwk(key_data)


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
    Validate a JWT access token against the cached Authentik JWKS.
    Returns an AuthenticatedUser on success, raises HTTPException on failure.
    Never fetches keys. Callers go through verify_token, which refreshes the cache first.
    """
    _require_auth_configured(settings)
    signing_key = _jwks_client.get_signing_key(token)

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


async def verify_token(token: str, settings: Settings) -> AuthenticatedUser:
    """Verify a bearer token, refreshing the key cache asynchronously when it cannot answer.

    The one entry point for request and socket authentication. Raises HTTPException: 503 when
    the server is not configured or has no keys yet, 401 for a token that does not verify.
    """
    _require_auth_configured(settings)
    await _jwks_client.ensure_key_for(token, settings)
    return validate_jwt(token, settings)


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

    user = await verify_token(credentials.credentials, settings)

    # attach user to request state for access by middleware/logging
    # user_role is populated later by RBAC dependency (_populate_request_state)
    request.state.user_id = user.user_id
    request.state.user_email = user.email
    request.state.user_groups = user.groups
    request.state.user_role = None
    request.state.user_type = None  # alias for user_role, kept for compat

    # note: the access log, app log and audit actor read the RequestContext. It is written only
    # here and in rbac._populate_request_state, both after the token verified.
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
        return await verify_token(token, settings)
    except HTTPException as exc:
        logger.warning(f"WebSocket JWT validation failed: {exc.detail}")
        return None
