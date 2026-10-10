"""The push rebind rules, sent through the real POST /api/v1/integrations/sync route.

Decision 19: the key must claim exactly one org after the push, active or not, so a key that also
claims a buyer org rebinds nothing and the warning names the key id and the count. Decision 15:
that org is the destination only while it is active and this push names the creator a partner
there (pushed role and stored row), read back after the member writes. Decision 14: a creator the
dashboard resolves to Admin, or whose role Authentik cannot confirm, never rebinds. A rebind that
fails partway leaves nothing. OrcaHub v1.32.0's sync body binds the partner into its own org only
while the key holds no buyer org.

Every case asserts the status, the detail, the rebind record, what reached the share audit (the
call that reads the cluster), and the committed binding. The fakes are the existing ones, and each
records the side effect the real call makes.
"""

import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Set, Tuple, Union

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import get_db
from app.models.db_models import Organization, OrganizationMember
from app.routers import integrations
from app.services import integration_cluster_rebind
from app.services.integration_cluster_rebind import REBIND_ACTION
from app.services.lxd_service import get_lxd_service
from tests._sqlite_session import AsyncSessionFacade, installation_rows, org_tables, sqlite_session
from tests.test_a_push_rebinds_only_into_the_org_its_key_claimed import (
    ATTACKER,
    K_ATTACKER,
    K_PARTNER,
    PARTNER,
    SECRETS,
    bound,
    key,
    org,
    org_id,
    personal_cluster,
    world,  # noqa: F401
)

_HTTP = httpx.AsyncClient  # note: the real class, captured before real_authentik replaces it

SYNC = "/api/v1/integrations/sync"
C = PARTNER
CO = "copartner@example.test"
FOUNDER = "founder@example.test"
P, Q, B, A = "ext-p", "ext-q", "ext-b", "ext-a"
SLUG = {P: "partner", Q: "second", B: "buyer", A: "attacker"}
CREATORS = {K_PARTNER: C, K_ATTACKER: ATTACKER, "": C}


def sync_app(api_key: Dict[str, Any], db: Any) -> FastAPI:
    """The external router with the real capability gate. Only the key lookup and the db are faked."""
    app = FastAPI()
    app.include_router(integrations.external_router, prefix="/api/v1")

    async def _key() -> Dict[str, Any]:
        return api_key

    async def _db() -> AsyncIterator[Any]:
        yield db

    async def _none() -> None:
        return None

    app.dependency_overrides[integrations.validate_api_key] = _key
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_lxd_service] = _none
    return app


async def send(
    app: FastAPI, method: str, path: str, body: Any = None, headers: Optional[Dict[str, str]] = None
) -> httpx.Response:
    content = None if method in ("GET", "HEAD", "OPTIONS") else json.dumps(body).encode()
    async with _HTTP(transport=httpx.ASGITransport(app=app), base_url="http://orcastra.test") as client:
        return await client.request(
            method, path, content=content, headers={"content-type": "application/json", **(headers or {})}
        )


def m(email: str, ext: str, role: str, action: Optional[str] = None) -> Dict[str, Any]:
    pushed: Dict[str, Any] = {"email": email, "name": None, "organization_id": ext, "role": role}
    if action is not None:
        pushed["action"] = action
    return pushed


def body(exts: Sequence[str], members: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"include_clusters": True, "include_projects": True,
            "organizations": [org(e, SLUG[e]) for e in exts], "members": members}


async def push(db: Any, key_id: str, exts: Sequence[str], members: List[Dict[str, Any]],
               *, path: str = SYNC, method: str = "POST", **key_extra: Any) -> httpx.Response:
    api_key = key(key_id, CREATORS.get(key_id, C), **key_extra)
    return await send(sync_app(api_key, db), method, path, body(exts, members))


async def claimed(db: AsyncSessionFacade, ext: str, *, owner: Optional[str] = K_PARTNER, active: bool = True,
                  rows: Sequence[Tuple[str, str]] = ()) -> int:
    db.add(Organization(name=SLUG[ext].title(), slug=SLUG[ext], source="orcahub", external_id=ext,
                        api_key_id=owner, created_by="system", is_active=active))
    await db.commit()
    oid = await org_id(db, SLUG[ext])
    for email, role in rows:
        db.add(OrganizationMember(organization_id=oid, user_email=email, role=role))
    await db.commit()
    return oid


