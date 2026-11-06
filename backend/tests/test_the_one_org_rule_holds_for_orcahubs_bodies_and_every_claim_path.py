"""Decision 19 against OrcaHub v1.32.0's own bodies, and against every path that claims an org for a key.

orcastra.service.js syncOrcastraClustersUnlocked pushes the connection's own org first and then
every buyer org with a PROCESSING or COMPLETED order on its plans, with each org's full roster (no
action field, name null for some users, roles PARTNER, TENANT and USER). provisioning.order.js
calls provision-tenant-access at purchase with the buyer org's id, name and slug, which creates
the buyer org under the seller's key before any push names it. The dashboard's
refresh-organizations pulls orgs from the key's callback. Those three paths claim an org for a key.
The only other writers of organizations.api_key_id move an org between keys and never release one:
an Administrator's owner transfer (PUT /integrations/organizations/{id}/owner-key) and an add-on
credential's rotation, which carries the old credential's orgs to the new one. So these are the
count's only inputs.

A buyer org's PARTNER can add any existing user, the seller's creator included, as PARTNER
(routes/organizations/members.js invite). Decision 19 says a buyer naming the creator PARTNER can
then neither steer nor block the rebind. The key's one claimed org can itself be a buyer org (a new
key, a second connection, a seller slug a local org holds), so a push rebinds only when its
organizations list names exactly one org and the key claims exactly that one.
"""

import ast
import re
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import JSON, MetaData, select
from sqlalchemy.dialects.postgresql import JSONB

from app.core import rbac
from app.core.auth import get_current_user
from app.core.database import get_db
from app.core.org_scope import org_scope_dependency
from app.models.db_models import (
    Organization,
    OrganizationPreferences,
    TenantAccessPolicy,
    TenantPolicyRule,
)
from app.routers import integrations
from app.services.integration_callback_url import APP_URL_SECRET_TAG, callback_url_tag
from app.services.lxd import registry as cluster_registry
from app.services.lxd.proxy_operations import PROXY_OPERATION_LEVELS
from app.services.lxd_service import get_lxd_service
from tests._callback_fakes import answer_callbacks  # noqa: E402
from tests._sqlite_session import AsyncSessionFacade, org_tables, sqlite_session
from tests.test_a_capability_grant_leaves_a_record import PARTNER as PARTNER_LOGIN
from tests.test_a_capability_grant_leaves_a_record import SECRETS, _FakeVault, _RecordingLogger
from tests.test_a_push_rebinds_only_into_the_org_its_key_claimed import K_ATTACKER, K_PARTNER, key, personal_cluster
from tests.test_the_one_org_rule_holds_through_the_sync_route import (
    C,
    F,
    WARN,
    expect,
    hub,  # noqa: F401
    placed,
    send,
    world,  # noqa: F401
)

_HTTP = httpx.AsyncClient
K_NEW = "oak_partner_new"
SELLER = "5b0c7a4e-0d7c-4f6e-9a57-1c3f0b8e2d11"
BUYER1 = "9e1f5d2a-4b6c-4c8d-8e0f-2a3b4c5d6e7f"
BUYER2 = "3c9d2e1f-6a7b-4c8d-9e0f-1a2b3c4d5e6f"
HUB = {SELLER: ("Acme Cloud", "acme-cloud", None), BUYER1: ("Buyer Co", "buyer-co", "bought a plan"),
       BUYER2: ("Second Buyer", "second-buyer", None)}
BUYER_LOGIN, T = "buyer@example.test", "t@example.test"
SALE = "sale-cluster"
FULL = ["view", "operate", "console", "snapshot", "file_transfer"]


def hub_body(orgs: Tuple[str, ...], members: List[Tuple[str, str, str]]) -> Dict[str, Any]:
    """syncOrcastraClustersUnlocked's body with BACKEND_URL unset: axios drops app_url."""
    return {
        "include_clusters": True, "include_projects": True,
        "organizations": [{"id": o, "name": HUB[o][0], "slug": HUB[o][1], "description": HUB[o][2]} for o in orgs],
        "members": [{"email": e, "name": None if r == "USER" else e.split("@")[0], "organization_id": o, "role": r}
                    for e, o, r in members],
    }


