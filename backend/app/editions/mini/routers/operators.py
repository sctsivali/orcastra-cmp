"""Operators: the people who sign in to Mini, their roles and their certificates.

Administrators manage every operator. A Partner issues and manages Tenant operators for the
organizations it runs (creator or partner member, as organization rosters already decide), and
nothing else: it cannot change a role, touch a Partner or Administrator, or reach an operator
another organization owns. Every change is audited in the same transaction it commits in.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser
from app.core.database import get_db
from app.core.dependencies import check_read_only_mode, require_partner, require_tenant
from app.core.rbac import Role, get_highest_role, resolve_roles
from app.editions.mini import identities
from app.editions.mini.audit import security_event
from app.editions.mini.client_cert import now_utc, presented_certificate
from app.editions.mini.issuing import bundle_response, issue_bundle
from app.editions.mini.settings import mini_settings
from app.models.db_models import Organization, OrganizationMember
from app.models.mini_identity_models import LocalIdentity, LocalIdentityCertificate
from app.services.cluster_parties import RUNNING_ROLES
from app.services.organization_service import OrganizationService

router = APIRouter(prefix="/operators", tags=["Operators"])
me_router = APIRouter(prefix="/me/certificate", tags=["Operators"])


class CertificateView(BaseModel):
    fingerprint: str
    common_name: str
    status: str
    not_after: Optional[datetime]
    created_at: datetime
    last_used_at: Optional[datetime]
    renewed_from_fingerprint: Optional[str] = None


class OperatorView(BaseModel):
    uuid: str
    email: str
    display_name: Optional[str]
    role: str
    status: str
    owner_org_id: Optional[int]
    owner_org_name: Optional[str] = None
    issued_by: Optional[str]
    last_login_at: Optional[datetime]
    created_at: datetime
    expires_at: Optional[datetime]
    expiring: bool
    certificates: List[CertificateView]
    manageable: bool


class IssueRequest(BaseModel):
    email: EmailStr
    display_name: Optional[str] = Field(default=None, max_length=255)
    role: str = Field(pattern="^(admin|partner|tenant)$")
    organization_id: Optional[int] = None
    ttl_days: Optional[int] = Field(default=None, ge=1, le=3650)


class UpdateRequest(BaseModel):
    role: Optional[str] = Field(default=None, pattern="^(admin|partner|tenant)$")
    status: Optional[str] = Field(default=None, pattern="^(active|revoked)$")
    display_name: Optional[str] = Field(default=None, max_length=255)


def _role(user: AuthenticatedUser) -> Role:
    return get_highest_role(resolve_roles(user))


async def _orgs_run_by(db: AsyncSession, email: str) -> set[int]:
    created = select(Organization.id).where(Organization.created_by == email)
    member = select(OrganizationMember.organization_id).where(
        OrganizationMember.user_email == email, OrganizationMember.role.in_(RUNNING_ROLES)
    )
    rows = (await db.execute(created.union(member))).scalars().all()
    return set(rows)


async def _may_manage(db: AsyncSession, user: AuthenticatedUser, identity: LocalIdentity) -> bool:
    role = _role(user)
    if role == Role.ADMIN:
        return True
    if role != Role.PARTNER or identity.role != "tenant" or identity.owner_org_id is None:
        return False
    return identity.owner_org_id in await _orgs_run_by(db, user.email)


async def _get(db: AsyncSession, uuid: str) -> LocalIdentity:
    identity = (await db.execute(select(LocalIdentity).where(LocalIdentity.uuid == uuid))).scalar_one_or_none()
    if identity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Operator not found")
    return identity


async def _usable_admins(db: AsyncSession, excluding_id: Optional[int] = None) -> int:
    now = now_utc()
    query = select(func.count(func.distinct(LocalIdentity.id))).select_from(LocalIdentity).join(
        LocalIdentityCertificate, LocalIdentityCertificate.identity_id == LocalIdentity.id
    ).where(
        LocalIdentity.role == "admin", LocalIdentity.status == "active", LocalIdentityCertificate.status == "active",
        or_(LocalIdentityCertificate.not_after.is_(None), LocalIdentityCertificate.not_after > now),
    )
    if excluding_id is not None:
        query = query.where(LocalIdentity.id != excluding_id)
    return (await db.execute(query)).scalar_one()


async def _view(db: AsyncSession, identity: LocalIdentity, user: AuthenticatedUser, org_names: dict) -> OperatorView:
    certs = (await db.execute(select(LocalIdentityCertificate).where(
        LocalIdentityCertificate.identity_id == identity.id).order_by(LocalIdentityCertificate.id.desc())
    )).scalars().all()
    by_id = {c.id: c.fingerprint for c in certs}
    active = [c for c in certs if c.status == "active" and c.not_after]
    expires = max((c.not_after for c in active), default=None)
    warn = timedelta(days=mini_settings().cert_expiry_warning_days)
    return OperatorView(
        uuid=identity.uuid, email=identity.email, display_name=identity.display_name, role=identity.role,
        status=identity.status, owner_org_id=identity.owner_org_id, owner_org_name=org_names.get(identity.owner_org_id),
        issued_by=identity.issued_by, last_login_at=identity.last_login_at, created_at=identity.created_at,
        expires_at=expires, expiring=bool(expires and expires - now_utc() < warn),
        certificates=[CertificateView(
            fingerprint=c.fingerprint, common_name=c.common_name, status=c.status, not_after=c.not_after,
            created_at=c.created_at, last_used_at=c.last_used_at,
            renewed_from_fingerprint=by_id.get(c.renewed_from_id) if c.renewed_from_id else None,
        ) for c in certs],
        manageable=await _may_manage(db, user, identity),
    )


@router.get("", response_model=List[OperatorView])
async def list_operators(
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
    state: Optional[str] = Query(default=None, alias="status", pattern="^(active|revoked|deleted)$"),
    expiring: bool = False,
):
    query = select(LocalIdentity).order_by(LocalIdentity.email)
    if _role(user) != Role.ADMIN:
        orgs = await _orgs_run_by(db, user.email)
        if not orgs:
            return []
        query = query.where(LocalIdentity.role == "tenant", LocalIdentity.owner_org_id.in_(orgs))
    if state:
        query = query.where(LocalIdentity.status == state)
    found = (await db.execute(query)).scalars().all()
    org_ids = {i.owner_org_id for i in found if i.owner_org_id}
    org_names = dict((await db.execute(select(Organization.id, Organization.name).where(Organization.id.in_(org_ids)))).all()) if org_ids else {}
    views = [await _view(db, identity, user, org_names) for identity in found]
    return [v for v in views if v.expiring] if expiring else views


@router.post("", dependencies=[Depends(check_read_only_mode)])
async def issue_operator(body: IssueRequest, user: AuthenticatedUser = Depends(require_partner()),
                         db: AsyncSession = Depends(get_db)):
    """Create an operator and hand its first certificate back once, as a password-protected .p12."""
    role = _role(user)
    if role != Role.ADMIN:
        if body.role != "tenant":
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="A Partner issues Tenant certificates only")
        if body.organization_id is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Choose the organization the Tenant belongs to")
        if body.organization_id not in await _orgs_run_by(db, user.email):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="You do not run that organization")
    if body.organization_id is not None and await db.get(Organization, body.organization_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Organization not found")
    email = str(body.email).lower()
    taken = (await db.execute(select(LocalIdentity.status).where(func.lower(LocalIdentity.email) == email))).scalar_one_or_none()
    if taken is not None:
        detail = ("That email belonged to a deleted operator; restore it instead of reusing the address"
                  if taken == "deleted" else "An operator with that email already exists")
        raise HTTPException(status.HTTP_409_CONFLICT, detail=detail)

    bundle = await issue_bundle(email, ttl_days=body.ttl_days)
    try:
        snapshot = await identities.create_identity(
            email=email, common_name=bundle.certificate.common_name, display_name=body.display_name,
            role=body.role, created_by=user.email, issued_by=user.email,
            # A Tenant belongs to the organization it was issued for, whose Partners then manage it.
            owner_org_id=body.organization_id if body.role == "tenant" else None,
            fingerprint=bundle.certificate.fingerprint, serial=bundle.certificate.serial,
            not_after=bundle.certificate.not_after, cert_common_name=bundle.certificate.common_name, session=db,
        )
    except identities.IdentityConflict:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="An operator with that email already exists")
    if body.organization_id is not None:
        try:
            await OrganizationService(db).add_member(org_id=body.organization_id, user_email=email,
                                                    role="member", invited_by=user.email)
        except ValueError:
            pass  # already a member under this address
    await security_event("identity.issue", target_type="identity", target_id=snapshot.uuid, severity="high",
                         details={"email": email, "role": body.role, "organization_id": body.organization_id,
                                  "fingerprint": bundle.certificate.fingerprint}, session=db)
    await db.commit()
    return bundle_response(bundle, operator_uuid=snapshot.uuid)


@router.patch("/{uuid}", response_model=OperatorView, dependencies=[Depends(check_read_only_mode)])
async def update_operator(uuid: str, body: UpdateRequest, user: AuthenticatedUser = Depends(require_partner()),
                          db: AsyncSession = Depends(get_db)):
    identity = await _get(db, uuid)
    if not await _may_manage(db, user, identity):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Operator not found")
    if identity.status == "deleted":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Restore the operator before changing it")
    admin = _role(user) == Role.ADMIN
    if body.role is not None and body.role != identity.role and not admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Only an Administrator changes a role")
    losing_admin = identity.role == "admin" and ((body.role and body.role != "admin") or body.status == "revoked")
    if losing_admin and await _usable_admins(db, excluding_id=identity.id) == 0:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="This is the last Administrator who can sign in")
    changes = {}
    if body.display_name is not None and body.display_name != identity.display_name:
        changes["display_name"] = body.display_name
    if body.role is not None and body.role != identity.role:
        changes["role"] = body.role
    if body.status is not None and body.status != identity.status:
        changes["status"] = body.status
    if changes:
        if "role" in changes or changes.get("status") == "revoked":
            # important: every session minted before this moment is refused from the next request.
            identity.tokens_valid_after = now_utc()
        for key, value in changes.items():
            setattr(identity, key, value)
        action = "identity.revoke" if changes.get("status") == "revoked" else "identity.update"
        await security_event(action, target_type="identity", target_id=identity.uuid, severity="high",
                             details={"email": identity.email, **changes}, session=db)
        await db.commit()
        identities.forget(identity.uuid)
    return await _view(db, identity, user, {})


@router.post("/{uuid}/certificates", dependencies=[Depends(check_read_only_mode)])
async def reissue_certificate(uuid: str, user: AuthenticatedUser = Depends(require_partner()),
                              db: AsyncSession = Depends(get_db)):
    """Another certificate for the same operator (a new device, a lost one replaced)."""
    identity = await _get(db, uuid)
    if not await _may_manage(db, user, identity):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Operator not found")
    if identity.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Only an active operator gets a certificate")
    bundle = await issue_bundle(identity.email)
    await identities.add_certificate(identity.id, fingerprint=bundle.certificate.fingerprint,
                                     serial=bundle.certificate.serial, not_after=bundle.certificate.not_after,
                                     cert_common_name=bundle.certificate.common_name, renewed_from_id=None, session=db)
    await security_event("identity.certificate_issue", target_type="identity", target_id=identity.uuid, severity="high",
                         details={"email": identity.email, "fingerprint": bundle.certificate.fingerprint}, session=db)
    await db.commit()
    identities.forget(identity.uuid)
    return bundle_response(bundle, operator_uuid=identity.uuid)


@router.delete("/{uuid}/certificates/{fingerprint}", response_model=OperatorView, dependencies=[Depends(check_read_only_mode)])
async def revoke_certificate(uuid: str, fingerprint: str, user: AuthenticatedUser = Depends(require_partner()),
                             db: AsyncSession = Depends(get_db)):
    identity = await _get(db, uuid)
    if not await _may_manage(db, user, identity):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Operator not found")
    cert = (await db.execute(select(LocalIdentityCertificate).where(
        LocalIdentityCertificate.identity_id == identity.id, LocalIdentityCertificate.fingerprint == fingerprint.lower()
    ))).scalar_one_or_none()
    if cert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Certificate not found")
    if cert.status == "active":
        if identity.role == "admin" and await _usable_admins(db, excluding_id=None) <= 1:
            others = (await db.execute(select(func.count()).select_from(LocalIdentityCertificate).where(
                LocalIdentityCertificate.identity_id == identity.id, LocalIdentityCertificate.status == "active",
                LocalIdentityCertificate.id != cert.id,
                or_(LocalIdentityCertificate.not_after.is_(None), LocalIdentityCertificate.not_after > now_utc()),
            ))).scalar_one()
            if others == 0:
                raise HTTPException(status.HTTP_409_CONFLICT, detail="This is the last certificate an Administrator can sign in with")
        cert.status, cert.revoked_at = "revoked", now_utc()
        await security_event("identity.certificate_revoke", target_type="identity", target_id=identity.uuid,
                             severity="high", details={"email": identity.email, "fingerprint": cert.fingerprint}, session=db)
        await db.commit()
        identities.forget(identity.uuid)
    return await _view(db, identity, user, {})


@router.delete("/{uuid}", response_model=OperatorView, dependencies=[Depends(check_read_only_mode)])
async def delete_operator(uuid: str, user: AuthenticatedUser = Depends(require_partner()),
                          db: AsyncSession = Depends(get_db)):
    """Tombstone: every certificate revoked, every session refused, the email kept reserved."""
    identity = await _get(db, uuid)
    if not await _may_manage(db, user, identity):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Operator not found")
    if identity.email == (user.email or "").lower():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="You cannot delete yourself")
    if identity.role == "admin" and await _usable_admins(db, excluding_id=identity.id) == 0:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="This is the last Administrator who can sign in")
    if identity.status != "deleted":
        now = now_utc()
        identity.status, identity.tokens_valid_after = "deleted", now
        await db.execute(update(LocalIdentityCertificate).where(
            LocalIdentityCertificate.identity_id == identity.id, LocalIdentityCertificate.status == "active"
        ).values(status="revoked", revoked_at=now))
        await security_event("identity.delete", target_type="identity", target_id=identity.uuid, severity="high",
                             details={"email": identity.email}, session=db)
        await db.commit()
        identities.forget(identity.uuid)
    return await _view(db, identity, user, {})


@router.post("/{uuid}/restore", response_model=OperatorView, dependencies=[Depends(check_read_only_mode)])
async def restore_operator(uuid: str, user: AuthenticatedUser = Depends(require_partner()),
                           db: AsyncSession = Depends(get_db)):
    """Bring a deleted or revoked operator back. Its old certificates stay revoked: issue a new one."""
    identity = await _get(db, uuid)
    if not await _may_manage(db, user, identity):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Operator not found")
    if identity.status != "active":
        identity.status = "active"
        await security_event("identity.restore", target_type="identity", target_id=identity.uuid, severity="high",
                             details={"email": identity.email}, session=db)
        await db.commit()
        identities.forget(identity.uuid)
    return await _view(db, identity, user, {})


# ---------------------------------------------------------------------------
# The caller's own certificate
# ---------------------------------------------------------------------------


class MyCertificate(BaseModel):
    email: str
    display_name: Optional[str]
    role: str
    fingerprint: Optional[str]
    common_name: Optional[str]
    not_after: Optional[datetime]
    days_left: Optional[int]
    expiring: bool
    renew_available: bool
    renew_opens_at: Optional[datetime]
    other_active_certificates: int


@me_router.get("", response_model=MyCertificate)
async def my_certificate(request: Request, user: AuthenticatedUser = Depends(require_tenant())):
    ms = mini_settings()
    presented = presented_certificate(request, ms)
    snapshot = await identities.snapshot_by_uuid((user.raw_claims or {}).get("sub", ""))
    if snapshot is None or presented is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No certificate for this session")
    cert = snapshot.certs.get(presented.fingerprint)
    not_after = cert.not_after if cert and cert.not_after else presented.not_after
    now = now_utc()
    opens = not_after - timedelta(days=ms.cert_renew_window_days)
    renewed_already = any(c.renewed_from_id == (cert.id if cert else None) for c in snapshot.certs.values())
    return MyCertificate(
        email=snapshot.email, display_name=snapshot.display_name, role=snapshot.role,
        fingerprint=presented.fingerprint, common_name=presented.common_name, not_after=not_after,
        days_left=max(0, (not_after - now).days),
        expiring=not_after - now < timedelta(days=ms.cert_expiry_warning_days),
        renew_available=ms.cert_self_renew_enabled and now >= opens and not renewed_already,
        renew_opens_at=opens,
        other_active_certificates=sum(1 for fp, c in snapshot.certs.items()
                                      if fp != presented.fingerprint and c.status == "active"),
    )


@me_router.post("/renew", dependencies=[Depends(check_read_only_mode)])
async def renew_my_certificate(request: Request, user: AuthenticatedUser = Depends(require_tenant()),
                               db: AsyncSession = Depends(get_db)):
    """A new certificate for the certificate this request presents, inside its renewal window.

    The old one keeps working until the new one is first used, or the overlap passes, so a
    renewal never locks anyone out mid-switch. One renewal per certificate.
    """
    ms = mini_settings()
    if not ms.cert_self_renew_enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Self-service renewal is turned off; ask an Administrator")
    presented = presented_certificate(request, ms)
    identity_uuid = (user.raw_claims or {}).get("sub", "")
    identity = (await db.execute(select(LocalIdentity).where(LocalIdentity.uuid == identity_uuid))).scalar_one_or_none()
    if presented is None or identity is None or identity.status != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="This session cannot renew a certificate")
    cert = (await db.execute(select(LocalIdentityCertificate).where(
        LocalIdentityCertificate.fingerprint == presented.fingerprint, LocalIdentityCertificate.identity_id == identity.id
    ).with_for_update())).scalar_one_or_none()
    if cert is None or cert.status != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="This certificate cannot be renewed")
    not_after = cert.not_after or presented.not_after
    if now_utc() < not_after - timedelta(days=ms.cert_renew_window_days):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail=f"Renewal opens {ms.cert_renew_window_days} days before the certificate expires")
    already = (await db.execute(select(func.count()).select_from(LocalIdentityCertificate).where(
        LocalIdentityCertificate.renewed_from_id == cert.id))).scalar_one()
    if already:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="This certificate was already renewed; import the new one")
    bundle = await issue_bundle(identity.email)
    await identities.add_certificate(identity.id, fingerprint=bundle.certificate.fingerprint,
                                     serial=bundle.certificate.serial, not_after=bundle.certificate.not_after,
                                     cert_common_name=bundle.certificate.common_name, renewed_from_id=cert.id, session=db)
    await security_event("identity.certificate_renew", target_type="identity", target_id=identity.uuid,
                         actor_user_id=identity.email, severity="high",
                         details={"from": cert.fingerprint, "to": bundle.certificate.fingerprint}, session=db)
    await db.commit()
    identities.forget(identity.uuid)
    return bundle_response(bundle, operator_uuid=identity.uuid)
