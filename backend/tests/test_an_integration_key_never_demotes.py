"""An integration key may grant a role, never demote one or touch role_admin.

The member sync called sync_user_role, which removes the user from every other role group
including role_admin, and a membership removal stripped every role group once the user had no
membership left. Any Partner could mint a key, sync a fresh organization, push an Admin into it
as TENANT and demote them. The reconcile also resolved each pushed org by external id alone, so
a key could purge another integration's roster.
"""

from itertools import combinations
from typing import Any, Dict, Iterable, List, Optional

import httpx
import pytest
from sqlalchemy import select

from app.models.db_models import Organization, OrganizationCluster, OrganizationMember
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
from tests._sqlite_session import AsyncSessionFacade, org_tables, sqlite_session

KEY = "oak_test"
OTHER = "oak_other"
ALL_GROUPS = (ROLE_ADMIN, ROLE_PARTNER, ROLE_TENANT)
EVERY_HOLDING = [set(c) for n in range(4) for c in combinations(ALL_GROUPS, n)]


# --- decide_upsert --------------------------------------------------------------------------

@pytest.mark.parametrize("target", ["tenant", "partner"])
def test_an_admin_is_never_touched(target: str) -> None:
    change = decide_upsert(target, {ROLE_ADMIN}, [KEY], KEY)
    assert change.is_noop and change.reason == "holds_admin"


def test_a_partner_pushed_as_tenant_is_not_demoted() -> None:
    change = decide_upsert("tenant", {ROLE_PARTNER}, [KEY], KEY)
    assert change.is_noop and change.reason == "no_demotion"


@pytest.mark.parametrize("owners", [[OTHER], [KEY, OTHER], [KEY, None], [None], []])
def test_a_partner_grant_needs_every_org_under_this_key(owners: List[Optional[str]]) -> None:
    change = decide_upsert("partner", {ROLE_TENANT}, owners, KEY)
    assert change.is_noop and change.reason == "membership_outside_this_key"


def test_a_partner_grant_where_every_org_is_owned() -> None:
    change = decide_upsert("partner", {ROLE_TENANT}, [KEY, KEY], KEY)
    assert change.add == ROLE_PARTNER
    assert change.remove == frozenset({ROLE_TENANT})
    assert change.reason == "grant_partner"


def test_a_partner_grant_to_a_user_without_groups_removes_nothing() -> None:
    change = decide_upsert("partner", set(), [KEY], KEY)
    assert change.add == ROLE_PARTNER and change.remove == frozenset()


def test_a_user_without_groups_pushed_as_tenant_gets_role_tenant() -> None:
    change = decide_upsert("tenant", set(), [], KEY)
    assert change.add == ROLE_TENANT and not change.remove


def test_an_existing_tenant_is_left_alone() -> None:
    assert decide_upsert("tenant", {ROLE_TENANT}, [], KEY).reason == "already_tenant"


def test_an_existing_partner_pushed_as_partner_is_left_alone() -> None:
    assert decide_upsert("partner", {ROLE_PARTNER}, [OTHER], KEY).reason == "already_partner"


@pytest.mark.parametrize("target", ["tenant", "partner"])
def test_unknown_groups_change_nothing(target: str) -> None:
    change = decide_upsert(target, None, [KEY], KEY)
    assert change.is_noop and change.reason == "authentik_unknown"


@pytest.mark.parametrize("target", ["tenant", "partner", "admin", "anything"])
@pytest.mark.parametrize("current", EVERY_HOLDING, ids=lambda g: "+".join(sorted(g)) or "none")
@pytest.mark.parametrize("owners", [[], [KEY], [OTHER], [KEY, None], [KEY, KEY]])
def test_no_upsert_ever_demotes(target: str, current: set, owners: List[Optional[str]]) -> None:
    """Every holding, every target, every ownership: nothing is ever taken but a superseded tenant."""
    change = decide_upsert(target, set(current), owners, KEY)
    assert change.add in (None, ROLE_PARTNER, ROLE_TENANT)
    assert ROLE_ADMIN not in change.remove and ROLE_PARTNER not in change.remove
    if change.remove:
        assert change.remove == frozenset({ROLE_TENANT}) and change.add == ROLE_PARTNER
    if ROLE_ADMIN in current:
        assert change.is_noop


