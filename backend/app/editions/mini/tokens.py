"""Session tokens this server mints after a certificate sign-in.

HS256 with a key only this server holds, its own issuer and audience (never confused with an
identity provider's token), and bound to the certificate it was minted for: ``cnf.x5t#S256`` is
the certificate's SHA-256 thumbprint (RFC 8705), and every request must present that certificate.
A copied token is useless without the private key of the certificate it names.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any, Dict, Tuple

import jwt

ISSUER = "orcastra-mini"
AUDIENCE = "orcastra-mini-dashboard"
_LEEWAY_SECONDS = 30
_REQUIRED = ["exp", "iat", "iss", "aud", "sub", "email", "cnf"]


def thumbprint(der: bytes) -> str:
    """The RFC 8705 x5t#S256 thumbprint: base64url SHA-256 of the DER certificate, no padding."""
    return base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode("ascii")


def mint(*, uuid: str, email: str, name: str, role: str, cert_thumbprint: str,
         secret: str, ttl_seconds: int, now: float | None = None) -> Tuple[str, int]:
    issued = int(now if now is not None else time.time())
    expires = issued + ttl_seconds
    claims: Dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": uuid,
        "email": email,
        "name": name or email,
        "groups": [f"role_{role}"],
        "iat": issued,
        "nbf": issued - 5,
        "exp": expires,
        "jti": secrets.token_hex(12),
        "cnf": {"x5t#S256": cert_thumbprint},
    }
    return jwt.encode(claims, secret, algorithm="HS256"), expires


def decode(token: str, secret: str) -> Dict[str, Any]:
    """The verified claims, or jwt.PyJWTError."""
    claims = jwt.decode(
        token,
        key=secret,
        algorithms=["HS256"],
        audience=AUDIENCE,
        issuer=ISSUER,
        leeway=_LEEWAY_SECONDS,
        options={"require": _REQUIRED},
    )
    cnf = claims.get("cnf")
    if not isinstance(cnf, dict) or not isinstance(cnf.get("x5t#S256"), str):
        raise jwt.InvalidTokenError("the token is not bound to a certificate")
    groups = claims.get("groups")
    if not isinstance(groups, list) or len(groups) != 1 or groups[0] not in ("role_admin", "role_partner", "role_tenant"):
        raise jwt.InvalidTokenError("the token carries no single role")
    return claims


def role_of(claims: Dict[str, Any]) -> str:
    return claims["groups"][0][len("role_"):]
