"""A seller's key keeps its grant on a cluster its party holds with the buyers that key sold to.

OrcaHub syncs every buyer organization with created_by "system", so a synced buyer joins the
seller's party only through the key's home organization (cluster_parties.home_partner_keys), which
fails closed: an administrator bound the home, or the home was bound again after the buyers. The
seller's key then read its own buyers as another party: its `*` grant was cut to the creator's
projects (403 on every buyer project) and every purchase's project create was held to the browser's
isolated config, which refused OrcaHub's features.*=false.

The key's own sale organizations (cluster_parties.key_sale_orgs: active OrcaHub organizations the
key owns through an owner row that lends a party, bound by provisioning with no registrant and no
grantor) now join its creator's party for the rules that judge that key: its bound, its view-only
`default` and whether its next purchase skips the whole-cluster audit. Anything else on the cluster
still bounds it, and a creator who reaches the cluster only by a purchase stays bounded. These run
the real queries on SQLite rows.
"""

import ast
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

from app.models.db_models import Organization, OrganizationCluster, OrganizationMember, OrganizationOwnerKey
from app.routers import integrations
from app.services import cluster_parties
from app.services import key_default_view_only as kdv
from app.services import key_party_bound as kpb
from app.services.organization_owners import mark_entry
from app.services.shared_cluster_audit import SharedClusterAuditReport, SharedClusterGuardError
from tests._raw_proxy_fakes import CLUSTER_ID, call_raw_proxy, key
from tests._sqlite_session import sqlite_session

# Taken at import, before the conftest seams replace them for every test.
REAL_BOUND = kpb._bound
REAL_JUDGE = kdv._judge

SELLER = "seller@example.test"
FOUNDER = "founder@example.test"
KEY = "oak_test"  # tests._raw_proxy_fakes.key's id
OTHER_KEY = "oak_other"
SECOND_KEY = "oak_second"
ADMIN = "ops@example.test"
STAR = {**key(["*"]), "created_by": SELLER}
SECOND = {**STAR, "id": SECOND_KEY}
BOUND = frozenset({"default", "seller-own"})
# What OrcaHub sends for a purchase's project (production, 16/11/2026).
ORCAHUB_PROJECT = {
    "features.images": "false",
    "features.networks": "false",
    "features.profiles": "false",
    "features.storage.volumes": "false",
    "limits.cpu": "4",
    "limits.memory": "8GiB",
    "user.description": "OrcaHub purchase orc-p-2",
}


def _org(org_id: int, created_by: str = "system", key_id: Optional[str] = KEY, *, source: str = "orcahub",
         active: bool = True, via: Optional[str] = None) -> Organization:
    org = Organization(id=org_id, name=f"org-{org_id}", slug=f"org-{org_id}", created_by=created_by,
                       is_active=active, source=source, api_key_id=key_id)
    if via is not None:
        # how the before_flush listener records the primary's owner row
        mark_entry(org, via)
    return org


def _sale(org_id: int) -> OrganizationCluster:
    return OrganizationCluster(organization_id=org_id, cluster_id=CLUSTER_ID)


async def _world(*, home: str = "granted", extra: Optional[list] = None) -> Any:
    """The seller's home (org 1, synced, the seller a partner there) and two synced buyers of the
    seller's key (orgs 2 and 3) on CLUSTER_ID. The home cannot be proven, so the old party read the
    buyers as another party's: `granted` (an administrator bound the home), `rebound` (the home was
    bound again after the buyers)."""
    db = sqlite_session(Organization, OrganizationMember, OrganizationCluster, OrganizationOwnerKey)
    db.add_all([
        _org(1), OrganizationMember(organization_id=1, user_email=SELLER, role="partner"),
        _org(2), _org(3),
        OrganizationMember(organization_id=2, user_email=FOUNDER, role="partner"),
    ])
    await db.commit()
    home_binding = (OrganizationCluster(organization_id=1, cluster_id=CLUSTER_ID, granted_by=ADMIN) if home == "granted"
                    else OrganizationCluster(organization_id=1, cluster_id=CLUSTER_ID, registered_by=SELLER))
    if home == "granted":
        db.add_all([home_binding, _sale(2), _sale(3)])
    else:
        db.add_all([_sale(2), _sale(3)])
        await db.commit()
        db.add(home_binding)
    await db.commit()
    if extra:
        db.add_all(extra)
        await db.commit()
    return db


