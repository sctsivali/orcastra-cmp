"""An organization's branding: what an account sees, and the organization's own row that partners edit.

Included before routers/organizations.py, whose `/{org_id}` route would otherwise take
`/my-preferences`. The answer is resolved through the layers in services/branding/resolve.py.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import (
    AuthenticatedUser,
    check_read_only_mode,
    require_admin,
    require_partner,
    require_tenant,
)
from app.core.rbac import Role, get_highest_role, resolve_roles
from app.services.branding import provider_link, store
from app.services.branding.resolve import resolve
from app.services.branding.validation import BrandingInvalid
from app.services.organization_roster import user_runs_org
from app.services.organization_service import OrganizationService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/organizations",
    tags=["Organizations"],
    dependencies=[Depends(require_tenant())],
)

_NO_STORE = "no-store, private"


class MyBrandingResponse(BaseModel):
    organization_id: int
    settings: dict
    # Always None: whoever last edited a buyer's branding is the seller's staff.
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None
    version: str
    # The layer on top: organization | provider | deployment | default
    source: str
    # organization, or deployment for an Administrator and an account in no organization
    subject: str


class MyBrandingVersionResponse(BaseModel):
    organization_id: int
    updated_at: Optional[str] = None
    version: str


class ProviderRef(BaseModel):
    id: int
    name: str
    link_source: Optional[str] = None


class OrgBrandingBody(BaseModel):
    settings: dict = Field(default_factory=dict)


class OrgBrandingResponse(BaseModel):
    organization_id: int
    # The organization's own row; {} when it has none and shows what it inherits.
    settings: dict
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None
    effective: dict
    # What shows under the organization's own row: provider | deployment | default
    inherits_from: str
    provider: Optional[ProviderRef] = None
    followers_count: int = 0
    version: str


class ProviderBody(BaseModel):
    provider_organization_id: Optional[int] = Field(default=None, ge=1)


def _roles(user: AuthenticatedUser) -> set[Role]:
    return set(resolve_roles(user))


def _admin_only(user: AuthenticatedUser) -> bool:
    roles = _roles(user)
    return Role.ADMIN in roles and Role.PARTNER not in roles


def _tenant_only(user: AuthenticatedUser) -> bool:
    return get_highest_role(resolve_roles(user)) == Role.TENANT


async def _require_org_access(db: AsyncSession, org_id: int, user: AuthenticatedUser, *, runs: bool = False):
    """The organization, when the caller may see it, or with `runs` when the caller runs it.

    A write passes `runs` (services.organization_roster.user_runs_org: an admin, the creator or a
    partner row). Seeing it takes any member row, so without `runs` a partner who only bought from
    another partner could rewrite that partner's branding.
    """
    svc = OrganizationService(db)
    org = await svc.get_organization(org_id)
    role = get_highest_role(resolve_roles(user)).value
    allowed = (
        await user_runs_org(db, org_id, user.email, role) if runs
        else await svc.user_has_access(org_id, user.email, role)
    )
    if org is None or not allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this organization")
    return org


async def _org_response(db: AsyncSession, org, user: AuthenticatedUser) -> OrgBrandingResponse:
    resolved = await store.org_branding(db, org.id)
    inherited = resolve(f"organization:{org.id}", store.inherited_layers(resolved))
    own = next((layer for layer in resolved.layers if layer.kind == "organization"), None)
    tenant = _tenant_only(user)
    provider = None
    if not tenant and org.provider_organization_id:
        provider_org = await OrganizationService(db).get_organization(org.provider_organization_id)
        if provider_org is not None:
            provider = ProviderRef(id=provider_org.id, name=provider_org.name, link_source=org.provider_link_source)
    return OrgBrandingResponse(
        organization_id=org.id,
        settings=own.settings if own else {},
        updated_by=None if tenant or own is None else own.updated_by,
        updated_at=store.stamp(own.updated_at) if own else None,
        effective=resolved.settings,
        inherits_from=inherited.source,
        provider=provider,
        followers_count=0 if tenant else await store.followers_count(db, org.id),
        version=resolved.version,
    )


@router.get("/my-preferences", response_model=MyBrandingResponse)
async def get_my_branding(
    response: Response,
    user: AuthenticatedUser = Depends(require_tenant()),
    db: AsyncSession = Depends(get_db),
):
    """The branding this account sees: its organization's, inherited where it has none, or the deployment's."""
    response.headers["Cache-Control"] = _NO_STORE
    account = await store.account_branding(db, user.email, admin_only=_admin_only(user))
    return MyBrandingResponse(
        organization_id=account.organization_id,
        settings=account.resolved.settings,
        updated_at=store.stamp(account.resolved.updated_at),
        version=account.resolved.version,
        source=account.resolved.source,
        subject=account.subject,
    )


@router.get("/my-preferences/version", response_model=MyBrandingVersionResponse)
async def get_my_branding_version(
    response: Response,
    user: AuthenticatedUser = Depends(require_tenant()),
    db: AsyncSession = Depends(get_db),
):
    """Polled by every signed-in browser; reads timestamps only, never the settings."""
    response.headers["Cache-Control"] = _NO_STORE
    account = await store.account_branding(db, user.email, admin_only=_admin_only(user), with_settings=False)
    return MyBrandingVersionResponse(
        organization_id=account.organization_id,
        updated_at=store.stamp(account.resolved.updated_at),
        version=account.resolved.version,
    )


@router.get("/{org_id}/preferences", response_model=OrgBrandingResponse)
async def get_org_branding(
    org_id: int,
    response: Response,
    user: AuthenticatedUser = Depends(require_tenant()),
    db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = _NO_STORE
    org = await _require_org_access(db, org_id, user)
    return await _org_response(db, org, user)


@router.put("/{org_id}/preferences", response_model=OrgBrandingResponse)
async def update_org_branding(
    org_id: int,
    body: OrgBrandingBody,
    response: Response,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_read_only_mode),
):
    """Replace the organization's own branding. Partner (an organization it runs) or Admin.

    A write by someone who is not a member of the organization is audited and also logged at WARNING.
    """
    response.headers["Cache-Control"] = _NO_STORE
    org = await _require_org_access(db, org_id, user, runs=True)
    # important: an Admin passes user_runs_org for every organization (break-glass). Such a write
    # must not be quiet: besides the audit record every write leaves, a write by somebody who does
    # not belong to the organization is logged at WARNING.
    members = {(m.user_email or "").lower() for m in org.members or []}
    if (user.email or "").lower() not in members:
        logger.warning(
            "Cross-organization branding write: %s is not a member of organization %s and is "
            "replacing its branding",
            user.email,
            org.id,
        )
    try:
        await store.save_org_branding(db, org.id, body.settings, user.email or "")
    except BrandingInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _org_response(db, org, user)


@router.delete("/{org_id}/preferences", status_code=status.HTTP_204_NO_CONTENT)
async def reset_org_branding(
    org_id: int,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_read_only_mode),
):
    """Remove the organization's own branding, so it shows its provider's or the deployment default again."""
    org = await _require_org_access(db, org_id, user, runs=True)
    await store.reset_org_branding(db, org.id)


@router.put("/{org_id}/provider", response_model=OrgBrandingResponse)
async def set_org_provider(
    org_id: int,
    body: ProviderBody,
    user: AuthenticatedUser = Depends(require_admin()),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_read_only_mode),
):
    """Admin: which organization this one takes its branding from, or none. Provisioning keeps the choice."""
    org = await _require_org_access(db, org_id, user)
    try:
        org = await provider_link.set_provider(db, org, body.provider_organization_id, user.email or "")
    except provider_link.ProviderInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _org_response(db, org, user)
