"""A cluster reached only through a sale is reached only while that sale is live.

Provisioning binds the seller's cluster to the buyer's organization (registered_by NULL). A buyer
founder that OrcaHub promoted to role_partner then saw the cluster through that binding for as
long as it stood, long after the term and the retention window ended. The binding now counts only
while the organization holds a live access rule on the cluster.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core import org_scope
from app.models.db_models import (
    Organization,
    OrganizationCluster,
    OrganizationMember,
    TenantAccessPolicy,
    TenantPolicyRule,
)
from tests._sqlite_session import org_tables, sqlite_session, with_json_tables

FOUNDER = "founder@buyer.test"
NOW = datetime.now(timezone.utc)


async def _world(access_ends_at=None, *, with_rule: bool = True, registered_by=None, source="orcahub",
                 created_by="system"):
    db = with_json_tables(sqlite_session(*org_tables()), TenantAccessPolicy, TenantPolicyRule)
    db.add(Organization(id=7, name="Buyer", slug="buyer", source=source, external_id="ext-buyer",
                        created_by=created_by, is_active=True))
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=7, user_email=FOUNDER, role="partner"),
        OrganizationCluster(organization_id=7, cluster_id="sale-cluster", registered_by=registered_by),
    ])
    if with_rule:
        db.add(TenantAccessPolicy(id=70, organization_id=7, name="auto:shop", source="orcahub",
                                  created_by="orcahub-provisioning", access_ends_at=access_ends_at))
        await db.commit()
        db.add(TenantPolicyRule(policy_id=70, cluster_id="sale-cluster", project_name="shop",
                                instance_name="*", permissions=["view"]))
    await db.commit()
    return db


async def test_a_live_sale_keeps_the_cluster() -> None:
    db = await _world(access_ends_at=NOW + timedelta(days=10))
    assert await org_scope._query_owned_clusters(FOUNDER, db) == {"sale-cluster"}


@pytest.mark.parametrize(
    "kwargs",
    [{"access_ends_at": NOW - timedelta(minutes=1)}, {"with_rule": False}],
    ids=["access-ended", "rule-revoked"],
)
async def test_an_ended_sale_takes_the_cluster_away(kwargs) -> None:
    db = await _world(**kwargs)
    assert await org_scope._query_owned_clusters(FOUNDER, db) == set()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"registered_by": "someone@seller.test"},
        {"source": "local"},
        {"created_by": FOUNDER},
    ],
    ids=["registered-binding", "local-organization", "organization-creator"],
)
async def test_only_a_sale_binding_is_scoped_to_its_sale(kwargs) -> None:
    db = await _world(with_rule=False, **kwargs)
    assert await org_scope._query_owned_clusters(FOUNDER, db) == {"sale-cluster"}


async def test_a_local_rule_the_founder_wrote_does_not_keep_the_sale_alive() -> None:
    """Security review 2026-10-27: only the purchase's own (OrcaHub) policies count."""
    db = await _world(access_ends_at=NOW - timedelta(minutes=1))
    db.add(TenantAccessPolicy(id=71, organization_id=7, name="founder-rule", source="local", created_by=FOUNDER))
    await db.commit()
    db.add(TenantPolicyRule(policy_id=71, cluster_id="sale-cluster", project_name="shop",
                            instance_name="*", permissions=["view", "operate"]))
    await db.commit()
    assert await org_scope._query_owned_clusters(FOUNDER, db) == set()
