"""Verifies a Mini session token on every request and every socket it opens.

A token is accepted only when all of these hold, checked in this order:
- its signature, issuer, audience and expiry (tokens.decode);
- the request came through nginx and presents a certificate whose thumbprint is the token's
  ``cnf`` (a copied token without the certificate's private key is refused);
- the identity is active, still holds the token's role, and the token is not older than the
  identity's ``tokens_valid_after`` (a revoke, role change or delete refuses every older token);
- that certificate is active on the identity and unexpired.
Any read that cannot run refuses: a database error is a 503, never a pass.
"""

from __future__ import annotations

import base64
import logging
from typing import Optional

import jwt
from fastapi import HTTPException, status
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket

from app.core.auth import AuthenticatedUser
from app.editions.mini import identities, tokens
from app.editions.mini.client_cert import now_utc, presented_certificate
from app.editions.mini.settings import mini_settings

logger = logging.getLogger(__name__)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def session_refusal(snapshot: Optional[identities.IdentitySnapshot], claims: dict, presented_fingerprint: Optional[str]) -> Optional[str]:
    """Why this session may not continue, or None. Shared by requests, sockets and the re-check."""
    if snapshot is None:
        return "identity_unknown"
    if not snapshot.active:
        return "identity_inactive"
    if tokens.role_of(claims) != snapshot.role:
        return "role_changed"
    if int(claims.get("iat", 0)) < snapshot.token_floor():
        return "session_superseded"
    thumb = claims["cnf"]["x5t#S256"]
    # The re-check of an open socket has no presented certificate: use the one the token names.
    fingerprint = presented_fingerprint or _fingerprint_for_thumbprint(snapshot, thumb)
    if fingerprint is None or not snapshot.certificate_usable(fingerprint, now_utc()):
        return "certificate_revoked"
    return None


def _fingerprint_for_thumbprint(snapshot: identities.IdentitySnapshot, thumb: str) -> Optional[str]:
    # The thumbprint is base64url of the same SHA-256 digest the fingerprint holds in hex.
    try:
        digest = base64.urlsafe_b64decode(thumb + "=" * (-len(thumb) % 4)).hex()
    except (ValueError, TypeError):
        return None
    return digest if digest in snapshot.certs else None


class CertificateTokenVerifier:
    def configured(self, settings) -> bool:
        ms = mini_settings()
        return bool(ms.local_jwt_secret and ms.auth_proxy_secret)

    async def verify(self, token: str, settings, connection: HTTPConnection) -> AuthenticatedUser:
        ms = mini_settings()
        try:
            claims = tokens.decode(token, ms.local_jwt_secret)
        except jwt.ExpiredSignatureError:
            raise _unauthorized("Token has expired")
        except jwt.PyJWTError:
            raise _unauthorized("Invalid token")
        presented = presented_certificate(connection, ms)
        if presented is None:
            raise _unauthorized("A client certificate is required")
        if presented.thumbprint != claims["cnf"]["x5t#S256"]:
            raise _unauthorized("The session belongs to another certificate")
        try:
            snapshot = await identities.snapshot_by_uuid(claims["sub"])
        except Exception as exc:  # noqa: BLE001 - an unreadable store refuses, it never admits
            logger.error("Identity store unreadable while verifying a session: %s", type(exc).__name__)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Identity store unavailable")
        refusal = session_refusal(snapshot, claims, presented.fingerprint)
        if refusal is not None:
            raise _unauthorized(f"Session refused: {refusal}")
        return user_from_claims(claims, snapshot)

    async def admit_socket(self, user: AuthenticatedUser, websocket: WebSocket) -> Optional[str]:
        claims = user.raw_claims or {}
        if not isinstance(claims.get("cnf"), dict) or claims.get("iss") != tokens.ISSUER:
            return "ticket_invalid"
        presented = presented_certificate(websocket, mini_settings())
        if presented is None or presented.thumbprint != claims["cnf"].get("x5t#S256"):
            return "certificate_mismatch"
        try:
            snapshot = await identities.snapshot_by_uuid(claims.get("sub", ""))
        except Exception:  # noqa: BLE001
            return "identity_unavailable"
        return session_refusal(snapshot, claims, presented.fingerprint)


def user_from_claims(claims: dict, snapshot: identities.IdentitySnapshot) -> AuthenticatedUser:
    role = f"role_{snapshot.role}"
    exp = claims.get("exp")
    return AuthenticatedUser(
        user_id=snapshot.email,
        email=snapshot.email,
        name=snapshot.display_name or snapshot.email,
        groups=[role],
        roles=[role],
        raw_claims=claims,
        session_exp=float(exp) if isinstance(exp, (int, float)) else None,
    )
