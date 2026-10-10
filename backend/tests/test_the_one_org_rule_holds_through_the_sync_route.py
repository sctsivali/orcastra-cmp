"""Decision 19's one-org rule, with decisions 14 and 15, sent through the real POST /integrations/sync.

A push rebinds the creator's personal clusters only when its organizations list names exactly one
organization and the key claims exactly that one after the push, any source, active or not. The
claim is organizations.api_key_id, written by the organization upsert, so the cases vary what the
key held before the push and what the push lists, adds, refuses, re-links or reactivates, and then
read what the rebind saw. Removes in the same push never unclaim an organization. Decision 15 still
picks the destination inside the one org, and decision 14 still refuses an Admin or unknown
creator, after the count.

Every case asserts the status, where each cluster ended up, the share audit, the rebind record,
the exact rebind warning and how many strict Authentik role reads the rebind made.
"""

import logging
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Tuple, Union

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import get_db
from app.models.db_models import Organization, OrganizationCluster, OrganizationMember
from app.routers import integrations
from app.services import integration_cluster_rebind
from app.services.integration_cluster_rebind import REBIND_ACTION
from app.services.integration_roles import ROLE_ADMIN, ROLE_PARTNER, ROLE_TENANT
from app.services.lxd_service import get_lxd_service
from tests._sqlite_session import AsyncSessionFacade, installation_rows, org_tables, sqlite_session
from tests.test_a_push_rebinds_only_into_the_org_its_key_claimed import (
    K_ATTACKER,
    K_PARTNER,
    PARTNER,
    SECRETS,
    key,
    personal_cluster,
    world,  # noqa: F401
)

_HTTP = httpx.AsyncClient
SYNC = "/api/v1/integrations/sync"
C, CO, F = PARTNER, "copartner@example.test", "founder@example.test"
NAMES = {"own": ("ext-own", "own"), "buyer": ("ext-buyer", "buyer"), "buyer2": ("ext-buyer2", "buyer-two"),
         "theirs": ("ext-theirs", "theirs"), "clash": ("ext-clash", "own-renamed")}
OrgRef = Union[str, Tuple[str, str]]

WARN = {
    "count": "Integration key {k} rebinds no personal cluster: it claims {n} organizations, exactly one is required",
    "names": "Integration key {k} rebinds no personal cluster: its push names {n} organizations, exactly one is "
             "required",
    "other": "Integration key {k} rebinds no personal cluster: the organization its push names is not the one it "
             "claims",
    "dest": "Integration key {k} rebinds no personal cluster: {n} organizations it claimed name its creator in "
            "this push, exactly one is required",
    "admin": "Integration key {k} rebinds no personal cluster: its creator is an Admin",
    "unknown": "Integration key {k} rebinds no personal cluster: its creator's role could not be read ({n})",
}


def route_app(api_key: Dict[str, Any], db: Any) -> FastAPI:
    """The external router with its real capability gates. The session is rolled back at the end of
    each request, the way closing the real one discards anything the handler left uncommitted."""
    app = FastAPI()
    app.include_router(integrations.external_router, prefix="/api/v1")

    async def _key() -> Dict[str, Any]:
        return api_key

    async def _db() -> AsyncIterator[Any]:
        try:
            yield db
        finally:
            await db.rollback()

    async def _none() -> None:
        return None

    app.dependency_overrides[integrations.validate_api_key] = _key
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_lxd_service] = _none
    return app


async def send(api_key: Dict[str, Any], db: Any, body: Any, *, method: str = "POST",
               path: str = SYNC) -> httpx.Response:
    async with _HTTP(transport=httpx.ASGITransport(app=route_app(api_key, db)), base_url="http://orcastra.test") as c:
        return await c.request(method, path, json=body)


def ref(name: OrgRef) -> Tuple[str, str]:
    return NAMES[name] if isinstance(name, str) else name


def U(email: str, org: OrgRef, role: str = "PARTNER", action: Optional[str] = None) -> Dict[str, Any]:
    pushed: Dict[str, Any] = {"email": email, "name": None, "organization_id": ref(org)[0], "role": role}
    if action is not None:
        pushed["action"] = action
    return pushed


def sync_body(orgs: Sequence[OrgRef], members: List[Dict[str, Any]]) -> Dict[str, Any]:
    listed = [{"id": ext, "name": slug.title(), "slug": slug, "description": None} for ext, slug in map(ref, orgs)]
    return {"include_clusters": True, "include_projects": True, "organizations": listed, "members": members}