# --- decide_orphan_revoke -------------------------------------------------------------------

def test_a_remaining_membership_keeps_everything() -> None:
    assert decide_orphan_revoke({ROLE_TENANT}, 1).reason == "still_a_member"


def test_unknown_groups_are_not_revoked() -> None:
    assert decide_orphan_revoke(None, 0).reason == "authentik_unknown"


@pytest.mark.parametrize("groups", [{ROLE_ADMIN}, {ROLE_PARTNER}, {ROLE_ADMIN, ROLE_TENANT}])
def test_an_orphaned_admin_or_partner_is_not_demoted(groups: set) -> None:
    change = decide_orphan_revoke(groups, 0)
    assert change.is_noop and change.reason == "no_demotion"


def test_an_orphaned_tenant_loses_only_role_tenant() -> None:
    change = decide_orphan_revoke({ROLE_TENANT}, 0)
    assert change.remove == frozenset({ROLE_TENANT}) and change.add is None


def test_nothing_held_is_a_noop() -> None:
    assert decide_orphan_revoke(set(), 0).reason == "nothing_held"


@pytest.mark.parametrize("current", EVERY_HOLDING, ids=lambda g: "+".join(sorted(g)) or "none")
@pytest.mark.parametrize("remaining", [0, 1])
def test_no_orphan_revoke_ever_takes_more_than_role_tenant(current: set, remaining: int) -> None:
    change = decide_orphan_revoke(set(current), remaining)
    assert change.add is None
    assert change.remove <= frozenset({ROLE_TENANT})


# --- the Authentik service refuses role_admin before any request ----------------------------

class _Recorder:
    """Answers like Authentik and records every request. `held` is the user's role groups."""

    def __init__(self, held: Iterable[str] = ()) -> None:
        self.requests: List[httpx.Request] = []
        self.held = set(held)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
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
            if path.endswith("/core/users/42/"):
                return httpx.Response(200, json={"groups_obj": [{"name": g} for g in sorted(self.held)]})
            if request.method == "POST" and "/core/groups/pk-" in path:
                group = path.split("/core/groups/pk-")[1].split("/")[0]
                if path.endswith("/remove_user/"):
                    self.held.discard(group)
                elif path.endswith("/add_user/"):
                    self.held.add(group)
            return httpx.Response(204)

        def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return real(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)

    def writes(self) -> List[tuple]:
        return [(r.url.path, r.method) for r in self.requests if r.method != "GET"]


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
async def test_role_admin_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, kwargs: Dict[str, Any]
) -> None:
    recorder = _Recorder()
    recorder.install(monkeypatch)
    with pytest.raises(ValueError):
        await _service().change_integration_role_groups("admin@example.test", **kwargs)
    assert recorder.requests == []


async def test_an_integration_change_never_creates_or_touches_role_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    recorder.install(monkeypatch)
    await _service().change_integration_role_groups(
        "tenant@example.test", add="role_partner", remove={"role_tenant"}
    )
    touched = " ".join(str(r.url) + r.content.decode() for r in recorder.requests)
    assert "role_admin" not in touched
    assert recorder.writes() == [
        ("/api/v3/core/groups/pk-role_tenant/remove_user/", "POST"),
        ("/api/v3/core/groups/pk-role_partner/add_user/", "POST"),
    ]


# --- the member sync and the reconcile ------------------------------------------------------

