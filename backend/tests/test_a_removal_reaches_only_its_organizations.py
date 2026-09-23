"""A member removal is told only to the OrcaHub organizations the user actually left.

push_member_removal_to_orcahub selected every source='orcahub' organization and posted the
removed email to each one's callback, so every integration, including one whose app_url points at
a planted endpoint, learned who was removed anywhere. The credential read behind every callback
also ran two blocking Vault reads on the event loop and ignored a revoked key.
"""

import threading
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.core.auth import AuthenticatedUser
from app.models.db_models import Organization, OrganizationMember
from app.routers import user_groups
from app.services import orcahub_sync
from tests._sqlite_session import org_tables, sqlite_session


class _Counting:
    def __init__(self) -> None:
        self.statements: List[Any] = []

    async def execute(self, stmt: Any, *_a: Any, **_k: Any) -> Any:
        self.statements.append(stmt)

        class _R:
            def scalars(self):
                return self

            def all(self):
                return [SimpleNamespace(id=7)]

        return _R()


@pytest.fixture
def pushes(monkeypatch):
    calls: List[Dict[str, Any]] = []

    async def spy(db: Any, org_id: int, members: List[Dict[str, Any]]) -> bool:
        calls.append({"org_id": org_id, "members": members})
        return True

    monkeypatch.setattr(orcahub_sync, "push_members_to_orcahub", spy)
    return calls


async def test_an_empty_set_makes_no_query_and_no_push(pushes):
    db = _Counting()
    assert await orcahub_sync.push_member_removal_to_orcahub(db, "tenant@example.test", set()) is True
    assert db.statements == []
    assert pushes == []


async def test_the_query_is_limited_to_the_orgs_the_user_left(pushes):
    db = _Counting()
    await orcahub_sync.push_member_removal_to_orcahub(db, "tenant@example.test", {7})
    sql = str(db.statements[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "organizations.id IN (7)" in sql
    assert "organizations.source = 'orcahub'" in sql
    assert "organizations.api_key_id IS NOT NULL" in sql
    assert pushes == [{"org_id": 7, "members": [{"email": "tenant@example.test", "action": "remove"}]}]


async def _seeded():
    db = sqlite_session(*org_tables())
    db.add_all([
        Organization(id=7, name="Mine", slug="mine", source="orcahub", external_id="e7",
                     api_key_id="oak_test", created_by="partner@example.test"),
        Organization(id=8, name="Theirs", slug="theirs", source="orcahub", external_id="e8",
                     api_key_id="oak_other", created_by="system"),
    ])
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=7, user_email="tenant@example.test", role="tenant"),
        OrganizationMember(organization_id=8, user_email="tenant@example.test", role="tenant"),
    ])
    await db.commit()
    return db


class _NoAuthentik:
    async def remove_user_from_all_roles(self, *_a: Any) -> bool:
        return True


@pytest.fixture
def removal_spy(monkeypatch):
    seen: List[set] = []

    async def spy(db: Any, email: str, organization_ids: Any) -> bool:
        seen.append(set(organization_ids))
        return True

    monkeypatch.setattr(orcahub_sync, "push_member_removal_to_orcahub", spy)
    monkeypatch.setattr(user_groups, "get_authentik_sync", lambda: _NoAuthentik())
    return seen


async def test_a_partner_removal_passes_only_the_partners_orgs(removal_spy):
    db = await _seeded()
    partner = AuthenticatedUser(user_id="p", email="partner@example.test", groups=["role_partner"])
    await user_groups.remove_user_from_all("tenant@example.test", user=partner, db=db)
    assert removal_spy == [{7}]
    left = (await db.execute(select(OrganizationMember.organization_id))).scalars().all()
    assert left == [8]


async def test_an_admin_removal_passes_every_org_it_emptied(removal_spy):
    db = await _seeded()
    admin = AuthenticatedUser(user_id="a", email="admin@example.test", groups=["role_admin"])
    await user_groups.remove_user_from_all("tenant@example.test", user=admin, db=db)
    assert removal_spy == [{7, 8}]


async def test_a_removal_that_removed_nothing_says_so(removal_spy):
    from fastapi import HTTPException

    db = await _seeded()
    partner = AuthenticatedUser(user_id="p", email="partner@example.test", groups=["role_partner"])
    with pytest.raises(HTTPException) as caught:
        await user_groups.remove_user_from_all("nobody@example.test", user=partner, db=db)
    assert caught.value.status_code == 404
    assert "not a member of any organization you run" in caught.value.detail
    assert removal_spy == []


