"""Admin management of local client-cert identities (AUTH_MODE=client-cert).

Lets an admin issue a client certificate (via Vault PKI) for a new user, list enrolled
identities, change a role, and revoke. This replaces Authentik's user/group directory in
the mini deployment. All endpoints are admin-only.
"""

import asyncio
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.dependencies import AuthenticatedUser, check_read_only_mode, require_admin
from app.core.rbac_cache import get_rbac_cache
from app.core.vault_client import get_vault_client
from app.models.db_models import LocalIdentity
from app.services.activity_logger import get_activity_logger
from app.services.pkcs12_util import cert_fingerprint_sha256, create_pfx

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

VALID_ROLES = {"admin", "partner", "tenant"}
VALID_STATUSES = {"active", "revoked"}

router = APIRouter(
    prefix="/local-identities",
    tags=["Local Identities (client-cert)"],
    dependencies=[Depends(require_admin())],
)


class LocalIdentityOut(BaseModel):
    fingerprint: str
    common_name: str
    display_name: Optional[str] = None
    role: str
    status: str
    serial: Optional[str] = None
    not_after: Optional[datetime] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None


class IdentityIssueRequest(BaseModel):
    common_name: str = Field(..., min_length=1, max_length=200, description="Label / username for the identity")
    display_name: Optional[str] = Field(None, max_length=200)
    role: str = Field("tenant")
    ttl_days: Optional[int] = Field(None, ge=1, le=3650)


class IdentityUpdateRequest(BaseModel):
    role: Optional[str] = None
    status: Optional[str] = None


def _slug(value: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9-]+", "-", value.strip().lower()).strip("-")
    return s or "user"


def _to_out(row: LocalIdentity) -> LocalIdentityOut:
    return LocalIdentityOut(
        fingerprint=row.fingerprint, common_name=row.common_name,
        display_name=row.display_name, role=row.role, status=row.status,
        serial=row.serial, not_after=row.not_after, created_by=row.created_by,
        created_at=row.created_at,
    )


@router.get("", response_model=list[LocalIdentityOut])
async def list_identities(
    db: AsyncSession = Depends(get_db),
    _admin: AuthenticatedUser = Depends(require_admin()),
) -> list[LocalIdentityOut]:
    rows = (await db.execute(select(LocalIdentity).order_by(LocalIdentity.created_at.desc()))).scalars().all()
    return [_to_out(r) for r in rows]


@router.post("/issue")
async def issue_identity(
    payload: IdentityIssueRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    admin: AuthenticatedUser = Depends(require_admin()),
    _ro: None = Depends(check_read_only_mode),
):
    """Issue a client cert via Vault PKI and return a one-time, password-protected .p12.

    The private key never persists - it is generated in Vault, bundled, streamed once, and
    dropped. The .p12 password is returned in the X-P12-Password header for out-of-band
    delivery to the user.
    """
    if payload.role not in VALID_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"role must be one of {sorted(VALID_ROLES)}")
    if not settings.vault_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Vault is required to issue certificates")

    # Build a NON-ROUTABLE internal CN (FQDN required by Vault PKI) so the cert subject
    # never looks like a visitable public domain. Always derive from the username slug.
    cn = f"{_slug(payload.common_name)}.{settings.client_cert_cn_suffix}"
    ttl_hours = (payload.ttl_days or settings.client_cert_ttl_days) * 24

    try:
        vc = get_vault_client(settings.vault_addr, settings.vault_token)
        issued = await asyncio.to_thread(
            vc.generate_lxd_certificate, cn, f"{ttl_hours}h", settings.vault_pki_role
        )
    except Exception as e:
        logger.error("Certificate issuance failed for %s: %s", cn, e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Vault certificate issuance failed: {e}")

    cert_pem = issued["cert"].encode("utf-8")
    key_pem = issued["key"].encode("utf-8")
    ca_pem = (issued.get("ca") or "").encode("utf-8") or None
    fingerprint = cert_fingerprint_sha256(cert_pem)

    password = secrets.token_urlsafe(18)
    try:
        p12 = create_pfx(cert_pem, key_pem, password=password, friendly_name=cn.encode(), ca_pem=ca_pem)
    except Exception as e:
        logger.error("PKCS#12 bundling failed for %s: %s", cn, e)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to bundle certificate")

    row = LocalIdentity(
        fingerprint=fingerprint,
        common_name=cn,
        display_name=payload.display_name or payload.common_name,
        role=payload.role,
        status="active",
        serial=None,
        not_after=datetime.now(timezone.utc),  # placeholder; refined below from cert
        created_by=admin.user_id,
    )
    # derive not_after from the issued cert for accuracy
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        parsed = x509.load_pem_x509_certificate(cert_pem, default_backend())
        row.not_after = getattr(parsed, "not_valid_after_utc", None) or parsed.not_valid_after.replace(tzinfo=timezone.utc)
        row.serial = format(parsed.serial_number, "x")
    except Exception:
        pass

    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "An identity with this certificate already exists")

    await activity_logger.log_security_event(
        action="identity.issue", target_type="identity", target_id=fingerprint,
        result="success", details={"common_name": cn, "role": payload.role, "issued_by": admin.user_id},
    )

    filename = f"{_slug(payload.common_name)}.p12"
    return StreamingResponse(
        iter([p12]),
        media_type="application/x-pkcs12",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-P12-Password": password,
            "Cache-Control": "no-store",
        },
    )


