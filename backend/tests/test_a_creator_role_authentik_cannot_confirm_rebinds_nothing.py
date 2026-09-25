"""The Admin check on a rebind reads Authentik strictly and resolves groups the way RBAC does.

The check read get_user_role_groups, which answers an empty set for a user search that did not
answer 200, for an email the search did not return and for the first of two accounts with the
email, and which keeps only the exact role_* names. Each of those read as "holds no role", so an
Admin through an expired token, a 429, a proxy 502, a duplicate account or an alias group such as
"authentik Admins" had their personal clusters moved and a rebind record written.

These run the real AuthentikSyncService against Authentik's HTTP answers (httpx MockTransport).
Every request it makes is recorded.
"""

import logging
from typing import Any, Callable, Dict, List

import httpx
import pytest

from app.core.auth import AuthenticatedUser
from app.core.rbac import AUTHENTIK_GROUP_MAP, Role, _resolve_roles_uncached
from app.services import authentik_sync, integration_cluster_rebind
from tests._sqlite_session import org_tables, sqlite_session
from tests.test_a_push_rebinds_only_into_the_org_its_key_claimed import (
    body,
    bound,
    key,
    member,
    org,
    org_id,
    personal_cluster,
    push,
    world,  # noqa: F401 - the fixture
)

CREATOR = "creator@example.test"
K_CREATOR = "oak_creator_minted"
BASE = "https://auth.invalid/api/v3"
TOKEN = "authentik-token-do-not-log"

Handler = Callable[[httpx.Request], httpx.Response]
_REAL_CLIENT = httpx.AsyncClient