def provision_body(org: str, emails: List[str], **over: Any) -> Dict[str, Any]:
    """provisionTenantAccessViaClient's body as provisioning.order.js fills it."""
    return {"organization_external_id": org, "cluster_id": SALE, "project_name": "orc-p-1", "instance_name": "*",
            "user_emails": emails, "permissions": FULL, "organization_name": HUB[org][0],
            "organization_slug": HUB[org][1], **over}


def seller_key(key_id: str = K_PARTNER) -> Dict[str, Any]:
    """Granted the sale cluster and one id no cluster is registered under."""
    grant = [{"cluster_id": c, "projects": ["*"], "permissions": ["admin"]} for c in (SALE, "ghost")]
    return {**key(key_id, C), "cluster_permissions": grant}


def world_db() -> AsyncSessionFacade:
    """The org tables plus the policy and preference tables provisioning writes (JSONB read as JSON)."""
    db = sqlite_session(*org_tables())
    copy = MetaData()
    for model in (*org_tables(), TenantAccessPolicy, TenantPolicyRule, OrganizationPreferences):
        table = model.__table__.to_metadata(copy)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    copy.create_all(db.sync.get_bind())
    return db


@pytest.fixture
def sale(hub: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:  # noqa: F811
    """hub plus the provisioning dependencies: the registry knows the sale cluster, shares are recorded."""
    async def _registered() -> frozenset:
        return frozenset({SALE})

    provisioned: List[Tuple[str, Any]] = []

    async def _share(_db: Any, cluster_id: str, **kwargs: Any) -> None:
        provisioned.append((cluster_id, kwargs.get("prospective_organization_id")))

    monkeypatch.setattr(cluster_registry, "registered_cluster_ids", _registered)
    monkeypatch.setattr(integrations, "assert_cluster_safe_to_share", _share)
    return {**hub, "provisioned": provisioned}


async def claims(db: AsyncSessionFacade) -> List[Tuple[str, Optional[str]]]:
    return sorted((await db.execute(select(Organization.slug, Organization.api_key_id))).all())


async def sync(db: AsyncSessionFacade, orgs: Tuple[str, ...], members: List[Tuple[str, str, str]],
               key_id: str = K_PARTNER) -> httpx.Response:
    response = await send(seller_key(key_id), db, hub_body(orgs, members))
    assert response.status_code == 200, response.text
    return response


async def provision(db: AsyncSessionFacade, body: Dict[str, Any], *, method: str = "POST",
                    path: str = "/api/v1/integrations/provision-tenant-access", key_id: str = K_PARTNER) -> httpx.Response:
    return await send(seller_key(key_id), db, body, method=method, path=path)


# note: OrcaHub's sync body, with and without buyer orgs

HUB_CASES = [
    ("seller_alone", (SELLER,), [(C, SELLER, "PARTNER")], "acme-cloud", None),
    ("seller_alone_with_user_and_tenant", (SELLER,), [(BUYER_LOGIN, SELLER, "USER"), (C, SELLER, "PARTNER"),
                                                      (T, SELLER, "TENANT")], "acme-cloud", None),
    ("seller_alone_without_the_creator", (SELLER,), [(F, SELLER, "PARTNER")], None, None),
    ("seller_alone_creator_as_user", (SELLER,), [(F, SELLER, "PARTNER"), (C, SELLER, "USER")], None, ("dest", 0)),
    ("seller_and_a_buyer", (SELLER, BUYER1), [(C, SELLER, "PARTNER"), (F, BUYER1, "PARTNER"),
                                              (BUYER_LOGIN, BUYER1, "USER")], None, ("count", 2)),
    ("seller_and_two_buyers", (SELLER, BUYER1, BUYER2), [(C, SELLER, "PARTNER"), (F, BUYER1, "PARTNER"),
                                                         (F, BUYER2, "PARTNER")], None, ("count", 3)),
    ("buyer_invited_the_creator_absent_from_the_seller", (SELLER, BUYER1),
     [(F, SELLER, "PARTNER"), (F, BUYER1, "PARTNER"), (C, BUYER1, "PARTNER")], None, ("count", 2)),
    ("creator_partner_in_seller_and_buyer", (SELLER, BUYER1),
     [(C, SELLER, "PARTNER"), (F, BUYER1, "PARTNER"), (C, BUYER1, "PARTNER")], None, ("count", 2)),
    ("creator_partner_in_seller_user_in_buyer", (SELLER, BUYER1),
     [(C, SELLER, "PARTNER"), (C, BUYER1, "USER"), (F, BUYER1, "PARTNER")], None, ("count", 2)),
    ("creator_tenant_in_buyer_only", (SELLER, BUYER1),
     [(F, SELLER, "PARTNER"), (C, BUYER1, "TENANT")], None, ("count", 2)),
]


@pytest.mark.parametrize("orgs, members, dest, warn", [c[1:] for c in HUB_CASES], ids=[c[0] for c in HUB_CASES])
async def test_orcahubs_sync_body(hub: Dict[str, Any], orgs: Tuple[str, ...],  # noqa: F811
                                  members: List[Tuple[str, str, str]], dest: Optional[str], warn: Any) -> None:
    db = world_db()
    await personal_cluster(db, "c-cluster", C)
    response = await sync(db, orgs, members)
    assert (response.json()["organizations_synced"], response.json()["members_synced"]) == (len(orgs), len(members))
    expect(hub, await placed(db), dest, warn)
    await sync(db, orgs, members)
    assert len(hub["log"].calls) == (1 if dest else 0), "the scheduled re-push moves nothing"


async def test_a_seller_org_already_claimed_and_absent_from_the_body_still_counts(hub: Dict[str, Any]) -> None:  # noqa: F811
    db = world_db()
    await sync(db, (SELLER,), [(F, SELLER, "PARTNER")])
    await personal_cluster(db, "c-cluster", C)
    await sync(db, (BUYER1,), [(F, BUYER1, "PARTNER"), (C, BUYER1, "PARTNER")])
    expect(hub, await placed(db), None, ("count", 2))


# note: provision-tenant-access claims the buyer org before any push names it

async def test_a_sale_before_the_next_push_blocks_the_rebind(sale: Dict[str, Any]) -> None:
    db = world_db()
    await personal_cluster(db, "c-cluster", C)
    response = await provision(db, provision_body(BUYER1, [BUYER_LOGIN]))
    assert response.status_code == 200, response.text
    assert response.json()["org_created"] is True and response.json()["cluster_mapped"] is True
    assert await claims(db) == [("buyer-co", K_PARTNER)]
    await sync(db, (SELLER,), [(C, SELLER, "PARTNER")])
    assert await claims(db) == [("acme-cloud", K_PARTNER), ("buyer-co", K_PARTNER)]
    expect(sale, await placed(db), None, ("count", 2))
    assert sale["provisioned"] == [(SALE, 1)]


async def test_onboarding_then_a_sale_then_a_later_cluster_waits_for_the_dashboard(sale: Dict[str, Any]) -> None:
    db = world_db()
    await personal_cluster(db, "c-cluster", C)
    await sync(db, (SELLER,), [(C, SELLER, "PARTNER")])
    assert (await provision(db, provision_body(BUYER1, [BUYER_LOGIN]))).status_code == 200
    await personal_cluster(db, "later-cluster", C)
    await sync(db, (SELLER, BUYER1), [(C, SELLER, "PARTNER"), (BUYER_LOGIN, BUYER1, "TENANT")])
    now = await placed(db)
    assert (now["c-cluster"], now["later-cluster"], now[SALE]) == ("acme-cloud", None, "buyer-co")
    assert [c["target_id"] for c in sale["log"].calls] == ["c-cluster"]
    assert sale["warnings"]() == [WARN["count"].format(k=K_PARTNER, n=2)]


async def test_a_sale_into_the_sellers_own_org_keeps_one_claim(sale: Dict[str, Any]) -> None:
    """The partner buys its own plan: the order's org is the seller org, so no second org is claimed."""
    db = world_db()
    await personal_cluster(db, "c-cluster", C)
    response = await provision(db, provision_body(SELLER, [C]))
    assert response.status_code == 200 and response.json()["org_created"] is True
    await sync(db, (SELLER,), [(C, SELLER, "PARTNER")])
    assert await claims(db) == [("acme-cloud", K_PARTNER)]
    expect(sale, await placed(db), "acme-cloud", None)


async def _other_key_owns_the_buyer(db: AsyncSessionFacade) -> None:
    await sync(db, (BUYER1,), [(F, BUYER1, "PARTNER")], key_id=K_ATTACKER)


async def _slug_taken(db: AsyncSessionFacade) -> None:
    db.add(Organization(name="Local", slug="buyer-co", source="local", created_by="someone@example.test"))
    await db.commit()


REFUSED = [
    ("cluster_outside_the_grant", None, {"cluster_id": "nuc-other"}, "POST", "", 403),
    ("unregistered_cluster", None, {"cluster_id": "ghost"}, "POST", "", 404),
    ("org_another_key_owns", _other_key_owns_the_buyer, {}, "POST", "", 409),
    ("slug_already_taken", _slug_taken, {}, "POST", "", 409),
    # Refused by the body model since 2026-10-27, before the organization is ever written.
    ("invalid_permission_before_any_write", None, {"permissions": ["view", "root"]}, "POST", "", 422),
    ("no_org_name_for_a_new_org", None, {"organization_name": None}, "POST", "", 404),
    ("get", None, {}, "GET", "", 405),
    ("put", None, {}, "PUT", "", 405),
    ("patch", None, {}, "PATCH", "", 405),
    ("delete", None, {}, "DELETE", "", 405),
    ("trailing_slash", None, {}, "POST", "/", 307),
    ("capitals", None, {}, "POST", "X", 404),
]


@pytest.mark.parametrize("setup, over, method, suffix, status", [c[1:] for c in REFUSED], ids=[c[0] for c in REFUSED])
async def test_a_provision_that_claims_nothing_leaves_one_org(
    sale: Dict[str, Any], setup: Optional[Callable[..., Any]], over: Dict[str, Any], method: str, suffix: str,
    status: int,
) -> None:
    db = world_db()
    if setup is not None:
        await setup(db)
    before = await claims(db)
    await personal_cluster(db, "c-cluster", C)
    path = "/api/v1/integrations/provision-tenant-access"
    path = path.upper() if suffix == "X" else path + suffix
    response = await provision(db, provision_body(BUYER1, [BUYER_LOGIN], **over), method=method, path=path)
    assert response.status_code == status, response.text
    assert await claims(db) == before
    await sync(db, (SELLER,), [(C, SELLER, "PARTNER")])
    expect(sale, await placed(db), "acme-cloud", None)


# note: refresh-organizations, the dashboard's pull from the key's callback

def refresh_app(db: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(integrations.router, prefix="/api/v1")

    async def _user() -> Any:
        return PARTNER_LOGIN

    async def _db() -> AsyncIterator[Any]:
        try:
            yield db
        finally:
            await db.rollback()

    async def _none() -> None:
        return None

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[org_scope_dependency] = _none
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_lxd_service] = _none
    return app


CALLBACKS = [
    ("a_flat_organizations_list", 200, {"organizations": [{"id": BUYER1, "name": "Buyer Co", "slug": "buyer-co"}]},
     200, [("acme-cloud", K_PARTNER), ("buyer-co", K_PARTNER)], None, ("count", 2)),
    ("orcahubs_enveloped_answer", 200, {"success": True, "data": {"organizations": [
        {"id": BUYER1, "name": "Buyer Co", "slug": "buyer-co", "description": None}]}},
     200, [("acme-cloud", K_PARTNER)], "acme-cloud", None),
    ("orcahubs_401_for_the_hash_header", 401, {"success": False, "error": "Missing API key credentials"},
     502, [("acme-cloud", K_PARTNER)], "acme-cloud", None),
]


@pytest.mark.parametrize("hub_status, answer, status, owned, dest, warn", [c[1:] for c in CALLBACKS],
                         ids=[c[0] for c in CALLBACKS])
async def test_a_refresh_counts_only_what_it_claimed(
    hub: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, hub_status: int, answer: Dict[str, Any],  # noqa: F811
    status: int, owned: List[Tuple[str, str]], dest: Optional[str], warn: Any,
) -> None:
    """The seller org is claimed first (C absent), then the partner refreshes, then C is pushed."""
    db = world_db()
    await sync(db, (SELLER,), [(F, SELLER, "PARTNER")])
    await personal_cluster(db, "c-cluster", C)
    # note: the URL as a sync with the key's secret stores it, bound to that secret
    vault = _FakeVault({**seller_key(), "app_url": "https://hub.example.test",
                        APP_URL_SECRET_TAG: callback_url_tag(SECRETS[1])})
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)
    monkeypatch.setattr(integrations, "get_current_allowed_clusters", lambda: {SALE})
    monkeypatch.setattr(rbac, "activity_logger", _RecordingLogger())
    asked: List[str] = []

    def _callback(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(hub_status, json=answer)

    answer_callbacks(monkeypatch, _callback)
    async with _HTTP(transport=httpx.ASGITransport(app=refresh_app(db)), base_url="http://orcastra.test") as client:
        response = await client.post(f"/api/v1/integrations/refresh-organizations?api_key_id={K_PARTNER}")
    assert response.status_code == status, response.text
    assert asked == ["https://hub.example.test/api/external/organizations"]
    assert await claims(db) == owned
    await sync(db, (SELLER,), [(C, SELLER, "PARTNER")])
    expect(hub, await placed(db), dest, warn)


# note: the count's inputs. Nothing else writes organizations.api_key_id, and MCP and /proxy reach none of it

_APP = Path(integrations.__file__).resolve().parents[1]


def _functions() -> List[Tuple[str, ast.AST]]:
    found = []
    for path in sorted(_APP.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            found.append((f"{path.relative_to(_APP)}:{fn.name}", fn))
    return found


def test_only_these_paths_write_a_claim() -> None:
    constructs, assigns, upserts = set(), set(), set()
    for where, fn in _functions():
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Organization" and any(
                    k.arg == "api_key_id" for k in node.keywords):
                constructs.add(where)
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_upsert_synced_organizations":
                upserts.add(where)
            targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
            for t in targets:
                if isinstance(t, ast.Attribute) and t.attr == "api_key_id":
                    assigns.add((where, ast.unparse(t.value)))
    assert constructs == {"routers/integrations.py:_upsert_synced_organizations",
                          "routers/integrations.py:provision_tenant_access"}
    # note: install is an ExtensionInstallation row, not an organization
    # note: the claim routes set the primary of an unclaimed org only. Every other primary change is
    # in organization_owners: an Administrator's transfer (replace_owners), an add-on credential's
    # rotation (carry), a removal or a lost capability that moves the primary to the next able owner
    # (remove_owner, drop_unable_primary) and an Administrator's choice (set_primary). None of them
    # sets the owner back to NULL.
    assert assigns == {("routers/integrations.py:_upsert_synced_organizations", "existing"),
                       ("routers/integrations.py:_upsert_synced_organizations", "slug_owner"),
                       ("services/organization_owners.py:replace_owners", "org"),
                       ("services/organization_owners.py:carry", "org"),
                       ("services/organization_owners.py:remove_owner", "org"),
                       ("services/organization_owners.py:drop_unable_primary", "org"),
                       ("services/organization_owners.py:set_primary", "org"),
                       ("services/extension_install.py:install_extension", "install"),
                       ("services/extension_install.py:rotate_credential", "install")}
    assert upserts == {"routers/integrations.py:sync_data", "routers/integrations.py:refresh_organizations"}
    text = "\n".join(p.read_text() for p in _APP.rglob("*.py")).lower()
    assert not re.search(r"update\s+organizations\s+set|insert\s+into\s+organizations\b", text)
    assert "update(organization)" not in text and "insert(organization)" not in text


def test_only_the_owner_module_writes_owner_rows() -> None:
    """organization_owner_keys is written by organization_owners and by the db_models listener that
    keeps the primary in the set, never by a route, a raw statement or another service."""
    constructs, deletes = set(), set()
    for where, fn in _functions():
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "OrganizationOwnerKey":
                constructs.add(where.split(":")[0])
            if isinstance(node, ast.Call) and (getattr(node.func, "attr", None) == "delete" or getattr(
                    node.func, "id", None) in ("delete", "update", "insert")) and "OrganizationOwnerKey" in ast.unparse(node):
                deletes.add(where.split(":")[0])
    assert constructs <= {"services/organization_owners.py", "models/db_models.py"}, constructs
    assert deletes <= {"services/organization_owners.py"}, deletes
    text = "\n".join(p.read_text() for p in _APP.rglob("*.py")).lower()
    assert not re.search(r"(insert\s+into|update|delete\s+from)\s+organization_owner_keys", text)


def test_no_ownership_by_primary_equality() -> None:
    """An owner check compares the caller with the owner set (organization_owners.is_owner,
    owned_by_clause), never with the primary alone, which would refuse every co-owner."""
    offending = set()
    for where, fn in _functions():
        if where.startswith(("services/organization_owners.py", "models/db_models.py")):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Compare) and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
                sides = [node.left, *node.comparators]
                if any(isinstance(side, ast.Attribute) and side.attr == "api_key_id"
                       and ast.unparse(side.value) in ("Organization", "org", "existing", "owner", "slug_owner")
                       for side in sides):
                    offending.add(where)
    # note: these compare with the primary because they are about the primary itself: whether a new
    # primary is already the one, and which owner key a callback is tried with first
    assert offending == {"routers/integrations.py:set_organization_primary",
                         "services/orcahub_sync.py:_get_orcahub_credentials"}, offending


