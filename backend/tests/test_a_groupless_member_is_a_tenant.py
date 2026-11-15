"""An account an active organization lists as a member is a Tenant, even with no role group.

A partner deployment signs buyers in through its own Authentik, federated to OrcaHub with a profile
mapping that deliberately sends no groups. The buyer's access is the membership and policy OrcaHub
provisioned for their email, so refusing every groupless session would lock out every buyer on
every partner deployment. The membership is the role: Tenant, never more, and only while the
organization is active. A groupless account with no membership still sees the no-role notice.
"""

import asyncio
import contextlib
import os
from types import SimpleNamespace
from typing import Optional

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("APP_VERSION", "0.0.0-test")
os.environ.setdefault("CORS_ORIGINS", "http://localhost")

from app.core import rbac, session_role, ws_auth  # noqa: E402
from app.core.auth import AuthenticatedUser, get_current_user  # noqa: E402
from app.core.rbac import NO_ROLE_ASSIGNED, Permission, Role  # noqa: E402
from app.models.db_models import Organization, OrganizationMember  # noqa: E402
from app.services import ws_identity  # noqa: E402
from app.services.authentik_role_reading import known_roles, unknown_role  # noqa: E402
from tests._sqlite_session import sqlite_session  # noqa: E402

BUYER = "buyer@example.test"


def _user(email: Optional[str], *groups: str) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=email or "sub-only", email=email, groups=list(groups))


def _request(path: str = "/api/v1/instances") -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""})


@pytest.fixture(autouse=True)
def _fresh_cache():
    session_role.forget_memberships()
    ws_identity.forget_identities()
    yield
    session_role.forget_memberships()
    ws_identity.forget_identities()


@pytest.fixture
def rows():
    """Two organizations: an active one with the buyer, and a deactivated one with a former buyer."""
    db = sqlite_session(Organization, OrganizationMember)
    active = Organization(name="Bought", slug="bought", created_by="orcahub-provisioning")
    gone = Organization(name="Gone", slug="gone", created_by="orcahub-provisioning", is_active=False)
    db.add_all([active, gone])
    db.sync.flush()
    db.add_all([
        OrganizationMember(organization_id=active.id, user_email=BUYER, role="tenant"),
        OrganizationMember(organization_id=gone.id, user_email="former@example.test", role="tenant"),
    ])
    db.sync.commit()
    try:
        yield db
    finally:
        db.sync.close()
        db.sync.bind.dispose()


def _holds(email: Optional[str], db) -> bool:
    return asyncio.run(session_role.holds_membership(email, db))


# --- who counts as a member -------------------------------------------------------------------


def test_a_member_of_an_active_organization_holds_a_membership(rows) -> None:
    assert _holds(BUYER, rows) is True


def test_a_member_of_a_deactivated_organization_does_not(rows) -> None:
    assert _holds("former@example.test", rows) is False


def test_a_stranger_does_not(rows) -> None:
    assert _holds("stranger@example.test", rows) is False


def test_the_email_is_compared_exactly_as_the_tenant_scope_compares_it(rows) -> None:
    # important: org_scope matches user_email exactly. Admitting a casing variant here would let in
    # an account whose scope then reads nothing, so both sides compare the same way.
    assert _holds(BUYER.upper(), rows) is False


@pytest.mark.parametrize("email", [None, "", "   "])
def test_an_account_without_an_email_never_queries(email) -> None:
    class Refuses:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("no query for an account without an email")

    assert _holds(email, Refuses()) is False


# --- the cache ----------------------------------------------------------------------------------


