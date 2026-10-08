"""Decision 14, the partial-rebind rollback and OrcaHub v1.32.0's sync body, through the sync route.

Decision 14 runs the real AuthentikSyncService against Authentik's HTTP answers: every group the
request path resolves to Admin never rebinds, and every answer that does not confirm a role is
unknown and never rebinds. The rollback cases refuse a later cluster in the share audit. The
OrcaHub cases send the body orcastra.service.js syncOrcastraClustersUnlocked builds: with a buyer
org in it the key claims two orgs and nothing binds (decision 19), with the seller org alone the
partner's clusters bind into it.
"""

import ast
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import anyio
import httpx
import pytest
from sqlalchemy import select

from app.core import url_safety
from app.core.auth import AuthenticatedUser
from app.core.config import Settings
from app.core.rbac import AUTHENTIK_GROUP_MAP, Role, _resolve_roles_uncached
from app.models.db_models import OrganizationMember
from app.routers import integrations
from app.services import authentik_sync, integration_cluster_rebind
from app.services.lxd.proxy_operations import PROXY_OPERATION_LEVELS
from tests._sqlite_session import org_tables, sqlite_session
from tests.test_a_capability_grant_leaves_a_record import _FakeVault
from tests.test_a_creator_role_authentik_cannot_confirm_rebinds_nothing import (
    TOKEN,
    directory,
    page,
    real_authentik,
    searching,
)
from tests.test_a_partial_rebind_is_rolled_back import LEAVER, after_the_request, claimed_org_with_a_leaver, reconcile
from tests.test_a_push_rebinds_only_into_the_org_its_key_claimed import (
    K_PARTNER,
    bound,
    key,
    personal_cluster,
    world,  # noqa: F401
)
from tests.test_the_push_rebind_rules_hold_through_the_sync_route import (
    C,
    FOUNDER,
    P,
    SYNC,
    assert_bound_to,
    assert_unmoved,
    body,
    m,
    push,
    rebind_warnings,
    send,
    sync_app,
)

Handler = Callable[[httpx.Request], httpx.Response]
USERS = "/api/v3/core/users/"


async def pushed_by_the_creator(db: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)
    await personal_cluster(db, "c-cluster", C)
    assert (await push(db, K_PARTNER, [P], [m(C, P, "PARTNER")])).status_code == 200


# note: decision 14: Admin is whatever the request path resolves to Admin

_ADMIN_NAMES = sorted(n for n, r in AUTHENTIK_GROUP_MAP.items() if r == Role.ADMIN)
GROUP_SETS = [[n] for n in _ADMIN_NAMES] + [
    ["ROLE_ADMIN"], ["Role_Admin"], ["AUTHENTIK ADMINS"], ["Authentik Admins"], [" admin "], ["admin\t"],
    ["Orcastra-Admins"], ["role_admins"], ["ROLE_ORCASTRA-ADMINS"], ["role_authentik admins"], ["role_role_admin"],
    ["role_partner", "authentik Admins"], ["staff", "admins", "billing"],
    ["role_partner"], ["partner"], ["orcastra-partners"], ["role_tenant"], ["tenant"], [], ["staff"],
    ["administrators"], ["authentik admin"], ["orcastra-admin"], ["role_"], ["superuser"], ["role_superuser"],
]


@pytest.mark.parametrize("groups", GROUP_SETS, ids=[repr(g) for g in GROUP_SETS])
async def test_decision_14_matches_the_request_path_resolver(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,  # noqa: F811
    groups: List[str],
) -> None:
    is_admin = Role.ADMIN in _resolve_roles_uncached(AuthenticatedUser("u", email=C, groups=groups))
    seen = real_authentik(monkeypatch, directory({C: groups}))
    db = sqlite_session(*org_tables())
    await pushed_by_the_creator(db, caplog)
    assert seen.count("GET /api/v3/core/users/1/") == 2
    if is_admin:
        await assert_unmoved(db, world)
        (warning,) = rebind_warnings(caplog)
        assert K_PARTNER in warning and "is an Admin" in warning
    else:
        await assert_bound_to(db, world, "partner")