async def test_a_buyer_partner_cannot_remove_from_the_sellers_org_it_only_buys_from(removal_spy):
    """The list shows a partner every org it belongs to, a tenant row included. The removal is not a silent 200."""
    from fastapi import HTTPException

    db = await _seeded()
    db.add(Organization(id=20, name="Seller", slug="seller", source="local", created_by="seller@example.test"))
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=20, user_email="partner@example.test", role="tenant"),
        OrganizationMember(organization_id=20, user_email="target@example.test", role="tenant"),
    ])
    await db.commit()
    partner = AuthenticatedUser(user_id="p", email="partner@example.test", groups=["role_partner"])
    with pytest.raises(HTTPException) as caught:
        await user_groups.remove_user_from_all("target@example.test", user=partner, db=db)
    assert caught.value.status_code == 404
    left = (await db.execute(
        select(OrganizationMember.organization_id).where(OrganizationMember.user_email == "target@example.test")
    )).scalars().all()
    assert left == [20]


async def test_a_stale_admin_row_does_not_run_its_org_for_the_list_removal(removal_spy):
    """The member routes and the user list agree: an admin row held by a non-admin runs nothing."""
    from fastapi import HTTPException

    db = await _seeded()
    db.add(Organization(id=30, name="Run", slug="run", source="local", created_by="founder@example.test"))
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=30, user_email="partner@example.test", role="admin"),
        OrganizationMember(organization_id=30, user_email="target@example.test", role="tenant"),
    ])
    await db.commit()
    partner = AuthenticatedUser(user_id="p", email="partner@example.test", groups=["role_partner"])
    with pytest.raises(HTTPException) as caught:
        await user_groups.remove_user_from_all("target@example.test", user=partner, db=db)
    assert caught.value.status_code == 404
    left = (await db.execute(
        select(OrganizationMember.organization_id).where(OrganizationMember.user_email == "target@example.test")
    )).scalars().all()
    assert left == [30]


async def test_a_partner_cannot_remove_a_co_partner_from_an_org_orcahub_manages(removal_spy):
    from fastapi import HTTPException

    db = await _seeded()
    db.add(OrganizationMember(organization_id=7, user_email="copartner@example.test", role="partner"))
    await db.commit()
    partner = AuthenticatedUser(user_id="p", email="partner@example.test", groups=["role_partner"])
    with pytest.raises(HTTPException) as caught:
        await user_groups.remove_user_from_all("copartner@example.test", user=partner, db=db)
    assert caught.value.status_code == 409
    assert "OrcaHub" in caught.value.detail and "mine" in caught.value.detail
    assert removal_spy == []
    left = (await db.execute(
        select(OrganizationMember.organization_id).where(OrganizationMember.user_email == "copartner@example.test")
    )).scalars().all()
    assert left == [7], "nothing was removed"


async def test_a_mixed_target_loses_the_rows_it_may_and_keeps_the_synced_partner_row(removal_spy):
    """A co-partner in an org OrcaHub manages, and a tenant in the caller's local org."""
    db = await _seeded()
    db.add(Organization(id=9, name="Local", slug="local", source="local", created_by="partner@example.test"))
    await db.commit()
    db.add_all([
        OrganizationMember(organization_id=7, user_email="copartner@example.test", role="partner"),
        OrganizationMember(organization_id=9, user_email="copartner@example.test", role="tenant"),
    ])
    await db.commit()
    partner = AuthenticatedUser(user_id="p", email="partner@example.test", groups=["role_partner"])
    answer = await user_groups.remove_user_from_all("copartner@example.test", user=partner, db=db)
    assert answer["kept"] == ["mine"]
    assert "OrcaHub" in answer["detail"] and "mine" in answer["detail"]
    left = (await db.execute(
        select(OrganizationMember.organization_id).where(OrganizationMember.user_email == "copartner@example.test")
    )).scalars().all()
    assert left == [7]
    assert removal_spy == [{9}], "only the tenant row that was removed is sent"


async def test_a_partner_still_removes_a_co_partner_from_a_local_org(removal_spy):
    db = await _seeded()
    db.add(Organization(id=9, name="Local", slug="local", source="local", created_by="partner@example.test"))
    await db.commit()
    db.add(OrganizationMember(organization_id=9, user_email="copartner@example.test", role="partner"))
    await db.commit()
    partner = AuthenticatedUser(user_id="p", email="partner@example.test", groups=["role_partner"])
    await user_groups.remove_user_from_all("copartner@example.test", user=partner, db=db)
    assert removal_spy == [set()], "a partner row is never sent to OrcaHub by a non-admin"
    left = (await db.execute(
        select(OrganizationMember.organization_id).where(OrganizationMember.user_email == "copartner@example.test")
    )).scalars().all()
    assert left == []


