"""A shell or console is re-authorized every minute and closed when access ends, not before.

Authorization used to run once, at connect, so a terminal opened before a tenant's grant was
revoked or an account was disabled stayed open for as long as the browser kept it. The guard
re-checks the account (Authentik), the caller's scope and the socket's own access rule, closes
with 4403 on a definite no, and never on an Authentik outage, which would cut every open shell.
The close reason says which no it was, so the page can tell the operator.
"""

import asyncio
import inspect

import httpx
import pytest
from fastapi import HTTPException

from app.core.auth import AuthenticatedUser
from app.services import authentik_role_reading, ws_identity, ws_session_guard
from app.services.authentik_role_reading import RoleReading, known_roles, unknown_role
from app.core.rbac import Role
from app.services.ws_identity import IdentityCheck, check_identity
from app.services.ws_session_guard import ACCESS_REVOKED, reauthorize_while_open


class _Socket:
    def __init__(self):
        self.closed = []

    async def close(self, code: int, reason: str = "") -> None:
        self.closed.append((code, reason))


USER = AuthenticatedUser(user_id="ops@example.test", email="ops@example.test", groups=["role_partner"])


def _guard(monkeypatch, identities, checks, *, rounds: float = 0.2):
    """Run the guard at a 10 ms interval; identities and checks are consumed per round."""
    async def identity(user):
        result = identities.pop(0) if identities else IdentityCheck(False, "active")
        if isinstance(result, Exception):
            raise result
        return result

    async def scope(user, **_kwargs):
        return None

    monkeypatch.setattr(ws_session_guard, "check_identity", identity)
    monkeypatch.setattr(ws_session_guard, "set_org_scope_for_ws", scope)

    async def check(user):
        outcome = checks.pop(0) if checks else None
        if isinstance(outcome, Exception):
            raise outcome

    socket = _Socket()

    async def run():
        task = asyncio.create_task(reauthorize_while_open(socket, USER, check, label="test", interval=0.01))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=rounds)
        except asyncio.TimeoutError:
            task.cancel()
        return task

    task = asyncio.run(run())
    return socket, task


def test_a_disabled_account_is_closed_with_4403(monkeypatch):
    socket, _ = _guard(monkeypatch, [IdentityCheck(False, "active"), IdentityCheck(True, "user_inactive")], [])

    assert socket.closed == [(ACCESS_REVOKED, "access_revoked:account_ended")]


def test_revoked_access_is_closed_with_4403(monkeypatch):
    socket, _ = _guard(monkeypatch, [], [None, HTTPException(status_code=403, detail="forbidden")])

    assert socket.closed == [(ACCESS_REVOKED, "access_revoked:access_ended")]


def test_an_authentik_outage_never_closes(monkeypatch):
    outage = [IdentityCheck(False, "authentik_unreachable")] * 15

    socket, _ = _guard(monkeypatch, outage, [])

    assert socket.closed == []


def test_five_failed_checks_in_a_row_close_and_a_success_resets_the_count(monkeypatch):
    broken = RuntimeError("database restarting")
    socket, _ = _guard(monkeypatch, [], [broken] * 4 + [None] + [broken] * 4, rounds=0.25)
    assert socket.closed == [], "four, a success, four: never five in a row"

    socket, _ = _guard(monkeypatch, [], [broken] * 5)
    assert socket.closed == [(1011, "reauthorize_failed")]


def test_without_a_user_there_is_nothing_to_guard():
    socket = _Socket()

    asyncio.run(reauthorize_while_open(socket, None, None, label="dev"))

    assert socket.closed == []


def test_one_authentik_read_serves_a_minute_of_checks(monkeypatch):
    reads = []

    async def read(email):
        reads.append(email)
        return known_roles(["role_partner"])

    monkeypatch.setattr(ws_identity, "_read", read)

    first = asyncio.run(check_identity(USER))
    second = asyncio.run(check_identity(USER))

    assert first == second == IdentityCheck(False, "active", frozenset({Role.PARTNER}))
    assert reads == ["ops@example.test"]


@pytest.mark.parametrize(
    "reading, ended",
    [
        (unknown_role("user_not_found"), True),
        (unknown_role("user_inactive"), True),
        (unknown_role("email_ambiguous"), True),
        (unknown_role("authentik_unreachable"), False),
        (unknown_role("user_search_failed"), False),
        (known_roles(["role_tenant"]), False),
    ],
)
def test_only_an_answer_about_the_account_ends_it(monkeypatch, reading: RoleReading, ended: bool):
    async def read(email):
        return reading

    monkeypatch.setattr(ws_identity, "_read", read)

    assert asyncio.run(check_identity(USER)).ended is ended


def test_authentik_reports_a_disabled_account_as_inactive():
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/core/users/"):
            return httpx.Response(200, json={"results": [{"pk": 7, "email": "ops@example.test"}], "pagination": {"count": 1}})
        return httpx.Response(200, json={"pk": 7, "is_active": False, "groups_obj": [{"name": "role_admin"}]})

    async def read():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await authentik_role_reading._read(client, "https://idp.invalid/api/v3", {}, "ops@example.test")

    assert asyncio.run(read()) == unknown_role("user_inactive")