def _status(code: int) -> Handler:
    return lambda _r: httpx.Response(code, text="upstream says no")


def _raises(error: type) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error("no answer", request=request)
    return handler


def _json(value: Any) -> Handler:
    return lambda _r: httpx.Response(200, json=value)


ONE = {"pk": 1, "email": C}
SEARCH_UNKNOWN = [
    *[(f"status_{s}", _status(s), "user_search_failed") for s in (201, 204, 301, 302, 400, 401, 403, 404, 408,
                                                                    429, 500, 502, 503, 504)],
    *[(e.__name__, _raises(e), "authentik_unreachable") for e in (
        httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.WriteTimeout, httpx.ConnectError,
        httpx.ReadError, httpx.RemoteProtocolError)],
    ("not_json", lambda _r: httpx.Response(200, text="<html>sign in</html>"), "user_search_unreadable"),
    ("a_list", _json([ONE]), "user_search_unreadable"),
    ("results_a_string", _json({"results": "x", "pagination": {"count": 1}}), "user_search_unreadable"),
    ("results_hold_a_string", _json({"results": ["x"], "pagination": {"count": 1}}), "user_search_unreadable"),
    ("no_pagination", _json({"results": [ONE]}), "user_search_incomplete"),
    ("pagination_a_list", _json({"results": [ONE], "pagination": [1]}), "user_search_incomplete"),
    ("count_a_string", _json({"results": [ONE], "pagination": {"count": "1"}}), "user_search_incomplete"),
    ("count_a_bool", _json({"results": [ONE], "pagination": {"count": True}}), "user_search_incomplete"),
    ("count_null", _json({"results": [ONE], "pagination": {"count": None}}), "user_search_incomplete"),
    ("count_above_page", _json(page([ONE], count=21)), "user_search_incomplete"),
    ("count_below_page", _json(page([ONE], count=0)), "user_search_incomplete"),
    ("someone_else", _json(page([{"pk": 9, "email": "someone@example.test"}])), "user_not_found"),
    ("empty", _json(page([])), "user_not_found"),
    ("email_null", _json(page([{"pk": 1, "email": None}])), "user_not_found"),
    ("email_missing", _json(page([{"pk": 1}])), "user_not_found"),
    ("same_email_twice", _json(page([ONE, {"pk": 2, "email": C}])), "email_ambiguous"),
    ("same_email_other_case", _json(page([{"pk": 2, "email": C.upper()}, ONE])), "email_ambiguous"),
    ("same_email_padded", _json(page([ONE, {"pk": 2, "email": f" {C} "}])), "email_ambiguous"),
    *[(f"pk_{name}", _json(page([{"pk": pk, "email": C}])), "user_search_unreadable")
      for name, pk in (("string", "1"), ("bool", True), ("null", None), ("float", 1.0), ("list", [1]))],
    ("pk_missing", _json(page([{"email": C}])), "user_search_unreadable"),
]


@pytest.mark.parametrize("answer, reason", [c[1:] for c in SEARCH_UNKNOWN], ids=[c[0] for c in SEARCH_UNKNOWN])
async def test_decision_14_a_search_that_confirms_no_role_rebinds_nothing(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,  # noqa: F811
    answer: Handler, reason: str,
) -> None:
    real_authentik(monkeypatch, searching(answer, ["role_partner"]))
    db = sqlite_session(*org_tables())
    await pushed_by_the_creator(db, caplog)
    await assert_unmoved(db, world)
    (warning,) = rebind_warnings(caplog)
    assert K_PARTNER in warning and f"could not be read ({reason})" in warning
    assert TOKEN not in caplog.text and C not in warning


