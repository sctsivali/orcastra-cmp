"""
Organization Management Router

CRUD for organizations, members, and cluster bindings.
Scoped by RBAC: Admin sees all, Partner sees own, Tenant sees assigned.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import get_cache_manager
from app.core.database import get_db
from app.core.dependencies import (
    AuthenticatedUser,
    require_admin,
    require_partner,
    require_tenant,
)
from app.core.rbac import Role, resolve_roles, get_highest_role
from app.core import org_scope
from app.core.org_scope import get_owned_clusters_ignoring_admin, verify_cluster_access
from app.core.sale_term import reaches_only_by_purchase
from app.core.whole_cluster_access import SHARED_CLUSTER_DETAIL, the_party_holds_it_whole
from app.services.organization_service import OrganizationService
from app.services.organization_roster import refuse_a_synced_partner, user_runs_org
from app.services.shared_cluster_audit import (
    SharedClusterGuardError,
    audit_shared_cluster,
)
from app.services.migration_fence import (
    MigrationFenceConflict,
    guard_cluster_mutation,
    guard_organization_mutation,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/organizations",
    tags=["Organizations"],
    dependencies=[Depends(require_tenant())],
)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class OrgCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None


class OrgUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None


class MemberAddRequest(BaseModel):
    user_email: str = Field(..., min_length=1, max_length=255)


class MemberUpdateRequest(BaseModel):
    pass  # reserved for future fields


class ClusterBindRequest(BaseModel):
    cluster_id: str = Field(..., min_length=1, max_length=255)


class OrgResponse(BaseModel):
    id: int
    name: str
    slug: str
    description: Optional[str]
    created_by: str
    is_active: bool
    member_count: int = 0
    cluster_count: int = 0
    # Whether the CALLER belongs to this organization. An Admin's list is every active
    # organization, and until now nothing in the payload told those apart from the caller's own,
    # so the branding editor could not warn before writing to a customer's organization. Free to
    # compute: Organization.members is lazy="selectin", so member_count already walks the same
    # collection. Defaults false so an older client sees the conservative answer.
    is_member: bool = False

    class Config:
        from_attributes = True


class MemberResponse(BaseModel):
    id: int
    organization_id: int
    user_email: str
    role: str
    invited_by: Optional[str]

    class Config:
        from_attributes = True


class ClusterBindingResponse(BaseModel):
    id: int
    organization_id: int
    cluster_id: str
    registered_by: Optional[str]

    class Config:
        from_attributes = True


class ClusterShareFindingResponse(BaseModel):
    scope: str
    code: str
    field: str
    count: int


class ClusterShareAuditResponse(BaseModel):
    cluster_id: str
    safe: bool
    complete: bool
    resources_checked: Dict[str, int]
    findings: list[ClusterShareFindingResponse]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_user_role(user: AuthenticatedUser) -> str:
    """Resolve highest role string for the current user."""
    roles = resolve_roles(user)
    return get_highest_role(roles).value


def _org_to_response(org, caller_email: Optional[str] = None, *, tenant: bool = False) -> OrgResponse:
    """Convert ORM Organization to response model.

    A tenant is not told who created the organization: on an OrcaHub buyer organization that is
    the seller's key minter, an address the buyer has no business with.
    """
    members = org.members or []
    return OrgResponse(
        id=org.id,
        name=org.name,
        slug=org.slug,
        description=org.description,
        created_by="" if tenant else org.created_by,
        is_active=org.is_active,
        member_count=len(members),
        cluster_count=len(org.clusters) if org.clusters else 0,
        is_member=bool(
            caller_email
            and any(
                (m.user_email or "").lower() == caller_email.lower() for m in members
            )
        ),
    )


@asynccontextmanager
async def _guard_organization_write(
    db: AsyncSession,
    organization_id: int,
) -> AsyncIterator[None]:
    try:
        async with guard_organization_mutation(
            db,
            organization_id=organization_id,
        ):
            yield
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc


@asynccontextmanager
async def _guard_cluster_write(
    db: AsyncSession,
    cluster_id: str,
) -> AsyncIterator[None]:
    try:
        async with guard_cluster_mutation(db, cluster_id=cluster_id):
            yield
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc


# ---------------------------------------------------------------------------
# Organization CRUD
# ---------------------------------------------------------------------------

@router.get("/", response_model=list[OrgResponse])
async def list_organizations(
    user: AuthenticatedUser = Depends(require_tenant()),
    db: AsyncSession = Depends(get_db),
):
    """List organizations visible to the current user."""
    svc = OrganizationService(db)
    role = _get_user_role(user)
    orgs = await svc.list_organizations(user_email=user.email, user_role=role)
    tenant = role == Role.TENANT.value
    return [_org_to_response(org, user.email, tenant=tenant) for org in orgs]


@router.get(
    "/cluster-share-audit/{cluster_id}",
    response_model=ClusterShareAuditResponse,
)
async def get_cluster_share_audit(
    cluster_id: str,
    _user: AuthenticatedUser = Depends(require_admin()),
):
    """Return an Admin-only, value-free pre-share safety verdict."""

    verify_cluster_access(cluster_id)
    return ClusterShareAuditResponse.model_validate(
        (await audit_shared_cluster(cluster_id)).as_dict()
    )


@router.get("/{org_id}", response_model=OrgResponse)
async def get_organization(
    org_id: int,
    user: AuthenticatedUser = Depends(require_tenant()),
    db: AsyncSession = Depends(get_db),
):
    """Get organization detail. Checks user has access."""
    svc = OrganizationService(db)
    role = _get_user_role(user)

    if not await svc.user_has_access(org_id, user.email, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this organization")

    org = await svc.get_organization(org_id)
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return _org_to_response(org, user.email, tenant=role == Role.TENANT.value)


@router.post("/", response_model=OrgResponse, status_code=status.HTTP_201_CREATED)
async def create_organization(
    body: OrgCreateRequest,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Create organization. Partner or Admin."""
    svc = OrganizationService(db)
    org = await svc.create_organization(
        name=body.name,
        description=body.description,
        created_by=user.email,
    )
    return _org_to_response(org, user.email)


