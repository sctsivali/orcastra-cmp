"""Branding held above a single organization: the deployment default, and rows migration 045 set aside.

An organization's own branding stays in organization_preferences. What an account sees is
resolved by services/branding/resolve.py: the built-in look, then this deployment's default, then
the organization that sold to it (organizations.provider_organization_id), then its own row.
"""

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from app.core.database import Base


class DeploymentBranding(Base):
    """The one branding row for the whole deployment: the sign-in page and every account without an organization's own."""

    __tablename__ = "deployment_branding"

    # Always 1: the CHECK keeps a second row from ever existing.
    id = Column(Integer, primary_key=True, default=1)
    settings = Column(JSONB, nullable=False)
    updated_by = Column(String(255), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_deployment_branding_single_row"),
    )


class OrganizationPreferencesRetired(Base):
    """An organization_preferences row migration 045 took out of the way, kept so its downgrade can put it back.

    Two kinds: an empty `{}` row (a reset, which now deletes the row so the organization inherits),
    and the one-time copy provisioning used to make of the seller's branding into a buyer, which
    the live link replaces. Nothing reads this table at runtime.
    """

    __tablename__ = "organization_preferences_retired"

    id = Column(Integer, primary_key=True, autoincrement=True)
    organization_id = Column(Integer, nullable=False, index=True)
    settings = Column(JSONB, nullable=False)
    updated_by = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)
    reason = Column(String(32), nullable=False)
    retired_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint("reason IN ('empty', 'provision_copy')", name="ck_org_prefs_retired_reason"),
    )