@pytest.fixture
def real_bound(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """The real bound and view-only readers; the creator is not an Admin; the bound's projects are BOUND."""
    state: Dict[str, Any] = {"admin": False}

    async def is_admin(_email: str, *, roles: Any = None) -> Optional[bool]:
        return state["admin"]

    async def projects(_db: Any, _email: str, cluster_ids: Any) -> Dict[str, set]:
        return {c: set(BOUND) for c in cluster_ids}

    monkeypatch.setattr(kpb, "_bound", REAL_BOUND)
    monkeypatch.setattr(kpb, "creator_is_admin", is_admin)
    monkeypatch.setattr(kdv, "creator_is_admin", is_admin)
    monkeypatch.setattr("app.core.org_scope.partner_projects_on", projects)
    kpb.forget_party_bounds()
    kdv.forget_key_default_verdicts()
    yield state
    kpb.forget_party_bounds()
    kdv.forget_key_default_verdicts()


def _wire_judge(monkeypatch: pytest.MonkeyPatch, db: Any) -> None:
    """The key doors open their own session for the view-only rule; hand them the test database."""
    async def judge(api_key: Any, cluster_id: str, _db: Any) -> Any:
        return await REAL_JUDGE(api_key, cluster_id, db)

    monkeypatch.setattr(kdv, "_judge", judge)


async def _unbounded(db: Any, api_key: Dict[str, Any] = STAR) -> bool:
    narrowed = await kpb.narrow_to_party_bound(dict(api_key), db=db)
    return narrowed == api_key and not kpb.is_party_bounded(narrowed, CLUSTER_ID)


# ── the party a key reads ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("home", ["granted", "rebound"])
async def test_the_buyers_a_key_sold_to_keep_its_grant(real_bound, home: str) -> None:
    db = await _world(home=home)
    assert await cluster_parties.clusters_held_by_another_party(db, SELLER, {CLUSTER_ID}) == {CLUSTER_ID}, (
        "the party the browser and key management read still cannot prove the home"
    )
    assert await cluster_parties.key_sale_orgs(db, KEY, {CLUSTER_ID}) == {2, 3}
    assert await cluster_parties.key_held_by_another_party(db, SELLER, KEY, CLUSTER_ID) is False
    assert await _unbounded(db)


def _foreign_buyer(org_id: int = 4, **binding: Any) -> list:
    return [_org(org_id), OrganizationCluster(organization_id=org_id, cluster_id=CLUSTER_ID, **binding)]


@pytest.mark.parametrize("extra", [
    pytest.param([_org(4, key_id=OTHER_KEY), _sale(4)], id="another-keys-buyer"),
    pytest.param([OrganizationCluster(organization_id=None, cluster_id=CLUSTER_ID, registered_by="other@example.test")],
                 id="unbound-registration-by-someone-else"),
    pytest.param(_foreign_buyer(registered_by="teammate@example.test"), id="key-owned-binding-with-a-registrant"),
    pytest.param(_foreign_buyer(granted_by=ADMIN), id="key-owned-binding-an-admin-granted"),
    pytest.param([_org(4, via="enroll"), _sale(4)], id="enrolled-owner-row"),
    pytest.param([_org(4, via="admin"), _sale(4)], id="admin-added-owner-row"),
    pytest.param([_org(4, created_by="local@example.test", source="local"), _sale(4)], id="local-organization"),
    pytest.param([_org(4, active=False), _sale(4)], id="inactive-organization"),
    pytest.param([_org(4, key_id=None), _sale(4)], id="unowned-organization"),
])
async def test_anything_else_on_the_cluster_still_bounds_the_key(real_bound, monkeypatch, extra: list) -> None:
    db = await _world(extra=extra)
    assert 4 not in await cluster_parties.key_sale_orgs(db, KEY, {CLUSTER_ID})
    assert await cluster_parties.key_held_by_another_party(db, SELLER, KEY, CLUSTER_ID) is True
    narrowed = await kpb.narrow_to_party_bound(dict(STAR), db=db)
    assert kpb.is_party_bounded(narrowed, CLUSTER_ID)
    assert narrowed["cluster_permissions"][0]["projects"] == sorted(BOUND)
    _wire_judge(monkeypatch, db)
    with pytest.raises(HTTPException) as refused:
        await kdv.refuse_key_default_operation(STAR, CLUSTER_ID, ["default"], db=db)
    assert refused.value.status_code == 403, "the view-only `default` rule agrees"


async def test_an_owner_row_lends_only_while_it_owns_the_organization(real_bound, monkeypatch) -> None:
    from app.services import organization_owners

    # The seller's key is a lending but not primary owner of buyer 4, whose primary is another key.
    db = await _world(extra=[_org(4, key_id=OTHER_KEY), _sale(4)])
    db.add(OrganizationOwnerKey(organization_id=4, api_key_id=KEY, added_via="claim"))
    await db.commit()
    monkeypatch.setattr(organization_owners, "multi_owner_enabled", lambda: False)
    assert 4 not in await cluster_parties.key_sale_orgs(db, KEY, {CLUSTER_ID}), "owner sets off: the primary owns it"
    assert await cluster_parties.key_held_by_another_party(db, SELLER, KEY, CLUSTER_ID) is True
    monkeypatch.setattr(organization_owners, "multi_owner_enabled", lambda: True)
    assert 4 in await cluster_parties.key_sale_orgs(db, KEY, {CLUSTER_ID})
    assert await cluster_parties.key_held_by_another_party(db, SELLER, KEY, CLUSTER_ID) is False


async def test_an_add_on_credential_counts_no_sale(real_bound, monkeypatch) -> None:
    from app.services import extension_scope

    async def snapshot(_db: Any, key_id: str) -> Dict[str, str]:
        return {"installed_by": SELLER}

    monkeypatch.setattr(extension_scope, "load_install_snapshot", snapshot)
    db = await _world()
    add_on = {**STAR, "extension_install_id": 7}
    assert cluster_parties.party_key_id(add_on) == ""
    assert await cluster_parties.key_sale_orgs(db, "", {CLUSTER_ID}) == set()
    narrowed = await kpb.narrow_to_party_bound(dict(add_on), db=db)
    assert kpb.is_party_bounded(narrowed, CLUSTER_ID), "judged by its installer's party alone"
    _wire_judge(monkeypatch, db)
    with pytest.raises(HTTPException):
        await kdv.refuse_key_default_operation(add_on, CLUSTER_ID, ["default"], db=db)


async def test_a_creator_reaching_the_cluster_only_by_a_purchase_stays_bounded(real_bound, monkeypatch) -> None:
    # The founder runs buyer 2 (a partner row), which the founder's own key owns, and nothing else
    # holds the cluster: the party alone calls it the founder's.
    db = sqlite_session(Organization, OrganizationMember, OrganizationCluster, OrganizationOwnerKey)
    db.add_all([_org(2, key_id="oak_founder"), OrganizationMember(organization_id=2, user_email=FOUNDER, role="partner")])
    await db.commit()
    db.add(_sale(2))
    await db.commit()
    founder_key = {**key(["*"]), "id": "oak_founder", "created_by": FOUNDER}
    assert await cluster_parties.clusters_held_by_another_party(db, FOUNDER, {CLUSTER_ID}) == set()
    assert await cluster_parties.key_sale_orgs(db, "oak_founder", {CLUSTER_ID}) == {2}
    assert await cluster_parties.key_held_by_another_party(db, FOUNDER, "oak_founder", CLUSTER_ID) is True
    assert kpb.is_party_bounded(await kpb.narrow_to_party_bound(dict(founder_key), db=db), CLUSTER_ID)
    _wire_judge(monkeypatch, db)
    with pytest.raises(HTTPException):
        await kdv.refuse_key_default_operation(founder_key, CLUSTER_ID, ["default"], db=db)


async def test_a_key_with_no_creator_counts_no_sale(real_bound) -> None:
    db = await _world()
    assert await cluster_parties.key_held_by_another_party(db, "", KEY, CLUSTER_ID) is True


async def test_an_admin_creators_key_stays_unbounded_and_an_unanswered_idp_stays_bounded(real_bound) -> None:
    db = await _world(extra=[_org(4, key_id=OTHER_KEY), _sale(4)])
    real_bound["admin"] = True
    assert await _unbounded(db)
    kpb.forget_party_bounds()
    real_bound["admin"] = None
    narrowed = await kpb.narrow_to_party_bound(dict(STAR), db=db)
    assert kpb.is_party_bounded(narrowed, CLUSTER_ID) and narrowed[kpb.ROLE_UNKNOWN] == [CLUSTER_ID]


async def test_the_view_only_default_agrees_where_the_key_is_unbounded(real_bound, monkeypatch) -> None:
    db = await _world()
    _wire_judge(monkeypatch, db)
    await kdv.refuse_key_default_operation(STAR, CLUSTER_ID, ["default"], db=db)
    assert await kdv._held_by_another_party(db, SELLER, CLUSTER_ID, KEY) is False
    assert await kdv._held_by_another_party(db, SELLER, CLUSTER_ID, SECOND_KEY) is True, "a key that sold nothing"


# ── what OrcaHub's purchase sends now reaches the cluster unchanged ────────────────────────────

async def test_a_raw_project_create_passes_orcahubs_config_through(real_bound, monkeypatch) -> None:
    db = await _world()
    _wire_judge(monkeypatch, db)
    api_key = await kpb.narrow_to_party_bound(dict(STAR), db=db)
    body = {"name": "orc-p-2", "config": dict(ORCAHUB_PROJECT)}
    result, session = await call_raw_proxy(api_key, "POST", "/1.0/projects", project="default", body=body)
    assert not isinstance(result, HTTPException), result
    posts = [kwargs for verb, url, kwargs in session.calls if verb == "POST" and url.endswith("/1.0/projects")]
    assert posts and posts[0]["json"] == body, "sent as OrcaHub wrote it"


async def test_a_structured_project_create_passes_orcahubs_config_through(real_bound, monkeypatch) -> None:
    from types import SimpleNamespace

    from app.routers.integrations import ProxyRequest, proxy_operation

    db = await _world()
    _wire_judge(monkeypatch, db)
    sent: List[Any] = []
    monkeypatch.setattr(integrations, "execute_proxy_operation",
                        lambda *args, **_kwargs: sent.append((args[2], args[3])) or {"ok": True})
    api_key = await kpb.narrow_to_party_bound(dict(STAR), db=db)
    params = {"name": "orc-p-2", "config": dict(ORCAHUB_PROJECT)}
    answer = await proxy_operation(
        request=ProxyRequest(cluster_id=CLUSTER_ID, operation="create_project", project="default", params=params),
        api_key=api_key, lxd_service=SimpleNamespace(), _=None,
    )
    assert answer.success is True, answer
    assert sent == [("create_project", params)]


async def test_where_another_party_holds_the_cluster_the_same_create_is_still_refused(real_bound, monkeypatch) -> None:
    """The production refusal, kept where it belongs: the cluster is another party's for this key."""
    async def no_one_else(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def holder(*_args: Any, **_kwargs: Any) -> int:
        return 1

    db = await _world(extra=[_org(4, key_id=OTHER_KEY), _sale(4)])
    monkeypatch.setattr("app.services.api_key_grant_bound.refuse_a_project_another_party_holds", no_one_else)
    monkeypatch.setattr("app.core.org_scope.org_to_hold_project_grant", holder)
    api_key = await kpb.narrow_to_party_bound(dict(STAR), db=db)
    body = {"name": "orc-p-2", "config": dict(ORCAHUB_PROJECT)}
    result, session = await call_raw_proxy(api_key, "POST", "/1.0/projects", project="default", body=body)
    assert isinstance(result, HTTPException) and 400 <= result.status_code < 500, result
    assert not [c for c in session.calls if c[0] == "POST"]


# ── provisioning agrees with the key's bound ────────────────────────────────────────────────────

def _refusal() -> SharedClusterGuardError:
    report = SharedClusterAuditReport(cluster_id=CLUSTER_ID, safe=False, complete=True,
                                      projects_checked=1, profiles_checked=1, instances_checked=1, findings=())
    return SharedClusterGuardError("cluster_not_safe_to_share", "not safe", report, status_code=409)


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[Any]]:
    calls: Dict[str, List[Any]] = {"whole": [], "reach": []}

    async def whole(_db: Any, cluster_id: str, **_kwargs: Any) -> None:
        calls["whole"].append(cluster_id)
        raise _refusal()

    async def reach(cluster_id: str, project_name: str) -> None:
        calls["reach"].append((cluster_id, project_name))

    monkeypatch.setattr(integrations, "assert_cluster_safe_to_share", whole)
    monkeypatch.setattr(integrations, "assert_purchase_reach_safe", reach)
    return calls


async def _second_purchase(db: Any, api_key_id: Optional[str]) -> bool:
    """Buyer 5 is synced too (created_by "system"). One that provisioning created would carry the
    minter as created_by, and cluster_parties.side_keys already puts the key on the minter's side
    through it."""
    return await integrations._ensure_provision_cluster_mapping(
        db, organization_id=5, cluster_id=CLUSTER_ID, registered_by=SELLER, key_minter=SELLER,
        project_name="orc-p-2", carries_the_key=True, api_key_id=api_key_id,
    )


async def test_a_second_purchase_after_a_synced_buyer_is_inside_the_party(real_bound, audits) -> None:
    db = await _world(extra=[_org(5)])
    assert await integrations._held_outside_the_key_party(db, CLUSTER_ID, SELLER, KEY) is False
    assert await _second_purchase(db, KEY) is True
    assert audits == {"whole": [], "reach": [(CLUSTER_ID, "orc-p-2")]}
    assert await _unbounded(db), "the key's bound agrees after the binding"


async def test_without_the_key_the_purchase_is_still_audited_whole(real_bound, audits) -> None:
    db = await _world(extra=[_org(5)])
    with pytest.raises(SharedClusterGuardError):
        await _second_purchase(db, None)
    assert audits["whole"] == [CLUSTER_ID]


async def test_provisioning_and_the_bound_agree_where_another_party_holds_the_cluster(real_bound, audits) -> None:
    db = await _world(extra=[_org(4, key_id=OTHER_KEY), _sale(4), _org(5)])
    assert await integrations._held_outside_the_key_party(db, CLUSTER_ID, SELLER, KEY) is True
    with pytest.raises(SharedClusterGuardError):
        await _second_purchase(db, KEY)
    assert audits["whole"] == [CLUSTER_ID]
    assert kpb.is_party_bounded(await kpb.narrow_to_party_bound(dict(STAR), db=db), CLUSTER_ID)


def _calls_in(function: ast.AST) -> List[str]:
    return [getattr(c.func, "id", None) or getattr(c.func, "attr", None) for c in ast.walk(function)
            if isinstance(c, ast.Call)]


def _function(module: str, name: str) -> ast.AST:
    source = (Path(integrations.__file__).resolve().parents[1] / module).read_text()
    return next(n for n in ast.walk(ast.parse(source))
                if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name)


def test_the_provision_route_passes_its_key_and_drops_the_bounds_after_the_commit() -> None:
    route = _function("routers/integrations.py", "provision_tenant_access")
    mapping = next(c for c in ast.walk(route) if isinstance(c, ast.Call)
                   and getattr(c.func, "id", None) == "_ensure_provision_cluster_mapping")
    passed = {kw.arg: ast.unparse(kw.value) for kw in mapping.keywords}
    assert passed["api_key_id"] == "cluster_parties.party_key_id(api_key)"
    source = ast.unparse(route)
    assert source.index("await db.commit()") < source.index("_provision_binding_changed(body.cluster_id)")


# ── the cached verdicts ─────────────────────────────────────────────────────────────────────────

async def test_two_keys_of_one_creator_get_separate_verdicts(real_bound) -> None:
    db = await _world()
    assert (await kpb._bound(STAR, CLUSTER_ID, db))[1] is None
    assert (await kpb._bound(SECOND, CLUSTER_ID, db))[1] == BOUND, "the second key sold nothing here"
    assert (await kpb._bound(STAR, CLUSTER_ID, db))[1] is None, "the first verdict is not overwritten"
    assert {k for k in kpb._bounds} == {(SELLER, CLUSTER_ID, KEY), (SELLER, CLUSTER_ID, SECOND_KEY)}
    kpb.forget_creator_bound(SELLER.upper(), CLUSTER_ID)
    assert kpb._bounds == {}, "a creator's bound is dropped for every key of theirs"
    await kpb._bound(STAR, CLUSTER_ID, db)
    await kdv._held_by_another_party(db, SELLER, CLUSTER_ID, KEY)
    kpb.forget_cluster_bounds(CLUSTER_ID)
    assert kpb._bounds == {} and kdv._held == {}


async def test_a_foreign_bind_drops_a_cached_unbounded_verdict(real_bound) -> None:
    db = await _world()
    assert await _unbounded(db)
    db.add_all([_org(4, key_id=OTHER_KEY)])
    await db.commit()
    db.add(_sale(4))
    await db.commit()
    assert await _unbounded(db), "served from the cache within the window"
    kpb.forget_cluster_bounds(CLUSTER_ID)  # what every binding path calls once it commits
    assert kpb.is_party_bounded(await kpb.narrow_to_party_bound(dict(STAR), db=db), CLUSTER_ID)


async def test_a_bind_on_another_worker_reaches_a_cached_unbounded_verdict(real_bound, monkeypatch) -> None:
    import time

    stamp: Dict[str, float] = {}

    class _Cache:
        def get(self, _ns: str, cluster_id: str) -> Optional[float]:
            return stamp.get(cluster_id)

        def set(self, _ns: str, cluster_id: str, value: float, ttl: Optional[int] = None) -> None:
            stamp[cluster_id] = value

    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: _Cache())
    db = await _world()
    assert await _unbounded(db)
    assert await kdv._held_by_another_party(db, SELLER, CLUSTER_ID, KEY) is False
    db.add(_org(4, key_id=OTHER_KEY))
    await db.commit()
    db.add(_sale(4))
    await db.commit()
    stamp[CLUSTER_ID] = time.time() + 1  # another worker bound it and published, this one kept its cache
    assert kpb.is_party_bounded(await kpb.narrow_to_party_bound(dict(STAR), db=db), CLUSTER_ID)
    assert await kdv._held_by_another_party(db, SELLER, CLUSTER_ID, KEY) is True


@pytest.mark.parametrize("module,function,call", [
    ("routers/integrations.py", "_provision_binding_changed", "forget_cluster_bounds"),
    ("routers/cluster_registration.py", "register_cluster", "forget_cluster_bounds"),
    ("routers/organizations.py", "_binding_changed", "forget_cluster_bounds"),
    ("services/organization_service.py", "delete_organization", "forget_cluster_bounds"),
])
def test_every_binding_create_or_removal_drops_the_bounds(module: str, function: str, call: str) -> None:
    assert call in _calls_in(_function(module, function)), f"{module}::{function}"


def test_a_deregistration_drops_the_bounds() -> None:
    source = (Path(integrations.__file__).resolve().parents[1] / "routers" / "cluster_registration.py").read_text()
    after_delete = source.split("deleted_rows = await asyncio.wait_for(_delete_binding()", 1)[1][:400]
    assert "forget_cluster_bounds(cluster_id)" in after_delete