def test_mcp_and_proxy_reach_no_claim_writer() -> None:
    mcp_imports = set()
    for path in (_APP / "mcp").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module == "app.routers.integrations":
                mcp_imports |= {a.name for a in node.names}
    assert mcp_imports == {"ProxyRequest", "proxy_operation", "validate_api_key"}
    writers = {"_upsert_synced_organizations", "provision_tenant_access", "refresh_organizations", "sync_data",
               "_upsert_synced_members", "rebind_creator_clusters", "transfer_organization_owner",
               "transfer_owner", "carry_owned_organizations", "add_owner", "remove_owner", "replace_owners",
               "carry", "set_primary", "drop_unable_primary", "enroll_owner_key", "remove_own_owner_key",
               "add_organization_owner", "remove_organization_owner", "set_organization_primary"}
    for where, fn in _functions():
        if where.startswith("mcp/") or where in ("routers/integrations.py:proxy_operation",
                                                  "routers/integrations.py:raw_proxy"):
            called = {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
                      for n in ast.walk(fn) if isinstance(n, ast.Call)}
            assert not called & writers, where
    tokens = ("sync", "provision", "refresh", "organization", "claim", "tenant_access")
    assert [n for n in PROXY_OPERATION_LEVELS if any(t in n.lower() for t in tokens)] == []


