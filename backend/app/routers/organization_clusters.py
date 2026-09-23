"""
Organization cluster bindings: list, bind and unbind the clusters an organization owns.

Split from organizations.py by responsibility. A binding is the row org scope is derived from,
so binding needs ownership of the cluster as well as access to the organization.
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import AuthenticatedUser, require_partner, require_tenant
from app.core.org_scope import get_owned_clusters_ignoring_admin
from app.routers.organizations import _get_user_role
from app.services.organization_service import OrganizationService

router = APIRouter(
    prefix="/organizations",
    tags=["Organizations"],
    dependencies=[Depends(require_tenant())],
)


class ClusterBindRequest(BaseModel):
    cluster_id: str = Field(..., min_length=1, max_length=255)


class ClusterBindingResponse(BaseModel):
    id: int
    organization_id: int
    cluster_id: str
    registered_by: Optional[str]

    class Config:
        from_attributes = True


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

    if not await svc.user_has_access(org_id, user.email, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this organization")

    # important: user_has_access checks the organization, not the cluster, and org scope is
    # derived from exactly the row written below. registered_by is itself an ownership claim
    # (org_scope._query_owned_clusters), so an admin binding a cluster they do not own gets no
    # stamp and break-glass still applies to them there.
    owned = await get_owned_clusters_ignoring_admin(user, db)
    if role != "admin" and body.cluster_id not in owned:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Not permitted to bind cluster {body.cluster_id}",
        )
    registrant = user.email if body.cluster_id in owned else None

    try:
        binding = await svc.bind_cluster(org_id, body.cluster_id, registered_by=registrant)
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

    if not await svc.user_has_access(org_id, user.email, role):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this organization")

    if not await svc.unbind_cluster(org_id, cluster_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cluster binding not found")
