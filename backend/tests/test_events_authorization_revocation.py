"""The browser event stream must lose a cluster as soon as refreshed scope does.

Project and instance predicates are not substitutes for the cluster boundary. Partners have no
tenant ACL, and an unshared cluster has no project bound, so both predicates can legitimately
return open after the organization binding that authorized the socket has been revoked.
"""

import asyncio
import contextvars
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.core import database as database_module  # noqa: E402
from app.core import org_scope  # noqa: E402
from app.core.auth import AuthenticatedUser  # noqa: E402
from app.routers import events  # noqa: E402


HOST = "nuc2"
PROJECT = "customer-a"
INSTANCE = "queued-before-revoke"


def _visible_under(
    *,
    allowed: set[str] | None,
    projects: dict[str, set[str]] | None = None,
    acl: dict | None = None,
) -> bool:
    """Judge one frame in a fresh context so another test's scope cannot leak into it."""
    ctx = contextvars.Context()
    ctx.run(org_scope.set_allowed_clusters, allowed)
    ctx.run(org_scope._visible_projects_var.set, projects)
    ctx.run(org_scope._tenant_acl_var.set, acl)
    frame = {
        "type": "lifecycle",
        "host": HOST,
        "action": "instance-created",
        "instance": "web-01",
        "project": PROJECT,
    }
    return ctx.run(events.frame_is_visible, HOST, frame)


def test_a_partner_binding_revocation_closes_the_cluster_gate() -> None:
    # A Partner has neither an ACL nor, on an unshared cluster, a project bound. The cluster
    # membership is therefore the only gate that changes when its organization is unbound.
    assert _visible_under(allowed={"nuc2"}) is True
    assert _visible_under(allowed=set()) is False


def test_tenant_and_shared_project_narrowing_both_fail_closed() -> None:
    acl = {"nuc2": {("customer-a", "web-01"): {"view"}}}
    assert (
        _visible_under(
            allowed={"nuc2"},
            projects={"nuc2": {"customer-a"}},
            acl=acl,
        )
        is True
    )

    # A tenant assignment revoke narrows the allowed cluster set even if an older ACL-shaped
    # frame would otherwise match.
    assert (
        _visible_under(
            allowed=set(),
            projects={"nuc2": {"customer-a"}},
            acl=acl,
        )
        is False
    )

    # A shared-cluster project-bound change is independently enforced while cluster access
    # remains valid.
    assert (
        _visible_under(
            allowed={"nuc2"},
            projects={"nuc2": {"customer-b"}},
            acl=acl,
        )
        is False
    )


class _WebSocket:
    def __init__(self, *, stop_after: asyncio.Event | None = None) -> None:
        self.accepted = False
        self.closed: list[tuple[int, str]] = []
        self.sent: list[dict] = []
        self._never = asyncio.Event()
        self._stop_after = stop_after

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, *, code: int, reason: str) -> None:
        self.closed.append((code, reason))

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive_text(self) -> str:
        if self._stop_after is not None:
            await self._stop_after.wait()
            # Finish the receiver task without an unhandled exception. The handler observes
            # FIRST_COMPLETED and cancels its sender, exactly as it does for a disconnected
            # browser, while the assertion can inspect the frame decision that woke us.
            raise asyncio.CancelledError
        await self._never.wait()
        return ""


class _Hub:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.queue.put_nowait(
            {
                "type": "lifecycle",
                "host": HOST,
                "action": "instance-created",
                "instance": INSTANCE,
                "project": PROJECT,
            }
        )
        self.rescopes: list[tuple[str, asyncio.Queue]] = []
        self.unsubscribed = False

    async def subscribe(self, host: str, accept=None) -> asyncio.Queue:
        assert host == HOST
        assert accept is not None
        return self.queue

    def rescope(self, host: str, queue: asyncio.Queue) -> None:
        self.rescopes.append((host, queue))

    async def unsubscribe(self, host: str, queue: asyncio.Queue) -> None:
        assert (host, queue) == (HOST, self.queue)
        self.unsubscribed = True


def _user(role: str, scenario: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=f"{role}-{scenario}",
        email=f"{role}@example.test",
        groups=[f"role_{role}"],
        roles=[f"role_{role}"],
    )