class FakeAuthentik:
    """Holds one user's role groups and applies each call to them, as Authentik would.

    The integration writer refuses role_admin before acting, like the real service, and the
    legacy writers are recorded and applied too, so a regression shows in the groups held.
    """

    def __init__(self, groups: Optional[set]) -> None:
        self.groups = groups
        self.changes: List[tuple] = []
        self.legacy_calls: List[tuple] = []

    async def get_user_role_groups(self, email: str) -> Optional[set]:
        return None if self.groups is None else set(self.groups)

    async def change_integration_role_groups(
        self, email: str, add: Optional[str] = None, remove: Iterable[str] = frozenset()
    ) -> bool:
        named = frozenset(remove) | ({add} if add else frozenset())
        if named - authentik_sync.INTEGRATION_ROLE_GROUPS:
            raise ValueError(f"an integration may not change {sorted(named)}")
        self.changes.append((email, add, frozenset(remove)))
        if self.groups is not None:
            self.groups -= set(remove)
            if add:
                self.groups.add(add)
        return True

    async def sync_user_role(self, email: str, role: str) -> bool:
        self.legacy_calls.append(("sync_user_role", email, role))
        self.groups = {f"role_{role}"}
        return True

    async def remove_user_from_all_roles(self, email: str) -> bool:
        self.legacy_calls.append(("remove_user_from_all_roles", email))
        self.groups = set()
        return True