DETAIL_UNKNOWN = [
    *[(f"status_{s}", _status(s), "user_detail_failed") for s in (201, 301, 401, 403, 404, 429, 500, 503)],
    ("ReadTimeout", _raises(httpx.ReadTimeout), "authentik_unreachable"),
    ("not_json", lambda _r: httpx.Response(200, text="oops"), "user_detail_unreadable"),
    ("a_list", _json([{"name": "role_partner"}]), "user_detail_unreadable"),
    ("groups_missing", _json({"pk": 1}), "user_detail_unreadable"),
    ("groups_null", _json({"pk": 1, "groups_obj": None}), "user_detail_unreadable"),
    ("groups_a_string", _json({"pk": 1, "groups_obj": "role_partner"}), "user_detail_unreadable"),
    ("groups_of_strings", _json({"pk": 1, "groups_obj": ["role_partner"]}), "user_detail_unreadable"),
    ("name_null", _json({"pk": 1, "groups_obj": [{"name": None}]}), "user_detail_unreadable"),
    ("name_a_number", _json({"pk": 1, "groups_obj": [{"name": 1}]}), "user_detail_unreadable"),
    ("name_missing", _json({"pk": 1, "groups_obj": [{"pk": "g"}]}), "user_detail_unreadable"),
    ("one_unreadable_beside_partner", _json({"pk": 1, "groups_obj": [{"name": "role_partner"}, {"name": None}]}),
     "user_detail_unreadable"),
]


@pytest.mark.parametrize("answer, reason", [c[1:] for c in DETAIL_UNKNOWN], ids=[c[0] for c in DETAIL_UNKNOWN])
async def test_decision_14_a_detail_read_that_confirms_no_role_rebinds_nothing(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,  # noqa: F811
    answer: Handler, reason: str,
) -> None:
    rest = directory({C: ["role_partner"]})
    real_authentik(monkeypatch, lambda r: answer(r) if r.url.path == f"{USERS}1/" else rest(r))
    db = sqlite_session(*org_tables())
    await pushed_by_the_creator(db, caplog)
    await assert_unmoved(db, world)
    (warning,) = rebind_warnings(caplog)
    assert f"could not be read ({reason})" in warning


async def test_decision_14_unconfigured_authentik_rebinds_nothing_and_asks_nothing(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,  # noqa: F811
) -> None:
    seen = real_authentik(monkeypatch, directory({C: ["role_partner"]}))
    authentik_sync.get_authentik_sync().enabled = False
    db = sqlite_session(*org_tables())
    await pushed_by_the_creator(db, caplog)
    await assert_unmoved(db, world)
    assert seen == []
    (warning,) = rebind_warnings(caplog)
    assert "could not be read (authentik_not_configured)" in warning


CONFIRMED = [
    ("one_account_other_case", _json(page([{"pk": 1, "email": C.upper()}]))),
    ("other_users_beside_it", _json(page([{"pk": 7, "email": "x" + C}, ONE, {"pk": 8, "email": None}]))),
]


@pytest.mark.parametrize("answer", [c[1] for c in CONFIRMED], ids=[c[0] for c in CONFIRMED])
async def test_decision_14_a_confirmed_partner_still_rebinds(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,  # noqa: F811
    answer: Handler,
) -> None:
    real_authentik(monkeypatch, searching(answer, ["role_partner"]))
    db = sqlite_session(*org_tables())
    await pushed_by_the_creator(db, caplog)
    await assert_bound_to(db, world, "partner")


async def test_decision_14_open_item_an_alias_admin_is_granted_partner_but_keeps_its_clusters(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,  # noqa: F811
) -> None:
    """A recorded open item: the member sync reads the lenient groups, so it adds role_partner."""
    seen = real_authentik(monkeypatch, directory({C: ["authentik Admins"]}))
    db = sqlite_session(*org_tables())
    await pushed_by_the_creator(db, caplog)
    await assert_unmoved(db, world)
    assert [s for s in seen if s.startswith("POST") and s.endswith("/add_user/")]


# note: a rebind that fails partway