def S(name: str, **kw: Any) -> Tuple[str, Dict[str, Any]]:
    return name, kw


async def seed(db: AsyncSessionFacade, name: str, *, owner: Optional[str] = K_PARTNER, active: bool = True,
               source: str = "orcahub", ext: Any = "", rows: Sequence[Tuple[str, str]] = ()) -> int:
    default_ext, slug = NAMES[name]
    db.add(Organization(name=slug.title(), slug=slug, source=source, api_key_id=owner, created_by="system",
                        is_active=active, external_id=default_ext if ext == "" else ext))
    await db.commit()
    oid = (await db.execute(select(Organization.id).where(Organization.slug == slug))).scalar_one()
    for email, role in rows:
        db.add(OrganizationMember(organization_id=oid, user_email=email, role=role))
    await db.commit()
    return oid


async def placed(db: AsyncSessionFacade) -> Dict[str, Optional[str]]:
    """Each cluster row, by cluster id, to the slug of the org holding it (None while personal)."""
    rows = (await db.execute(select(OrganizationCluster.cluster_id, Organization.slug).outerjoin(
        Organization, Organization.id == OrganizationCluster.organization_id))).all()
    return {cluster: slug for cluster, slug in rows}


@pytest.fixture
def hub(world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
        caplog: pytest.LogCaptureFixture) -> Dict[str, Any]:
    """world plus the strict role reads the rebind makes and the rebind warnings it logs."""
    strict: List[str] = []
    lenient = world["directory"].read_current_roles

    async def _strict(email: str) -> Any:
        strict.append(email)
        return await lenient(email)

    monkeypatch.setattr(world["directory"], "read_current_roles", _strict)
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)

    def warnings() -> List[str]:
        return [r.getMessage() for r in caplog.records if r.name == integration_cluster_rebind.__name__]

    return {**world, "strict": strict, "warnings": warnings, "caplog": caplog}


def expect(hub: Dict[str, Any], placed_now: Dict[str, Optional[str]], dest: Optional[str],
           warn: Optional[Tuple[str, Any]], key_id: str = K_PARTNER, cluster: str = "c-cluster") -> None:
    """dest is the slug the creator's cluster must sit in (None: still personal)."""
    assert placed_now[cluster] == dest
    if dest is None:
        assert hub["shares"] == [] and hub["log"].calls == []
    else:
        (record,) = hub["log"].calls
        assert record["action"] == REBIND_ACTION and record["target_id"] == cluster
        assert record["details"]["api_key_id"] == key_id and record["details"]["from_organization_id"] is None
        assert [s[0] for s in hub["shares"]] == [cluster]
    warned = [WARN[warn[0]].format(k=key_id, n=warn[1] if len(warn) > 1 else "")] if warn else []
    assert hub["warnings"]() == warned
    assert not any(secret in hub["caplog"].text for secret in SECRETS)
    reads = 1 if dest is not None or (warn and warn[0] in ("admin", "unknown")) else 0
    assert hub["strict"] == [C] * reads


async def run(hub: Dict[str, Any], seeds: Sequence[Tuple[str, Dict[str, Any]]],
              pushes: Sequence[Tuple[Sequence[OrgRef], List[Dict[str, Any]]]], **key_extra: Any) -> AsyncSessionFacade:
    db = sqlite_session(*org_tables())
    if key_extra.get("extension_install_id") is not None:
        # note: an add-on credential claims only while it is its installation's current key
        installation_rows(db, (K_PARTNER, None))
    for name, kw in seeds:
        await seed(db, name, **kw)
    await personal_cluster(db, "c-cluster", C)
    for orgs, members in pushes:
        response = await send(key(K_PARTNER, C, **key_extra), db, sync_body(orgs, members))
        assert response.status_code == 200, response.text
    return db


def one(orgs: Sequence[OrgRef], members: List[Dict[str, Any]]) -> List[Tuple[Sequence[OrgRef], List[Dict[str, Any]]]]:
    return [(orgs, members)]


OWN = one(["own"], [U(C, "own")])