@pytest.fixture
def authentik(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    holder: Dict[str, Any] = {"svc": FakeAuthentik({ROLE_ADMIN})}
    monkeypatch.setattr(authentik_sync, "get_authentik_sync", lambda: holder["svc"])

    async def _no_reconcile(*_a: Any, **_k: Any) -> int:
        return 0

    monkeypatch.setattr(integrations, "reconcile_personal_clusters", _no_reconcile)
    return holder


async def _fresh_org(owner: Optional[str] = KEY) -> AsyncSessionFacade:
    db = sqlite_session(*org_tables())
    db.add(Organization(
        name="Fresh", slug="fresh", source="orcahub", external_id="ext-x",
        api_key_id=owner, created_by="system", is_active=True,
    ))
    await db.commit()
    return db


def _push(email: str, role: str, action: str = "upsert") -> List[SyncMember]:
    return [SyncMember(email=email, organization_id="ext-x", role=role, action=action)]


@pytest.mark.parametrize("role", ["TENANT", "PARTNER", "USER"])
async def test_an_admin_pushed_into_a_fresh_org_keeps_role_admin(
    authentik: Dict[str, Any], role: str
) -> None:
    db = await _fresh_org()
    synced, _ = await integrations._upsert_synced_members(db, _push("admin@example.test", role), KEY)
    assert synced == 1
    assert authentik["svc"].changes == []
    assert authentik["svc"].legacy_calls == [], "sync_user_role must never run for an integration"
    assert authentik["svc"].groups == {ROLE_ADMIN}


async def test_a_partner_pushed_as_tenant_keeps_role_partner(authentik: Dict[str, Any]) -> None:
    authentik["svc"] = FakeAuthentik({ROLE_PARTNER})
    db = await _fresh_org()
    await integrations._upsert_synced_members(db, _push("partner@example.test", "TENANT"), KEY)
    assert authentik["svc"].groups == {ROLE_PARTNER}
    assert authentik["svc"].legacy_calls == []


async def test_removing_an_admin_keeps_role_admin(authentik: Dict[str, Any]) -> None:
    db = await _fresh_org()
    await integrations._upsert_synced_members(db, _push("admin@example.test", "TENANT"), KEY)
    _, removed = await integrations._upsert_synced_members(
        db, _push("admin@example.test", "TENANT", action="remove"), KEY
    )
    assert removed == 1
    assert authentik["svc"].changes == []
    assert authentik["svc"].legacy_calls == []
    assert authentik["svc"].groups == {ROLE_ADMIN}


async def test_an_orphaned_tenant_loses_role_tenant_only(authentik: Dict[str, Any]) -> None:
    authentik["svc"] = FakeAuthentik({ROLE_TENANT})
    db = await _fresh_org()
    await integrations._upsert_synced_members(db, _push("tenant@example.test", "TENANT"), KEY)
    await integrations._upsert_synced_members(
        db, _push("tenant@example.test", "TENANT", action="remove"), KEY
    )
    assert authentik["svc"].changes == [("tenant@example.test", None, frozenset({ROLE_TENANT}))]
    assert authentik["svc"].legacy_calls == []
    assert authentik["svc"].groups == set()


async def test_a_partner_is_granted_only_inside_the_keys_own_orgs(authentik: Dict[str, Any]) -> None:
    authentik["svc"] = FakeAuthentik(set())
    db = await _fresh_org()
    db.add(Organization(
        name="Elsewhere", slug="elsewhere", source="local", created_by="partner@example.test",
        is_active=True,
    ))
    await db.commit()
    await integrations._upsert_synced_members(db, _push("partner@example.test", "PARTNER"), KEY)
    assert authentik["svc"].changes == [], "a user who created a dashboard org is not promoted"

    authentik["svc"] = FakeAuthentik(set())
    await integrations._upsert_synced_members(db, _push("solo@example.test", "PARTNER"), KEY)
    assert authentik["svc"].changes == [("solo@example.test", ROLE_PARTNER, frozenset())]
    assert authentik["svc"].groups == {ROLE_PARTNER}


async def test_membership_owners_reads_memberships_and_created_orgs() -> None:
    db = await _fresh_org()
    db.add(OrganizationMember(organization_id=1, user_email="Someone@Example.test", role="tenant"))
    db.add(Organization(name="L", slug="l", source="local", created_by="someone@example.test"))
    await db.commit()
    owners = await membership_owners(db, "someone@example.test")
    assert sorted(owners, key=str) == sorted([KEY, None], key=str)


# --- the same rule through the real Authentik client ----------------------------------------

@pytest.fixture
def real_authentik(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    holder: Dict[str, Any] = {}

    def install(held: Iterable[str]) -> _Recorder:
        recorder = _Recorder(held)
        recorder.install(monkeypatch)
        holder["recorder"] = recorder
        return recorder

    monkeypatch.setattr(authentik_sync, "get_authentik_sync", _service)

    async def _no_reconcile(*_a: Any, **_k: Any) -> int:
        return 0

    monkeypatch.setattr(integrations, "reconcile_personal_clusters", _no_reconcile)
    holder["install"] = install
    return holder


async def test_a_pushed_admin_sees_no_group_write_at_all(real_authentik: Dict[str, Any]) -> None:
    """The pushed entry and its removal leave an Admin's groups untouched: no POST is ever sent."""
    recorder = real_authentik["install"]({ROLE_ADMIN})
    db = await _fresh_org()
    await integrations._upsert_synced_members(db, _push("admin@example.test", "TENANT"), KEY)
    await integrations._upsert_synced_members(
        db, _push("admin@example.test", "TENANT", action="remove"), KEY
    )
    assert recorder.writes() == []
    assert recorder.held == {ROLE_ADMIN}


async def test_a_fresh_user_gets_role_tenant_and_nothing_else(real_authentik: Dict[str, Any]) -> None:
    recorder = real_authentik["install"](())
    db = await _fresh_org()
    await integrations._upsert_synced_members(db, _push("new@example.test", "TENANT"), KEY)
    assert recorder.writes() == [("/api/v3/core/groups/pk-role_tenant/add_user/", "POST")]
    assert not any("role_admin" in str(r.url) for r in recorder.requests)
    assert recorder.held == {ROLE_TENANT}


# --- the reconcile --------------------------------------------------------------------------

async def _reconcile(db: AsyncSessionFacade, keep: str) -> int:
    return await integrations._reconcile_synced_members(
        db,
        [SyncOrganization(id="ext-x", name="Fresh", slug="fresh")],
        [SyncMember(email=keep, organization_id="ext-x", role="TENANT")],
        KEY,
        dry_run=False,
    )


async def _members(db: AsyncSessionFacade) -> List[str]:
    return sorted((await db.execute(select(OrganizationMember.user_email))).scalars().all())


@pytest.mark.parametrize("owner", [OTHER, None])
async def test_the_reconcile_refuses_an_org_this_key_does_not_own(
    authentik: Dict[str, Any], owner: Optional[str]
) -> None:
    db = await _fresh_org(owner=owner)
    for email in ("a@example.test", "b@example.test"):
        db.add(OrganizationMember(organization_id=1, user_email=email, role="tenant"))
    await db.commit()
    removed = await _reconcile(db, keep="c@example.test")
    assert removed == 0
    assert await _members(db) == ["a@example.test", "b@example.test"]
    assert authentik["svc"].changes == [] and authentik["svc"].legacy_calls == []


async def test_the_reconcile_still_prunes_the_keys_own_org(authentik: Dict[str, Any]) -> None:
    authentik["svc"] = FakeAuthentik({ROLE_TENANT})
    db = await _fresh_org(owner=KEY)
    for email in ("a@example.test", "b@example.test"):
        db.add(OrganizationMember(organization_id=1, user_email=email, role="tenant"))
    await db.commit()
    removed = await _reconcile(db, keep="a@example.test")
    assert removed == 1
    assert await _members(db) == ["a@example.test"]
    assert authentik["svc"].changes == [("b@example.test", None, frozenset({ROLE_TENANT}))]


async def test_a_pruned_admin_keeps_role_admin(authentik: Dict[str, Any]) -> None:
    db = await _fresh_org(owner=KEY)
    for email in ("a@example.test", "admin@example.test"):
        db.add(OrganizationMember(organization_id=1, user_email=email, role="tenant"))
    await db.commit()
    removed = await _reconcile(db, keep="a@example.test")
    assert removed == 1
    assert authentik["svc"].legacy_calls == []
    assert authentik["svc"].groups == {ROLE_ADMIN}


# --- role_partner only through the key's home organization ----------------------------------

async def _seller_and_buyer() -> AsyncSessionFacade:
    """The seller's home org holds the cluster its partner registered. A buyer org the same key
    synced later is bound to that cluster."""
    db = sqlite_session(*org_tables())
    db.add(Organization(
        name="Seller", slug="seller", source="orcahub", external_id="ext-home",
        api_key_id=KEY, created_by="system", is_active=True,
    ))
    db.add(Organization(
        name="Buyer", slug="buyer", source="orcahub", external_id="ext-buyer",
        api_key_id=KEY, created_by="system", is_active=True,
    ))
    await db.commit()
    db.add(OrganizationMember(organization_id=1, user_email="seller@example.test", role="partner"))
    db.add(OrganizationCluster(organization_id=1, cluster_id="c1", registered_by="seller@example.test"))
    db.add(OrganizationCluster(organization_id=2, cluster_id="c1"))
    await db.commit()
    return db


async def test_the_seller_is_granted_role_partner_in_the_keys_home(authentik: Dict[str, Any]) -> None:
    authentik["svc"] = FakeAuthentik(set())
    db = await _seller_and_buyer()
    await integrations._upsert_synced_members(
        db, [SyncMember(email="seller@example.test", organization_id="ext-home", role="PARTNER")], KEY
    )
    assert authentik["svc"].groups == {ROLE_PARTNER}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "role_partner only through the key's home organization needs the party oracle and a "
        "per-key home record, which do not exist on this line yet"
    ),
)
async def test_a_buyer_founder_is_not_promoted_outside_the_keys_home(
    authentik: Dict[str, Any],
) -> None:
    authentik["svc"] = FakeAuthentik(set())
    db = await _seller_and_buyer()
    await integrations._upsert_synced_members(
        db, [SyncMember(email="founder@example.test", organization_id="ext-buyer", role="PARTNER")], KEY
    )
    assert ROLE_PARTNER not in (authentik["svc"].groups or set())
