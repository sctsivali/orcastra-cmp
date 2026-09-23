"""The events socket re-reads its caller's scope instead of trusting the one from connect.

The scope was read once when the socket opened, and a socket stays open for as long as the
instances page does. A tenant whose policy was revoked, or an organization unbound from the
cluster, kept receiving that cluster's creation and lifecycle frames until the tab closed.
"""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from app.core import org_scope
from app.routers import events
from tests.instance_access_fakes import CLUSTER, scoped

USER = SimpleNamespace(email="tenant@example.test", user_id="u1", groups=["role_tenant"])


def _setter(*scopes: Optional[set[str]]):
    """A stand-in for set_org_scope_for_ws that hands out these cluster sets in order."""
    remaining = list(scopes)

    async def set_scope(user: Any) -> None:
        allowed = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        org_scope._allowed_clusters_var.set(allowed)
        org_scope._tenant_acl_var.set(None)
        org_scope._visible_projects_var.set(None)

    return set_scope


async def test_a_scope_that_lost_the_cluster_is_refused(monkeypatch):
    monkeypatch.setattr(events, "set_org_scope_for_ws", _setter(set()))
    with scoped():
        assert await events._rescope(USER, CLUSTER) is False


async def test_a_scope_that_still_holds_the_cluster_is_kept(monkeypatch):
    monkeypatch.setattr(events, "set_org_scope_for_ws", _setter({CLUSTER}))
    with scoped():
        assert await events._rescope(USER, CLUSTER) is True


async def test_a_failed_re_read_is_a_refusal(monkeypatch):
    async def broken(user: Any) -> None:
        org_scope._allowed_clusters_var.set({CLUSTER})
        raise RuntimeError("database went away before the ACL narrowed the set")

    monkeypatch.setattr(events, "set_org_scope_for_ws", broken)
    with scoped():
        assert await events._rescope(USER, CLUSTER) is False


def test_the_refresh_interval_is_bounded():
    assert 0 < events.SCOPE_REFRESH_S <= 300


def test_the_sender_re_reads_before_it_judges_a_frame():
    tree = ast.parse(Path(events.__file__).read_text())
    sender = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_sender"
    )
    body = ast.unparse(sender)
    assert "_rescope(" in body
    assert body.index("_rescope(") < body.index("check_tenant_permission(")


class FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.sent: list[dict] = []
        self.closed: Optional[tuple[int, str]] = None

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive_text(self) -> str:
        await asyncio.Event().wait()
        return ""

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


class FakeHub:
    def __init__(self, frames: list[dict]):
        self.queue: asyncio.Queue = asyncio.Queue()
        for frame in frames:
            self.queue.put_nowait(frame)
        self.unsubscribed = False

    async def subscribe(self, host: str) -> asyncio.Queue:
        return self.queue

    async def unsubscribe(self, host: str, queue: asyncio.Queue) -> None:
        self.unsubscribed = True


async def test_a_revoked_caller_is_closed_on_the_next_frame(monkeypatch):
    hub = FakeHub([{"type": "lifecycle", "instance": "web01", "project": "default"}])
    monkeypatch.setattr(events, "get_events_hub", lambda: hub)
    monkeypatch.setattr(events, "SCOPE_REFRESH_S", 0.0)
    monkeypatch.setattr(events, "set_org_scope_for_ws", _setter({CLUSTER}, set()))
    ws = FakeWebSocket()
    with scoped():
        await asyncio.wait_for(events.events_websocket(websocket=ws, host=CLUSTER, user=USER), 5)
    assert ws.accepted
    assert ws.sent == []
    assert ws.closed == (1008, "Forbidden")
    assert hub.unsubscribed


async def test_a_caller_still_in_scope_keeps_receiving(monkeypatch):
    frame = {"type": "lifecycle", "instance": "web01", "project": "default"}
    hub = FakeHub([frame])
    monkeypatch.setattr(events, "get_events_hub", lambda: hub)
    monkeypatch.setattr(events, "SCOPE_REFRESH_S", 0.0)
    monkeypatch.setattr(events, "set_org_scope_for_ws", _setter({CLUSTER}))
    ws = FakeWebSocket()

    async def run() -> None:
        await events.events_websocket(websocket=ws, host=CLUSTER, user=USER)

    with scoped():
        task = asyncio.create_task(run())
        for _ in range(50):
            if ws.sent:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert ws.sent == [frame]
    assert ws.closed is None