def rebind_warnings(caplog: pytest.LogCaptureFixture) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.name == integration_cluster_rebind.__name__]


async def assert_bound_to(db: Any, world: Dict[str, Any], slug: str, key_id: str = K_PARTNER,  # noqa: F811
                          cluster: str = "c-cluster") -> None:
    to = await org_id(db, slug)
    assert (await bound(db))[cluster] == to
    assert world["shares"] == [(cluster, to)]
    (record,) = world["log"].calls
    assert record["action"] == REBIND_ACTION and record["target_id"] == cluster
    assert record["details"] == {"cluster_id": cluster, "from_organization_id": None, "to_organization_id": to,
                                 "registered_by": C, "api_key_id": key_id}
    assert not any(secret in json.dumps(record) for secret in SECRETS)


async def assert_unmoved(db: Any, world: Dict[str, Any], cluster: str = "c-cluster") -> None:  # noqa: F811
    assert (await bound(db))[cluster] is None
    assert world["shares"] == [] and world["log"].calls == []


# note: decisions 15 and 19: the destination

async def _inactive(db: Any) -> Dict[str, int]:
    return {P: await claimed(db, P, active=False)}


async def _unclaimed(db: Any) -> Dict[str, int]:
    return {P: await claimed(db, P, owner=None)}


async def _inactive_second(db: Any) -> Dict[str, int]:
    return {Q: await claimed(db, Q, active=False)}


async def _unclaimed_second(db: Any) -> Dict[str, int]:
    return {Q: await claimed(db, Q, owner=None, rows=[(C, "partner")])}


async def _local_org(db: Any) -> Dict[str, int]:
    db.add(Organization(name="Local", slug="local", source="local", created_by=C, is_active=True))
    await db.commit()
    db.add(OrganizationMember(organization_id=await org_id(db, "local"), user_email=C, role="partner"))
    await db.commit()
    return {}


NAMES_NONE = "its push names 0 organizations, exactly one is required"