# note: the key's one claimed org is a buyer org, and the buyer names the creator PARTNER. Only the push's
# own list of orgs keeps the buyer from choosing the destination.

NEW_KEY = [((SELLER, BUYER1), (SELLER, BUYER1, BUYER2)), ((SELLER,), (SELLER, BUYER2))]


@pytest.mark.parametrize("old_claims, new_push", NEW_KEY, ids=["rotated_key", "second_connection"])
async def test_a_new_key_whose_one_org_is_a_buyer_org_binds_nothing_into_it(
    hub: Dict[str, Any], old_claims: Tuple[str, ...], new_push: Tuple[str, ...],  # noqa: F811
) -> None:
    """The creator mints a new key (not a rotate in place) or a second connection's key. The old key
    still owns the seller org, so the new key's push is refused there and claims only the new buyer
    org, whose PARTNER added the creator as PARTNER."""
    db = world_db()
    await sync(db, old_claims, [(F, o, "PARTNER") for o in old_claims])
    await personal_cluster(db, "c-cluster", C)
    await sync(db, new_push, [(F, o, "PARTNER") for o in new_push] + [(C, BUYER2, "PARTNER")], key_id=K_NEW)
    assert [slug for slug, owner in await claims(db) if owner == K_NEW] == ["second-buyer"]
    expect(hub, await placed(db), None, ("names", len(new_push)), key_id=K_NEW)