# note: id, seeds, pushes, destination slug, warning. The count is read after the org upsert.
COUNTS = [
    ("none_before_one_pushed", [], OWN, "own", None),
    ("none_before_two_pushed", [], one(["own", "buyer"], [U(C, "own"), U(F, "buyer")]), None, ("count", 2)),
    ("none_before_three_pushed", [], one(["own", "buyer", "buyer2"], [U(C, "own")]), None, ("count", 3)),
    ("none_before_the_same_org_pushed_twice", [], one(["own", "own"], [U(C, "own")]), "own", None),
    ("own_before_own_pushed", [S("own")], OWN, "own", None),
    ("own_before_members_only", [S("own")], one([], [U(C, "own")]), None, ("names", 0)),
    ("own_before_own_and_a_buyer_pushed", [S("own")], one(["own", "buyer"], [U(C, "own"), U(F, "buyer")]),
     None, ("count", 2)),
    ("own_before_a_buyer_alone_names_the_creator", [S("own", rows=[(C, "partner")])],
     one(["buyer"], [U(F, "buyer"), U(C, "buyer")]), None, ("count", 2)),
    ("buyer_before_own_pushed", [S("buyer")], OWN, None, ("count", 2)),
    ("own_and_buyer_before_own_pushed", [S("own"), S("buyer")], OWN, None, ("count", 2)),
    ("own_and_buyer_before_members_only", [S("own"), S("buyer")], one([], [U(C, "own")]), None, ("count", 2)),
    ("own_and_two_buyers_before_own_pushed", [S("own"), S("buyer"), S("buyer2")], OWN, None, ("count", 3)),
    ("another_keys_org_does_not_count", [S("buyer", owner=K_ATTACKER)], OWN, "own", None),
    ("an_unclaimed_org_does_not_count", [S("buyer", owner=None)], OWN, "own", None),
    ("an_empty_owner_does_not_count", [S("buyer", owner="")], OWN, "own", None),
    ("the_key_id_in_capitals_does_not_count", [S("buyer", owner=K_PARTNER.upper())], OWN, "own", None),
    ("the_key_id_padded_does_not_count", [S("buyer", owner=f" {K_PARTNER}")], OWN, "own", None),
    ("the_key_id_with_a_newline_does_not_count", [S("buyer", owner=f"{K_PARTNER}\n")], OWN, "own", None),
    ("a_local_org_holding_the_key_id_counts", [S("buyer", source="local", ext=None)], OWN, None, ("count", 2)),
    ("a_refused_org_in_the_push_does_not_count", [S("theirs", owner=K_ATTACKER)],
     one(["own", "theirs"], [U(C, "own"), U(C, "theirs")]), None, ("names", 2)),
    ("a_slug_a_local_org_holds_is_skipped_and_not_counted", [S("buyer", owner=None, source="local", ext=None)],
     one(["own", "buyer"], [U(C, "own"), U(F, "buyer")]), None, ("names", 2)),
    ("the_one_org_pushed_is_refused_and_another_is_claimed", [S("own"), S("theirs", owner=K_ATTACKER)],
     one(["theirs"], [U(C, "own")]), None, ("other",)),
    ("the_one_org_pushed_is_skipped_and_a_buyer_is_claimed",
     [S("buyer"), S("own", owner=None, source="local", ext=None)],
     one(["own"], [U(F, "buyer"), U(C, "buyer")]), None, ("other",)),
    ("the_push_claims_an_unclaimed_org_and_counts_it", [S("buyer", owner=None)],
     one(["own", "buyer"], [U(C, "own"), U(F, "buyer")]), None, ("count", 2)),
    ("its_own_retired_org_is_relinked_not_added", [S("own", ext="ext-own-old")], OWN, "own", None),
    ("an_unclaimed_retired_org_is_relinked_and_counted", [S("buyer", owner=None, ext="ext-buyer-old")],
     one(["own", "buyer"], [U(C, "own")]), None, ("count", 2)),
    ("another_keys_retired_org_is_not_relinked", [S("buyer", owner=K_ATTACKER, ext="ext-buyer-old")],
     one(["own", "buyer"], [U(C, "own")]), None, ("names", 2)),
    ("an_external_id_in_capitals_is_a_second_org", [S("own")],
     one([("EXT-OWN", "own-caps")], [U(C, ("EXT-OWN", "own-caps"))]), None, ("count", 2)),
    ("an_external_id_in_capitals_on_its_own_slug_relinks", [S("own")],
     one([("EXT-OWN", "own")], [U(C, ("EXT-OWN", "own"))]), "own", None),
]