# note: id, setup, pushes [(key id, orgs, members)], destination slug or None, count warned (or the warning's
# own words) or None
D15 = [
    ("own_org_as_partner", None, [(K_PARTNER, [P], [m(C, P, "PARTNER")])], "partner", None),
    ("two_claimed_orgs_as_partner", None,
     [(K_PARTNER, [P, Q], [m(C, P, "PARTNER"), m(C, Q, "PARTNER")])], None, 2),
    ("partner_in_own_user_in_buyer", None,
     [(K_PARTNER, [P, B], [m(C, P, "PARTNER"), m(C, B, "USER"), m(FOUNDER, B, "PARTNER")])], None, 2),
    ("user_in_a_buyer_org_only", None, [(K_PARTNER, [P, B], [m(CO, P, "PARTNER"), m(C, B, "USER")])], None, 2),
    ("buyer_org_names_the_creator_as_partner", None,
     [(K_PARTNER, [P, B], [m(FOUNDER, P, "PARTNER"), m(FOUNDER, B, "PARTNER"), m(C, B, "PARTNER")])], None, 2),
    ("partner_in_own_and_in_a_buyer_org", None,
     [(K_PARTNER, [P, B], [m(C, P, "PARTNER"), m(C, B, "PARTNER"), m(FOUNDER, B, "PARTNER")])], None, 2),
    ("buyer_org_claimed_by_an_earlier_push", None,
     [(K_PARTNER, [B], [m(FOUNDER, B, "PARTNER")]), (K_PARTNER, [P], [m(C, P, "PARTNER")])], None, 2),
    ("inactive_org_claimed_earlier", _inactive_second, [(K_PARTNER, [P], [m(C, P, "PARTNER")])], None, 2),
    ("inactive_own_org_beside_a_buyer_naming_the_creator", _inactive,
     [(K_PARTNER, [B], [m(FOUNDER, B, "PARTNER"), m(C, B, "PARTNER")])], None, 2),
    ("unclaimed_org_does_not_count", _unclaimed_second, [(K_PARTNER, [P], [m(C, P, "PARTNER")])], "partner", None),
    ("removed_later_in_the_same_push", None,
     [(K_PARTNER, [P, Q], [m(C, P, "PARTNER"), m(C, Q, "PARTNER"), m(C, Q, "PARTNER", "remove")])], None, 2),
    ("removed_from_its_only_org", None,
     [(K_PARTNER, [P], [m(C, P, "PARTNER"), m(C, P, "PARTNER", "remove")])], None, 0),
    ("remove_in_capitals_is_an_upsert_on_both_sides", None,
     [(K_PARTNER, [P, Q], [m(C, P, "PARTNER"), m(C, Q, "PARTNER"), m(C, Q, "PARTNER", "REMOVE")])], None, 2),
    ("partner_then_user_in_one_org", None, [(K_PARTNER, [P], [m(C, P, "PARTNER"), m(C, P, "USER")])], None, 0),
    ("partner_then_user_beside_another_partner", None,
     [(K_PARTNER, [P], [m(CO, P, "PARTNER"), m(C, P, "PARTNER"), m(C, P, "USER")])], None, 0),
    ("user_then_partner_in_one_org", None, [(K_PARTNER, [P], [m(C, P, "USER"), m(C, P, "PARTNER")])], "partner", None),
    ("org_another_key_claimed", None,
     [(K_ATTACKER, [A], [m(ATTACKER, A, "PARTNER")]), (K_PARTNER, [A], [m(C, A, "PARTNER")])], None, None),
    ("push_also_names_an_org_another_key_claimed", None,
     [(K_ATTACKER, [A], [m(C, A, "PARTNER")]), (K_PARTNER, [P], [m(C, P, "PARTNER"), m(C, A, "PARTNER")])],
     "partner", None),
    ("another_key_pushed_the_creator_first", None,
     [(K_ATTACKER, [A], [m(C, A, "PARTNER")]), (K_PARTNER, [P], [m(C, P, "PARTNER")])], "partner", None),
    ("members_only_for_an_org_claimed_earlier", None,
     [(K_PARTNER, [P], [m(CO, P, "PARTNER")]), (K_PARTNER, [], [m(C, P, "PARTNER")])], None, NAMES_NONE),
    ("members_only_for_an_inactive_claimed_org", _inactive, [(K_PARTNER, [], [m(C, P, "PARTNER")])], None,
     NAMES_NONE),
    ("pushed_org_reactivates_an_inactive_one", _inactive, [(K_PARTNER, [P], [m(C, P, "PARTNER")])], "partner", None),
    ("unclaimed_org_claimed_by_this_push", _unclaimed, [(K_PARTNER, [P], [m(C, P, "PARTNER")])], "partner", None),
    ("members_only_for_an_unclaimed_org", _unclaimed, [(K_PARTNER, [], [m(C, P, "PARTNER")])], None, None),
    ("older_dashboard_org_is_not_consulted", _local_org, [(K_PARTNER, [P], [m(C, P, "PARTNER")])], "partner", None),
    ("key_with_an_empty_id", None, [("", [P], [m(C, P, "PARTNER")])], None, 0),
]


@pytest.mark.parametrize("setup, pushes, dest, warned", [c[1:] for c in D15], ids=[c[0] for c in D15])
async def test_decision_15_destination(
    world: Dict[str, Any], caplog: pytest.LogCaptureFixture, setup: Any,  # noqa: F811
    pushes: List[Tuple[str, List[str], List[Dict[str, Any]]]], dest: Optional[str], warned: Union[int, str, None],
) -> None:
    db = sqlite_session(*org_tables())
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)
    if setup is not None:
        await setup(db)
    await personal_cluster(db, "c-cluster", C)
    for key_id, exts, members in pushes:
        response = await push(db, key_id, exts, members)
        assert response.status_code == 200, response.text
    if dest is None:
        await assert_unmoved(db, world)
    else:
        await assert_bound_to(db, world, dest, pushes[-1][0])
    warnings = rebind_warnings(caplog)
    if warned is None:
        assert warnings == []
    else:
        (warning,) = warnings
        said = warned if isinstance(warned, str) else f" {warned} organizations"
        assert said in warning and (pushes[-1][0] or "") in warning