@router.patch("/{fingerprint}", response_model=LocalIdentityOut)
async def update_identity(
    fingerprint: str,
    payload: IdentityUpdateRequest,
    db: AsyncSession = Depends(get_db),
    admin: AuthenticatedUser = Depends(require_admin()),
    _ro: None = Depends(check_read_only_mode),
) -> LocalIdentityOut:
    """Change an identity's role and/or status. Revoking or changing role forces re-auth."""
    if payload.role is not None and payload.role not in VALID_ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"role must be one of {sorted(VALID_ROLES)}")
    if payload.status is not None and payload.status not in VALID_STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"status must be one of {sorted(VALID_STATUSES)}")

    row = (await db.execute(select(LocalIdentity).where(LocalIdentity.fingerprint == fingerprint))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Identity not found")

    # Guard: never strand the deployment without an admin.
    if row.role == "admin" and (payload.role and payload.role != "admin" or payload.status == "revoked"):
        remaining = (await db.execute(
            select(LocalIdentity).where(
                LocalIdentity.role == "admin", LocalIdentity.status == "active",
                LocalIdentity.fingerprint != fingerprint,
            )
        )).scalars().first()
        if remaining is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Cannot demote/revoke the last active admin")

    role_changed = payload.role is not None and payload.role != row.role
    status_changed = payload.status is not None and payload.status != row.status
    if payload.role is not None:
        row.role = payload.role
    if payload.status is not None:
        row.status = payload.status
    await db.commit()

    cache = get_rbac_cache()
    if status_changed and row.status == "active":
        cache.clear_identity_revocation(fingerprint)
    elif role_changed or (status_changed and row.status == "revoked"):
        # Force existing sessions to re-authenticate with the new role/state.
        cache.revoke_identity_sessions(fingerprint)

    await activity_logger.log_security_event(
        action="identity.update", target_type="identity", target_id=fingerprint,
        result="success",
        details={"role": row.role, "status": row.status, "updated_by": admin.user_id},
    )
    return _to_out(row)


@router.delete("/{fingerprint}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_identity(
    fingerprint: str,
    db: AsyncSession = Depends(get_db),
    admin: AuthenticatedUser = Depends(require_admin()),
    _ro: None = Depends(check_read_only_mode),
):
    """Hard-delete an identity (prefer revoke for an audit trail)."""
    row = (await db.execute(select(LocalIdentity).where(LocalIdentity.fingerprint == fingerprint))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Identity not found")
    if row.role == "admin" and row.status == "active":
        remaining = (await db.execute(
            select(LocalIdentity).where(
                LocalIdentity.role == "admin", LocalIdentity.status == "active",
                LocalIdentity.fingerprint != fingerprint,
            )
        )).scalars().first()
        if remaining is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Cannot delete the last active admin")

    await db.delete(row)
    await db.commit()
    get_rbac_cache().revoke_identity_sessions(fingerprint)
    await activity_logger.log_security_event(
        action="identity.delete", target_type="identity", target_id=fingerprint,
        result="success", details={"deleted_by": admin.user_id},
    )
    return None
