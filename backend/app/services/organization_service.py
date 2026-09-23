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
from app.services.cluster_parties import RUNNING_ROLES

logger = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    """Generate URL-safe slug from organization name."""
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return re.sub(r"-+", "-", slug).strip("-")


def synced_partner_refusal(member_email: str, slugs: "list[str]") -> str:
    """Why a non-admin may not change a partner row in an organization OrcaHub manages."""
    return (
        f"{member_email} is a partner of {', '.join(slugs)}, which OrcaHub manages. An Orcastra "
        "admin can remove them here, or a platform admin in OrcaHub."
    )


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
        self, org_id: int, cluster_id: str, registered_by: Optional[str]
    ) -> OrganizationCluster:
        """Bind a Vault cluster to an organization."""
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

    async def user_runs_org(self, org_id: int, user_email: str, user_role: str) -> bool:
        """Whether a user may change an organization's roster: admin, its creator, or its partner row.

        important: user_has_access answers "may this user see the organization" and is true for a tenant
        row too, so a partner who only buys from another partner could add, change and remove that
        partner's organization members with it.
        """
        if user_role == "admin":
            return True
        org = await self.get_organization(org_id)
        if not org:
            return False
        if org.created_by == user_email:
            return True
        result = await self.db.execute(
            select(OrganizationMember.id).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_email == user_email,
                OrganizationMember.role.in_(RUNNING_ROLES),
            )
        )
        return result.first() is not None

    async def synced_partner_orgs(self, member_email: str, org_ids: list[int]) -> dict[int, str]:
        """OrcaHub-synced organizations, among org_ids, where member_email is a partner (id to slug).

        note: OrcaHub owns those rosters and lets only a platform admin remove a partner. A dashboard
        change to such a row is not sent there, so the next sync would quietly undo it.
        """
        if not org_ids:
            return {}
        result = await self.db.execute(
            select(Organization.id, Organization.slug)
            .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
            .where(
                OrganizationMember.user_email == member_email,
                OrganizationMember.organization_id.in_(org_ids),
                OrganizationMember.role.in_(RUNNING_ROLES),
                Organization.source == "orcahub",
                Organization.api_key_id.isnot(None),
            )
            .order_by(Organization.slug)
        )
        return {org_id: slug for org_id, slug in result.all()}

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
