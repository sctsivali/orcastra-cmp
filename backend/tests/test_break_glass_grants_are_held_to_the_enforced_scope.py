"""The break-glass grant list is held to the scope the request enforces.

It uses the set org_scope_dependency narrows by the Tenant ACL, not the org-derived
`get_user_allowed_clusters()`: otherwise a Tenant narrowed off a cluster of its organization reads
the grants there, with the reason, ticket, admin e-mail and key id. The list is also held to the
caller's organizations, so on a cluster several organizations hold it shows nothing from the others.
"""

import asyncio

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.routers import break_glass

TENANT = AuthenticatedUser(user_id="t", email="t@example.com", groups=["role_tenant"])
ADMIN = AuthenticatedUser(user_id="a", email="a@example.com", groups=["role_admin"])


def _list(user, enforced, monkeypatch, orgs=(6,)):
    seen = {}

    async def list_grants(_db, **kwargs):
        seen.update(kwargs)
        return []

    async def org_ids(_user, _db):
        return list(orgs)

    monkeypatch.setattr(break_glass.break_glass_service, "list_grants", list_grants)
    monkeypatch.setattr(break_glass, "get_user_org_ids", org_ids, raising=False)
    token = org_scope._allowed_clusters_var.set(enforced)
    try:
        asyncio.run(break_glass.list_grants(mine=False, user=user, db=object()))
    finally:
        org_scope._allowed_clusters_var.reset(token)
    return seen


def test_a_tenant_reads_grants_on_the_clusters_it_may_reach_for_its_organizations(monkeypatch) -> None:
    seen = _list(TENANT, {"kept"}, monkeypatch, orgs=(6, 480))
    assert seen["cluster_filter"] == {"kept"}
    assert seen["organization_filter"] == {6, 480}


def test_an_admin_reads_every_grant(monkeypatch) -> None:
    seen = _list(ADMIN, None, monkeypatch)
    assert seen["cluster_filter"] is None
    assert seen["organization_filter"] is None
