"""An integration key may grant a role, never demote one or touch role_admin.

The member sync called sync_user_role, which removes the user from every other role group
including role_admin, and a membership removal stripped every role group once the user had no
membership left. Any Partner could mint a key, sync a fresh organization, push an Admin into it
as TENANT and demote them. The reconcile also resolved each pushed org by external id alone, so
a key could purge another integration's roster.
"""

from typing import Any, List, Optional

import httpx
import pytest
from sqlalchemy import select

from app.models.db_models import Organization, OrganizationMember
from app.routers import integrations
from app.routers.integrations import SyncMember, SyncOrganization
from app.services import authentik_sync
from app.services.integration_roles import (
    ROLE_ADMIN,
    ROLE_PARTNER,
    ROLE_TENANT,
    decide_orphan_revoke,
    decide_upsert,
    membership_owners,
)
from tests._sqlite_session import org_tables, sqlite_session

KEY = "oak_test"
OTHER = "oak_other"


# --- decide_upsert --------------------------------------------------------------------------

@pytest.mark.parametrize("target", ["tenant", "partner"])
def test_an_admin_is_never_touched(target):
    change = decide_upsert(target, {ROLE_ADMIN}, [KEY], KEY)
    assert change.is_noop and change.reason == "holds_admin"


def test_a_partner_pushed_as_tenant_is_not_demoted():
    change = decide_upsert("tenant", {ROLE_PARTNER}, [KEY], KEY)
    assert change.is_noop and change.reason == "no_demotion"


@pytest.mark.parametrize("owners", [[OTHER], [KEY, OTHER], [KEY, None], [None], []])
def test_a_partner_grant_needs_every_org_under_this_key(owners):
    change = decide_upsert("partner", {ROLE_TENANT}, owners, KEY)
    assert change.is_noop and change.reason == "membership_outside_this_key"


def test_a_partner_grant_where_every_org_is_owned():
    change = decide_upsert("partner", {ROLE_TENANT}, [KEY, KEY], KEY)
    assert change.add == ROLE_PARTNER
    assert change.remove == frozenset({ROLE_TENANT})
    assert change.reason == "grant_partner"


def test_a_partner_grant_to_a_user_without_groups_removes_nothing():
    change = decide_upsert("partner", set(), [KEY], KEY)
    assert change.add == ROLE_PARTNER and change.remove == frozenset()


def test_a_user_without_groups_pushed_as_tenant_gets_role_tenant():
    change = decide_upsert("tenant", set(), [], KEY)
    assert change.add == ROLE_TENANT and not change.remove


def test_an_existing_tenant_is_left_alone():
    assert decide_upsert("tenant", {ROLE_TENANT}, [], KEY).reason == "already_tenant"


def test_an_existing_partner_pushed_as_partner_is_left_alone():
    assert decide_upsert("partner", {ROLE_PARTNER}, [OTHER], KEY).reason == "already_partner"


@pytest.mark.parametrize("target", ["tenant", "partner"])
def test_unknown_groups_change_nothing(target):
    change = decide_upsert(target, None, [KEY], KEY)
    assert change.is_noop and change.reason == "authentik_unknown"


# --- decide_orphan_revoke -------------------------------------------------------------------

def test_a_remaining_membership_keeps_everything():
    assert decide_orphan_revoke({ROLE_TENANT}, 1).reason == "still_a_member"


def test_unknown_groups_are_not_revoked():
    assert decide_orphan_revoke(None, 0).reason == "authentik_unknown"


@pytest.mark.parametrize("groups", [{ROLE_ADMIN}, {ROLE_PARTNER}, {ROLE_ADMIN, ROLE_TENANT}])
def test_an_orphaned_admin_or_partner_is_not_demoted(groups):
    change = decide_orphan_revoke(groups, 0)
    assert change.is_noop and change.reason == "no_demotion"


def test_an_orphaned_tenant_loses_only_role_tenant():
    change = decide_orphan_revoke({ROLE_TENANT}, 0)
    assert change.remove == frozenset({ROLE_TENANT}) and change.add is None


