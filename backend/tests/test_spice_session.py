"""Unit tests for SpiceSessionManager - one shared LXD operation per console.

A SPICE console is four channels (main/display/inputs/cursor) opened as four
WebSockets sharing one ticket. These must collapse to ONE LXD console operation
(one control socket, one tracker entry) reused by every channel, and be torn down
exactly once when the last channel leaves. Driven with asyncio.run so the suite
needs no pytest-asyncio plugin.
"""

import asyncio

import app.services.spice_session as ss


class FakeControlWS:
    """Async-iterable stand-in for the upstream control WebSocket."""

    def __init__(self) -> None:
        self.closed = False
        self._evt = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self._evt.wait()  # block until closed, then end iteration
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True
        self._evt.set()


class FakeTracked:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.connected = False

    def mark_connected(self) -> None:
        self.connected = True


class FakeTracker:
    def __init__(self) -> None:
        self.registered: list[str] = []
        self.unregistered: list[str] = []

    def register(self, *, session_id, **_kw):
        self.registered.append(session_id)
        return FakeTracked(session_id)

    def unregister(self, session_id) -> None:
        self.unregistered.append(session_id)


def _patch_success(monkeypatch):
    """Wire the manager to a fake LXD/console stack that always succeeds."""
    create_calls = {"n": 0}
    control_wss: list[FakeControlWS] = []

    async def fake_create(**_kw):
        create_calls["n"] += 1
        return (
            {"operation_id": "op-1", "ws_url": "wss://lxd/data", "control_url": "wss://lxd/ctrl"},
            None,
        )

    async def fake_connect(_url, **_kw):
        ws = FakeControlWS()
        control_wss.append(ws)
        return ws

    class FakeConsoleService:
        create_console_session = staticmethod(fake_create)

        def get_ssl_context(self, _host):
            return object()

    tracker = FakeTracker()
    monkeypatch.setattr(ss, "get_console_service", lambda: FakeConsoleService())
    monkeypatch.setattr(ss, "get_session_tracker", lambda: tracker)
    monkeypatch.setattr(ss.websockets, "connect", fake_connect)
    return create_calls, control_wss, tracker


def test_four_channels_share_one_operation(monkeypatch):
    create_calls, control_wss, tracker = _patch_success(monkeypatch)

    async def scenario():
        mgr = ss.SpiceSessionManager()
        params = dict(instance_name="vm1", host="n1", project="default")

        sessions = await asyncio.gather(
            *[mgr.acquire("ticket-A", **params) for _ in range(4)]
        )

        # ONE operation, ONE control socket, ONE tracker entry for four channels.
        assert create_calls["n"] == 1
        assert len(control_wss) == 1
        assert tracker.registered == ["op-1"]
        assert len({id(s) for s in sessions}) == 1
        assert sessions[0].tracked.connected is True
        assert sessions[0].refcount == 4
        assert mgr.active_count() == 1

        # Releasing three channels keeps the operation alive.
        for _ in range(3):
            await mgr.release("ticket-A")
        assert mgr.active_count() == 1
        assert control_wss[0].closed is False
        assert tracker.unregistered == []

        # The last release tears it down exactly once.
        await mgr.release("ticket-A")
        assert mgr.active_count() == 0
        assert control_wss[0].closed is True
        assert tracker.unregistered == ["op-1"]

    asyncio.run(scenario())


def test_distinct_tickets_get_distinct_operations(monkeypatch):
    create_calls, control_wss, _tracker = _patch_success(monkeypatch)

    async def scenario():
        mgr = ss.SpiceSessionManager()
        params = dict(instance_name="vm1", host="n1", project="default")
        await mgr.acquire("ticket-A", **params)
        await mgr.acquire("ticket-B", **params)
        assert create_calls["n"] == 2  # one per ticket
        assert mgr.active_count() == 2
        await mgr.release("ticket-A")
        await mgr.release("ticket-B")
        assert mgr.active_count() == 0

    asyncio.run(scenario())


def test_create_failure_raises_and_leaks_nothing(monkeypatch):
    async def fake_create(**_kw):
        return (None, "instance not running")

    class FakeConsoleService:
        create_console_session = staticmethod(fake_create)

        def get_ssl_context(self, _host):
            return object()

    monkeypatch.setattr(ss, "get_console_service", lambda: FakeConsoleService())
    monkeypatch.setattr(ss, "get_session_tracker", lambda: FakeTracker())

    async def scenario():
        mgr = ss.SpiceSessionManager()
        params = dict(instance_name="vm1", host="n1", project="default")
        raised = False
        try:
            await mgr.acquire("ticket-A", **params)
        except ss.ConsoleSessionError as e:
            raised = True
            assert "not running" in str(e)
        assert raised is True
        assert mgr.active_count() == 0  # failed entry reaped, no leak

    asyncio.run(scenario())
