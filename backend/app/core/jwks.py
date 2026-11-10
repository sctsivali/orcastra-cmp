"""The identity provider's signing keys, cached per worker and refreshed without blocking a request.

Every signed-in request verifies its JWT against keys published at the IdP's JWKS endpoint.
`ensure_keys` refreshes asynchronously, once per worker at a time and at most once per
`_FETCH_SPACING`, and never raises, so an IdP outage never holds a request for the HTTP timeout.
`get_signing_key` makes no network call: it answers from the keys held for up to
`AUTH_JWKS_MAX_STALE` after the last successful fetch, and otherwise refuses with a 503 the client
can act on.

Serving keys past their TTL is the usual JWKS cache trade-off: a key the IdP revoked during an
outage keeps verifying until the outage ends or max-stale passes. Access tokens still expire on
their own schedule.
"""

import asyncio
import logging
import math
import time
from typing import Any, Dict, Optional

import httpx
import jwt
from fastapi import HTTPException, status

from app.core.config import Settings

logger = logging.getLogger(__name__)

__all__ = ["IDENTITY_PROVIDER_UNAVAILABLE", "JWKSClient"]

IDENTITY_PROVIDER_UNAVAILABLE = "identity_provider_unavailable"
_UNAVAILABLE_MESSAGE = (
    "The identity provider could not be reached to verify this sign-in. Retry shortly."
)
_HTTP_TIMEOUT = 5.0
_DEFAULT_MAX_STALE = 86400


def _wall() -> float:
    return time.time()


def _mono() -> float:
    return time.monotonic()


def _unverified_kid(token: str) -> Optional[str]:
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except Exception:  # noqa: BLE001 - any unparsable token is answered by get_signing_key
        return None
    return kid if isinstance(kid, str) and kid else None


def _usable(jwks: Any) -> bool:
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    return isinstance(keys, list) and any(isinstance(k, dict) and k.get("kid") for k in keys)