def test_nothing_held_is_a_noop():
    assert decide_orphan_revoke(set(), 0).reason == "nothing_held"


# --- the Authentik service refuses role_admin before any request ----------------------------

class _Recorder:
    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []

    def install(self, monkeypatch) -> None:
        real = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            path = request.url.path
            if path.endswith("/core/groups/") and request.method == "GET":
                name = request.url.params.get("search", "")
                return httpx.Response(200, json={"results": [{"name": name, "pk": f"pk-{name}"}]})
            if path.endswith("/core/users/"):
                email = request.url.params.get("search", "")
                return httpx.Response(200, json={"results": [{"email": email, "pk": 42}]})
            return httpx.Response(204)

        def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return real(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)


def _service() -> authentik_sync.AuthentikSyncService:
    svc = authentik_sync.AuthentikSyncService.__new__(authentik_sync.AuthentikSyncService)
    svc.api_url = "https://auth.example.test/api/v3"
    svc.token = "t"
    svc.enabled = True
    return svc


@pytest.mark.parametrize("kwargs", [
    {"remove": {"role_admin"}},
    {"add": "role_admin"},
    {"add": "role_partner", "remove": {"role_admin"}},
    {"add": "role_superusers"},
])
async def test_role_admin_is_refused_before_any_request(monkeypatch, kwargs):
    recorder = _Recorder()
    recorder.install(monkeypatch)
    with pytest.raises(ValueError):
        await _service().change_integration_role_groups("admin@example.test", **kwargs)
    assert recorder.requests == []


async def test_an_integration_change_never_creates_or_touches_role_admin(monkeypatch):
    recorder = _Recorder()
    recorder.install(monkeypatch)
    await _service().change_integration_role_groups(
        "tenant@example.test", add="role_partner", remove={"role_tenant"}
    )
    touched = " ".join(str(r.url) + r.content.decode() for r in recorder.requests)
    assert "role_admin" not in touched
    writes = [(r.url.path, r.method) for r in recorder.requests if r.method == "POST"]
    assert writes == [
        ("/api/v3/core/groups/pk-role_tenant/remove_user/", "POST"),
        ("/api/v3/core/groups/pk-role_partner/add_user/", "POST"),
    ]


# --- the member sync and the reconcile ------------------------------------------------------

class FakeAuthentik:
    def __init__(self, groups: Optional[set]) -> None:
        self.groups = groups
        self.changes: List[tuple] = []
        self.legacy_calls: List[tuple] = []

    async def get_user_role_groups(self, email: str) -> Optional[set]:
        return None if self.groups is None else set(self.groups)

    async def change_integration_role_groups(self, email: str, add=None, remove=frozenset()) -> bool:
        self.changes.append((email, add, frozenset(remove)))
        return True

    async def sync_user_role(self, *args: Any) -> bool:
        self.legacy_calls.append(("sync_user_role",) + args)
        return True

    async def remove_user_from_all_roles(self, *args: Any) -> bool:
        self.legacy_calls.append(("remove_user_from_all_roles",) + args)
        return True


@pytest.fixture
def authentik(monkeypatch):
    holder = {"svc": FakeAuthentik({ROLE_ADMIN})}
    monkeypatch.setattr(authentik_sync, "get_authentik_sync", lambda: holder["svc"])

    async def _no_reconcile(*_a: Any, **_k: Any) -> int:
        return 0

    monkeypatch.setattr(integrations, "reconcile_personal_clusters", _no_reconcile)
    return holder


async def _fresh_org(owner: str = KEY):
    db = sqlite_session(*org_tables())
    db.add(Organization(
        name="Fresh", slug="fresh", source="orcahub", external_id="ext-x",
        api_key_id=owner, created_by="system", is_active=True,
    ))
    await db.commit()
    return db


async def test_an_admin_pushed_into_a_fresh_org_keeps_role_admin(authentik):
    db = await _fresh_org()
    synced, _ = await integrations._upsert_synced_members(
        db, [SyncMember(email="admin@example.test", organization_id="ext-x", role="TENANT")], KEY
    )
    assert synced == 1
    assert authentik["svc"].changes == []
    assert authentik["svc"].legacy_calls == [], "sync_user_role must never run for an integration"


