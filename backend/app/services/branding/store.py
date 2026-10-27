"""Reading the branding layers of an organization and writing its own row or the deployment default.

Every write is validated (validation.py) and audited. The version reads select timestamps only:
the settings hold base64 images, and every signed-in browser asks for the version every 45 seconds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.branding_models import DeploymentBranding
from app.models.db_models import Organization, OrganizationPreferences
from app.services.activity_logger import get_activity_logger
from app.services.branding.resolve import Layer, Resolved, resolve
from app.services.branding.subject import subject_organization_id
from app.services.branding.validation import validate_branding

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountBranding:
    # 0 when the account sees the deployment default (an Administrator, or no organization).
    organization_id: int
    subject: str  # organization | deployment
    resolved: Resolved


async def _deployment_layer(db: AsyncSession, *, with_settings: bool) -> List[Layer]:
    columns = (DeploymentBranding.updated_at, DeploymentBranding.updated_by)
    if with_settings:
        columns += (DeploymentBranding.settings,)
    row = (await db.execute(select(*columns).where(DeploymentBranding.id == 1))).first()
    if row is None:
        return []
    return [Layer("deployment", row[2] if with_settings else {"_": True}, row[0], row[1])]


async def _org_layers(db: AsyncSession, organization_id: int, *, with_settings: bool) -> List[Layer]:
    provider_id = (await db.execute(
        select(Organization.provider_organization_id).where(Organization.id == organization_id)
    )).scalar_one_or_none()
    ids = [provider_id, organization_id] if provider_id else [organization_id]
    columns = (
        OrganizationPreferences.organization_id,
        OrganizationPreferences.updated_at,
        OrganizationPreferences.updated_by,
    )
    if with_settings:
        columns += (OrganizationPreferences.settings,)
    query = (
        select(*columns)
        .join(Organization, Organization.id == OrganizationPreferences.organization_id)
        .where(OrganizationPreferences.organization_id.in_(ids))
    )
    if provider_id:
        # A deactivated provider stops lending its look; the organization's own row still counts.
        query = query.where(
            (Organization.id == organization_id) | (Organization.is_active == True)  # noqa: E712
        )
    rows = {row[0]: row for row in (await db.execute(query)).all()}
    layers: List[Layer] = []
    for kind, org_id in (("provider", provider_id), ("organization", organization_id)):
        row = rows.get(org_id) if org_id else None
        if row is not None:
            settings = row[3] if with_settings else {"_": True}
            layers.append(Layer(kind, settings, row[1], row[2], org_id))
    return layers


async def org_branding(db: AsyncSession, organization_id: int, *, with_settings: bool = True) -> Resolved:
    layers = await _deployment_layer(db, with_settings=with_settings)
    layers += await _org_layers(db, organization_id, with_settings=with_settings)
    return resolve(f"organization:{organization_id}", layers)


async def deployment_branding(db: AsyncSession, *, with_settings: bool = True) -> Resolved:
    return resolve("deployment", await _deployment_layer(db, with_settings=with_settings))


async def account_branding(
    db: AsyncSession, email: Optional[str], *, admin_only: bool, with_settings: bool = True,
) -> AccountBranding:
    organization_id = None if admin_only else await subject_organization_id(db, email)
    if organization_id is None:
        return AccountBranding(0, "deployment", await deployment_branding(db, with_settings=with_settings))
    return AccountBranding(
        organization_id, "organization", await org_branding(db, organization_id, with_settings=with_settings),
    )


async def own_row(db: AsyncSession, organization_id: int) -> Optional[OrganizationPreferences]:
    return (await db.execute(
        select(OrganizationPreferences).where(OrganizationPreferences.organization_id == organization_id)
    )).scalar_one_or_none()


async def followers_count(db: AsyncSession, organization_id: int) -> int:
    return int((await db.execute(
        select(func.count(Organization.id)).where(
            Organization.provider_organization_id == organization_id,
            Organization.is_active == True,  # noqa: E712
        )
    )).scalar_one())


def _sections(settings: Dict[str, Any]) -> List[str]:
    return sorted(key for key in settings if key != "lastModified")


async def save_org_branding(
    db: AsyncSession, organization_id: int, settings: Dict[str, Any], actor: str,
) -> OrganizationPreferences:
    """Validate and store the organization's own row. Raises BrandingInvalid before any write."""
    clean = validate_branding(settings)
    row = await own_row(db, organization_id)
    created = row is None
    if row is None:
        row = OrganizationPreferences(organization_id=organization_id, settings=clean, updated_by=actor)
        db.add(row)
    else:
        row.settings = clean
        row.updated_by = actor
    await db.commit()
    await db.refresh(row)
    get_activity_logger().log_activity(
        action="organization.branding.update",
        target_type="organization",
        target_id=str(organization_id),
        target_organization_id=organization_id,
        details={"sections": _sections(clean), "created": created},
    )
    return row


async def reset_org_branding(db: AsyncSession, organization_id: int) -> bool:
    """Delete the organization's own row, so it shows its provider's or the deployment's again."""
    row = await own_row(db, organization_id)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    get_activity_logger().log_activity(
        action="organization.branding.reset",
        target_type="organization",
        target_id=str(organization_id),
        target_organization_id=organization_id,
    )
    return True


async def deployment_row(db: AsyncSession) -> Optional[DeploymentBranding]:
    return (await db.execute(select(DeploymentBranding).where(DeploymentBranding.id == 1))).scalar_one_or_none()


async def save_deployment_branding(db: AsyncSession, settings: Dict[str, Any], actor: str) -> DeploymentBranding:
    clean = validate_branding(settings)
    row = await deployment_row(db)
    if row is None:
        row = DeploymentBranding(id=1, settings=clean, updated_by=actor)
        db.add(row)
    else:
        row.settings = clean
        row.updated_by = actor
    await db.commit()
    await db.refresh(row)
    get_activity_logger().log_activity(
        action="deployment.branding.update",
        target_type="deployment",
        target_id="branding",
        details={"sections": _sections(clean)},
    )
    return row


async def reset_deployment_branding(db: AsyncSession) -> bool:
    row = await deployment_row(db)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    get_activity_logger().log_activity(
        action="deployment.branding.reset", target_type="deployment", target_id="branding",
    )
    return True


def stamp(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def inherited_layers(resolved: Resolved) -> Tuple[Layer, ...]:
    return tuple(layer for layer in resolved.layers if layer.kind != "organization")