def real_authentik(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> List[str]:
    """The real service, configured, with every HTTP request it makes recorded and answered."""
    seen: List[str] = []

    def _recording(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return handler(request)

    def _client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("verify", None)
        return _REAL_CLIENT(*args, transport=httpx.MockTransport(_recording), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    svc = authentik_sync.AuthentikSyncService()
    svc.api_url, svc.token, svc.enabled = BASE, TOKEN, True
    monkeypatch.setattr(authentik_sync, "get_authentik_sync", lambda: svc)
    return seen


def page(users: List[Dict[str, Any]], count: int = -1) -> Dict[str, Any]:
    """A list answer as Authentik's pagination class shapes it."""
    total = len(users) if count < 0 else count
    return {"pagination": {"next": 0, "previous": 0, "count": total, "current": 1,
                           "total_pages": 1, "start_index": 1, "end_index": total},
            "results": users}


def directory(groups: Dict[str, List[str]], *, search_status: int = 200, detail_status: int = 200) -> Handler:
    """Authentik: one account per email holding the named groups. Group writes are accepted."""
    pks = {email: i + 1 for i, email in enumerate(groups)}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/v3/core/users/":
            if search_status != 200:
                return httpx.Response(search_status, text="upstream says no")
            term = request.url.params.get("search", "").lower()
            return httpx.Response(200, json=page(
                [{"pk": pk, "email": email} for email, pk in pks.items() if term in email.lower()]
            ))
        for email, pk in pks.items():
            if request.method == "GET" and path == f"/api/v3/core/users/{pk}/":
                if detail_status != 200:
                    return httpx.Response(detail_status, text="upstream says no")
                return httpx.Response(200, json={
                    "pk": pk, "email": email, "groups_obj": [{"name": g} for g in groups[email]],
                })
        if request.method == "GET" and path == "/api/v3/core/groups/":
            return httpx.Response(200, json=page([]))
        if request.method == "POST" and path == "/api/v3/core/groups/":
            return httpx.Response(201, json={"pk": "g-" + request.content.decode()[:12]})
        return httpx.Response(204)

    return handler


def searching(answer: Callable[[httpx.Request], httpx.Response], groups: List[str]) -> Handler:
    """The user search answered by `answer`, everything else by a directory holding the creator."""
    rest = directory({CREATOR: groups})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v3/core/users/":
            return answer(request)
        return rest(request)

    return handler


async def pushed(db: Any) -> None:
    await push(db, key(K_CREATOR, CREATOR),
               body([org("ext-c", "held")], [member(CREATOR, "ext-c", "PARTNER")]))


async def assert_nothing_moved(db: Any, world: Dict[str, Any], caplog: pytest.LogCaptureFixture,  # noqa: F811
                               reason: str) -> None:
    assert (await bound(db))["creator-cluster"] is None
    assert world["shares"] == [] and world["log"].calls == []
    (warning,) = [r for r in caplog.records if r.name == integration_cluster_rebind.__name__]
    assert K_CREATOR in warning.getMessage()
    assert f"could not be read ({reason})" in warning.getMessage()
    assert TOKEN not in caplog.text and CREATOR not in warning.getMessage()


@pytest.fixture
def creator_db(caplog: pytest.LogCaptureFixture) -> Any:
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)
    return sqlite_session(*org_tables())


def _admin_groups() -> List[str]:
    names = sorted(name for name, role in AUTHENTIK_GROUP_MAP.items() if role == Role.ADMIN)
    variants = ["Role_Admin", "authentik Admins", "ORCASTRA-ADMINS", " admin ", "role_admins", "role_orcastra-admins"]
    return names + variants


def _other_groups() -> List[List[str]]:
    names = sorted(name for name, role in AUTHENTIK_GROUP_MAP.items() if role != Role.ADMIN)
    return [[name] for name in names] + [["Role_Partner"], [], ["staff", "billing"]]


# ── Admin is every group RBAC resolves to Admin ────────────────────────────────────────────────

@pytest.mark.parametrize("group", _admin_groups())
async def test_an_admin_through_any_group_rbac_maps_to_admin_never_rebinds(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, creator_db: Any,  # noqa: F811
    caplog: pytest.LogCaptureFixture, group: str,
) -> None:
    # note: the request path's resolver makes this login an Admin, so require_partner let it mint the key
    assert Role.ADMIN in _resolve_roles_uncached(AuthenticatedUser("u", email=CREATOR, groups=[group]))
    seen = real_authentik(monkeypatch, directory({CREATOR: [group]}))
    await personal_cluster(creator_db, "creator-cluster", CREATOR)

    await pushed(creator_db)
    assert (await bound(creator_db))["creator-cluster"] is None
    assert world["shares"] == [] and world["log"].calls == []
    assert seen.count("GET /api/v3/core/users/1/") == 2, "the member sync's read and the strict one"
    (warning,) = [r for r in caplog.records if r.name == integration_cluster_rebind.__name__]
    assert K_CREATOR in warning.getMessage() and "is an Admin" in warning.getMessage()


@pytest.mark.parametrize("groups", _other_groups())
async def test_a_partner_or_tenant_creator_still_rebinds(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, creator_db: Any,  # noqa: F811
    groups: List[str],
) -> None:
    assert Role.ADMIN not in _resolve_roles_uncached(AuthenticatedUser("u", email=CREATOR, groups=groups))
    seen = real_authentik(monkeypatch, directory({CREATOR: groups}))
    await personal_cluster(creator_db, "creator-cluster", CREATOR)

    await pushed(creator_db)
    held = await org_id(creator_db, "held")
    assert (await bound(creator_db))["creator-cluster"] == held
    assert world["shares"] == [("creator-cluster", held)]
    assert [c["action"] for c in world["log"].calls] == [integration_cluster_rebind.REBIND_ACTION]
    assert seen.count("GET /api/v3/core/users/1/") == 2


# ── a role Authentik did not confirm is unknown ────────────────────────────────────────────────

@pytest.mark.parametrize("status", [302, 400, 401, 403, 404, 429, 500, 502, 503])
async def test_a_user_search_that_did_not_answer_200_rebinds_nothing(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, creator_db: Any,  # noqa: F811
    caplog: pytest.LogCaptureFixture, status: int,
) -> None:
    seen = real_authentik(monkeypatch, directory({CREATOR: ["role_admin"]}, search_status=status))
    await personal_cluster(creator_db, "creator-cluster", CREATOR)
    await pushed(creator_db)
    await assert_nothing_moved(creator_db, world, caplog, "user_search_failed")
    assert not [s for s in seen if s.startswith("GET /api/v3/core/users/1")]


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError,
                                     httpx.RemoteProtocolError])