@pytest.mark.parametrize("module_name", ["terminal", "console"])
def test_both_interactive_sockets_run_the_guard_until_they_close(module_name):
    from importlib import import_module

    module = import_module(f"app.routers.{module_name}")
    handler = module.terminal_websocket if module_name == "terminal" else module.console_websocket
    source = inspect.getsource(handler)

    assert f'ws_user("{module_name}")' in source
    assert "reauthorize_while_open(" in source and "instance_session_check(host, project, instance_name)" in source
    assert "reauthorize.cancel()" in source


@pytest.mark.parametrize(
    "module_name, handler_name, kind",
    [("events", "events_websocket", "events"), ("tasks_ws", "tasks_websocket", "tasks"), ("monitoring", "monitoring_websocket", "monitoring")],
)
def test_every_feed_closes_when_its_session_ends(module_name, handler_name, kind):
    from importlib import import_module

    source = inspect.getsource(getattr(import_module(f"app.routers.{module_name}"), handler_name))

    assert f'ws_user("{kind}")' in source
    assert "close_when_session_ends(" in source and "session.cancel()" in source


def test_a_demoted_caller_is_closed_and_an_unchanged_one_kept(monkeypatch):
    """USER opened as a Partner; Authentik now says Tenant, so its frozen groups are stale."""
    demoted, _ = _guard(monkeypatch, [IdentityCheck(False, "active", frozenset({Role.TENANT}))], [])
    unchanged, _ = _guard(monkeypatch, [IdentityCheck(False, "active", frozenset({Role.PARTNER}))] * 10, [])

    assert demoted.closed == [(ACCESS_REVOKED, "access_revoked:role_changed")]
    assert unchanged.closed == []


def _plant_admin_role_for(user_id: str) -> None:
    """What a Redis writer could do while roles were cached per user: plant Admin under the old key."""
    from app.core.cache import get_cache_manager

    get_cache_manager().set("rbac_user", f"roles:{user_id}", {"roles": ["admin"]}, ttl=60)


@pytest.mark.parametrize(
    "identity",
    [IdentityCheck(False, "user_search_failed"), IdentityCheck(False, "active", frozenset({Role.PARTNER}))],
    ids=["authentik-could-not-say", "roles-unchanged"],
)
def test_a_round_resolves_scope_from_the_ticket_groups_alone(monkeypatch, identity):
    from app.core import rbac

    _plant_admin_role_for(USER.user_id)
    seen = []

    async def identity_check(user):
        return identity

    async def scope(user, **_kwargs):
        seen.append(rbac.get_highest_role(rbac.resolve_roles(user)))

    monkeypatch.setattr(ws_session_guard, "check_identity", identity_check)
    monkeypatch.setattr(ws_session_guard, "set_org_scope_for_ws", scope)

    async def check(user):
        seen.append(rbac.get_highest_role(rbac.resolve_roles(user)))

    async def run():
        task = asyncio.create_task(reauthorize_while_open(_Socket(), USER, check, label="t", interval=0.01))
        await asyncio.sleep(0.1)
        task.cancel()

    asyncio.run(run())

    assert seen and set(seen) == {Role.PARTNER}, "the ticket's own groups, never a planted Admin"


def test_a_session_without_an_email_cannot_be_rechecked_and_ends():
    nameless = AuthenticatedUser(user_id="sub-without-email", email=None, groups=["role_admin"])

    assert asyncio.run(check_identity(nameless)) == IdentityCheck(True, "email_missing")


def test_the_role_check_reads_authentik_not_the_shared_cache(monkeypatch):
    calls = []

    async def scope(user, **_kwargs):
        calls.append("scope")

    async def identity(user):
        return IdentityCheck(False, "active", frozenset({Role.ADMIN}))

    monkeypatch.setattr(ws_session_guard, "check_identity", identity)
    monkeypatch.setattr(ws_session_guard, "set_org_scope_for_ws", scope)
    socket = _Socket()

    async def run():
        task = asyncio.create_task(reauthorize_while_open(socket, USER, lambda u: None, label="t", interval=0.01))
        await asyncio.sleep(0.1)
        task.cancel()

    asyncio.run(run())

    assert socket.closed == [(ACCESS_REVOKED, "access_revoked:role_changed")]
    assert calls == [], "scope is never resolved, so the frozen role is never cached, once roles differ"


def test_a_file_transfer_still_hears_the_plain_reason(monkeypatch):
    """reauthorize_until feeds StreamCut, which keeps its sentence; only sockets carry the token."""
    from app.services.ws_session_guard import reauthorize_until

    async def identity(user):
        return IdentityCheck(True, "user_inactive")

    async def scope(user, **_kwargs):
        return None

    monkeypatch.setattr(ws_session_guard, "check_identity", identity)
    monkeypatch.setattr(ws_session_guard, "set_org_scope_for_ws", scope)
    heard = []

    async def refused(reason):
        heard.append(reason)

    asyncio.run(reauthorize_until(USER, lambda u: None, refused, label="t", interval=0.01))

    assert heard == ["Access revoked"]