@pytest.mark.parametrize("enabled", [True, False], ids=["offboarding_on", "offboarding_off"])
@pytest.mark.parametrize("refused", [["a-ok"], ["b-refused"], ["a-ok", "b-refused"]], ids=["first", "last", "both"])
async def test_a_partial_rebind_leaves_nothing_and_the_next_push_moves_all(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,  # noqa: F811
    enabled: bool, refused: List[str],
) -> None:
    reconcile(monkeypatch, enabled=enabled, dry_run=False)
    db = await claimed_org_with_a_leaver()
    world["fail"].update(refused)
    ours = body([P], [m(C, P, "PARTNER")])
    response = await send(sync_app(key(K_PARTNER, C), db), "POST", SYNC, ours)
    assert response.status_code == 200 and response.json()["members_removed"] == (1 if enabled else 0)
    assert await after_the_request(db) == {"a-ok": None, "b-refused": None}
    assert world["log"].calls == [] and world["shares"][0] == ("a-ok", 1)
    assert "share audit refused" in caplog.text
    emails = (await db.execute(select(OrganizationMember.user_email))).scalars().all()
    assert sorted(emails) == sorted([C] + ([] if enabled else [LEAVER]))

    world["fail"].clear()
    assert (await send(sync_app(key(K_PARTNER, C), db), "POST", SYNC, ours)).status_code == 200
    assert await after_the_request(db) == {"a-ok": 1, "b-refused": 1}
    assert sorted(c["target_id"] for c in world["log"].calls) == ["a-ok", "b-refused"]


# note: OrcaHub v1.32.0's own sync body

SELLER, BUYER_ORG = "5b0c7a4e-0d7c-4f6e-9a57-1c3f0b8e2d11", "9e1f5d2a-4b6c-4c8d-8e0f-2a3b4c5d6e7f"
HUB_ORGS = {
    SELLER: {"id": SELLER, "name": "Acme Cloud", "slug": "acme-cloud", "description": None},
    BUYER_ORG: {"id": BUYER_ORG, "name": "Buyer Co", "slug": "buyer-co", "description": "bought a plan"},
}


def orcahub_body(members: List[Dict[str, Any]], app_url: Any = "https://hub.example.test",
                 orgs: Tuple[str, ...] = (SELLER, BUYER_ORG)) -> Dict[str, Any]:
    """orcastra.client.js sync(): axios drops app_url when it is undefined."""
    sent: Dict[str, Any] = {
        "include_clusters": True, "include_projects": True,
        "organizations": [HUB_ORGS[ext] for ext in orgs],
        "members": members,
    }
    if app_url is not None:
        sent["app_url"] = app_url
    return sent


def hub(email: str, ext: str, role: str) -> Dict[str, Any]:
    name = None if role == "USER" else email.split("@")[0]
    return {"email": email, "name": name, "organization_id": ext, "role": role}


@pytest.fixture
def onboarding(world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:  # noqa: F811
    vault = _FakeVault({k: v for k, v in key(K_PARTNER, C).items() if k not in ("raw", "hash")})
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)
    # note: a real secret, because the sync stores app_url only for the key's current secret
    monkeypatch.setenv("SECRET_KEY", "unit-test-secret")
    secret, secret_hash = integrations.generate_api_key_secret()
    vault.store[_FakeVault._path(K_PARTNER, "secret")] = {"hash": secret_hash, "raw": secret}
    lookups: List[str] = []

    def _resolve(host: str, *_a: Any, **_k: Any) -> List[Any]:
        lookups.append(host)
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(url_safety.socket, "getaddrinfo", _resolve)
    return {**world, "vault": vault, "lookups": lookups, "as_the_key": {"X-API-Key-Secret": secret}}


def assert_nothing_bound_and_warned(caplog: pytest.LogCaptureFixture, onboarding: Dict[str, Any],
                                    clusters: Dict[str, Optional[int]]) -> None:
    assert all(org is None for org in clusters.values())
    assert onboarding["shares"] == [] and onboarding["log"].calls == []
    (warning,) = rebind_warnings(caplog)
    assert K_PARTNER in warning and "it claims 2 organizations" in warning and "@" not in warning