async def test_a_seller_org_whose_slug_a_local_org_holds_leaves_no_buyer_the_destination(
    hub: Dict[str, Any],  # noqa: F811
) -> None:
    """Another party's dashboard org holds the seller's slug, so every push skips the seller org."""
    db = world_db()
    db.add(Organization(name="Local", slug="acme-cloud", source="local", created_by="someone@example.test"))
    await db.commit()
    await personal_cluster(db, "c-cluster", C)
    await sync(db, (SELLER, BUYER1), [(F, SELLER, "PARTNER"), (F, BUYER1, "PARTNER"), (C, BUYER1, "PARTNER")])
    assert await claims(db) == [("acme-cloud", None), ("buyer-co", K_PARTNER)]
    expect(hub, await placed(db), None, ("names", 2))


async def test_a_one_org_push_the_key_does_not_hold_leaves_its_buyer_org_unchosen(
    sale: Dict[str, Any],
) -> None:
    """The seller slug is held locally and a sale claimed the buyer org. A push naming the seller org
    alone still carries a member row for the buyer org naming the creator PARTNER."""
    db = world_db()
    db.add(Organization(name="Local", slug="acme-cloud", source="local", created_by="someone@example.test"))
    await db.commit()
    await personal_cluster(db, "c-cluster", C)
    assert (await provision(db, provision_body(BUYER1, [BUYER_LOGIN]))).status_code == 200
    await sync(db, (SELLER,), [(F, BUYER1, "PARTNER"), (C, BUYER1, "PARTNER")])
    assert await claims(db) == [("acme-cloud", None), ("buyer-co", K_PARTNER)]
    expect(sale, await placed(db), None, ("other",))


@pytest.mark.parametrize("old_claims, new_push", NEW_KEY, ids=["rotated_key", "second_connection"])
async def test_the_new_key_without_the_invite_binds_nothing(
    hub: Dict[str, Any], old_claims: Tuple[str, ...], new_push: Tuple[str, ...],  # noqa: F811
) -> None:
    """The same keys without the buyer's invite: the creator is named nowhere this key claimed."""
    db = world_db()
    await sync(db, old_claims, [(F, o, "PARTNER") for o in old_claims])
    await personal_cluster(db, "c-cluster", C)
    await sync(db, new_push, [(C, SELLER, "PARTNER"), (F, BUYER2, "PARTNER")], key_id=K_NEW)
    assert (await placed(db))["c-cluster"] is None
    assert hub["log"].calls == [] and hub["warnings"]() == [] and hub["strict"] == []
