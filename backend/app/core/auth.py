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
from fastapi import Depends, HTTPException, Request, WebSocket, status
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


def validate_jwt(token: str, settings: Settings) -> AuthenticatedUser:
    """
    Validate a JWT access token against the Authentik JWKS.
    Returns an AuthenticatedUser on success, raises HTTPException on failure.
    """
    signing_key = _jwks_client.get_signing_key(token, settings)

    try:
        # build audience list - Authentik may issue tokens with client_id as audience
        audience = settings.authentik_audience or None

        payload = jwt.decode(
            token,
            key=signing_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=settings.authentik_issuer,
            options={
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": bool(audience),
                "require": ["exp", "iss", "sub"],
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


# ---------------------------------------------------------------------------
# Mini (client-cert) mode: locally-minted HS256 session tokens
#
# In client-cert mode the reverse proxy verifies the user's TLS client cert and the
# backend mints its own short-lived JWT. This keeps the rest of the app (bearer tokens,
# RBAC, websockets) byte-for-byte identical to Authentik mode - only the token SOURCE
# differs. The two token families are kept apart by algorithm (HS256 vs RS256) and by
# distinct issuer/audience so a token from one mode is always rejected by the other.
# ---------------------------------------------------------------------------

def _local_jwt_secret(settings: Settings) -> str:
    """Signing secret for minted tokens; falls back to SECRET_KEY only if unset."""
    return settings.local_jwt_secret or settings.secret_key


def mint_local_jwt(
    fingerprint: str,
    display_name: Optional[str],
    role: str,
    settings: Settings,
) -> tuple[str, int]:
    """Mint a short-lived HS256 session token for a client-cert identity.

    Returns (token, ttl_seconds). The role is embedded as a ``role_<role>`` group so the
    existing ``resolve_roles`` / ``AUTHENTIK_GROUP_MAP`` machinery works unchanged.
    """
    secret = _local_jwt_secret(settings)
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Client-cert auth not configured (LOCAL_JWT_SECRET missing)",
        )
    now = int(time.time())
    ttl = settings.local_jwt_ttl_seconds
    payload = {
        "sub": fingerprint,
        "email": display_name,
        "name": display_name,
        "groups": [f"role_{role}"],
        "iss": settings.local_jwt_issuer,
        "aud": settings.local_jwt_audience,
        "iat": now,
        "exp": now + ttl,
    }
    token = jwt.encode(payload, secret, algorithm="HS256")
    return token, ttl


def validate_local_jwt(token: str, settings: Settings) -> AuthenticatedUser:
    """Validate a locally-minted HS256 session token (client-cert mode)."""
    secret = _local_jwt_secret(settings)
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Client-cert auth not configured (LOCAL_JWT_SECRET missing)",
        )
    try:
        payload = jwt.decode(
            token,
            key=secret,
            algorithms=["HS256"],  # pinned - never accept RS256 here (alg-confusion guard)
            audience=settings.local_jwt_audience,
            issuer=settings.local_jwt_issuer,
            options={
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": True,
                "require": ["exp", "iss", "sub"],
            },
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError as e:
        logger.warning(f"Local JWT validation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    fingerprint = payload.get("sub", "")
    groups = payload.get("groups", [])
    roles = [g for g in groups if g.startswith("role_")]
    return AuthenticatedUser(
        user_id=fingerprint,
        email=payload.get("email"),
        name=payload.get("name"),
        groups=groups,
        roles=roles,
        raw_claims=payload,
    )


def _validate_token(token: str, settings: Settings) -> AuthenticatedUser:
    """Dispatch token validation by AUTH_MODE. Always fail-closed (raises on invalid)."""
    if settings.auth_mode == "client-cert":
        return validate_local_jwt(token, settings)
    if not settings.authentik_issuer:
        # authentik mode but misconfigured - never fall through to anonymous access
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication backend not configured",
        )
    return validate_jwt(token, settings)


def _ensure_session_active(user: AuthenticatedUser, settings: Settings) -> None:
    """Enforce app-layer revocation for client-cert sessions.

    TLS ``optional_no_ca`` does not consult a CRL, and minted tokens outlive a revoke,
    so a revoked/role-changed identity is flagged in the shared cache and rejected here
    on every request (near-immediate revocation without a per-request DB hit).
    """
    if settings.auth_mode != "client-cert":
        return
    try:
        from app.core.rbac_cache import get_rbac_cache
        if get_rbac_cache().is_identity_session_revoked(user.user_id):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session revoked",
                headers={"WWW-Authenticate": "Bearer"},
            )
    except HTTPException:
        raise
    except Exception as e:  # cache unreachable - rely on short token TTL, never fail open
        logger.warning(f"Revocation cache check failed (relying on token TTL): {e}")


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(oauth2_scheme),
    settings: Settings = Depends(get_settings),
) -> Optional[AuthenticatedUser]:
    """
    FastAPI dependency that extracts and validates the current user.

    - If AUTH_ENABLED=true: requires a valid Bearer token, raises 401 otherwise.
      The token is validated per AUTH_MODE (Authentik RS256 vs locally-minted HS256).
    - If AUTH_ENABLED=false: returns None (dev bypass; main.py refuses to start this
      combination unless DEBUG=true).
    """
    if not settings.auth_enabled:
        return None

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = _validate_token(credentials.credentials, settings)
    _ensure_session_active(user, settings)

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
    if not settings.auth_enabled:
        return None

    if not credentials:
        return None

    try:
        user = _validate_token(credentials.credentials, settings)
        _ensure_session_active(user, settings)
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
    Returns ``None`` when auth is disabled; validates JWT otherwise.
    Does NOT close the socket - callers decide how to handle ``None``.
    """
    from starlette.websockets import WebSocket as _WS  # type check only

    if not settings.auth_enabled:
        return None

    token = websocket.query_params.get("token")
    if not token:
        logger.warning("WebSocket connection rejected: no token in query params")
        return None

    try:
        user = _validate_token(token, settings)
        _ensure_session_active(user, settings)
        return user
    except HTTPException as exc:
        logger.warning(f"WebSocket JWT validation failed: {exc.detail}")
        return None