PUSHED_ROLES = [("PARTNER", True), ("partner", True), ("Partner", True), ("pArTnEr", True),
                ("USER", False), ("TENANT", False), ("user", False), ("OWNER", False), ("ADMIN", False),
                (" PARTNER", False), ("PARTNER ", False), ("PARTNER\n", False), ("", False),
                ("ＰＡＲＴＮＥＲ", False), ("PАRTNER", False),
                ("role_partner", False), ("PARTNERS", False)]


@pytest.mark.parametrize("role, moves", PUSHED_ROLES, ids=[repr(r) for r, _ in PUSHED_ROLES])
async def test_decision_15_pushed_role_spellings(world: Dict[str, Any], role: str, moves: bool) -> None:  # noqa: F811
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "c-cluster", C)
    assert (await push(db, K_PARTNER, [P], [m(C, P, role)])).status_code == 200
    stored = (await db.execute(select(OrganizationMember.role))).scalars().all()
    assert stored == ["partner" if moves else "tenant"]
    if moves:
        await assert_bound_to(db, world, "partner")
    else:
        await assert_unmoved(db, world)


class FailingMemberWrites(AsyncSessionFacade):
    """The database refuses a commit carrying one of `refused` (org id, email, role written or "delete")."""

    def __init__(self, db: AsyncSessionFacade, refused: Set[Tuple[int, str, str]]) -> None:
        super().__init__(db.sync)
        self.refused = refused
        self.refusals: List[Tuple[int, str, str]] = []

    async def commit(self) -> None:
        pending = [(o, o.role) for o in [*self.sync.new, *self.sync.dirty] if isinstance(o, OrganizationMember)]
        pending += [(o, "delete") for o in self.sync.deleted if isinstance(o, OrganizationMember)]
        hit = [(o.organization_id, o.user_email, what) for o, what in pending]
        hit = [h for h in hit if h in self.refused]
        if hit:
            self.refusals.extend(hit)
            raise RuntimeError("the database refused the member write")
        self.sync.commit()


# note: id, orgs the key claimed, rows before the push (org, email, role), members pushed, the creator's
# write that fails (org, role or "delete"), destination, count warned. With two claimed orgs a failed write
# never leaves one destination (decision 19). With one, the rows read back decide (decision 15).
READ_BACK = [
    ("second_org_member_write_failed", [P, Q], [], [m(C, P, "PARTNER"), m(C, Q, "PARTNER")],
     (Q, "partner"), None, 2),
    ("second_org_write_failed_beside_another_partner", [P, Q], [(Q, CO, "partner")],
     [m(C, P, "PARTNER"), m(C, Q, "PARTNER")], (Q, "partner"), None, 2),
    ("stale_partner_row_update_to_user_failed", [P, Q], [(P, C, "partner")],
     [m(C, P, "USER"), m(C, Q, "PARTNER")], (P, "tenant"), None, 2),
    ("tenant_row_update_to_partner_failed", [P, Q], [(P, C, "tenant")], [m(C, P, "PARTNER"), m(C, Q, "PARTNER")],
     (P, "partner"), None, 2),
    ("failed_remove_is_still_no_destination", [P, Q], [(Q, C, "partner")],
     [m(C, P, "PARTNER"), m(C, Q, "PARTNER", "remove")], (Q, "delete"), None, 2),
    ("only_org_partner_write_failed_then_user_landed", [P], [], [m(C, P, "PARTNER"), m(C, P, "USER")],
     (P, "partner"), None, 0),
    ("only_org_partner_write_failed_beside_another_partner", [P], [(P, CO, "partner")],
     [m(C, P, "PARTNER"), m(C, P, "USER")], (P, "partner"), None, 0),
    ("only_org_tenant_row_update_to_partner_failed", [P], [(P, C, "tenant")], [m(C, P, "PARTNER"), m(C, P, "USER")],
     (P, "partner"), None, 0),
    ("only_org_the_one_write_failed", [P], [], [m(C, P, "PARTNER")], (P, "partner"), None, None),
]


