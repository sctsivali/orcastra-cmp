"""A rule written in a buyer organization on a cluster it reached by purchase ends with the purchase.

Only OrcaHub's own policies carry the paid term, so a local rule a founder wrote while the term ran
went on granting after it ended (security review 2026-10-27). Such a rule now follows the phase of
the purchase on that cluster, and a new one may be written only by the seller or an Administrator.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.core.sale_term import sold_to_without_registering
from app.models.db_models import (
    Organization,
    OrganizationCluster,
    OrganizationMember,
    TenantAccessPolicy,
    TenantPolicyAssignment,
    TenantPolicyRule,
)
from tests._sqlite_session import org_tables, sqlite_session, with_json_tables

TENANT = AuthenticatedUser(user_id="t", email="tenant@buyer.test", groups=["role_tenant"])
NOW = datetime.now(timezone.utc)


async def _world(*, purchase=(None, None), registered_by=None, buyer_source="orcahub"):
    term_ended_at, access_ends_at = purchase
    db = with_json_tables(sqlite_session(*org_tables()), TenantAccessPolicy, TenantPolicyRule)
    db.add(Organization(id=7, name="Buyer", slug="buyer", source=buyer_source, created_by="minter@seller.test"))
    db.add(Organization(id=5, name="Seller", slug="seller", source="local", created_by="ops@seller.test"))
    await db.commit()
    member = OrganizationMember(organization_id=7, user_email="tenant@buyer.test", role="tenant")
    db.add_all([
        member,
        OrganizationMember(organization_id=5, user_email="ops@seller.test", role="member"),
        OrganizationCluster(organization_id=7, cluster_id="sold", registered_by=registered_by),
        OrganizationCluster(organization_id=5, cluster_id="sold", registered_by="ops@seller.test"),
        TenantAccessPolicy(id=70, organization_id=7, name="auto:shop", source="orcahub", created_by="orcahub",
                           term_ended_at=term_ended_at, access_ends_at=access_ends_at),
        TenantAccessPolicy(id=71, organization_id=7, name="founder-rule", source="local", created_by="founder@buyer.test"),
    ])
    await db.commit()
    db.add_all([
        TenantPolicyRule(policy_id=70, cluster_id="sold", project_name="shop", instance_name="*", permissions=["view"]),
        TenantPolicyRule(policy_id=71, cluster_id="sold", project_name="extra", instance_name="*",
                         permissions=["view", "operate", "console"]),
        TenantPolicyAssignment(policy_id=71, org_member_id=member.id, assigned_by="founder@buyer.test"),
    ])
    await db.commit()
    return db


async def _local_grant(db):
    grants = await org_scope.get_tenant_policy_grants(TENANT, db)
    return next((g for g in grants if g.policy_name == "founder-rule"), None)


async def test_while_the_purchase_runs_the_local_rule_grants_as_written() -> None:
    grant = await _local_grant(await _world())
    assert grant is not None and set(grant.permissions) == {"view", "operate", "console"}


async def test_in_retention_the_local_rule_is_view_only_with_the_purchase_dates() -> None:
    ends = NOW + timedelta(days=3)
    grant = await _local_grant(await _world(purchase=(NOW - timedelta(days=1), ends)))
    assert grant is not None and set(grant.permissions) == {"view"}
    assert grant.read_only is True
    assert grant.access_ends_at.replace(tzinfo=timezone.utc) == ends


async def test_after_the_purchase_ends_the_local_rule_grants_nothing() -> None:
    assert await _local_grant(await _world(purchase=(NOW - timedelta(days=9), NOW - timedelta(days=1)))) is None


@pytest.mark.parametrize("kwargs", [
    {"registered_by": "ops@seller.test"},
    {"buyer_source": "local"},
], ids=["registered-binding", "local-organization"])
async def test_a_binding_that_is_not_a_purchase_is_untouched(kwargs) -> None:
    grant = await _local_grant(await _world(purchase=(NOW - timedelta(days=9), NOW - timedelta(days=1)), **kwargs))
    assert grant is not None and "operate" in grant.permissions


async def test_only_the_seller_side_may_write_a_rule_on_a_sold_cluster() -> None:
    db = await _world()
    assert await sold_to_without_registering(db, 7, ["sold"], "founder@buyer.test") == {"sold"}
    assert await sold_to_without_registering(db, 7, ["sold"], "OPS@seller.test") == set()
    assert await sold_to_without_registering(db, 5, ["sold"], "founder@buyer.test") == set(), "not a purchase there"


async def test_the_policy_routes_refuse_the_founder_and_let_an_administrator_through() -> None:
    from fastapi import HTTPException

    from app.routers import tenant_access

    db = await _world()
    founder = AuthenticatedUser(user_id="f", email="founder@buyer.test", groups=["role_partner"])
    admin = AuthenticatedUser(user_id="a", email="admin@op.test", groups=["role_admin"])
    with pytest.raises(HTTPException) as refused:
        await tenant_access._refuse_rules_a_purchase_does_not_pay_for(db, founder, 7, ["sold"])
    assert refused.value.status_code == 403 and "purchase" in refused.value.detail
    await tenant_access._refuse_rules_a_purchase_does_not_pay_for(db, admin, 7, ["sold"])