INACTIVE = [
    ("own_inactive_reactivated_by_the_push", [S("own", active=False)], OWN, "own", None),
    ("own_inactive_named_only_in_members", [S("own", active=False)], one([], [U(C, "own")]), None, ("names", 0)),
    ("own_inactive_kept_by_an_update_that_failed",
     [S("own", active=False), S("clash", owner=None, source="local", ext=None)],
     one([("ext-own", "own-renamed")], [U(C, "own")]), None, ("dest", 0)),
    ("an_inactive_buyer_counts", [S("buyer", active=False)], OWN, None, ("count", 2)),
    ("own_inactive_beside_a_buyer_naming_the_creator", [S("own", active=False, rows=[(C, "partner")])],
     one(["buyer"], [U(F, "buyer"), U(C, "buyer")]), None, ("count", 2)),
    ("own_inactive_reactivated_beside_a_buyer", [S("own", active=False)],
     one(["own", "buyer"], [U(C, "own"), U(F, "buyer")]), None, ("count", 2)),
    ("both_inactive_own_reactivated", [S("own", active=False), S("buyer", active=False)], OWN, None, ("count", 2)),
    ("another_keys_inactive_org_does_not_count", [S("buyer", owner=K_ATTACKER, active=False)], OWN, "own", None),
    ("an_unclaimed_inactive_org_does_not_count", [S("buyer", owner=None, active=False)], OWN, "own", None),
    ("an_unclaimed_inactive_org_the_push_claims_counts", [S("buyer", owner=None, active=False)],
     one(["own", "buyer"], [U(C, "own")]), None, ("count", 2)),
]

REMOVES = [
    ("upsert_then_remove_in_the_only_org", [], one(["own"], [U(C, "own"), U(C, "own", action="remove")]),
     None, ("dest", 0)),
    ("remove_then_upsert_in_the_only_org", [], one(["own"], [U(C, "own", action="remove"), U(C, "own")]),
     "own", None),
    ("remove_then_upsert_over_an_existing_row", [S("own", rows=[(C, "partner")])],
     one(["own"], [U(C, "own", action="remove"), U(C, "own")]), "own", None),
    ("only_a_remove_of_the_creator", [S("own", rows=[(C, "partner")])],
     one(["own"], [U(C, "own", action="remove")]), None, None),
    ("a_remove_of_another_member", [S("own", rows=[(C, "partner"), (CO, "partner")])],
     one(["own"], [U(CO, "own", action="remove"), U(C, "own")]), "own", None),
    ("the_creator_removed_from_the_buyer_org", [],
     one(["own", "buyer"], [U(C, "own"), U(C, "buyer"), U(C, "buyer", action="remove")]), None, ("count", 2)),
    ("every_buyer_member_removed_in_the_push", [],
     one(["own", "buyer"], [U(C, "own"), U(F, "buyer"), U(F, "buyer", action="remove")]), None, ("count", 2)),
    ("a_claimed_buyer_emptied_by_removes", [S("buyer", rows=[(F, "partner")])],
     one(["own"], [U(C, "own"), U(F, "buyer", action="remove")]), None, ("count", 2)),
    ("a_remove_in_another_keys_org", [S("theirs", owner=K_ATTACKER, rows=[(C, "partner")])],
     one(["own"], [U(C, "own"), U(C, "theirs", action="remove")]), "own", None),
]


@pytest.mark.parametrize("seeds, pushes, dest, warn", [c[1:] for c in COUNTS + INACTIVE + REMOVES],
                         ids=[c[0] for c in COUNTS + INACTIVE + REMOVES])
async def test_the_count_after_the_push_decides(
    hub: Dict[str, Any], seeds: Any, pushes: Any, dest: Optional[str], warn: Optional[Tuple[str, Any]],
) -> None:
    db = await run(hub, seeds, pushes)
    expect(hub, await placed(db), dest, warn)


async def test_a_push_never_unclaims_an_organization(hub: Dict[str, Any]) -> None:
    """Removes, omissions and a members-only push leave every claim in place."""
    db = await run(hub, [S("own"), S("buyer", rows=[(F, "partner")])],
                   one([], [U(F, "buyer", action="remove"), U(C, "own"), U(C, "buyer", action="remove")]))
    owners = (await db.execute(select(Organization.slug, Organization.api_key_id))).all()
    assert sorted(owners) == [("buyer", K_PARTNER), ("own", K_PARTNER)]
    expect(hub, await placed(db), None, ("count", 2))