@router.patch("/{org_id}", response_model=OrgResponse)
async def update_organization(
    org_id: int,
    body: OrgUpdateRequest,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Update organization. Partner (own) or Admin."""
    svc = OrganizationService(db)
    role = _get_user_role(user)

    if not await user_runs_org(db, org_id, user.email, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this organization")

    org = await svc.update_organization(org_id, name=body.name, description=body.description)
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return _org_to_response(org, user.email)


@router.delete("/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_organization(
    org_id: int,
    user: AuthenticatedUser = Depends(require_admin()),
    db: AsyncSession = Depends(get_db),
):
    """Delete organization. Admin only."""
    svc = OrganizationService(db)
    org = await svc.get_organization(org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    async with _guard_organization_write(db, org_id):
        # important: migration records keep their organization (RESTRICT foreign keys), so an
        # organization with any is refused up front with the reason. It used to reach the delete
        # and answer 500 from the database's refusal.
        jobs, plans = await svc.migration_history(org_id)
        if jobs or plans:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"This organization still owns migration history ({jobs} jobs, {plans} plans). "
                    "Migration records are kept for audit and stay with the organization they ran "
                    "for, so it cannot be deleted."
                ),
            )
        try:
            deleted = await svc.delete_organization(org_id)
        except IntegrityError as exc:
            await db.rollback()
            logger.warning("Organization %s delete refused by a foreign key: %s", org_id, exc.orig)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This organization is still referenced by other records, so it was not deleted.",
            ) from exc
        if not deleted:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    # The organization's tenant policies, members and bindings went with it.
    try:
        get_cache_manager().invalidate_after_access_change()
    except Exception as exc:  # noqa: BLE001 - the delete stands; scoped keys already hash the ACL
        logger.warning("Cache invalidation after deleting organization %s failed: %s", org_id, exc)


# ---------------------------------------------------------------------------
# Member management
# ---------------------------------------------------------------------------

@router.get("/{org_id}/members", response_model=list[MemberResponse])
async def list_members(
    org_id: int,
    user: AuthenticatedUser = Depends(require_tenant()),
    db: AsyncSession = Depends(get_db),
):
    """List organization members."""
    svc = OrganizationService(db)
    role = _get_user_role(user)

    if not await svc.user_has_access(org_id, user.email, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this organization")

    members = await svc.list_members(org_id)
    if role == Role.TENANT.value:
        # important: a tenant reads its own membership only. The full roster named every other
        # buyer and the seller's staff on an OrcaHub organization.
        caller = (user.email or "").lower()
        members = [m for m in members if (m.user_email or "").lower() == caller]
    return [MemberResponse.model_validate(m) for m in members]


@router.post("/{org_id}/members", response_model=MemberResponse, status_code=status.HTTP_201_CREATED)
async def add_member(
    org_id: int,
    body: MemberAddRequest,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Add member to organization. Partner (own org) or Admin."""
    svc = OrganizationService(db)
    role = _get_user_role(user)

    async with _guard_organization_write(db, org_id):
        # important: whoever runs the organization, not whoever can see it (a tenant row can).
        if not await user_runs_org(db, org_id, user.email, role):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this organization")

        try:
            member = await svc.add_member(
                org_id=org_id,
                user_email=body.user_email,
                role="member",
                invited_by=user.email,
            )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

    return MemberResponse.model_validate(member)


@router.patch("/{org_id}/members/{member_email}", response_model=MemberResponse)
async def update_member(
    org_id: int,
    member_email: str,
    body: MemberUpdateRequest,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Update member role. Partner (own org) or Admin."""
    svc = OrganizationService(db)
    role = _get_user_role(user)

    async with _guard_organization_write(db, org_id):
        if not await user_runs_org(db, org_id, user.email, role):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this organization")
        if role != "admin":
            await refuse_a_synced_partner(db, member_email, org_id)

        member = await svc.update_member_role(org_id, member_email, "member")
        if not member:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    return MemberResponse.model_validate(member)


@router.delete("/{org_id}/members/{member_email}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    org_id: int,
    member_email: str,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Remove member from organization. Partner (own org) or Admin."""
    svc = OrganizationService(db)
    role = _get_user_role(user)

    async with _guard_organization_write(db, org_id):
        if not await user_runs_org(db, org_id, user.email, role):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this organization")
        if role != "admin":
            await refuse_a_synced_partner(db, member_email, org_id)

        if not await svc.remove_member(org_id, member_email):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")


# ---------------------------------------------------------------------------
# Cluster bindings
# ---------------------------------------------------------------------------

@router.get("/{org_id}/clusters", response_model=list[ClusterBindingResponse])
async def list_cluster_bindings(
    org_id: int,
    user: AuthenticatedUser = Depends(require_tenant()),
    db: AsyncSession = Depends(get_db),
):
    """List clusters bound to this organization."""
    svc = OrganizationService(db)
    role = _get_user_role(user)

    if not await svc.user_has_access(org_id, user.email, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this organization")

    clusters = await svc.list_clusters(org_id)
    if role == Role.TENANT.value:
        # important: a tenant sees only the bindings its own grants reach, the same scope every
        # scoped route enforces, and not who registered them. A binding whose sale ended (or that
        # serves another buyer) named a cluster the tenant can no longer use.
        allowed = await org_scope.get_user_allowed_clusters(user, db)
        acl, _grants, _visible = await org_scope.get_tenant_scope(user, db)
        scope = org_scope._narrow_by_tenant_acl(allowed, acl) or set()
        return [
            ClusterBindingResponse(id=c.id, organization_id=c.organization_id, cluster_id=c.cluster_id, registered_by=None)
            for c in clusters
            if c.cluster_id in scope
        ]
    return [ClusterBindingResponse.model_validate(c) for c in clusters]


@router.post("/{org_id}/clusters", response_model=ClusterBindingResponse, status_code=status.HTTP_201_CREATED)
async def bind_cluster(
    org_id: int,
    body: ClusterBindRequest,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Bind a cluster to this organization. Partner (own org) or Admin."""
    svc = OrganizationService(db)
    role = _get_user_role(user)

    if not await user_runs_org(db, org_id, user.email, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this organization")

    # important: `user_has_access` checks the ORGANISATION, which is the caller's own. Nothing
    # checked the CLUSTER, and org scope is derived from exactly the row this writes
    # (org_scope._query_owned_clusters). A partner could therefore create an org, bind another
    # tenant's cluster id to it, and gain full org scope over that cluster: instances, networks,
    # storage, console and terminal, with break-glass treating it as owned rather than foreign.
    #
    # Same rule the API-key doors already apply: you may only grant what you already hold.
    # Personally registered clusters count as owned, so the org-less partner bootstrap that
    # reconcile_personal_clusters exists for still works.
    owned = await get_owned_clusters_ignoring_admin(user, db)
    if role != "admin":
        if body.cluster_id not in owned:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Not permitted to bind cluster {body.cluster_id}",
            )
        # important: a cluster reached only by purchase is the seller's. Bound here, the new row
        # would carry the buyer as registrant and outlive the purchase and its term
        # (security review 2026-10-27, core/sale_term.py).
        if await reaches_only_by_purchase(db, user.email, body.cluster_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Cluster {body.cluster_id} reaches you through a purchase, so it cannot be bound to "
                    "another organization."
                ),
            )
    # important: owned counts any membership, and a tenant row reaches a cluster but never owns it.
    # A binding needs the whole cluster through the caller's party (core/whole_cluster_access), and
    # registered_by is itself an ownership claim (org_scope._query_owned_clusters), so only such an
    # owner is stamped. An admin binding a cluster it does not own writes no registrant, so
    # break-glass still treats that cluster as foreign to the admin.
    whole = body.cluster_id in owned and await the_party_holds_it_whole(body.cluster_id, db, user)
    if role != "admin" and not whole:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=SHARED_CLUSTER_DETAIL)
    registrant = user.email if whole else None

    async with _guard_cluster_write(db, body.cluster_id):
        if not await user_runs_org(db, org_id, user.email, role):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this organization")
        try:
            binding = await svc.bind_cluster(org_id, body.cluster_id, registered_by=registrant)
        except SharedClusterGuardError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

    return ClusterBindingResponse.model_validate(binding)


@router.delete("/{org_id}/clusters/{cluster_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unbind_cluster(
    org_id: int,
    cluster_id: str,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Unbind a cluster from this organization. Partner (own org) or Admin."""
    svc = OrganizationService(db)
    role = _get_user_role(user)

    async with _guard_cluster_write(db, cluster_id):
        # important: whoever runs the organization. Seeing it is any member row, so a partner who
        # only bought from another partner could otherwise unbind that partner's clusters.
        if not await user_runs_org(db, org_id, user.email, role):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this organization")

        if not await svc.unbind_cluster(org_id, cluster_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cluster binding not found")
