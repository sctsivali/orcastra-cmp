"""The owner-set invariants every ownership test ends on.

I1: a claimed organization's primary (organizations.api_key_id) is in its owner set.
I2: an organization with owner rows has a primary, so no claimed organization is left unowned.
"""

from typing import Any

from sqlalchemy import select

from app.models.db_models import Organization
from app.services.organization_owners import owners_of


async def assert_owner_invariants(db: Any) -> None:
    rows = (await db.execute(select(Organization.id, Organization.slug, Organization.api_key_id))).all()
    owners = await owners_of(db, [org_id for org_id, _slug, _primary in rows])
    for org_id, slug, primary in rows:
        keys = {row.api_key_id for row in owners.get(org_id, [])}
        if primary:
            assert primary in keys, f"{slug}: primary {primary} is not in its owner set {sorted(keys)}"
        else:
            assert not keys, f"{slug}: owner rows {sorted(keys)} without a primary"
