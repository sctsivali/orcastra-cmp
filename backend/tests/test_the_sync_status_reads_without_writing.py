"""The sync status reads Authentik and never writes to it.

User Groups polls GET /user-groups/sync-status every 30 seconds, as a partner too. It answered by
way of _ensure_role_groups_exist, so a missing role group was created on the spot (role_admin as a
superuser), a refused token still read as "connected" because a 401 on the search fell through to
a create that also failed quietly, and the raw exception text, host and port included, went back
to the caller. These run the real service against Authentik's HTTP answers (httpx MockTransport).
"""

from typing import Any, List

import httpx
import pytest

from app.core.auth import AuthenticatedUser
from app.routers import user_groups
from app.services import authentik_sync


def _user(email: str, group: str = "role_partner") -> AuthenticatedUser:
    return AuthenticatedUser(user_id=f"{group}:{email}", email=email, groups=[group])


_REAL_CLIENT = httpx.AsyncClient


def _authentik_http(monkeypatch: pytest.MonkeyPatch, handler) -> List[str]:
    seen: List[str] = []

    def _recording(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return handler(request)

    def _client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("verify", None)
        return _REAL_CLIENT(*args, transport=httpx.MockTransport(_recording), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    return seen


def _service() -> authentik_sync.AuthentikSyncService:
    svc = authentik_sync.AuthentikSyncService()
    svc.api_url, svc.token, svc.enabled = "https://auth.invalid/api/v3", "t0k3n", True
    return svc


async def test_a_missing_role_group_is_reported_not_created(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("search")
        found = [{"pk": f"pk-{name}", "name": name}] if name != "role_admin" else []
        return httpx.Response(200, json={"results": found})

    seen = _authentik_http(monkeypatch, handler)
    status = await _service().get_sync_status()
    assert set(seen) == {"GET"}, "a status read must not create a group"
    assert status["connected"] is True
    assert sorted(status["groups"]) == ["role_partner", "role_tenant"]


@pytest.mark.parametrize("code", [401, 403])
async def test_a_refused_token_is_not_connected(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    seen = _authentik_http(monkeypatch, lambda _r: httpx.Response(code, json={"detail": "nope"}))
    status = await _service().get_sync_status()
    assert status["connected"] is False and status["error"] == "token refused"
    assert set(seen) == {"GET"}


async def test_the_status_route_names_no_raw_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connect to 10.9.8.7:9000 failed: secret-host")

    _authentik_http(monkeypatch, handler)
    monkeypatch.setattr(user_groups, "get_authentik_sync", _service)
    answer = await user_groups.get_sync_status(user=_user("runner@example.com"))
    assert answer.authentik_reachable is False
    assert "10.9.8.7" not in answer.details and "secret-host" not in answer.details


# ── an admin removal of an email Authentik has never seen ─────────────────────────────────────
# An invited address that never signed in has no Authentik account. The removal treated "no such
# account" as "not confirmed" and answered 502 after the membership rows were already deleted, so
# the dialog reported a failure for a removal that had happened.


def _directory(users_status: int, users: List[dict], count: int = -1):
    total = len(users) if count < 0 else count

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/core/users/"):
            return httpx.Response(users_status, json={"pagination": {"count": total}, "results": users})
        if request.url.path.endswith("/core/groups/") and request.method == "GET":
            name = request.url.params.get("search")
            return httpx.Response(200, json={"results": [{"pk": f"pk-{name}", "name": name}]})
        return httpx.Response(204)
    return handler


async def test_an_email_with_no_account_has_no_roles_to_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _authentik_http(monkeypatch, _directory(200, [{"email": "someone-else@example.com", "pk": 7}]))
    assert await _service().remove_user_from_all_roles("never-signed-in@example.com") is True
    assert "POST" not in seen, "nothing to remove, so nothing is written"


async def test_a_failed_account_search_is_still_not_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _authentik_http(monkeypatch, _directory(503, []))
    assert await _service().remove_user_from_all_roles("someone@example.com") is False
    assert "POST" not in seen


async def test_a_search_page_that_does_not_hold_every_match_is_not_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Authentik's search matches substrings, so near-matches can push the account to a later page."""
    near = [{"email": f"admin{i}@example.com", "pk": i} for i in range(20)]
    seen = _authentik_http(monkeypatch, _directory(200, near, count=21))
    assert await _service().remove_user_from_all_roles("admin@example.com") is False
    assert "POST" not in seen


async def test_two_accounts_with_one_email_are_not_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    twins = [{"email": "Admin@Example.com", "pk": 1}, {"email": " admin@example.com", "pk": 2}]
    seen = _authentik_http(monkeypatch, _directory(200, twins))
    assert await _service().remove_user_from_all_roles("admin@example.com") is False
    assert "POST" not in seen, "stripping one twin and answering done would leave the other its roles"


@pytest.mark.parametrize("blank", ["", " ", "\t"])
async def test_a_blank_email_matches_no_account(monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
    """An empty search lists every account, and a service account with no email would have matched it."""
    seen = _authentik_http(monkeypatch, _directory(200, [{"email": None, "pk": 3}, {"email": "", "pk": 4}]))
    assert await _service().remove_user_from_all_roles(blank) is False
    assert seen.count("GET") <= 3 and "POST" not in seen
