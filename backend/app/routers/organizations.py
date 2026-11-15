"""
Organizations, their members and their cluster bindings.

Scoped by RBAC: Admin sees all, Partner sees its own, Tenant sees the ones it belongs to.
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import asdict
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
    check_read_only_mode,
    require_admin,
    require_partner,
    require_tenant,
)
from app.core.rbac import Role, resolve_roles, get_highest_role
from app.core import org_scope
from app.core.org_scope import get_owned_clusters_ignoring_admin, verify_cluster_access
from app.core.sale_term import reaches_only_by_purchase
from app.core.whole_cluster_access import SHARED_CLUSTER_DETAIL, the_party_holds_it_whole
from app.services import cluster_bind_preview
from app.services.key_party_bound import forget_cluster_bounds
from app.services.lxd.registry import registered_cluster_ids
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
    # Whether the caller belongs to this organization. An Admin's list holds every active
    # organization, so the branding editor reads this to warn before writing to someone else's.
    # Organization.members is lazy="selectin", so member_count already loads the same collection.
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
    granted_by: Optional[str] = None

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
    """Build the response for an organization.

    A tenant is not told who created the organization: on an OrcaHub buyer organization that is
    the seller's key minter.
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


async def _require_organization(svc: OrganizationService, org_id: int) -> None:
    """Raise 404 unless the organization exists.

    An Admin runs every organization (user_runs_org reads no row for it), so without this a member
    or binding written into a missing organization would fail on the foreign key with a 500.
    """
    if await svc.get_organization(org_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")


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

@router.get("", response_model=list[OrgResponse])
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


@router.post("", response_model=OrgResponse, status_code=status.HTTP_201_CREATED, dependencies=[Depends(check_read_only_mode)])
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


@router.patch("/{org_id}", response_model=OrgResponse, dependencies=[Depends(check_read_only_mode)])
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


@router.delete("/{org_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(check_read_only_mode)])
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
        # Migration records keep their organization (RESTRICT foreign keys), so an organization
        # with any is refused up front with the reason instead of failing in the database.
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
        # important: a tenant reads its own membership only. On an OrcaHub organization the full
        # roster names every other buyer and the seller's staff.
        caller = (user.email or "").lower()
        members = [m for m in members if (m.user_email or "").lower() == caller]
    return [MemberResponse.model_validate(m) for m in members]


@router.post("/{org_id}/members", response_model=MemberResponse, status_code=status.HTTP_201_CREATED, dependencies=[Depends(check_read_only_mode)])
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
        # important: the caller must run the organization; a tenant row can see it too.
        if not await user_runs_org(db, org_id, user.email, role):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this organization")
        await _require_organization(svc, org_id)

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


@router.patch("/{org_id}/members/{member_email}", response_model=MemberResponse, dependencies=[Depends(check_read_only_mode)])
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


@router.delete("/{org_id}/members/{member_email}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(check_read_only_mode)])
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
        # scoped route enforces, and not who registered them. Otherwise a binding whose sale ended,
        # or that serves another buyer, names a cluster the tenant cannot use.
        allowed = await org_scope.get_user_allowed_clusters(user, db)
        acl, _grants, _visible = await org_scope.get_tenant_scope(user, db)
        scope = org_scope._narrow_by_tenant_acl(allowed, acl) or set()
        return [
            ClusterBindingResponse(
                id=c.id, organization_id=c.organization_id, cluster_id=c.cluster_id,
                registered_by=None, granted_by=None,
            )
            for c in clusters
            if c.cluster_id in scope
        ]
    # important: seeing an organization takes any member row, and a buyer's founder is a partner
    # of the buyer. Who registered or granted a binding is told only to an administrator or a
    # partner who runs the organization; Synced Organizations shows everyone the binding's kind.
    if role != Role.ADMIN.value and not await user_runs_org(db, org_id, user.email, role):
        return [
            ClusterBindingResponse(
                id=c.id, organization_id=c.organization_id, cluster_id=c.cluster_id,
                registered_by=None, granted_by=None,
            )
            for c in clusters
        ]
    return [ClusterBindingResponse.model_validate(c) for c in clusters]


class BindDecision(BaseModel):
    """Whether this caller may bind the cluster to the organization, and with which stamps."""
    refusal: Optional[str] = None
    registrant: Optional[str] = None
    granter: Optional[str] = None


async def bind_decision(db: AsyncSession, user: AuthenticatedUser, role: str, org_id: int, cluster_id: str) -> BindDecision:
    """The bind's own authorization, shared by the bind, its preview and the candidate list.

    important: user_runs_org checks the organization, which is the caller's own, and org scope is
    derived from exactly the row a bind writes (org_scope._query_owned_clusters). Without a check
    on the cluster, a partner could bind another party's cluster id to its own organization and
    gain full org scope over it. As on the API-key routes, only what the caller already holds may
    be granted. Personally registered clusters count as owned, so the org-less partner bootstrap
    (reconcile_personal_clusters) still works.
    """
    if not await user_runs_org(db, org_id, user.email, role):
        return BindDecision(refusal="No access to this organization")
    owned = await get_owned_clusters_ignoring_admin(user, db)
    if role != "admin":
        if cluster_id not in owned:
            return BindDecision(refusal=f"Not permitted to bind cluster {cluster_id}")
        # important: a cluster reached only by purchase is the seller's. Bound here, the new row
        # would carry the buyer as registrant and outlive the purchase and its term
        # (core/sale_term.py).
        if await reaches_only_by_purchase(db, user.email, cluster_id):
            return BindDecision(refusal=(
                f"Cluster {cluster_id} reaches you through a purchase, so it cannot be bound to "
                "another organization."
            ))
    # important: owned counts any membership, and a tenant row reaches a cluster but never owns it.
    # A binding needs the whole cluster through the caller's party (core/whole_cluster_access), and
    # registered_by is itself an ownership claim (org_scope._query_owned_clusters), so only such an
    # owner is stamped. An admin binding a cluster it does not own writes no registrant, so
    # break-glass still treats that cluster as foreign to the admin, and records the admin as the
    # grantor so the row is not read as a sale provisioning made (core/sale_term.py).
    whole = cluster_id in owned and await the_party_holds_it_whole(cluster_id, db, user)
    if role != "admin" and not whole:
        return BindDecision(refusal=SHARED_CLUSTER_DETAIL)
    registrant = user.email if whole else None
    return BindDecision(registrant=registrant, granter=user.email if registrant is None else None)


def _access_changed() -> None:
    try:
        get_cache_manager().invalidate_after_access_change()
    except Exception as exc:  # noqa: BLE001 - the binding stands; scoped keys already hash the scope
        logger.warning("Cache invalidation after a binding change failed: %s", type(exc).__name__)


def _binding_changed(cluster_id: str) -> None:
    """A binding decides cluster scope, the party and every key's bound on the cluster, so the
    shared lists and the keys' cached bounds are dropped at once rather than after their TTLs."""
    forget_cluster_bounds(cluster_id)
    _access_changed()


@router.post("/{org_id}/clusters", response_model=ClusterBindingResponse, status_code=status.HTTP_201_CREATED, dependencies=[Depends(check_read_only_mode)])
async def bind_cluster(
    org_id: int,
    body: ClusterBindRequest,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Bind a cluster to this organization. Partner (own org, whole cluster) or Admin."""
    svc = OrganizationService(db)
    role = _get_user_role(user)

    if not await user_runs_org(db, org_id, user.email, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this organization")
    await _require_organization(svc, org_id)
    decision = await bind_decision(db, user, role, org_id, body.cluster_id)
    if decision.refusal:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=decision.refusal)

    async with _guard_cluster_write(db, body.cluster_id):
        if not await user_runs_org(db, org_id, user.email, role):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this organization")
        try:
            binding = await svc.bind_cluster(
                org_id, body.cluster_id, registered_by=decision.registrant, granted_by=decision.granter,
            )
        except SharedClusterGuardError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

    _binding_changed(body.cluster_id)
    return ClusterBindingResponse.model_validate(binding)


class OrganizationOnClusterResponse(BaseModel):
    id: int
    name: str


class ClusterBindPreviewResponse(BaseModel):
    cluster_id: str
    organization_id: int
    allowed: bool
    refusal: Optional[str] = None
    binding_kind: Optional[str] = None
    already_bound: Optional[bool] = None
    several_organizations_before: Optional[bool] = None
    several_organizations_after: Optional[bool] = None
    holders_lose_whole_cluster_tools: Optional[bool] = None
    organizations_on_cluster: Optional[list[OrganizationOnClusterResponse]] = None
    share_audit: Optional[Dict] = None
    notes: list[str] = []


@router.get("/{org_id}/cluster-candidates", response_model=list[str])
async def list_cluster_bind_candidates(
    org_id: int,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """The clusters this caller may bind to the organization and that are not bound to it yet."""
    role = _get_user_role(user)
    if not await user_runs_org(db, org_id, user.email, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this organization")
    bound = await cluster_bind_preview.bound_cluster_ids(db, org_id)
    if role == "admin":
        registry = await registered_cluster_ids()
        known = set(registry) if registry is not None else await cluster_bind_preview.every_bound_cluster_id(db)
        return sorted(known - bound)
    owned = await get_owned_clusters_ignoring_admin(user, db)
    candidates = []
    for cluster_id in sorted(owned - bound):
        if not (await bind_decision(db, user, role, org_id, cluster_id)).refusal:
            candidates.append(cluster_id)
    return candidates


@router.get("/{org_id}/clusters/preview", response_model=ClusterBindPreviewResponse)
async def preview_cluster_bind(
    org_id: int,
    cluster_id: str,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """What binding `cluster_id` to the organization would change. Nothing is written.

    A caller the bind would refuse gets the same refusal and nothing else about the cluster.
    """
    role = _get_user_role(user)
    decision = await bind_decision(db, user, role, org_id, cluster_id)
    if decision.refusal:
        return ClusterBindPreviewResponse.model_validate(
            asdict(cluster_bind_preview.refused(cluster_id, org_id, decision.refusal))
        )
    preview = await cluster_bind_preview.preview_bind(
        db,
        organization_id=org_id,
        cluster_id=cluster_id,
        registrant=decision.registrant,
        granter=decision.granter,
        show_organizations=role == "admin",
    )
    return ClusterBindPreviewResponse.model_validate(asdict(preview))


@router.delete("/{org_id}/clusters/{cluster_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(check_read_only_mode)])
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
        # important: the caller must run the organization. Seeing it takes any member row, so a
        # partner who only bought from another partner could otherwise unbind that partner's
        # clusters.
        if not await user_runs_org(db, org_id, user.email, role):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this organization")

        if not await svc.unbind_cluster(org_id, cluster_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cluster binding not found")
    _binding_changed(cluster_id)
