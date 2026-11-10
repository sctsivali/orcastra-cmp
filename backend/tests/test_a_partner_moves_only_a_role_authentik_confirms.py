"""A Partner's role assignment reads the target's role strictly before it pushes anything.

The push strips every other role group, so a target read as "holds nothing" when Authentik could
not say would let a Partner demote an Admin. Only an email no account carries counts as holding
nothing. An alias group such as "authentik Admins" is Admin, as the request path resolves it.

These run the real AuthentikSyncService against Authentik's HTTP answers (httpx MockTransport).
"""

from typing import Any, Dict, List, Optional

import httpx
import pytest
from fastapi import HTTPException

from app.routers import user_groups
from app.routers.user_groups import refuse_unless_a_partner_may_move
from app.services import authentik_sync
from tests.test_a_creator_role_authentik_cannot_confirm_rebinds_nothing import (
    Handler,
    directory,
    page,
    searching,
)
from tests.test_a_creator_role_authentik_cannot_confirm_rebinds_nothing import real_authentik as _real

TARGET = "target@example.test"


def real_authentik(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    """The real service behind the router, which imported get_authentik_sync by name."""
    _real(monkeypatch, handler)
    monkeypatch.setattr(user_groups, "get_authentik_sync", authentik_sync.get_authentik_sync)


async def _status(email: str = TARGET) -> Optional[int]:
    try:
        await refuse_unless_a_partner_may_move(email)
    except HTTPException as exc:
        return exc.status_code
    return None


@pytest.mark.parametrize(
    "groups, expected",
    [
        (["role_tenant"], None),
        ([], None),
        (["role_partner"], 403),
        (["role_admin"], 403),
        (["authentik Admins"], 403),
    ],
    ids=["tenant", "no-role", "partner", "admin", "alias-admin"],
)
async def test_the_target_role_decides(monkeypatch: pytest.MonkeyPatch, groups: List[str], expected) -> None:
    real_authentik(monkeypatch, directory({TARGET: groups}))
    assert await _status() == expected


async def test_an_email_no_account_carries_holds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    real_authentik(monkeypatch, directory({"someone@example.test": ["role_admin"]}))
    assert await _status() is None


@pytest.mark.parametrize("search_status", [401, 429, 502, 503])
async def test_a_failed_search_is_unknown_and_refused(monkeypatch: pytest.MonkeyPatch, search_status: int) -> None:
    real_authentik(monkeypatch, directory({TARGET: ["role_admin"]}, search_status=search_status))
    assert await _status() == 503


async def test_a_failed_detail_read_is_unknown_and_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    real_authentik(monkeypatch, directory({TARGET: ["role_admin"]}, detail_status=503))
    assert await _status() == 503


async def test_a_page_missing_matches_is_unknown_and_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page([{"pk": 7, "email": "other@example.test"}], count=40))

    real_authentik(monkeypatch, searching(answer, ["role_admin"]))
    assert await _status() == 503


async def test_two_accounts_with_the_email_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page([{"pk": 1, "email": TARGET}, {"pk": 2, "email": TARGET.upper()}]))

    real_authentik(monkeypatch, searching(answer, ["role_tenant"]))
    assert await _status() == 403


async def test_a_disabled_account_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/core/users/":
            return httpx.Response(200, json=page([{"pk": 1, "email": TARGET}]))
        body: Dict[str, Any] = {"pk": 1, "email": TARGET, "is_active": False,
                                "groups_obj": [{"name": "role_admin"}]}
        return httpx.Response(200, json=body)

    real_authentik(monkeypatch, handler)
    assert await _status() == 403