@pytest.mark.parametrize("claims, rows, members, failing, dest, warned", [c[1:] for c in READ_BACK],
                         ids=[c[0] for c in READ_BACK])
async def test_decision_15_reads_the_member_rows_back(
    world: Dict[str, Any], caplog: pytest.LogCaptureFixture, claims: List[str],  # noqa: F811
    rows: List[Tuple[str, str, str]], members: List[Dict[str, Any]], failing: Tuple[str, str],
    dest: Optional[str], warned: Optional[int],
) -> None:
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)
    base = sqlite_session(*org_tables())
    ids = {e: await claimed(base, e, rows=[(who, r) for o, who, r in rows if o == e]) for e in claims}
    refused = (ids[failing[0]], C, failing[1])
    db = FailingMemberWrites(base, {refused})
    await personal_cluster(db, "c-cluster", C)
    assert (await push(db, K_PARTNER, [], members)).status_code == 200
    assert db.refusals == [refused]
    if dest is None:
        await assert_unmoved(db, world)
    else:
        await assert_bound_to(db, world, dest)
    warnings = rebind_warnings(caplog)
    if warned is None:
        assert warnings == []
    else:
        (warning,) = warnings
        assert f" {warned} organizations" in warning and K_PARTNER in warning and "@" not in warning


# note: who triggers a rebind

@pytest.mark.parametrize("pushed", [CO, C.upper(), f" {C}", f"{C} ", "Partner@Example.test"])
async def test_only_the_exact_creator_triggers_a_rebind(world: Dict[str, Any], pushed: str) -> None:  # noqa: F811
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "c-cluster", C)
    await personal_cluster(db, "other-cluster", pushed)
    assert (await push(db, K_PARTNER, [P], [m(pushed, P, "PARTNER")])).status_code == 200
    assert await bound(db) == {"c-cluster": None, "other-cluster": None}
    assert world["shares"] == [] and world["log"].calls == []


@pytest.mark.parametrize("exts, has_cluster, strict_reads", [([P], False, 0), ([P, Q], False, 0), ([P], True, 1)],
                         ids=["one_org_nothing_unbound", "two_orgs_nothing_unbound", "one_org_with_a_cluster"])
async def test_the_role_is_read_only_when_something_would_move(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,  # noqa: F811
    exts: List[str], has_cluster: bool, strict_reads: int,
) -> None:
    """One extra Authentik read, only for a creator with an unbound cluster and one destination."""
    strict: List[str] = []
    lenient = world["directory"].read_current_roles

    async def _strict(email: str) -> Any:
        strict.append(email)
        return await lenient(email)

    monkeypatch.setattr(world["directory"], "read_current_roles", _strict)
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)
    db = sqlite_session(*org_tables())
    if has_cluster:
        await personal_cluster(db, "c-cluster", C)
    assert (await push(db, K_PARTNER, exts, [m(C, e, "PARTNER") for e in exts])).status_code == 200
    assert len(strict) == strict_reads and rebind_warnings(caplog) == []
    assert len(world["log"].calls) == strict_reads


CREATED_BY = [None, "", "   ", True, 1, 1.5, [C], {"email": C}]
INSTALL_IDS = [7, "7", 0, "", False, [7]]


@pytest.mark.parametrize("created_by", CREATED_BY, ids=[repr(v) for v in CREATED_BY])
async def test_a_key_without_a_usable_creator_rebinds_nothing(world: Dict[str, Any], created_by: Any) -> None:  # noqa: F811
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "c-cluster", C)
    api_key = {**key(K_PARTNER, C), "created_by": created_by}
    assert (await send(sync_app(api_key, db), "POST", SYNC, body([P], [m(C, P, "PARTNER")]))).status_code == 200
    await assert_unmoved(db, world)


@pytest.mark.parametrize("install_id", [*INSTALL_IDS, None], ids=[repr(v) for v in [*INSTALL_IDS, None]])
async def test_an_add_on_credential_never_rebinds(world: Dict[str, Any], install_id: Any) -> None:  # noqa: F811
    db = sqlite_session(*org_tables())
    if install_id is not None:
        installation_rows(db, (K_PARTNER, None))
    await personal_cluster(db, "c-cluster", C)
    assert (await push(db, K_PARTNER, [P], [m(C, P, "PARTNER")], extension_install_id=install_id)).status_code == 200
    if install_id is None:
        await assert_bound_to(db, world, "partner")
    else:
        await assert_unmoved(db, world)