class JWKSClient:
    """Caches the JWKS of the Authentik OIDC provider for one worker."""

    # At most one fetch attempt per this many seconds per worker, after a success or a failure.
    # It is both the hold that stops a down IdP being hammered and the bound on how often tokens
    # carrying unknown kids can make this worker call out.
    _FETCH_SPACING = 30.0

    # A bounded set of kids a fresh JWKS did not contain, with the time each was recorded.
    _ABSENT_KID_TTL = 60.0
    _ABSENT_KID_MAX = 512

    def __init__(self) -> None:
        self._jwks: Dict[str, Any] = {}
        self._jwks_uri: str = ""
        self._last_fetched: float = 0.0  # wall clock of the keys held
        self._next_attempt: float = 0.0  # monotonic
        self._last_error: Optional[str] = None
        self._lock = asyncio.Lock()
        self._refresh_task: Optional["asyncio.Task[None]"] = None
        self._absent_kids: Dict[str, float] = {}

    # ── network ──────────────────────────────────────────────────────────────────────────────

    async def _discover_jwks_uri_async(self, issuer: str) -> str:
        discovery_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(discovery_url)
            resp.raise_for_status()
            jwks_uri = resp.json().get("jwks_uri", "")
        if not jwks_uri:
            raise ValueError("jwks_uri not found in OIDC discovery document")
        logger.info("Discovered JWKS URI: %s", jwks_uri)
        return jwks_uri

    async def _fetch_jwks_async(self, jwks_uri: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(jwks_uri)
            resp.raise_for_status()
            return resp.json()

    async def _refresh(self, settings: Settings, kid: Optional[str]) -> None:
        """Fetch the JWKS once, if one is due. Never raises.

        Single flight: concurrent callers wait on the lock and then find the attempt already
        made. A failure keeps the keys held and holds further attempts for `_FETCH_SPACING`.
        """
        if _mono() < self._next_attempt:
            return
        async with self._lock:
            if _mono() < self._next_attempt:
                return
            self._next_attempt = _mono() + self._FETCH_SPACING
            try:
                if not self._jwks_uri:
                    self._jwks_uri = await self._discover_jwks_uri_async(settings.authentik_issuer)
                jwks = await self._fetch_jwks_async(self._jwks_uri)
            except Exception as exc:  # noqa: BLE001 - every failure means the same thing here
                self._last_error = type(exc).__name__
                logger.warning(
                    "JWKS refresh failed (%s); %s",
                    self._last_error,
                    "serving the keys already held" if self._jwks else "no keys are held yet",
                )
                return
            if not _usable(jwks):
                # important: a reply without keys must not replace a working set, or one bad
                # answer from the IdP would sign every user out.
                self._last_error = "EmptyKeySet"
                logger.warning("JWKS refresh returned no usable keys; keeping the keys held")
                return
            self._jwks = jwks
            self._last_fetched = _wall()
            self._last_error = None
            logger.info("Fetched %d keys from the JWKS endpoint", len(jwks["keys"]))
            if kid is not None:
                if self._find(kid) is None:
                    self._remember_absent_kid(kid)
                else:
                    self._forget_absent_kid(kid)

    def _refresh_in_background(self, settings: Settings) -> None:
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        if _mono() < self._next_attempt:
            return
        self._refresh_task = asyncio.get_running_loop().create_task(self._refresh(settings, None))

    async def warm_up(self, settings: Settings) -> None:
        """Fetch at startup so the first request does not wait for the IdP."""
        if settings.authentik_issuer:
            await self._refresh(settings, None)

    async def ensure_keys(self, token: str, settings: Settings) -> None:
        """Make sure the keys this token needs are held, as far as the IdP allows. Never raises.

        A known kid past the TTL is answered from the keys held while one refresh runs in the
        background, so no signed-in request waits on the IdP for a routine refresh.
        """
        kid = _unverified_kid(token)
        if kid is None:
            return
        age = self._age()
        if self._find(kid) is not None and age <= self._max_stale(settings):
            if age > settings.auth_jwks_cache_ttl:
                self._refresh_in_background(settings)
            return
        if self._find(kid) is None and self._kid_is_known_absent(kid):
            return
        await self._refresh(settings, kid)

    # ── answering ────────────────────────────────────────────────────────────────────────────

    def get_signing_key(self, token: str, settings: Settings) -> Any:
        """The key that signed `token`, from the keys held. Makes no network call."""
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
        if not self._jwks.get("keys") or self._age() > self._max_stale(settings):
            raise self._unavailable()
        key_data = self._find(kid)
        if key_data is not None:
            return jwt.algorithms.RSAAlgorithm.from_jwk(key_data)
        if self._kid_is_known_absent(kid):
            # A fresh fetch within the last minute did not contain it.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                # The kid is the caller's own input; leaving it out keeps the reply uniform.
                detail="Unable to find the token's signing key",
            )
        # The refresh that would tell a rotated key from a forged kid failed or is held back.
        raise self._unavailable()

    def snapshot(self, settings: Settings) -> Dict[str, Any]:
        """What /health reports: whether keys are held, how old, and whether that is by fallback."""
        held = bool(self._jwks.get("keys"))
        age = self._age() if held else None
        return {
            "keys_held": held,
            "key_count": len(self._jwks.get("keys", [])) if held else 0,
            "age_seconds": int(age) if age is not None else None,
            "serving_stale": bool(held and age is not None and age > settings.auth_jwks_cache_ttl
                                  and self._last_error is not None),
            "max_stale_seconds": self._max_stale(settings),
            "last_error": self._last_error,
        }

    # ── helpers ──────────────────────────────────────────────────────────────────────────────

    def _find(self, kid: str) -> Optional[Dict[str, Any]]:
        for key_data in self._jwks.get("keys", []):
            if isinstance(key_data, dict) and key_data.get("kid") == kid:
                return key_data
        return None

    def _age(self) -> float:
        return _wall() - self._last_fetched if self._last_fetched else math.inf

    @staticmethod
    def _max_stale(settings: Settings) -> int:
        configured = getattr(settings, "auth_jwks_max_stale", _DEFAULT_MAX_STALE)
        # Never shorter than the TTL, or keys would go stale before a routine refresh is due.
        return max(int(configured), int(settings.auth_jwks_cache_ttl))

    def _unavailable(self) -> HTTPException:
        wait = max(1, math.ceil(self._next_attempt - _mono())) if self._next_attempt else 1
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": IDENTITY_PROVIDER_UNAVAILABLE, "message": _UNAVAILABLE_MESSAGE},
            headers={"Retry-After": str(wait)},
        )

    def _kid_is_known_absent(self, kid: str) -> bool:
        recorded = self._absent_kids.get(kid)
        if recorded is None:
            return False
        if _wall() - recorded > self._ABSENT_KID_TTL:
            self._absent_kids.pop(kid, None)
            return False
        return True

    def _remember_absent_kid(self, kid: str) -> None:
        # Bounded, so tokens carrying random kids cannot grow the negative cache without limit.
        if len(self._absent_kids) >= self._ABSENT_KID_MAX:
            self._absent_kids.clear()
        self._absent_kids[kid] = _wall()

    def _forget_absent_kid(self, kid: str) -> None:
        self._absent_kids.pop(kid, None)