ONBOARD = [
    ("partner_and_buyer", [hub(C, SELLER, "PARTNER"), hub(FOUNDER, BUYER_ORG, "PARTNER"),
                           hub("buyer@example.test", BUYER_ORG, "USER")], "https://hub.example.test"),
    ("no_app_url", [hub(C, SELLER, "PARTNER"), hub(FOUNDER, BUYER_ORG, "PARTNER")], None),
    ("seller_added_as_user_in_a_buyer_org", [hub(C, SELLER, "PARTNER"), hub(C, BUYER_ORG, "USER"),
                                             hub(FOUNDER, BUYER_ORG, "PARTNER")], None),
    ("seller_org_with_a_tenant", [hub(C, SELLER, "PARTNER"), hub("t@example.test", SELLER, "TENANT"),
                                  hub(FOUNDER, BUYER_ORG, "PARTNER")], None),
]


@pytest.mark.parametrize("members, app_url", [c[1:] for c in ONBOARD], ids=[c[0] for c in ONBOARD])
async def test_orcahubs_sync_body_with_a_buyer_org_binds_nothing(
    onboarding: Dict[str, Any], caplog: pytest.LogCaptureFixture, members: List[Dict[str, Any]], app_url: Any,
) -> None:
    """The seller's key claims the buyer org too, so it claims two orgs. The partner binds from the dashboard."""
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "c-cluster", C)
    await personal_cluster(db, "founder-cluster", FOUNDER)
    sent = orcahub_body(members, app_url)
    response = await send(sync_app(key(K_PARTNER, C), db), "POST", SYNC, sent, onboarding["as_the_key"])
    assert response.status_code == 200, response.text
    assert (response.json()["organizations_synced"], response.json()["members_synced"]) == (2, len(members))
    assert_nothing_bound_and_warned(caplog, onboarding, await bound(db))
    stored = _FakeVault._path(K_PARTNER, "metadata")
    assert [w[1].get("app_url") for w in onboarding["vault"].writes if w[0] == stored] == ([app_url] if app_url else [])
    assert onboarding["lookups"] == (["hub.example.test"] if app_url else [])

    again = await send(sync_app(key(K_PARTNER, C), db), "POST", SYNC, sent)
    assert again.status_code == 200 and onboarding["log"].calls == []
    assert (await bound(db)) == {"c-cluster": None, "founder-cluster": None}


SELLER_ALONE = [
    ("partner_only", [hub(C, SELLER, "PARTNER")], "https://hub.example.test"),
    ("no_app_url", [hub(C, SELLER, "PARTNER")], None),
    ("with_a_tenant", [hub(C, SELLER, "PARTNER"), hub("t@example.test", SELLER, "TENANT")], None),
    ("with_a_user", [hub("u@example.test", SELLER, "USER"), hub(C, SELLER, "PARTNER")], "https://hub.example.test"),
]


@pytest.mark.parametrize("members, app_url", [c[1:] for c in SELLER_ALONE], ids=[c[0] for c in SELLER_ALONE])
async def test_orcahubs_sync_body_with_the_seller_org_alone_binds_into_it(
    onboarding: Dict[str, Any], members: List[Dict[str, Any]], app_url: Any,
) -> None:
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "c-cluster", C)
    sent = orcahub_body(members, app_url, orgs=(SELLER,))
    response = await send(sync_app(key(K_PARTNER, C), db), "POST", SYNC, sent, onboarding["as_the_key"])
    assert response.status_code == 200, response.text
    assert (response.json()["organizations_synced"], response.json()["members_synced"]) == (1, len(members))
    await assert_bound_to(db, onboarding, "acme-cloud")
    stored = _FakeVault._path(K_PARTNER, "metadata")
    assert [w[1].get("app_url") for w in onboarding["vault"].writes if w[0] == stored] == ([app_url] if app_url else [])
    assert onboarding["lookups"] == (["hub.example.test"] if app_url else [])

    again = await send(sync_app(key(K_PARTNER, C), db), "POST", SYNC, sent)
    assert again.status_code == 200 and len(onboarding["log"].calls) == 1