CAPS = [(["inventory.read"], 403, "org.sync"), (["org.sync"], 403, "inventory.read"), ([], 403, "inventory.read"),
        (["inventory.read", "org.sync"], 200, None), (None, 200, None)]


@pytest.mark.parametrize("caps, status, named", CAPS,
                         ids=["no_org_sync", "no_inventory_read", "none", "both", "legacy"])
async def test_the_capability_gate_comes_first(
    world: Dict[str, Any], caps: Optional[List[str]], status: int, named: Optional[str],  # noqa: F811
) -> None:
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "c-cluster", C)
    api_key = key(K_PARTNER, C)
    if caps is None:
        api_key.pop("capabilities")
    else:
        api_key["capabilities"] = caps
    response = await send(sync_app(api_key, db), "POST", SYNC, body([P], [m(C, P, "PARTNER")]))
    assert response.status_code == status
    if named is None:
        await assert_bound_to(db, world, "partner")
        return
    assert response.json()["detail"] == f"API key does not have the '{named}' capability"
    await assert_unmoved(db, world)
    assert (await db.execute(select(Organization.id))).all() == [] and world["directory"].reads == []


SPELLINGS = [(SYNC, 200), ("/api/v1/integrations/%73ync", 200), ("/api/v1/integrations/%73%79%6e%63", 200),
             (f"{SYNC}?include_clusters=false&organizations=x", 200), (f"{SYNC}/", 307), (f"{SYNC}%2F", 307),
             ("/api/v1/integrations/SYNC", 404), ("/api/v1/Integrations/sync", 404),
             ("/api/v1/integrations//sync", 404), ("/api/v1/integrations/sync/x", 404)]


@pytest.mark.parametrize("path, status", SPELLINGS, ids=[s[0] for s in SPELLINGS])
async def test_route_spellings(world: Dict[str, Any], path: str, status: int) -> None:  # noqa: F811
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "c-cluster", C)
    response = await push(db, K_PARTNER, [P], [m(C, P, "PARTNER")], path=path)
    assert response.status_code == status
    if status == 200:
        await assert_bound_to(db, world, "partner")
    else:
        await assert_unmoved(db, world)
        assert (await db.execute(select(Organization.id))).all() == []


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def test_every_other_method_is_405_and_moves_nothing(world: Dict[str, Any], method: str) -> None:  # noqa: F811
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "c-cluster", C)
    response = await push(db, K_PARTNER, [P], [m(C, P, "PARTNER")], method=method)
    assert response.status_code == 405
    await assert_unmoved(db, world)
    assert (await db.execute(select(Organization.id))).all() == []


BAD_VALUES = [True, 1, 1.5, None, ["PARTNER"], {"role": "PARTNER"}]


@pytest.mark.parametrize("field", ["role", "email", "organization_id", "action"])
@pytest.mark.parametrize("value", BAD_VALUES, ids=[repr(v) for v in BAD_VALUES])
async def test_a_non_string_member_field_is_422_and_writes_nothing(
    world: Dict[str, Any], field: str, value: Any,  # noqa: F811
) -> None:
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "c-cluster", C)
    response = await send(sync_app(key(K_PARTNER, C), db), "POST", SYNC,
                          body([P], [{**m(C, P, "PARTNER"), field: value}]))
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "members", 0, field]
    await assert_unmoved(db, world)
    assert (await db.execute(select(Organization.id))).all() == [] and world["directory"].reads == []


@pytest.mark.parametrize("value", [True, 1, "x", {"a": 1}, None], ids=["bool", "number", "string", "object", "null"])
async def test_members_that_are_not_a_list_are_422(world: Dict[str, Any], value: Any) -> None:  # noqa: F811
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "c-cluster", C)
    response = await send(sync_app(key(K_PARTNER, C), db), "POST", SYNC, {**body([P], []), "members": value})
    assert response.status_code == 422
    await assert_unmoved(db, world)
    assert (await db.execute(select(Organization.id))).all() == []
