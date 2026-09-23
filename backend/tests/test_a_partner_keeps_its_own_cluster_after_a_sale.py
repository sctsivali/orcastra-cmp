"""A partner's first sale must not bound the partner on its own cluster.

Provisioning binds every buyer organization to the partner's cluster, so a partner cluster with one
sale carries two bindings. The project bound counted bindings, and every gate built on it (power,
console, delete, metrics, tags, file push, project edits, uploads, operations, sessions) then
refused the partner's own projects and its buyers' projects. The bound now applies only where
another party holds the cluster. These run the real get_visible_projects on SQLite rows.
"""

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

from app.core.auth import AuthenticatedUser
from app.core.org_scope import get_visible_projects
from app.models.db_models import (
    Organization,
    OrganizationCluster,
    OrganizationMember,
    TenantAccessPolicy,
    TenantPolicyAssignment,
    TenantPolicyRule,
)
from tests._sqlite_session import sqlite_session


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(_element, _compiler, **_kw):  # the policy tables carry a JSONB column
    return "JSON"


PARTNER = AuthenticatedUser(user_id="p", email="partner@example.test", groups=["role_partner"])


async def _world():
    db = sqlite_session(Organization, OrganizationMember, OrganizationCluster, TenantAccessPolicy, TenantPolicyRule,
                        TenantPolicyAssignment)
    db.add_all([
        # the partner's home org and a buyer its key provisioned: one party
        Organization(id=1, name="home", slug="home", created_by="system", is_active=True, source="orcahub", api_key_id="oak_a"),
        Organization(id=2, name="buyer", slug="buyer", created_by="system", is_active=True, source="orcahub", api_key_id="oak_a"),
        # another partner's org
        Organization(id=3, name="other", slug="other", created_by="system", is_active=True, source="orcahub", api_key_id="oak_b"),
    ])
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=1, user_email=PARTNER.email, role="partner"),
        OrganizationCluster(organization_id=1, cluster_id="own", registered_by=PARTNER.email),
        OrganizationCluster(organization_id=2, cluster_id="own"),
        OrganizationCluster(organization_id=1, cluster_id="mixed"),
        OrganizationCluster(organization_id=3, cluster_id="mixed"),
    ])
    await db.commit()
    return db


async def test_a_sale_does_not_bound_the_partner_on_its_own_cluster() -> None:
    db = await _world()
    bound = await get_visible_projects(PARTNER, db)
    assert bound is not None and "own" not in bound, "the partner's own cluster with a buyer is not shared"


async def test_a_cluster_another_partner_holds_keeps_the_bound() -> None:
    db = await _world()
    bound = await get_visible_projects(PARTNER, db)
    assert bound == {"mixed": {"default"}}


@pytest.mark.parametrize("extra_binding", [None, 3])
async def test_nothing_is_bounded_when_no_other_party_holds_anything(extra_binding) -> None:
    db = sqlite_session(Organization, OrganizationMember, OrganizationCluster, TenantAccessPolicy, TenantPolicyRule,
                        TenantPolicyAssignment)
    db.add_all([
        Organization(id=1, name="home", slug="home", created_by="system", is_active=True, api_key_id="oak_a"),
        Organization(id=2, name="buyer", slug="buyer", created_by="system", is_active=True, api_key_id="oak_a"),
        Organization(id=3, name="buyer2", slug="buyer2", created_by="system", is_active=True, api_key_id="oak_a"),
    ])
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=1, user_email=PARTNER.email, role="partner"),
        OrganizationCluster(organization_id=1, cluster_id="own", registered_by=PARTNER.email),
        OrganizationCluster(organization_id=2, cluster_id="own"),
    ] + ([OrganizationCluster(organization_id=extra_binding, cluster_id="own")] if extra_binding else []))
    await db.commit()
    assert await get_visible_projects(PARTNER, db) is None