@pytest.mark.parametrize("action", ["REMOVE", "Remove", " remove", "remove ", "delete", "removed", "", "upsert"])
async def test_only_the_exact_remove_spelling_removes(hub: Dict[str, Any], action: str) -> None:
    db = await run(hub, [], one(["own"], [U(C, "own"), U(C, "own", action=action)]))
    expect(hub, await placed(db), "own", None)


# note: decision 14 inside the one-org rule. The count comes first, so two orgs never ask Authentik.

D14 = [
    ("partner_one_org", {ROLE_PARTNER}, ["own"], {}, "own", None),
    ("tenant_one_org", {ROLE_TENANT}, ["own"], {}, "own", None),
    ("no_group_one_org", set(), ["own"], {}, "own", None),
    ("admin_one_org", {ROLE_ADMIN}, ["own"], {}, None, ("admin",)),
    ("alias_admin_one_org", {"authentik Admins"}, ["own"], {}, None, ("admin",)),
    ("admin_beside_partner_one_org", {ROLE_ADMIN, ROLE_PARTNER}, ["own"], {}, None, ("admin",)),
    ("unknown_one_org", None, ["own"], {}, None, ("unknown", "authentik_unreachable")),
    ("admin_two_orgs", {ROLE_ADMIN}, ["own", "buyer"], {}, None, ("count", 2)),
    ("unknown_two_orgs", None, ["own", "buyer"], {}, None, ("count", 2)),
    ("add_on_one_org", {ROLE_PARTNER}, ["own"], {"extension_install_id": 7}, None, None),
    ("add_on_two_orgs", {ROLE_PARTNER}, ["own", "buyer"], {"extension_install_id": "7"}, None, None),
]


@pytest.mark.parametrize("groups, orgs, extra, dest, warn", [c[1:] for c in D14], ids=[c[0] for c in D14])
async def test_decision_14_after_the_count(
    hub: Dict[str, Any], groups: Optional[set], orgs: List[str], extra: Dict[str, Any], dest: Optional[str],
    warn: Optional[Tuple[str, ...]],
) -> None:
    hub["directory"].groups[C] = groups
    db = await run(hub, [], one(orgs, [U(C, o) for o in orgs]), **extra)
    expect(hub, await placed(db), dest, warn)


# note: decision 15 inside the one-org rule: named in this push, as partner, in that org, row read back

D15 = [
    ("named_user_in_the_only_org", [], one(["own"], [U(CO, "own"), U(C, "own", "USER")]), None, ("dest", 0)),
    ("named_tenant_in_the_only_org", [], one(["own"], [U(C, "own", "TENANT")]), None, ("dest", 0)),
    ("absent_from_the_push", [S("own", rows=[(C, "partner")])], one(["own"], [U(CO, "own")]), None, None),
    ("partner_only_in_another_keys_org", [S("theirs", owner=K_ATTACKER)],
     one(["own", "theirs"], [U(CO, "own"), U(C, "theirs")]), None, None),
    ("partner_in_another_keys_org_user_in_own", [S("theirs", owner=K_ATTACKER)],
     one(["own", "theirs"], [U(C, "own", "USER"), U(C, "theirs")]), None, ("names", 2)),
    ("partner_in_another_keys_org_unlisted_user_in_own", [S("theirs", owner=K_ATTACKER)],
     one(["own"], [U(C, "own", "USER"), U(C, "theirs")]), None, ("dest", 0)),
    ("members_only_into_another_keys_org", [S("own"), S("theirs", owner=K_ATTACKER)],
     one([], [U(C, "theirs")]), None, None),
    ("a_stale_partner_row_and_user_in_this_push", [S("own", rows=[(C, "partner")])],
     one(["own"], [U(C, "own", "USER")]), None, ("dest", 0)),
    ("partner_twice_in_the_only_org", [], one(["own"], [U(C, "own"), U(C, "own", "partner")]), "own", None),
    ("user_then_partner_in_the_only_org", [], one(["own"], [U(C, "own", "USER"), U(C, "own")]), "own", None),
]


