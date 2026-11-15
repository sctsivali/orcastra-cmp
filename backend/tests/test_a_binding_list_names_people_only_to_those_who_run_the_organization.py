"""GET /organizations/{id}/clusters names the registrant and the granting administrator only to an
administrator or a partner who runs the organization.

Seeing an organization takes any member row, and a buyer's founder is a partner of the buyer, so
a partner who only belongs to an organization read the emails of whoever registered or granted its
clusters. The binding kind in Synced Organizations already says how each binding came to be.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from app.core.auth import AuthenticatedUser
from app.routers import organizations
from app.services.organization_service import OrganizationService

ROWS = [SimpleNamespace(id=1, organization_id=7, cluster_id="lent", registered_by=None, granted_by="ops@example.test"),
        SimpleNamespace(id=2, organization_id=7, cluster_id="own", registered_by="seller@example.test", granted_by=None)]
PARTNER = AuthenticatedUser(user_id="role_partner:p@example.test", email="p@example.test", groups=["role_partner"])
ADMIN = AuthenticatedUser(user_id="role_admin:ops@example.test", email="ops@example.test", groups=["role_admin"])


@pytest.fixture
def org(monkeypatch) -> dict:
    state = {"runs": False}

    async def has_access(self: Any, *_a: Any) -> bool:
        return True

    async def rows(self: Any, _org_id: int):
        return ROWS

    async def runs(*_a: Any) -> bool:
        return state["runs"]

    monkeypatch.setattr(OrganizationService, "user_has_access", has_access)
    monkeypatch.setattr(OrganizationService, "list_clusters", rows)
    monkeypatch.setattr(organizations, "user_runs_org", runs)
    return state


async def test_a_member_partner_reads_no_emails(org) -> None:
    answer = await organizations.list_cluster_bindings(7, PARTNER, object())
    assert [(b.cluster_id, b.registered_by, b.granted_by) for b in answer] == [("lent", None, None), ("own", None, None)]


async def test_a_partner_who_runs_it_and_an_admin_read_them(org) -> None:
    org["runs"] = True
    answer = await organizations.list_cluster_bindings(7, PARTNER, object())
    assert answer[0].granted_by == "ops@example.test" and answer[1].registered_by == "seller@example.test"
    org["runs"] = False
    answer = await organizations.list_cluster_bindings(7, ADMIN, object())
    assert answer[0].granted_by == "ops@example.test"
