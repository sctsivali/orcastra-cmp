"""Organization personalization inheritance helpers."""

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import (
    Organization,
    OrganizationMember,
    OrganizationPreferences,
    UserPreferences,
)


@dataclass(frozen=True)
class OrgPreferencesPayload:
    organization_id: int
    settings: dict
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass(frozen=True)
class OrgPreferencesVersionPayload:
    organization_id: int
    updated_at: Optional[str] = None


def _isoformat(value) -> Optional[str]:
    return value.isoformat() if value else None


async def _get_primary_org_id(db: AsyncSession, user_email: str) -> Optional[int]:
    result = await db.execute(
        select(OrganizationMember.organization_id)
        .where(OrganizationMember.user_email == user_email)
        .order_by(OrganizationMember.joined_at.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _get_org_preferences(
    db: AsyncSession,
    organization_id: int,
) -> Optional[OrganizationPreferences]:
    result = await db.execute(
        select(OrganizationPreferences).where(
            OrganizationPreferences.organization_id == organization_id,
        )
    )
    return result.scalar_one_or_none()


async def _get_legacy_owner_user_preferences(
    db: AsyncSession,
    organization_id: int,
) -> Optional[UserPreferences]:
    org_result = await db.execute(
        select(Organization).where(Organization.id == organization_id)
    )
    org = org_result.scalar_one_or_none()
    if not org:
        return None

    candidate_emails: list[str] = []
    if org.created_by and org.created_by != "system":
        candidate_emails.append(org.created_by)

    partners_result = await db.execute(
        select(OrganizationMember.user_email)
        .where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.role == "partner",
        )
        .order_by(OrganizationMember.joined_at.asc())
    )
    for email in partners_result.scalars().all():
        if email not in candidate_emails:
            candidate_emails.append(email)

    for email in candidate_emails:
        prefs_result = await db.execute(
            select(UserPreferences)
            .where(UserPreferences.user_email == email)
            .order_by(UserPreferences.updated_at.desc())
            .limit(1)
        )
        prefs = prefs_result.scalar_one_or_none()
        if prefs and prefs.settings:
            return prefs

    return None


async def get_org_preferences_payload(
    db: AsyncSession,
    organization_id: int,
) -> OrgPreferencesPayload:
    prefs = await _get_org_preferences(db, organization_id)
    if prefs is not None:
        return OrgPreferencesPayload(
            organization_id=prefs.organization_id,
            settings=prefs.settings or {},
            updated_by=prefs.updated_by,
            updated_at=_isoformat(prefs.updated_at),
        )

    fallback = await _get_legacy_owner_user_preferences(db, organization_id)
    if fallback:
        return OrgPreferencesPayload(
            organization_id=organization_id,
            settings=fallback.settings or {},
            updated_by=fallback.user_email,
            updated_at=_isoformat(fallback.updated_at),
        )

    return OrgPreferencesPayload(organization_id=organization_id, settings={})


async def get_user_org_preferences_payload(
    db: AsyncSession,
    user_email: str,
) -> OrgPreferencesPayload:
    organization_id = await _get_primary_org_id(db, user_email)
    if organization_id is None:
        return OrgPreferencesPayload(organization_id=0, settings={})
    return await get_org_preferences_payload(db, organization_id)


async def get_org_preferences_version(
    db: AsyncSession,
    organization_id: int,
) -> OrgPreferencesVersionPayload:
    prefs = await _get_org_preferences(db, organization_id)
    if prefs is not None:
        return OrgPreferencesVersionPayload(
            organization_id=organization_id,
            updated_at=_isoformat(prefs.updated_at),
        )

    fallback = await _get_legacy_owner_user_preferences(db, organization_id)
    return OrgPreferencesVersionPayload(
        organization_id=organization_id,
        updated_at=_isoformat(fallback.updated_at) if fallback else None,
    )


async def get_user_org_preferences_version(
    db: AsyncSession,
    user_email: str,
) -> OrgPreferencesVersionPayload:
    organization_id = await _get_primary_org_id(db, user_email)
    if organization_id is None:
        return OrgPreferencesVersionPayload(organization_id=0, updated_at=None)
    return await get_org_preferences_version(db, organization_id)


async def reset_org_preferences_to_defaults(
    db: AsyncSession,
    organization_id: int,
    updated_by: str,
) -> None:
    prefs = await _get_org_preferences(db, organization_id)
    if prefs:
        prefs.settings = {}
        prefs.updated_by = updated_by
    else:
        db.add(OrganizationPreferences(
            organization_id=organization_id,
            settings={},
            updated_by=updated_by,
        ))
    await db.commit()