async def test_removing_an_admin_keeps_role_admin(authentik):
    db = await _fresh_org()
    await integrations._upsert_synced_members(
        db, [SyncMember(email="admin@example.test", organization_id="ext-x", role="TENANT")], KEY
    )
    _, removed = await integrations._upsert_synced_members(
        db,
        [SyncMember(email="admin@example.test", organization_id="ext-x", role="TENANT", action="remove")],
        KEY,
    )
    assert removed == 1
    assert authentik["svc"].changes == []
    assert authentik["svc"].legacy_calls == []


async def test_an_orphaned_tenant_loses_role_tenant_only(authentik):
    authentik["svc"] = FakeAuthentik({ROLE_TENANT})
    db = await _fresh_org()
    await integrations._upsert_synced_members(
        db, [SyncMember(email="tenant@example.test", organization_id="ext-x", role="TENANT")], KEY
    )
    await integrations._upsert_synced_members(
        db,
        [SyncMember(email="tenant@example.test", organization_id="ext-x", role="TENANT", action="remove")],
        KEY,
    )
    assert authentik["svc"].changes == [("tenant@example.test", None, frozenset({ROLE_TENANT}))]
    assert authentik["svc"].legacy_calls == []


async def test_a_partner_is_granted_only_inside_the_keys_own_orgs(authentik):
    authentik["svc"] = FakeAuthentik(set())
    db = await _fresh_org()
    db.add(Organization(
        name="Elsewhere", slug="elsewhere", source="local", created_by="partner@example.test",
        is_active=True,
    ))
    await db.commit()
    await integrations._upsert_synced_members(
        db, [SyncMember(email="partner@example.test", organization_id="ext-x", role="PARTNER")], KEY
    )
    assert authentik["svc"].changes == [], "a user who created a dashboard org is not promoted"

    authentik["svc"] = FakeAuthentik(set())
    await integrations._upsert_synced_members(
        db, [SyncMember(email="solo@example.test", organization_id="ext-x", role="PARTNER")], KEY
    )
    assert authentik["svc"].changes == [("solo@example.test", ROLE_PARTNER, frozenset())]


async def test_membership_owners_reads_memberships_and_created_orgs():
    db = await _fresh_org()
    db.add(OrganizationMember(organization_id=1, user_email="Someone@Example.test", role="tenant"))
    db.add(Organization(name="L", slug="l", source="local", created_by="someone@example.test"))
    await db.commit()
    owners = await membership_owners(db, "someone@example.test")
    assert sorted(owners, key=str) == sorted([KEY, None], key=str)


async def test_the_reconcile_refuses_another_keys_org(authentik):
    db = await _fresh_org(owner=OTHER)
    for email in ("a@example.test", "b@example.test"):
        db.add(OrganizationMember(organization_id=1, user_email=email, role="tenant"))
    await db.commit()
    removed = await integrations._reconcile_synced_members(
        db,
        [SyncOrganization(id="ext-x", name="Fresh", slug="fresh")],
        [SyncMember(email="c@example.test", organization_id="ext-x", role="TENANT")],
        KEY,
        dry_run=False,
    )
    left = (await db.execute(select(OrganizationMember.user_email))).scalars().all()
    assert removed == 0
    assert sorted(left) == ["a@example.test", "b@example.test"]


async def test_the_reconcile_still_prunes_the_keys_own_org(authentik):
    authentik["svc"] = FakeAuthentik({ROLE_TENANT})
    db = await _fresh_org(owner=KEY)
    for email in ("a@example.test", "b@example.test"):
        db.add(OrganizationMember(organization_id=1, user_email=email, role="tenant"))
    await db.commit()
    removed = await integrations._reconcile_synced_members(
        db,
        [SyncOrganization(id="ext-x", name="Fresh", slug="fresh")],
        [SyncMember(email="a@example.test", organization_id="ext-x", role="TENANT")],
        KEY,
        dry_run=False,
    )
    left = (await db.execute(select(OrganizationMember.user_email))).scalars().all()
    assert removed == 1
    assert left == ["a@example.test"]