def _install_scope_sequence(monkeypatch, *, role: str, outcome: str) -> dict[str, int]:
    """Drive the real WebSocket scope resolver through an initial and refreshed read."""
    state = {"reads": 0}

    class _SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def _allowed(_user, _db):
        state["reads"] += 1
        if role == "partner" and outcome == "cluster-revoked" and state["reads"] > 1:
            return set()
        return {HOST}

    async def _tenant_scope(_user, _db):
        if role == "partner":
            return None, None, None
        if state["reads"] == 1:
            return (
                {HOST: {(PROJECT, INSTANCE): {"view"}}},
                [SimpleNamespace(id="initial-grant")],
                {HOST: {PROJECT}},
            )
        if outcome == "cluster-revoked":
            # The organization still owns the cluster, but the Tenant assignment no longer
            # does. The real resolver intersects the org cluster set with this empty ACL.
            return {}, [], {}
        return (
            {HOST: {(PROJECT, "still-authorized"): {"view"}}},
            [SimpleNamespace(id="remaining-grant")],
            {HOST: {PROJECT}},
        )

    async def _visible_projects(_user, _db, **_kwargs):
        if state["reads"] == 1:
            return {HOST: {PROJECT}}
        if outcome == "cluster-revoked":
            return {}
        return {HOST: {"default"}}

    async def _org_names(_user, _db):
        return []

    monkeypatch.setattr(database_module, "async_session_maker", _SessionContext)
    monkeypatch.setattr(org_scope, "get_user_allowed_clusters", _allowed)
    monkeypatch.setattr(org_scope, "get_tenant_scope", _tenant_scope)
    monkeypatch.setattr(org_scope, "get_visible_projects", _visible_projects)
    monkeypatch.setattr(org_scope, "_resolve_user_org_names", _org_names)

    async def _no_read_only(*_args):
        return frozenset()

    monkeypatch.setattr(org_scope, "_partner_read_only", _no_read_only)
    # The fake session answers no query, so the party read behind `default` view-only is stubbed too.
    monkeypatch.setattr(org_scope, "_default_view_only", _no_read_only)
    return state


def _clear_scope() -> None:
    org_scope.set_allowed_clusters(None)
    org_scope._visible_projects_var.set(None)
    org_scope._tenant_acl_var.set(None)
    org_scope._resolved_tenant_grants_var.set(None)


@pytest.mark.parametrize("role", ["partner", "tenant"])
async def test_partner_and_tenant_revocation_close_1008_before_a_queued_frame_is_sent(
    monkeypatch,
    role: str,
) -> None:
    websocket = _WebSocket()
    hub = _Hub()
    state = _install_scope_sequence(monkeypatch, role=role, outcome="cluster-revoked")

    monkeypatch.setattr(events, "get_events_hub", lambda: hub)
    monkeypatch.setattr(events, "get_settings", lambda: SimpleNamespace(auth_enabled=True))
    # Refresh before draining the queue. This deterministically models a frame queued under the
    # old binding immediately before the periodic authorization refresh.
    monkeypatch.setattr(events, "SCOPE_REFRESH_S", 0.0)

    try:
        await events.events_websocket(
            websocket,
            host=HOST,
            user=_user(role, "cluster-revoked"),
        )
    finally:
        # pytest-asyncio can reuse the task context; leave the authorization defaults clean.
        _clear_scope()

    assert websocket.accepted is True
    assert state["reads"] == 2
    assert websocket.closed == [(1008, "Forbidden")]
    assert websocket.sent == [], "a frame queued under the revoked binding must not be sent"
    assert hub.rescopes == [], "a denied scope must not be installed in the shared hub"
    assert hub.unsubscribed is True


@pytest.mark.parametrize("role", ["partner", "tenant"])
async def test_partner_and_tenant_scope_narrowing_rejects_an_already_queued_frame(
    monkeypatch,
    role: str,
) -> None:
    frame_judged = asyncio.Event()
    websocket = _WebSocket(stop_after=frame_judged)
    hub = _Hub()
    state = _install_scope_sequence(monkeypatch, role=role, outcome="frame-revoked")
    original_frame_is_visible = events.frame_is_visible

    def _record_send_time_decision(host: str, payload: dict) -> bool:
        visible = original_frame_is_visible(host, payload)
        frame_judged.set()
        return visible

    monkeypatch.setattr(events, "frame_is_visible", _record_send_time_decision)
    monkeypatch.setattr(events, "get_events_hub", lambda: hub)
    monkeypatch.setattr(events, "get_settings", lambda: SimpleNamespace(auth_enabled=True))
    monkeypatch.setattr(events, "SCOPE_REFRESH_S", 1.0)
    # Keep a positive queue timeout, then make the scope due immediately after the queued frame
    # is returned. Replacing the router's module reference avoids altering asyncio's own clock.
    ticks = iter((0.0, 0.0, 2.0, 2.0))
    monkeypatch.setattr(
        events,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks, 2.0)),
    )

    try:
        await events.events_websocket(
            websocket,
            host=HOST,
            user=_user(role, "frame-revoked"),
        )
    finally:
        _clear_scope()

    assert websocket.accepted is True
    assert state["reads"] == 2
    assert websocket.closed == []
    assert websocket.sent == [], "a frame queued under the old project/ACL must be dropped"
    assert hub.rescopes == [(HOST, hub.queue)]
    assert frame_judged.is_set(), "the refreshed scope must judge the queued frame before send"
    assert hub.unsubscribed is True