async def test_an_admin_removing_a_partner_is_sent_to_orcahub(removal_spy):
    db = await _seeded()
    db.add(OrganizationMember(organization_id=7, user_email="copartner@example.test", role="partner"))
    await db.commit()
    admin = AuthenticatedUser(user_id="a", email="admin@example.test", groups=["role_admin"])
    await user_groups.remove_user_from_all("copartner@example.test", user=admin, db=db)
    assert removal_spy == [{7}]


class _FakeVault:
    def __init__(self, metadata: Dict[str, Any], secret: Dict[str, Any]) -> None:
        self.metadata = metadata
        self.secret = secret
        self.paths: List[str] = []

    def vault_read(self, path: str) -> Dict[str, Any]:
        self.paths.append(path)
        return self.metadata if path.endswith("/metadata") else self.secret


def _org() -> SimpleNamespace:
    return SimpleNamespace(id=7, source="orcahub", api_key_id="oak_test")


async def test_credentials_are_read_off_the_loop(monkeypatch):
    threads: List[int] = []

    def blocking(api_key_id: str) -> Dict[str, str]:
        threads.append(threading.get_ident())
        return {"app_url": "https://example.test", "api_key_id": api_key_id, "api_key_secret": "s"}

    monkeypatch.setattr(orcahub_sync, "_read_orcahub_credentials_blocking", blocking)
    creds = await orcahub_sync._get_orcahub_credentials(_org())
    assert creds["api_key_id"] == "oak_test"
    assert threads and threads[0] != threading.get_ident()


@pytest.mark.parametrize("state", [
    {"is_revoked": True},
    {"is_active": False},
    {"expires_at": "2000-01-01T00:00:00"},
])
def test_a_dead_key_gets_no_credentials(monkeypatch, state):
    from app.routers import integrations

    vault = _FakeVault({"app_url": "https://example.test", **state}, {"raw": "secret"})
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)
    assert orcahub_sync._read_orcahub_credentials_blocking("oak_test") is None
    assert not any(p.endswith("/secret") for p in vault.paths), "the raw secret is never read for it"


def test_a_live_key_gets_its_credentials(monkeypatch):
    from app.routers import integrations

    vault = _FakeVault({"app_url": "https://example.test", "is_active": True}, {"raw": "secret"})
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)
    assert orcahub_sync._read_orcahub_credentials_blocking("oak_test") == {
        "app_url": "https://example.test", "api_key_id": "oak_test", "api_key_secret": "secret",
    }


class _Authentik:
    def __init__(self) -> None:
        self.cleared: List[str] = []

    async def get_user_role_groups(self, _email: str) -> set:
        return set()

    async def sync_user_role(self, *_a: Any) -> bool:
        return True

    async def remove_user_from_all_roles(self, email: str) -> bool:
        self.cleared.append(email)
        return True


@pytest.mark.parametrize("row_role,allowed", [("partner", True), ("admin", False), ("tenant", False), ("member", False)])
async def test_assign_needs_a_row_that_runs_the_org(monkeypatch, row_role: str, allowed: bool) -> None:
    from fastapi import HTTPException

    authentik = _Authentik()
    monkeypatch.setattr(user_groups, "get_authentik_sync", lambda: authentik)

    async def no_push(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(orcahub_sync, "push_members_to_orcahub", no_push)
    db = sqlite_session(*org_tables())
    db.add(Organization(id=40, name="Run", slug="run", source="local", created_by="founder@example.test"))
    await db.commit()
    db.add(OrganizationMember(organization_id=40, user_email="partner@example.test", role=row_role))
    await db.commit()
    partner = AuthenticatedUser(user_id="p", email="partner@example.test", groups=["role_partner"])
    body = user_groups.AssignRoleRequest(user_email="new@example.com", role="tenant", organization_id=40)
    if allowed:
        await user_groups.assign_user_role(body, user=partner, db=db)
        rows = (await db.execute(select(OrganizationMember.role).where(OrganizationMember.user_email == "new@example.com"))).scalars().all()
        assert rows == ["tenant"]
    else:
        with pytest.raises(HTTPException) as caught:
            await user_groups.assign_user_role(body, user=partner, db=db)
        assert caught.value.status_code == 403


async def test_an_admin_removing_a_user_with_no_rows_still_clears_the_role_groups(monkeypatch, removal_spy) -> None:
    authentik = _Authentik()
    monkeypatch.setattr(user_groups, "get_authentik_sync", lambda: authentik)
    db = await _seeded()
    admin = AuthenticatedUser(user_id="a", email="admin@example.test", groups=["role_admin"])
    answer = await user_groups.remove_user_from_all("orphan@example.test", user=admin, db=db)
    assert "removed" in answer["detail"]
    assert authentik.cleared == ["orphan@example.test"], "an admin removal must reach Authentik even with no org rows"
