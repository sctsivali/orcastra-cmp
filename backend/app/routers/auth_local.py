"""Client-certificate login + first-admin bootstrap (AUTH_MODE=client-cert).

These endpoints are the login surface for the mini deployment. They are unauthenticated
by design (they establish identity), but every path requires the request to arrive through
the trusted reverse proxy carrying a verified client certificate. The role is read from the
local identity store, never from the certificate, and a short-lived HS256 session token is
minted so the rest of the app keeps using bearer auth unchanged.
"""

import hmac
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import mint_local_jwt
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.rbac_cache import get_rbac_cache
from app.models.db_models import LocalIdentity
from app.services.activity_logger import get_activity_logger
from app.services.client_cert_auth import extract_client_cert

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(prefix="/auth", tags=["Authentication (client-cert)"])


class CertLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str
    display_name: str
    fingerprint: str
    groups: list[str]


class BootstrapRequest(BaseModel):
    bootstrap_token: str = Field(..., min_length=1)


def _require_cert_mode(settings: Settings) -> None:
    if settings.auth_mode != "client-cert":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Client-cert authentication is not enabled",
        )


@router.post("/cert-login", response_model=CertLoginResponse)
async def cert_login(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CertLoginResponse:
    """Exchange a verified client certificate for a session token."""
    _require_cert_mode(settings)

    identity = extract_client_cert(request, settings)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A trusted client certificate is required",
        )

    row = (
        await db.execute(
            select(LocalIdentity).where(LocalIdentity.fingerprint == identity.fingerprint)
        )
    ).scalar_one_or_none()

    if row is None:
        # TOFU: unknown cert. Surface the fingerprint so an admin can enroll it.
        await activity_logger.log_security_event(
            action="auth.cert_login", target_type="identity",
            target_id=identity.fingerprint, result="denied",
            details={"reason": "not_enrolled", "common_name": identity.common_name},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Certificate not enrolled (fingerprint {identity.fingerprint})",
        )

    if row.status != "active":
        await activity_logger.log_security_event(
            action="auth.cert_login", target_type="identity",
            target_id=identity.fingerprint, result="denied",
            details={"reason": f"status_{row.status}"},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This identity is not active",
        )

    if row.not_after and row.not_after < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Certificate has expired",
        )

    display = row.display_name or row.common_name
    token, ttl = mint_local_jwt(row.fingerprint, display, row.role, settings)

    # A fresh successful login clears any stale revoke flag (re-enabled identity).
    try:
        get_rbac_cache().clear_identity_revocation(row.fingerprint)
    except Exception:
        pass

    await activity_logger.log_security_event(
        action="auth.cert_login", target_type="identity",
        target_id=row.fingerprint, result="success",
        details={"role": row.role, "common_name": row.common_name},
    )
    return CertLoginResponse(
        access_token=token, expires_in=ttl, role=row.role, display_name=display,
        fingerprint=row.fingerprint, groups=[f"role_{row.role}"],
    )


@router.post("/cert-bootstrap", response_model=CertLoginResponse)
async def cert_bootstrap(
    payload: BootstrapRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CertLoginResponse:
    """Enroll the FIRST admin certificate (trust-on-first-use).

    Refused once an active admin exists, refused when the bootstrap token is unset, and
    made single-winner by a unique partial index so two racing requests cannot both win.
    """
    _require_cert_mode(settings)

    if not settings.bootstrap_admin_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bootstrap is disabled (BOOTSTRAP_ADMIN_TOKEN is unset)",
        )
    if not hmac.compare_digest(payload.bootstrap_token, settings.bootstrap_admin_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid bootstrap token",
        )

    identity = extract_client_cert(request, settings)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A trusted client certificate is required",
        )

    admin_count = (
        await db.execute(
            select(func.count())
            .select_from(LocalIdentity)
            .where(LocalIdentity.role == "admin", LocalIdentity.status == "active")
        )
    ).scalar_one()
    if admin_count and admin_count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An admin already exists; bootstrap is closed",
        )

    dup = (
        await db.execute(
            select(LocalIdentity).where(LocalIdentity.fingerprint == identity.fingerprint)
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This certificate is already enrolled",
        )

    row = LocalIdentity(
        fingerprint=identity.fingerprint,
        common_name=identity.common_name or "admin",
        display_name=identity.common_name or "Administrator",
        role="admin",
        status="active",
        serial=identity.serial,
        not_after=identity.not_after,
        created_by="bootstrap",
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # Lost the race against another bootstrap (unique partial index fired).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An admin already exists; bootstrap is closed",
        )

    token, ttl = mint_local_jwt(row.fingerprint, row.display_name, row.role, settings)
    await activity_logger.log_security_event(
        action="auth.cert_bootstrap", target_type="identity",
        target_id=row.fingerprint, result="success",
        details={"common_name": row.common_name},
    )
    logger.info("Bootstrap admin enrolled: fingerprint=%s...", row.fingerprint[:16])
    return CertLoginResponse(
        access_token=token, expires_in=ttl, role=row.role,
        display_name=row.display_name or row.common_name,
        fingerprint=row.fingerprint, groups=[f"role_{row.role}"],
    )
