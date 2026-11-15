"""Sockets refuse a caller with no role, and an open shell ends when its caller loses every role.

The REST routes refuse such a caller (rbac.refuse_unmapped); a socket must not hand it the live
feeds instead. A terminal or console also outlives the token that opened it, so the periodic
re-check asks whether any group maps to a role. Reading "no role group" as Tenant would keep a
shell open for a Tenant whose role was removed.
"""

import asyncio
from types import SimpleNamespace
from typing import Optional

import pytest

from app.core import session_role, ws_auth
from app.core.auth import AuthenticatedUser
from app.core.rbac import Role
from app.services import ws_identity
from app.services.authentik_role_reading import known_roles


def _settings() -> SimpleNamespace:
    return SimpleNamespace(auth_enabled=True, authentik_issuer="https://idp.example.test/", ws_query_token_enabled=True)


def _socket(**query: str) -> SimpleNamespace:
    return SimpleNamespace(query_params=query, path_params={})


def _identify_as(monkeypatch: pytest.MonkeyPatch, user: Optional[AuthenticatedUser]) -> None:
    """The ticket path: a redeemed ticket names this caller."""
    async def redeemed(_kind, _websocket, _ticket):
        return user

    monkeypatch.setattr(ws_auth, "_redeemed", redeemed)


@pytest.mark.parametrize("groups", [[], ["Everyone"]])
def test_a_socket_refuses_a_caller_with_no_role(monkeypatch: pytest.MonkeyPatch, groups: list) -> None:
    _identify_as(monkeypatch, AuthenticatedUser(user_id="u", email="u@example.test", groups=groups))
    assert asyncio.run(ws_auth.ws_user("events")(_socket(ticket="t"), _settings())) is None


def test_a_spent_ticket_is_still_refused_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    _identify_as(monkeypatch, None)
    assert asyncio.run(ws_auth.ws_user("events")(_socket(ticket="t"), _settings())) is None


def test_a_socket_admits_a_caller_with_a_role(monkeypatch: pytest.MonkeyPatch) -> None:
    user = AuthenticatedUser(user_id="u", email="u@example.test", groups=["role_tenant"])
    _identify_as(monkeypatch, user)
    assert asyncio.run(ws_auth.ws_user("terminal")(_socket(ticket="t"), _settings())) is user


def test_the_legacy_token_path_is_checked_too(monkeypatch: pytest.MonkeyPatch) -> None:
    async def token_user(_websocket, _settings):
        return AuthenticatedUser(user_id="u", email="u@example.test", groups=[])

    monkeypatch.setattr(ws_auth, "get_ws_user", token_user)
    assert asyncio.run(ws_auth.ws_user("console")(_socket(token="jwt"), _settings())) is None


def test_the_reading_says_whether_a_group_mapped() -> None:
    none = known_roles(["Everyone"])
    assert none.roles == frozenset({Role.TENANT}), "the key rebind still reads the Tenant default"
    assert none.has_role is False
    tenant = known_roles(["Everyone", "role_tenant"])
    assert (tenant.roles, tenant.has_role) == (frozenset({Role.TENANT}), True)


@pytest.fixture(autouse=True)
def _fresh_identities():
    ws_identity.forget_identities()
    yield
    ws_identity.forget_identities()


@pytest.fixture(autouse=True)
def _no_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    # These callers hold no membership; a groupless member stays a Tenant (its own module).
    async def holds(_email, _db=None, **_kwargs):
        return False

    monkeypatch.setattr(session_role, "holds_membership", holds)


def _reading(monkeypatch: pytest.MonkeyPatch, groups: list) -> None:
    async def read(_email):
        return known_roles(groups)

    monkeypatch.setattr(ws_identity, "_read", read)


def test_an_open_shell_ends_when_every_role_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    _reading(monkeypatch, ["Everyone"])
    user = AuthenticatedUser(user_id="u", email="u@example.test", groups=["role_tenant"])
    check = asyncio.run(ws_identity.check_identity(user))
    assert (check.ended, check.reason) == (True, "no_role_assigned")


def test_an_open_shell_with_a_role_carries_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _reading(monkeypatch, ["role_tenant"])
    user = AuthenticatedUser(user_id="u", email="u@example.test", groups=["role_tenant"])
    check = asyncio.run(ws_identity.check_identity(user))
    assert (check.ended, check.reason, check.roles) == (False, "active", frozenset({Role.TENANT}))
