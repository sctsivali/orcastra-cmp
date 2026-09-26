"""
Organization Service - CRUD operations for organizations, members, and cluster bindings.

Handles scoping: Admin sees all, Partner sees own, Tenant sees assigned.
"""

import re
import logging
from typing import Optional

from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.db_models import Organization, OrganizationMember, OrganizationCluster
from app.services.shared_cluster_audit import (
    assert_cluster_safe_to_share,
    serialize_cluster_share,
)

logger = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    """Generate URL-safe slug from organization name."""
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return re.sub(r"-+", "-", slug).strip("-")


class OrganizationService:
    """Service layer for organization management."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Organization CRUD
    # ------------------------------------------------------------------

    async def list_organizations(
        self,
        user_email: Optional[str] = None,
        user_role: Optional[str] = None,
    ) -> list[Organization]:
        """
        List organizations scoped by role.
        - Admin: all orgs
        - Partner: orgs created by this user
        - Tenant: orgs where user is a member
        """
        query = select(Organization).where(Organization.is_active == True)

        if user_role == "admin" or user_role is None:
            # admin or dev mode - all
            pass
        elif user_role == "partner":
            # partner: orgs they created OR orgs where they are a member
            member_org_ids = (
                select(OrganizationMember.organization_id)
                .where(OrganizationMember.user_email == user_email)
            )
            query = query.where(
                (Organization.created_by == user_email) | (Organization.id.in_(member_org_ids))
            )
        else:
            # tenant: only orgs where they're a member
            member_org_ids = (
                select(OrganizationMember.organization_id)
                .where(OrganizationMember.user_email == user_email)
            )
            query = query.where(Organization.id.in_(member_org_ids))

        query = query.order_by(Organization.name)
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def get_organization(self, org_id: int) -> Optional[Organization]:
        """Get a single organization by ID."""
        result = await self.db.execute(
            select(Organization)
            .where(Organization.id == org_id)
            .options(selectinload(Organization.members), selectinload(Organization.clusters))
        )
        return result.scalar_one_or_none()

    async def get_organization_by_slug(self, slug: str) -> Optional[Organization]:
        """Get a single organization by slug."""
        result = await self.db.execute(
            select(Organization).where(Organization.slug == slug)
        )
        return result.scalar_one_or_none()

    async def create_organization(
        self, name: str, description: Optional[str], created_by: str
    ) -> Organization:
        """Create a new organization. Creator is automatically added as partner member."""
        slug = _slugify(name)

        # ensure slug uniqueness
        existing = await self.get_organization_by_slug(slug)
        if existing:
            # append numeric suffix
            count = await self.db.execute(
                select(func.count()).select_from(Organization).where(Organization.slug.like(f"{slug}%"))
            )
            slug = f"{slug}-{count.scalar() + 1}"

        org = Organization(
            name=name,
            slug=slug,
            description=description,
            created_by=created_by,
        )
        self.db.add(org)
        await self.db.flush()  # get org.id

        # auto-add creator as member
        member = OrganizationMember(
            organization_id=org.id,
            user_email=created_by,
            role="member",
            invited_by=created_by,
        )
        self.db.add(member)
        await self.db.commit()
        await self.db.refresh(org)
        return org

    async def update_organization(
        self, org_id: int, name: Optional[str] = None, description: Optional[str] = None
    ) -> Optional[Organization]:
        """Update organization details."""
        org = await self.get_organization(org_id)
        if not org:
            return None
        if name is not None:
            org.name = name
        if description is not None:
            org.description = description
        await self.db.commit()
        await self.db.refresh(org)
        return org

    async def delete_organization(self, org_id: int) -> bool:
        """Delete organization (cascades members + cluster bindings)."""
        org = await self.get_organization(org_id)
        if not org:
            return False
        await self.db.delete(org)
        await self.db.commit()
        return True

    # ------------------------------------------------------------------
    # Member management
    # ------------------------------------------------------------------

    async def list_members(self, org_id: int) -> list[OrganizationMember]:
        """List all members of an organization."""
        result = await self.db.execute(
            select(OrganizationMember)
            .where(OrganizationMember.organization_id == org_id)
            .order_by(OrganizationMember.role, OrganizationMember.user_email)
        )
        return list(result.scalars().all())

    async def add_member(
        self, org_id: int, user_email: str, role: str, invited_by: str
    ) -> OrganizationMember:
        """Add a member to an organization."""
        # check for existing membership
        existing = await self.db.execute(
            select(OrganizationMember)
            .where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_email == user_email,
            )
        )
        if existing.scalar_one_or_none():
            raise ValueError(f"User {user_email} is already a member of this organization")

        member = OrganizationMember(
            organization_id=org_id,
            user_email=user_email,
            role=role,
            invited_by=invited_by,
        )
        self.db.add(member)
        await self.db.commit()
        await self.db.refresh(member)
        return member

    async def update_member_role(
        self, org_id: int, user_email: str, new_role: str
    ) -> Optional[OrganizationMember]:
        """Update a member's role within an organization."""
        result = await self.db.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_email == user_email,
            )
        )
        member = result.scalar_one_or_none()
        if not member:
            return None
        member.role = new_role
        await self.db.commit()
        await self.db.refresh(member)
        return member

    async def remove_member(self, org_id: int, user_email: str) -> bool:
        """Remove a member from an organization."""
        result = await self.db.execute(
            delete(OrganizationMember).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_email == user_email,
            )
        )
        await self.db.commit()
        return result.rowcount > 0

    # ------------------------------------------------------------------
    # Cluster binding
    # ------------------------------------------------------------------

    async def list_clusters(self, org_id: int) -> list[OrganizationCluster]:
        """List clusters bound to an organization."""
        result = await self.db.execute(
            select(OrganizationCluster)
            .where(OrganizationCluster.organization_id == org_id)
            .order_by(OrganizationCluster.registered_at)
        )
        return list(result.scalars().all())

    async def bind_cluster(
        self, org_id: int, cluster_id: str, registered_by: str
    ) -> OrganizationCluster:
        """Bind a Vault cluster to an organization."""
        async with serialize_cluster_share(self.db, cluster_id):
            await assert_cluster_safe_to_share(
                self.db,
                cluster_id,
                prospective_organization_id=org_id,
                prospective_registered_by=registered_by,
            )
            existing = await self.db.execute(
                select(OrganizationCluster).where(
                    OrganizationCluster.organization_id == org_id,
                    OrganizationCluster.cluster_id == cluster_id,
                )
            )
            if existing.scalar_one_or_none():
                raise ValueError(f"Cluster {cluster_id} is already bound to this organization")

            binding = OrganizationCluster(
                organization_id=org_id,
                cluster_id=cluster_id,
                registered_by=registered_by,
            )
            self.db.add(binding)
            await self.db.commit()
        await self.db.refresh(binding)
        return binding

    async def unbind_cluster(self, org_id: int, cluster_id: str) -> bool:
        """Remove cluster binding from an organization."""
        result = await self.db.execute(
            delete(OrganizationCluster).where(
                OrganizationCluster.organization_id == org_id,
                OrganizationCluster.cluster_id == cluster_id,
            )
        )
        await self.db.commit()
        return result.rowcount > 0

    async def get_cluster_bindings(self, cluster_id: str) -> list[OrganizationCluster]:
        """Return every OrganizationCluster row for a cluster, oldest first."""
        result = await self.db.execute(
            select(OrganizationCluster)
            .where(OrganizationCluster.cluster_id == cluster_id)
            .order_by(OrganizationCluster.id)
        )
        return list(result.scalars().all())

    async def reassign_cluster_organization(
        self,
        cluster_id: str,
        organization_id: Optional[int],
        registered_by: str,
    ) -> OrganizationCluster:
        """Keep exactly one OrganizationCluster row for this cluster.

        Updates the oldest row (or inserts if none exist) and deletes extras.
        organization_id=None leaves the cluster unbound.
        Raises LookupError if organization_id is set but the org does not exist.
        """
        if organization_id is not None:
            org = await self.get_organization(organization_id)
            if not org:
                raise LookupError("Organization not found")

        bindings = await self.get_cluster_bindings(cluster_id)
        if not bindings:
            binding = OrganizationCluster(
                organization_id=organization_id,
                cluster_id=cluster_id,
                registered_by=registered_by,
            )
            self.db.add(binding)
            await self.db.commit()
            await self.db.refresh(binding)
            return binding

        keeper = bindings[0]
        for extra in bindings[1:]:
            await self.db.delete(extra)
        await self.db.flush()

        keeper.organization_id = organization_id
        keeper.registered_by = registered_by
        await self.db.commit()
        await self.db.refresh(keeper)
        return keeper

    async def get_cluster_org_stamps(
        self, cluster_ids: list[str]
    ) -> dict[str, tuple[Optional[int], Optional[str]]]:
        """Map cluster_id -> (organization_id, organization_name). First binding wins."""
        if not cluster_ids:
            return {}
        result = await self.db.execute(
            select(
                OrganizationCluster.cluster_id,
                OrganizationCluster.organization_id,
                Organization.name,
            )
            .outerjoin(Organization, Organization.id == OrganizationCluster.organization_id)
            .where(OrganizationCluster.cluster_id.in_(cluster_ids))
            .order_by(OrganizationCluster.id)
        )
        stamps: dict[str, tuple[Optional[int], Optional[str]]] = {}
        for cluster_id, org_id, org_name in result.all():
            if cluster_id in stamps:
                continue
            stamps[cluster_id] = (org_id, org_name)
        return stamps

    # ------------------------------------------------------------------
    # Helpers for scoped access
    # ------------------------------------------------------------------

    async def user_has_access(self, org_id: int, user_email: str, user_role: str) -> bool:
        """Check if user has access to a given organization."""
        if user_role == "admin":
            return True

        org = await self.get_organization(org_id)
        if not org:
            return False

        if user_role == "partner" and org.created_by == user_email:
            return True

        result = await self.db.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_email == user_email,
            )
        )
        return result.scalar_one_or_none() is not None

    async def get_user_cluster_ids(self, user_email: str, user_role: str) -> list[str]:
        """
        Get list of cluster IDs the user can access.
        Admin: all, Partner: own orgs, Tenant: assigned orgs.
        Returns empty list for admin (meaning "all" - caller should skip filtering).

        note: currently unused. It scopes by org membership only and does NOT honor
        OrganizationCluster.registered_by, so it would miss a partner's own unbound
        clusters. Do not wire it into a live scope path - use
        app.core.org_scope.get_user_allowed_clusters instead.
        """
        if user_role == "admin":
            return []  # empty = no filter = all

        # find org IDs user belongs to
        if user_role == "partner":
            org_ids_q = (
                select(Organization.id)
                .where(Organization.created_by == user_email)
            )
            member_org_ids_q = (
                select(OrganizationMember.organization_id)
                .where(OrganizationMember.user_email == user_email)
            )
            from sqlalchemy import union
            combined = union(org_ids_q, member_org_ids_q)
            result = await self.db.execute(combined)
            org_ids = [row[0] for row in result.fetchall()]
        else:
            result = await self.db.execute(
                select(OrganizationMember.organization_id)
                .where(OrganizationMember.user_email == user_email)
            )
            org_ids = [row[0] for row in result.fetchall()]

        if not org_ids:
            return ["__no_access__"]  # sentinel - no clusters accessible

        cluster_result = await self.db.execute(
            select(OrganizationCluster.cluster_id)
            .where(OrganizationCluster.organization_id.in_(org_ids))
        )
        cluster_ids = [row[0] for row in cluster_result.fetchall()]
        return cluster_ids if cluster_ids else ["__no_access__"]
