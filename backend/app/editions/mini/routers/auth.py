"""Certificate sign-in: login, first-administrator enrollment, and the sign-in page's status read.

nginx sends /api/v1/auth/* straight here with the presented certificate and its proxy secret; the
frontend's NextAuth provider calls cert-login server-side the same way. These routes take no
bearer: the certificate (whose private key the TLS handshake proved) is the credential.
"""

from __future__ import annotations

import hmac
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

from app.core.request_security import get_client_ip
from app.core.config import get_settings
from app.editions.mini import identities, tokens
from app.editions.mini.audit import security_event
from app.editions.mini.client_cert import now_utc, presented_certificate
from app.editions.mini.settings import mini_settings
from app.middleware.security import WindowedRateLimiter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["Certificate sign-in"])

_limiter = WindowedRateLimiter(max_buckets=5_000)
LOGIN_LIMIT = (60, 60)       # per client address, per minute
BOOTSTRAP_LIMIT = (5, 600)   # per client address, per ten minutes
STATUS_LIMIT = (120, 60)


def _client(request: Request) -> str:
    settings = get_settings()
    return get_client_ip(request, settings.trusted_proxy_cidrs, True)


def _limit(request: Request, bucket: str, budget: tuple) -> None:
    key = f"{bucket}:{_client(request)}"
    if not _limiter.allow(key, budget[0], budget[1]):
        retry = _limiter.retry_after(key, budget[1])
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many attempts; try again shortly",
                            headers={"Retry-After": str(retry)})


def _require_json(request: Request) -> None:
    # A cross-site form cannot send application/json without a preflight, so a page elsewhere
    # cannot make a visitor's browser enroll or sign in its certificate here.
    if not (request.headers.get("content-type") or "").lower().startswith("application/json"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Send application/json")


class CertLoginResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    expires_at: int
    role: str
    email: str
    display_name: Optional[str]
    groups: list[str]
    certificate_expires_at: Optional[str]


class CertStatusResponse(BaseModel):
    certificate_presented: bool
    enrolled: bool
    active: bool
    fingerprint: Optional[str] = None
    common_name: Optional[str] = None
    expires_at: Optional[str] = None
    bootstrap_available: bool


@router.get("/cert-status", response_model=CertStatusResponse)
async def cert_status(request: Request) -> CertStatusResponse:
    """What the sign-in page needs to explain the next step. Says nothing about other identities."""
    _limit(request, "status", STATUS_LIMIT)
    ms = mini_settings()
    presented = presented_certificate(request, ms)
    snapshot = await identities.snapshot_by_certificate(presented.fingerprint) if presented else None
    usable = bool(snapshot and snapshot.active and snapshot.certificate_usable(presented.fingerprint, now_utc()))
    bootstrap = bool(ms.bootstrap_admin_token) and not await identities.active_admin_exists()
    return CertStatusResponse(
        certificate_presented=presented is not None,
        enrolled=snapshot is not None,
        active=usable,
        fingerprint=presented.fingerprint if presented else None,
        common_name=presented.common_name if presented else None,
        expires_at=presented.not_after.isoformat() if presented else None,
        bootstrap_available=bootstrap,
    )


@router.post("/cert-login", response_model=CertLoginResponse)
async def cert_login(request: Request) -> CertLoginResponse:
    _limit(request, "login", LOGIN_LIMIT)
    ms = mini_settings()
    presented = presented_certificate(request, ms)
    if presented is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="No client certificate was presented")
    now = now_utc()
    snapshot = await identities.snapshot_by_certificate(presented.fingerprint)
    if snapshot is None:
        # Trust on first use: the fingerprint is what an Administrator needs to enroll it.
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail={
            "code": "certificate_not_enrolled", "fingerprint": presented.fingerprint,
            "message": "This certificate is not enrolled. Send its fingerprint to an Administrator.",
        })
    refusal = None
    if not snapshot.active:
        refusal = "identity_inactive"
    elif not presented.valid_at(now) or not snapshot.certificate_usable(presented.fingerprint, now):
        refusal = "certificate_revoked_or_expired"
    if refusal is not None:
        await security_event("auth.cert_login", target_type="identity", target_id=snapshot.uuid, result="failure",
                             details={"reason": refusal, "fingerprint": presented.fingerprint})
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail={"code": refusal, "message": "This certificate may not sign in."})

    token, expires = tokens.mint(
        uuid=snapshot.uuid, email=snapshot.email, name=snapshot.display_name or snapshot.email,
        role=snapshot.role, cert_thumbprint=presented.thumbprint, secret=ms.local_jwt_secret,
        ttl_seconds=ms.local_jwt_ttl_seconds,
    )
    # important: fail closed. A sign-in that cannot be audited does not hand out a session.
    await security_event("auth.cert_login", target_type="identity", target_id=snapshot.uuid,
                         actor_user_id=snapshot.email,
                         details={"role": snapshot.role, "fingerprint": presented.fingerprint})
    await identities.record_login(snapshot, presented.fingerprint)
    cert_state = snapshot.certificates.get(presented.fingerprint)
    return CertLoginResponse(
        access_token=token, expires_in=expires - int(now.timestamp()), expires_at=expires,
        role=snapshot.role, email=snapshot.email, display_name=snapshot.display_name,
        groups=[f"role_{snapshot.role}"],
        certificate_expires_at=cert_state.not_after.isoformat() if cert_state and cert_state.not_after else None,
    )


class BootstrapRequest(BaseModel):
    bootstrap_token: str = Field(min_length=16, max_length=256)
    email: Optional[EmailStr] = None
    display_name: Optional[str] = Field(default=None, max_length=255)


@router.post("/cert-bootstrap", response_model=CertLoginResponse)
async def cert_bootstrap(request: Request, body: BootstrapRequest) -> CertLoginResponse:
    """Enroll the presented certificate as the first Administrator while none exists."""
    _limit(request, "bootstrap", BOOTSTRAP_LIMIT)
    _require_json(request)
    ms = mini_settings()
    expected = ms.bootstrap_admin_token
    if not expected or not hmac.compare_digest(body.bootstrap_token.encode(), expected.encode()):
        await security_event("auth.cert_bootstrap", target_type="identity", target_id="-", result="failure",
                             details={"reason": "token_refused"})
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="The bootstrap token is not valid")
    presented = presented_certificate(request, ms)
    if presented is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="No client certificate was presented")
    if await identities.active_admin_exists():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="An Administrator already exists; ask them to issue a certificate")
    email = (str(body.email) if body.email else identities.default_email(presented.common_name, ms.client_cert_cn_suffix))
    from app.core.database import async_session_maker

    async with async_session_maker() as session:
        try:
            snapshot = await identities.create_identity(
                email=email, common_name=presented.common_name or "admin", display_name=body.display_name,
                role="admin", created_by="bootstrap", issued_by=None, owner_org_id=None,
                fingerprint=presented.fingerprint, serial=presented.serial, not_after=presented.not_after,
                cert_common_name=presented.common_name or "admin", session=session,
            )
        except identities.IdentityConflict:
            raise HTTPException(status.HTTP_409_CONFLICT, detail="This certificate or email is already enrolled")
        await security_event("auth.cert_bootstrap", target_type="identity", target_id=snapshot.uuid,
                             actor_user_id=email, severity="critical",
                             details={"fingerprint": presented.fingerprint, "email": email}, session=session)
        try:
            await session.commit()
        except Exception:
            # The single-bootstrap-admin index makes a race single-winner.
            raise HTTPException(status.HTTP_409_CONFLICT, detail="An Administrator was enrolled at the same moment")
    return await cert_login(request)
