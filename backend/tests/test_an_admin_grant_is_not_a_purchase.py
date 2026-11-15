"""A binding an administrator grants to an OrcaHub organization is a grant, not a purchase.

Provisioning binds a seller's cluster to a buyer's OrcaHub organization with no registrant, and
every reader treats that shape as a sale: the buyer's partners reach the cluster only while a live
OrcaHub policy names it, and keys their creators minted are held to the paid term. An administrator
binding an OrcaHub organization from the dashboard wrote the same shape, so the binding meant to let
that organization hold its own projects read as a purchase that had already ended. The bind now
records who granted it (organization_clusters.granted_by), and the sale readers skip granted rows.
A provisioned row (no registrant, no grantor) is still a sale.
"""

from typing import Any

import pytest

from app.core import org_scope, sale_term
from app.models.db_models import (
    Organization,
    OrganizationCluster,
    OrganizationMember,
    OrganizationOwnerKey,
    TenantAccessPolicy,
    TenantPolicyRule,
)
from app.services.organization_service import OrganizationService
from tests._sqlite_session import org_tables, sqlite_session, with_json_tables

PARTNER = "buyer-founder@example.test"
ADMIN = "ops@example.test"


async def _world(granted_by: Any) -> Any:
    db = with_json_tables(
        sqlite_session(Organization, OrganizationMember, OrganizationCluster, OrganizationOwnerKey),
        TenantAccessPolicy, TenantPolicyRule,
    )
    db.add_all([
        Organization(id=1, name="Synced", slug="synced", created_by="system", is_active=True, source="orcahub"),
        OrganizationMember(organization_id=1, user_email=PARTNER, role="partner"),
        OrganizationCluster(organization_id=1, cluster_id="lent", registered_by=None, granted_by=granted_by),
    ])
    await db.commit()
    return db


async def test_a_granted_binding_is_reached_outright() -> None:
    db = await _world(granted_by=ADMIN)
    full, sale_only = await sale_term._reach(db, PARTNER)
    assert "lent" in full and "lent" not in sale_only


async def test_a_provisioned_binding_is_still_a_sale() -> None:
    db = await _world(granted_by=None)
    full, sale_only = await sale_term._reach(db, PARTNER)
    assert "lent" not in full and sale_only == {"lent": {1}}


async def test_a_granted_binding_counts_in_the_partners_scope_without_a_live_purchase() -> None:
    assert "lent" in await org_scope._query_owned_clusters(PARTNER, await _world(granted_by=ADMIN))
    assert "lent" not in await org_scope._query_owned_clusters(PARTNER, await _world(granted_by=None))


async def test_a_rule_on_a_granted_binding_is_not_a_purchase_the_writer_did_not_sell() -> None:
    granted = await sale_term.sold_to_without_registering(await _world(granted_by=ADMIN), 1, ["lent"], PARTNER)
    sold = await sale_term.sold_to_without_registering(await _world(granted_by=None), 1, ["lent"], PARTNER)
    assert granted == set() and sold == {"lent"}


async def test_the_service_stores_the_grantor(monkeypatch: pytest.MonkeyPatch) -> None:
    async def safe(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr("app.services.organization_service.assert_cluster_safe_to_share", safe)
    db = sqlite_session(*org_tables())
    db.add(Organization(id=1, name="Synced", slug="synced", created_by="system", is_active=True, source="orcahub"))
    await db.commit()
    binding = await OrganizationService(db).bind_cluster(1, "lent", registered_by=None, granted_by=ADMIN)
    assert binding.granted_by == ADMIN and binding.registered_by is None