async def test_a_user_search_that_timed_out_or_failed_in_transport_rebinds_nothing(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, creator_db: Any,  # noqa: F811
    caplog: pytest.LogCaptureFixture, failure: type,
) -> None:
    def _fails(request: httpx.Request) -> httpx.Response:
        raise failure("no answer", request=request)

    real_authentik(monkeypatch, searching(_fails, ["role_partner"]))
    await personal_cluster(creator_db, "creator-cluster", CREATOR)
    await pushed(creator_db)
    await assert_nothing_moved(creator_db, world, caplog, "authentik_unreachable")


async def test_a_creator_the_search_does_not_return_rebinds_nothing(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, creator_db: Any,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _someone_else(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page([{"pk": 9, "email": "someone@example.test"}]))

    seen = real_authentik(monkeypatch, searching(_someone_else, ["role_admin"]))
    await personal_cluster(creator_db, "creator-cluster", CREATOR)
    await pushed(creator_db)
    await assert_nothing_moved(creator_db, world, caplog, "user_not_found")
    assert "GET /api/v3/core/users/9/" not in seen


@pytest.mark.parametrize("order", ["admin_second", "admin_first"])
async def test_two_accounts_carrying_the_creators_email_rebind_nothing(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, creator_db: Any,  # noqa: F811
    caplog: pytest.LogCaptureFixture, order: str,
) -> None:
    """The role was read from whichever account the search listed first."""
    accounts = [{"pk": 2, "email": "Creator@Example.test"}, {"pk": 1, "email": CREATOR}]
    if order == "admin_first":
        accounts.reverse()

    def _two(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page(accounts))

    real_authentik(monkeypatch, searching(_two, ["role_admin"]))
    await personal_cluster(creator_db, "creator-cluster", CREATOR)
    await pushed(creator_db)
    await assert_nothing_moved(creator_db, world, caplog, "email_ambiguous")


@pytest.mark.parametrize("count", [2, 150])
async def test_a_search_page_that_does_not_hold_every_match_rebinds_nothing(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, creator_db: Any,  # noqa: F811
    caplog: pytest.LogCaptureFixture, count: int,
) -> None:
    """A second account with the email could sit on a page that was not returned."""
    def _truncated(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page([{"pk": 1, "email": CREATOR}], count=count))

    real_authentik(monkeypatch, searching(_truncated, ["role_partner"]))
    await personal_cluster(creator_db, "creator-cluster", CREATOR)
    await pushed(creator_db)
    await assert_nothing_moved(creator_db, world, caplog, "user_search_incomplete")


@pytest.mark.parametrize(
    "answer, reason",
    [
        (lambda: httpx.Response(200, text="<html>login</html>"), "user_search_unreadable"),
        (lambda: httpx.Response(200, json=[{"pk": 1, "email": CREATOR}]), "user_search_unreadable"),
        (lambda: httpx.Response(200, json={"results": "none"}), "user_search_unreadable"),
        (lambda: httpx.Response(200, json={"results": [{"pk": 1, "email": CREATOR}]}), "user_search_incomplete"),
        (lambda: httpx.Response(200, json=page([{"pk": "1", "email": CREATOR}])), "user_search_unreadable"),
        (lambda: httpx.Response(200, json=page([{"pk": True, "email": CREATOR}])), "user_search_unreadable"),
    ],
    ids=["not_json", "a_list", "results_not_a_list", "no_pagination", "pk_a_string", "pk_a_bool"],
)
async def test_a_user_search_body_it_cannot_read_rebinds_nothing(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, creator_db: Any,  # noqa: F811
    caplog: pytest.LogCaptureFixture, answer: Callable[[], httpx.Response], reason: str,
) -> None:
    real_authentik(monkeypatch, searching(lambda _r: answer(), ["role_partner"]))
    await personal_cluster(creator_db, "creator-cluster", CREATOR)
    await pushed(creator_db)
    await assert_nothing_moved(creator_db, world, caplog, reason)


@pytest.mark.parametrize("status", [401, 404, 429, 503])
async def test_a_failed_detail_read_rebinds_nothing(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, creator_db: Any,  # noqa: F811
    caplog: pytest.LogCaptureFixture, status: int,
) -> None:
    real_authentik(monkeypatch, directory({CREATOR: ["role_partner"]}, detail_status=status))
    await personal_cluster(creator_db, "creator-cluster", CREATOR)
    await pushed(creator_db)
    await assert_nothing_moved(creator_db, world, caplog, "user_detail_failed")


@pytest.mark.parametrize(
    "record",
    [{"pk": 1}, {"pk": 1, "groups_obj": None}, {"pk": 1, "groups_obj": ["role_partner"]},
     {"pk": 1, "groups_obj": [{"name": None}]}],
    ids=["no_groups", "groups_null", "names_not_objects", "name_not_text"],
)
async def test_a_detail_body_it_cannot_read_rebinds_nothing(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, creator_db: Any,  # noqa: F811
    caplog: pytest.LogCaptureFixture, record: Dict[str, Any],
) -> None:
    rest = directory({CREATOR: ["role_partner"]})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/core/users/1/":
            return httpx.Response(200, json=record)
        return rest(request)

    real_authentik(monkeypatch, handler)
    await personal_cluster(creator_db, "creator-cluster", CREATOR)
    await pushed(creator_db)
    await assert_nothing_moved(creator_db, world, caplog, "user_detail_unreadable")


# ── the existing callers keep their reading ────────────────────────────────────────────────────

async def test_get_user_role_groups_keeps_its_answers_for_the_member_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """The member sync's grant decisions read these answers, so the strict read is a new method."""
    real_authentik(monkeypatch, directory({CREATOR: ["authentik Admins", "role_partner"]}, search_status=503))
    svc = authentik_sync.get_authentik_sync()
    assert await svc.get_user_role_groups(CREATOR) == set()
    assert (await svc.read_current_roles(CREATOR)).roles is None

    real_authentik(monkeypatch, directory({CREATOR: ["authentik Admins", "role_partner"]}))
    svc = authentik_sync.get_authentik_sync()
    assert await svc.get_user_role_groups(CREATOR) == {"role_partner"}
    assert (await svc.read_current_roles(CREATOR)).roles == frozenset({Role.ADMIN, Role.PARTNER})
    assert await svc.get_user_role_groups("nobody@example.test") == set()
    assert (await svc.read_current_roles("nobody@example.test")).reason == "user_not_found"

    real_authentik(monkeypatch, directory({CREATOR: ["role_partner"]}, detail_status=503))
    assert await authentik_sync.get_authentik_sync().get_user_role_groups(CREATOR) is None


async def test_an_unconfigured_authentik_is_unknown_without_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = real_authentik(monkeypatch, directory({CREATOR: ["role_partner"]}))
    svc = authentik_sync.get_authentik_sync()
    svc.enabled = False
    reading = await svc.read_current_roles(CREATOR)
    assert (reading.roles, reading.reason) == (None, "authentik_not_configured")
    assert seen == []


async def test_the_strict_read_asks_for_the_creator_and_sends_the_configured_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: List[httpx.Request] = []
    rest = directory({CREATOR: ["role_partner"]})

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return rest(request)

    real_authentik(monkeypatch, handler)
    reading = await authentik_sync.get_authentik_sync().read_current_roles(CREATOR)
    assert reading.roles == frozenset({Role.PARTNER}) and reading.reason == "read"
    assert [(r.method, r.url.path, r.url.params.get("search")) for r in asked] == [
        ("GET", "/api/v3/core/users/", CREATOR), ("GET", "/api/v3/core/users/1/", None),
    ]
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in asked)
