"""A buyer founder who reaches cluster `sold` only through an OrcaHub purchase, on in-memory SQLite.

Shared by the suites that hold a founder's unattended access (keys, schedules) to the purchase's
paid term (core/sale_term.py). `own` is a cluster the founder registered itself, which no term
touches.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models.db_models import (
    Organization,
    OrganizationCluster,
    OrganizationMember,
    TenantAccessPolicy,
    TenantPolicyRule,
)
from tests._sqlite_session import org_tables, sqlite_session, with_json_tables

FOUNDER = "Founder@Buyer.test"
NOW = datetime.now(timezone.utc)
PAST, FUTURE = NOW - timedelta(days=1), NOW + timedelta(days=5)

# (term_ended_at, access_ends_at) of the purchase's policy per phase.
ACTIVE_TERM = {"term_ended_at": None, "access_ends_at": None}
RETENTION_TERM = {"term_ended_at": PAST, "access_ends_at": FUTURE}
ENDED_TERM = {"term_ended_at": PAST - timedelta(days=2), "access_ends_at": PAST}


async def purchase_world(*, term_ended_at=PAST, access_ends_at=FUTURE, registered_elsewhere=False):
    db = with_json_tables(sqlite_session(*org_tables()), TenantAccessPolicy, TenantPolicyRule)
    db.add(Organization(id=7, name="Buyer", slug="buyer", source="orcahub", created_by="minter@seller.test"))
    db.add(Organization(id=8, name="Founder's own", slug="own", source="local", created_by="x@y.test"))
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=7, user_email="founder@buyer.test", role="partner"),
        OrganizationMember(organization_id=8, user_email="founder@buyer.test", role="partner"),
        OrganizationCluster(organization_id=7, cluster_id="sold", registered_by=None),
        OrganizationCluster(organization_id=8, cluster_id="own", registered_by="founder@buyer.test"),
        TenantAccessPolicy(id=70, organization_id=7, name="auto:shop", source="orcahub",
                           created_by="orcahub-provisioning", term_ended_at=term_ended_at, access_ends_at=access_ends_at),
    ])
    if registered_elsewhere:
        db.add(OrganizationCluster(organization_id=8, cluster_id="sold", registered_by="x@y.test"))
    await db.commit()
    db.add(TenantPolicyRule(policy_id=70, cluster_id="sold", project_name="shop", instance_name="*",
                            permissions=["view", "operate", "console"]))
    await db.commit()
    return db