def test_an_answer_is_cached_briefly_then_read_again(rows, monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(session_role, "_now", lambda: clock[0])
    assert _holds(BUYER, rows) is True
    assert _holds("stranger@example.test", rows) is False
    asked = len(rows.statements)

    clock[0] += session_role.NO_MEMBERSHIP_CACHE_SECONDS / 2
    assert _holds(BUYER, rows) is True
    assert _holds("stranger@example.test", rows) is False
    assert len(rows.statements) == asked, "a yes and a no are both answered from the cache at first"

    clock[0] += session_role.MEMBERSHIP_CACHE_SECONDS - 1
    assert _holds(BUYER, rows) is True
    assert len(rows.statements) == asked + 1, "an expired answer is read again"


def test_a_no_is_remembered_only_briefly(rows, monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(session_role, "_now", lambda: clock[0])
    assert _holds("stranger@example.test", rows) is False
    asked = len(rows.statements)
    clock[0] += session_role.NO_MEMBERSHIP_CACHE_SECONDS + 0.1
    assert _holds("stranger@example.test", rows) is False
    assert len(rows.statements) == asked + 1, "a no is read again once its short window ends"
    assert session_role.NO_MEMBERSHIP_CACHE_SECONDS < session_role.MEMBERSHIP_CACHE_SECONDS


def test_a_membership_made_after_a_refusal_counts_once_the_short_no_expires(rows, monkeypatch) -> None:
    """A buyer who opens the dashboard just before OrcaHub's provisioning lands is refused; the
    membership it then writes counts within seconds, not after the whole membership window."""
    clock = [1000.0]
    monkeypatch.setattr(session_role, "_now", lambda: clock[0])
    late = "late-buyer@example.test"
    assert _holds(late, rows) is False
    active_id = rows.sync.query(Organization.id).filter_by(slug="bought").scalar()
    rows.sync.add(OrganizationMember(organization_id=active_id, user_email=late, role="tenant"))
    rows.sync.commit()
    clock[0] += session_role.NO_MEMBERSHIP_CACHE_SECONDS + 0.1
    assert _holds(late, rows) is True


def test_a_fresh_read_skips_a_cached_no_and_remembers_the_answer(rows, monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(session_role, "_now", lambda: clock[0])
    late = "late-buyer@example.test"
    assert _holds(late, rows) is False
    active_id = rows.sync.query(Organization.id).filter_by(slug="bought").scalar()
    rows.sync.add(OrganizationMember(organization_id=active_id, user_email=late, role="tenant"))
    rows.sync.commit()
    assert asyncio.run(session_role.holds_membership(late, rows, fresh=True)) is True
    asked = len(rows.statements)
    assert _holds(late, rows) is True
    assert len(rows.statements) == asked, "the fresh answer is what the next caller reads"


def test_a_removed_membership_stops_counting_once_the_answer_expires(rows, monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(session_role, "_now", lambda: clock[0])
    assert _holds(BUYER, rows) is True
    rows.sync.query(OrganizationMember).filter_by(user_email=BUYER).delete()
    rows.sync.commit()
    clock[0] += session_role.MEMBERSHIP_CACHE_SECONDS + 1
    assert _holds(BUYER, rows) is False


def test_a_read_that_started_earlier_never_overwrites_a_newer_answer() -> None:
    """A slow read that began before a fresh one finishes after it; the newer answer stays."""
    session_role._remember(BUYER, True, 1010.0)
    session_role._remember(BUYER, False, 1005.0)
    assert session_role._answers[BUYER] == (1010.0, True)


def test_the_cache_stays_bounded(rows, monkeypatch) -> None:
    monkeypatch.setattr(session_role, "MAX_CACHED_ACCOUNTS", 8)
    for n in range(50):
        _holds(f"flood-{n}@example.test", rows)
    assert len(session_role._answers) <= 8


def test_a_failed_read_refuses_and_is_not_remembered(rows) -> None:
    class Broken:
        async def execute(self, *_args, **_kwargs):
            raise ConnectionError("database down")

    assert _holds(BUYER, Broken()) is False
    assert _holds(BUYER, rows) is True, "the failure was not cached as a no"


def test_without_a_session_it_opens_its_own(rows, monkeypatch) -> None:
    @contextlib.asynccontextmanager
    async def own_session():
        yield rows

    monkeypatch.setattr(session_role, "_session", own_session)
    assert asyncio.run(session_role.holds_membership(BUYER)) is True


def test_an_unreachable_database_refuses_rather_than_raising() -> None:
    # The hermetic DATABASE_URL points at a closed port: the real session path must fail closed.
    assert asyncio.run(session_role.holds_membership("nobody@example.test")) is False


# --- REST: the three dependencies ------------------------------------------------------------


@pytest.fixture
def member(monkeypatch):
    async def holds(email, db=None, **_kwargs):
        return email == BUYER

    monkeypatch.setattr(session_role, "holds_membership", holds)


@pytest.fixture
def audit(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(rbac.activity_logger, "log_activity", lambda **kwargs: calls.append(kwargs))
    return calls


TENANT_DEPENDENCIES = {
    "require_roles(tenant)": rbac.require_roles(Role.TENANT),
    "require_permissions(instance:read)": rbac.require_permissions(Permission.INSTANCE_READ),
    "require_authenticated()": rbac.require_authenticated(),
}


@pytest.mark.parametrize("name", sorted(TENANT_DEPENDENCIES))
@pytest.mark.parametrize("groups", [(), ("Everyone",)])
def test_a_groupless_member_passes_as_a_tenant(name, groups, member, audit) -> None:
    request = _request()
    user = _user(BUYER, *groups)
    assert asyncio.run(TENANT_DEPENDENCIES[name](request=request, user=user)) is user
    assert request.state.user_role == "tenant"
    assert audit == []


@pytest.mark.parametrize(
    "dependency",
    [rbac.require_roles(Role.PARTNER), rbac.require_roles(Role.ADMIN), rbac.require_permissions(Permission.ORG_WRITE)],
)
def test_a_groupless_member_is_never_more_than_a_tenant(dependency, member, audit) -> None:
    with pytest.raises(HTTPException) as refused:
        asyncio.run(dependency(request=_request(), user=_user(BUYER)))
    assert refused.value.status_code == 403
    detail = refused.value.detail
    assert not (isinstance(detail, dict) and detail.get("error") == NO_ROLE_ASSIGNED), (
        "the account has a role, Tenant; it lacks the higher one"
    )


@pytest.mark.parametrize("name", sorted(TENANT_DEPENDENCIES))
def test_a_groupless_non_member_is_still_refused(name, member, audit) -> None:
    request = _request()
    with pytest.raises(HTTPException) as refused:
        asyncio.run(TENANT_DEPENDENCIES[name](request=request, user=_user("stranger@example.test")))
    assert refused.value.detail["error"] == NO_ROLE_ASSIGNED
    assert request.state.user_role is None
    assert [call["details"]["reason"] for call in audit] == [NO_ROLE_ASSIGNED]


def test_session_has_role_reads_groups_before_the_database(monkeypatch) -> None:
    async def never(email, db=None, **_kwargs):
        raise AssertionError("a mapped group needs no membership read")

    monkeypatch.setattr(session_role, "holds_membership", never)
    assert asyncio.run(rbac.session_has_role(_user(BUYER, "role_partner"))) is True


@pytest.fixture
def member_client(monkeypatch):
    from app.main import create_app

    async def holds(email, db=None, **_kwargs):
        return email == BUYER

    monkeypatch.setattr(session_role, "holds_membership", holds)
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: _user(BUYER)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.mark.parametrize("path", ["/api/v1/tenant-access/my-access", "/api/v1/user-preferences"])
def test_through_the_application_a_member_is_not_refused_for_its_role(member_client, path) -> None:
    response = member_client.get(path)
    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
    detail = body.get("detail") if isinstance(body, dict) else None
    assert not (isinstance(detail, dict) and detail.get("error") == NO_ROLE_ASSIGNED), response.text


def test_through_the_application_an_admin_route_refuses_a_member_as_a_tenant(member_client) -> None:
    response = member_client.get("/api/v1/organizations/cluster-share-audit/qa-cluster")
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert not (isinstance(detail, dict) and detail.get("error") == NO_ROLE_ASSIGNED)


# --- sockets ----------------------------------------------------------------------------------


def _settings() -> SimpleNamespace:
    return SimpleNamespace(auth_enabled=True, authentik_issuer="https://idp.example.test/", ws_query_token_enabled=True)


def _socket(**query: str) -> SimpleNamespace:
    return SimpleNamespace(query_params=query, path_params={})


def test_a_socket_admits_a_groupless_member_by_ticket(monkeypatch, member) -> None:
    user = _user(BUYER)

    async def redeemed(_kind, _websocket, _ticket):
        return user

    monkeypatch.setattr(ws_auth, "_redeemed", redeemed)
    assert asyncio.run(ws_auth.ws_user("terminal")(_socket(ticket="t"), _settings())) is user


def test_a_socket_admits_a_groupless_member_by_token(monkeypatch, member) -> None:
    user = _user(BUYER)

    async def token_user(_websocket, _settings):
        return user

    monkeypatch.setattr(ws_auth, "get_ws_user", token_user)
    assert asyncio.run(ws_auth.ws_user("console")(_socket(token="jwt"), _settings())) is user


def test_a_socket_still_refuses_a_groupless_non_member(monkeypatch, member) -> None:
    async def redeemed(_kind, _websocket, _ticket):
        return _user("stranger@example.test")

    monkeypatch.setattr(ws_auth, "_redeemed", redeemed)
    assert asyncio.run(ws_auth.ws_user("events")(_socket(ticket="t"), _settings())) is None


# --- the minute re-check of an open terminal, console or file stream --------------------------


def _authentik_says(monkeypatch, reading) -> None:
    async def read(_email):
        return reading

    monkeypatch.setattr(ws_identity, "_read", read)


def test_an_open_shell_of_a_groupless_member_carries_on_as_a_tenant(monkeypatch, member) -> None:
    _authentik_says(monkeypatch, known_roles(["Everyone"]))
    check = asyncio.run(ws_identity.check_identity(_user(BUYER)))
    assert (check.ended, check.reason, check.roles) == (False, "active", frozenset({Role.TENANT}))


def test_an_open_shell_ends_when_the_membership_goes_too(monkeypatch, member) -> None:
    _authentik_says(monkeypatch, known_roles(["Everyone"]))
    check = asyncio.run(ws_identity.check_identity(_user("stranger@example.test")))
    assert (check.ended, check.reason) == (True, "no_role_assigned")


def test_a_disabled_account_ends_whatever_its_membership(monkeypatch, member) -> None:
    _authentik_says(monkeypatch, unknown_role("user_inactive"))
    check = asyncio.run(ws_identity.check_identity(_user(BUYER)))
    assert check.ended is True


# --- a download ticket redeemed in a new tab --------------------------------------------------


class _ScopeReached(Exception):
    """Raised where the redeem leaves the role gate and starts reading the caller's scope."""


def _redeem_as(monkeypatch, email: str):
    from app.routers import instance_file_transfer as transfer

    user = _user(email)
    download = SimpleNamespace(identity=SimpleNamespace(user=lambda: user), host="h", project="p", instance="i")

    async def redeem(_ticket):
        return download

    async def scope(_user, interactive=False):
        raise _ScopeReached()

    monkeypatch.setattr(transfer.file_ticket, "redeem", redeem)
    monkeypatch.setattr(transfer, "set_org_scope_for_ws", scope)
    monkeypatch.setattr(transfer, "get_settings", lambda: SimpleNamespace(auth_enabled=True))
    return asyncio.run(transfer.redeem_download(request=_request("/api/v1/files/download"), ticket="t"))


def test_a_groupless_member_redeems_a_download_ticket(monkeypatch, member) -> None:
    with pytest.raises(_ScopeReached):
        _redeem_as(monkeypatch, BUYER)


def test_a_groupless_non_member_cannot_redeem_one(monkeypatch, member) -> None:
    with pytest.raises(HTTPException) as refused:
        _redeem_as(monkeypatch, "stranger@example.test")
    assert refused.value.status_code == 403


# --- a database blip, and what the logs carry ---------------------------------------------------


class _Down:
    async def execute(self, *_args, **_kwargs):
        raise ConnectionError(f"could not reach the database for {BUYER}")


def test_the_minute_recheck_counts_a_failed_read_instead_of_ending_the_shell(monkeypatch) -> None:
    # important: the session guard closes after five failed checks in a row; ending on the first
    # database blip would cut every groupless buyer's open shell at once.
    _authentik_says(monkeypatch, known_roles(["Everyone"]))

    @contextlib.asynccontextmanager
    async def down_session():
        yield _Down()

    monkeypatch.setattr(session_role, "_session", down_session)
    with pytest.raises(ConnectionError):
        asyncio.run(ws_identity.check_identity(_user(BUYER)))


def test_a_strict_read_raises_and_is_not_remembered(rows) -> None:
    with pytest.raises(ConnectionError):
        asyncio.run(session_role.holds_membership(BUYER, _Down(), strict=True))
    assert _holds(BUYER, rows) is True


def test_the_logs_carry_no_email(rows, caplog) -> None:
    caplog.set_level("DEBUG", logger="app.core.session_role")
    assert _holds(BUYER, rows) is True
    assert _holds(BUYER, _Down()) is True, "answered from the cache"
    session_role.forget_memberships()
    assert _holds(BUYER, _Down()) is False
    above_debug = [r for r in caplog.records if r.levelno > 10]
    assert above_debug, "a failed read is still reported"
    for record in above_debug:
        assert BUYER not in record.getMessage()
        assert record.exc_info is None, "a traceback would carry the bound email"


# --- a burst of one account's requests on a cold cache -------------------------------------------


def test_a_burst_on_a_cold_cache_reads_the_membership_once(rows) -> None:
    class Slow:
        def __init__(self) -> None:
            self.reads = 0

        async def execute(self, statement, *args, **kwargs):
            self.reads += 1
            await asyncio.sleep(0.05)
            return await rows.execute(statement, *args, **kwargs)

    slow = Slow()

    async def burst():
        return await asyncio.gather(*(session_role.holds_membership(BUYER, slow) for _ in range(25)))

    assert set(asyncio.run(burst())) == {True}
    assert slow.reads == 1, "concurrent requests of one account share one read"


def test_a_failed_shared_read_refuses_every_waiter_and_is_not_remembered(rows) -> None:
    class SlowDown:
        async def execute(self, *_args, **_kwargs):
            await asyncio.sleep(0.05)
            raise ConnectionError("database down")

    async def burst():
        return await asyncio.gather(*(session_role.holds_membership(BUYER, SlowDown()) for _ in range(5)))

    assert set(asyncio.run(burst())) == {False}
    assert _holds(BUYER, rows) is True
