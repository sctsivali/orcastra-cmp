"""A tenant reading its organization learns its own membership and its own clusters, nothing more.

On an OrcaHub buyer organization the roster names every other buyer and the seller's staff, the
creator is the seller's key minter, and the cluster bindings include sales that already ended.
None of that is the tenant's. A partner and an administrator still read all of it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List

import pytest

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.routers import organizations

ME = "buyer@example.test"


def _user(group: str, email: str = ME) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=f"{group}:{email}", email=email, groups=[group])


class _Svc:
    def __init__(self, _db: Any) -> None:
        pass

    async def user_has_access(self, *_args) -> bool:
        return True

    async def list_members(self, _org_id: int) -> List[Any]:
        return [
            SimpleNamespace(id=1, organization_id=7, user_email="BUYER@example.test", role="tenant", invited_by=None),
            SimpleNamespace(id=2, organization_id=7, user_email="other-buyer@example.test", role="tenant", invited_by=None),
            SimpleNamespace(id=3, organization_id=7, user_email="staff@seller.test", role="partner", invited_by=None),
        ]

    async def list_clusters(self, _org_id: int) -> List[Any]:
        return [
            SimpleNamespace(id=10, organization_id=7, cluster_id="nuc2", registered_by="ops@seller.test"),
            SimpleNamespace(id=11, organization_id=7, cluster_id="sold-01", registered_by=None),
        ]

    async def get_organization(self, _org_id: int) -> Any:
        return SimpleNamespace(id=7, name="Buyer", slug="buyer", description=None, created_by="minter@seller.test",
                               is_active=True, members=[], clusters=[])


@pytest.fixture(autouse=True)
def svc(monkeypatch):
    monkeypatch.setattr(organizations, "OrganizationService", _Svc)

    async def allowed(_user, _db):
        return {"nuc2", "sold-01"}

    async def scope(_user, _db):
        return {"nuc2": {("shop", "*"): {"view"}}}, [], None

    monkeypatch.setattr(org_scope, "get_user_allowed_clusters", allowed)
    monkeypatch.setattr(org_scope, "get_tenant_scope", scope)


async def test_a_tenant_reads_only_its_own_membership() -> None:
    members = await organizations.list_members(7, user=_user("role_tenant"), db=None)
    assert [m.user_email for m in members] == ["BUYER@example.test"]


async def test_a_tenant_reads_only_the_clusters_its_grants_reach_and_not_who_registered_them() -> None:
    bindings = await organizations.list_cluster_bindings(7, user=_user("role_tenant"), db=None)
    assert [(b.cluster_id, b.registered_by) for b in bindings] == [("nuc2", None)]


async def test_a_tenant_is_not_told_who_created_the_organization() -> None:
    org = await organizations.get_organization(7, user=_user("role_tenant"), db=None)
    assert org.created_by == ""


async def test_a_partner_still_reads_the_whole_organization(monkeypatch) -> None:
    async def runs(*_a) -> bool:
        return True

    # Every binding is listed to a partner; who registered or granted it only to one who runs the
    # organization (test_a_binding_list_names_people_only_to_those_who_run_the_organization.py).
    monkeypatch.setattr(organizations, "user_runs_org", runs)
    partner = _user("role_partner", "staff@seller.test")
    assert len(await organizations.list_members(7, user=partner, db=None)) == 3
    assert len(await organizations.list_cluster_bindings(7, user=partner, db=None)) == 2
    assert (await organizations.get_organization(7, user=partner, db=None)).created_by == "minter@seller.test"