@pytest.mark.parametrize("seeds, pushes, dest, warn", [c[1:] for c in D15], ids=[c[0] for c in D15])
async def test_decision_15_inside_the_one_org(
    hub: Dict[str, Any], seeds: Any, pushes: Any, dest: Optional[str], warn: Optional[Tuple[str, Any]],
) -> None:
    db = await run(hub, seeds, pushes)
    expect(hub, await placed(db), dest, warn)


async def test_a_cluster_registered_later_binds_while_the_key_still_claims_one_org(hub: Dict[str, Any]) -> None:
    """Scheduled pushes of the same one org: nothing moves twice, a new personal cluster moves once."""
    db = await run(hub, [], OWN)
    assert (await send(key(K_PARTNER, C), db, sync_body(["own"], [U(C, "own")]))).status_code == 200
    await personal_cluster(db, "later-cluster", C)
    assert (await send(key(K_PARTNER, C), db, sync_body(["own"], [U(C, "own")]))).status_code == 200
    assert await placed(db) == {"c-cluster": "own", "later-cluster": "own"}
    assert [c["target_id"] for c in hub["log"].calls] == ["c-cluster", "later-cluster"]
    assert hub["warnings"]() == [] and hub["strict"] == [C, C]


async def test_a_key_with_an_empty_id_counts_nothing(hub: Dict[str, Any]) -> None:
    """Its push stamps the org with an empty owner, and an empty owner is never a claim."""
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "c-cluster", C)
    assert (await send(key("", C), db, sync_body(["own"], [U(C, "own")]))).status_code == 200
    assert (await db.execute(select(Organization.slug, Organization.api_key_id))).all() == [("own", "")]
    expect(hub, await placed(db), None, ("count", 0), key_id="")


async def test_the_count_is_per_key(hub: Dict[str, Any]) -> None:
    """Another key's two orgs never block this key's one, and this key's one never lets the other bind."""
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "c-cluster", C)
    await personal_cluster(db, "a-cluster", "attacker@example.test")
    other = key(K_ATTACKER, "attacker@example.test")
    assert (await send(other, db, sync_body(["theirs", "buyer"], [U("attacker@example.test", "theirs")]))).status_code == 200
    assert (await send(key(K_PARTNER, C), db, sync_body(["own"], [U(C, "own")]))).status_code == 200
    assert await placed(db) == {"c-cluster": "own", "a-cluster": None}
    assert hub["warnings"]() == [WARN["count"].format(k=K_ATTACKER, n=2)]


# note: value types and bounds on the organization half of the body

ORG_FIELDS = ["id", "name", "slug", "description"]
BAD = [True, 1, 1.5, None, ["x"], {"x": 1}]


@pytest.mark.parametrize("field", ORG_FIELDS)
@pytest.mark.parametrize("value", BAD, ids=[repr(v) for v in BAD])
async def test_a_bad_organization_field_claims_nothing(hub: Dict[str, Any], field: str, value: Any) -> None:
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "c-cluster", C)
    sent = sync_body(["own"], [U(C, "own")])
    sent["organizations"][0][field] = value
    response = await send(key(K_PARTNER, C), db, sent)
    if field == "description" and value is None:
        assert response.status_code == 200
        expect(hub, await placed(db), "own", None)
        return
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "organizations", 0, field]
    assert (await db.execute(select(Organization.id))).all() == []
    expect(hub, await placed(db), None, None)


@pytest.mark.parametrize("value", [True, 1, "own", {"id": "ext-own"}, None, [None], ["ext-own"]],
                         ids=["bool", "number", "string", "object", "null", "list_of_null", "list_of_string"])
async def test_organizations_that_are_not_a_list_of_objects_claim_nothing(hub: Dict[str, Any], value: Any) -> None:
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "c-cluster", C)
    response = await send(key(K_PARTNER, C), db, {**sync_body([], [U(C, "own")]), "organizations": value})
    assert response.status_code == 422
    assert (await db.execute(select(Organization.id))).all() == []
    expect(hub, await placed(db), None, None)


async def test_more_than_a_thousand_organizations_claim_nothing(hub: Dict[str, Any]) -> None:
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "c-cluster", C)
    many = [(f"ext-{n}", f"org-{n}") for n in range(1001)]
    response = await send(key(K_PARTNER, C), db, sync_body(many, [U(C, many[0])]))
    assert response.status_code == 422
    assert (await db.execute(select(Organization.id))).all() == []
    expect(hub, await placed(db), None, None)