async def test_a_buyer_org_naming_the_sellers_creator_as_partner_is_never_the_destination(
    onboarding: Dict[str, Any], caplog: pytest.LogCaptureFixture,
) -> None:
    """OrcaHub lets a buyer org's PARTNER add any existing user as PARTNER (members.js invite,
    organizationRoleSchema). With the creator absent from the seller's roster the buyer org was the
    one destination. The key claims both orgs, so nothing binds."""
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "c-cluster", C)
    sent = orcahub_body([hub(FOUNDER, SELLER, "PARTNER"), hub(FOUNDER, BUYER_ORG, "PARTNER"),
                         hub(C, BUYER_ORG, "PARTNER")], None)
    assert (await send(sync_app(key(K_PARTNER, C), db), "POST", SYNC, sent)).status_code == 200
    assert_nothing_bound_and_warned(caplog, onboarding, await bound(db))


@pytest.mark.parametrize("in_buyer_org", ["PARTNER", "USER", None])
async def test_a_key_that_also_claims_a_buyer_org_binds_nothing_into_its_own(
    onboarding: Dict[str, Any], caplog: pytest.LogCaptureFixture, in_buyer_org: Optional[str],
) -> None:
    """The creator is a partner in the seller org. The key claims the buyer org too, so nothing binds
    whether the buyer org names the creator PARTNER, USER or not at all: the buyer decides nothing."""
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "c-cluster", C)
    named = [hub(C, BUYER_ORG, in_buyer_org)] if in_buyer_org else []
    sent = orcahub_body([hub(C, SELLER, "PARTNER"), hub(FOUNDER, BUYER_ORG, "PARTNER"), *named], None)
    assert (await send(sync_app(key(K_PARTNER, C), db), "POST", SYNC, sent)).status_code == 200
    assert_nothing_bound_and_warned(caplog, onboarding, await bound(db))


# note: no other surface reaches the rebind or the key edits

_TOKENS = ("sync", "member", "organization", "org_", "api_key", "credential", "capabilit", "permission", "rebind")


def test_mcp_has_no_tool_for_a_sync_a_member_or_a_key_edit() -> None:
    from app.mcp.server import build_mcp_server

    mcp = build_mcp_server(Settings(
        MCP_ADMIN_TOOLS_ENABLED=True, MCP_GUEST_TOOLS_ENABLED=True, MCP_DESKTOP_TOOLS_ENABLED=True))
    names = [tool.name for tool in anyio.run(mcp.list_tools)]
    assert len(names) > 20
    assert [n for n in names if any(t in n.lower() for t in _TOKENS)] == []


def test_proxy_has_no_operation_for_a_sync_a_member_or_a_key_edit() -> None:
    assert [n for n in PROXY_OPERATION_LEVELS if any(t in n.lower() for t in _TOKENS)] == []


def test_only_the_sync_route_and_key_create_reach_the_rebind() -> None:
    """reconcile_personal_clusters moves clusters. Key create keeps the ordering until Phase 2 (15)."""
    app_dir = Path(integrations.__file__).resolve().parents[1]
    callers: Dict[str, List[str]] = {}
    for path in app_dir.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)]:
                name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                if name in ("reconcile_personal_clusters", "rebind_creator_clusters", "_upsert_synced_members"):
                    callers.setdefault(name, []).append(f"{path.relative_to(app_dir)}:{fn.name}")
    assert sorted(callers["reconcile_personal_clusters"]) == [
        "routers/integrations.py:create_api_key", "services/integration_cluster_rebind.py:rebind_creator_clusters"]
    assert callers["rebind_creator_clusters"] == ["routers/integrations.py:_upsert_synced_members"]
    assert callers["_upsert_synced_members"] == ["routers/integrations.py:sync_data"]
