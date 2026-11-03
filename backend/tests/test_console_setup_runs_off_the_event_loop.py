"""Opening a console or a shell never parks the event loop.

The LXD operation POST, the instance read and the certificate material are blocking calls. They
used to run inline in the async socket handler, so while one console opened every other socket on
the worker (consoles, shells, live feeds) froze, long enough for uvicorn's keepalive to drop them.
"""

import asyncio
import json
import threading
from contextlib import asynccontextmanager

from app.services import console_proxy, terminal_proxy
from tests._interactive_fakes import FakeBrowser, FakeLxdSocket, FakeTracker
from tests._loop_probes import WorkerGate, assert_loop_runs_callbacks, wait_for_thread_event


def _wire(module, monkeypatch, tracker):
    @asynccontextmanager
    async def admitted(**_kwargs):
        yield

    async def open_socket(url, _ssl, *, pin, timeout, max_size=None):
        return FakeLxdSocket(url)

    monkeypatch.setattr(module, "guard_instance_mutation", admitted)
    monkeypatch.setattr(module, "socket_material", lambda host: ("wss://c", object(), "pin"))
    monkeypatch.setattr(module, "open_lxd_socket", open_socket)
    monkeypatch.setattr(module, "get_session_tracker", lambda: tracker)


async def test_a_console_operation_is_created_off_the_loop(monkeypatch):
    gate = WorkerGate(threading.get_ident())
    tracker = FakeTracker()

    class Service:
        def open_console_operation(self, *args):
            gate.hold()
            return {"operation_id": "op-1", "ws_url": "wss://c/d", "control_url": "wss://c/c"}

    class Activity:
        def log_activity(self, **kwargs):
            pass

    _wire(console_proxy, monkeypatch, tracker)
    monkeypatch.setattr(console_proxy, "get_console_service", lambda: Service())
    monkeypatch.setattr(console_proxy, "get_activity_logger", lambda: Activity())
    browser = FakeBrowser()
    task = asyncio.create_task(console_proxy.ConsoleProxy(browser, "vm-a", "nuc2", "default", "vga").run())
    try:
        await wait_for_thread_event(gate.entered, task=task, what="the console operation POST")
        assert gate.thread_id != gate.loop_thread, "the LXD console POST ran on the event loop thread"
        await assert_loop_runs_callbacks()
    finally:
        gate.release.set()
        browser.leave()
        await task


async def test_a_shell_is_opened_off_the_loop(monkeypatch):
    gate = WorkerGate(threading.get_ident())
    tracker = FakeTracker()

    class Service:
        def open_exec_operation(self, *args):
            gate.hold()
            return {"operation_id": "exec-1", "ws_url": "wss://c/d", "control_url": "wss://c/c"}

    _wire(terminal_proxy, monkeypatch, tracker)
    monkeypatch.setattr(terminal_proxy, "get_terminal_service", lambda: Service())
    browser = FakeBrowser()
    browser.type_text(json.dumps({"width": 80, "height": 24, "binary": True}))
    task = asyncio.create_task(terminal_proxy.TerminalProxy(browser, "vm-a", "nuc2", "default").run())
    try:
        await wait_for_thread_event(gate.entered, task=task, what="the exec POST")
        assert gate.thread_id != gate.loop_thread, "the LXD exec POST ran on the event loop thread"
        await assert_loop_runs_callbacks()
    finally:
        gate.release.set()
        browser.leave()
        await task


async def test_the_console_service_calls_of_the_router_are_offloaded():
    """Capabilities, boot order and ISO calls are plain functions run through the pool now."""
    import inspect

    from app.routers import console
    from app.services.console_service import ConsoleService

    for name in ("get_instance_capabilities", "get_startup_order", "update_startup_order",
                 "get_instance_iso", "attach_iso", "eject_iso", "open_console_operation"):
        assert not inspect.iscoroutinefunction(getattr(ConsoleService, name)), name
    source = inspect.getsource(console)
    assert "await console_service." not in source, "a blocking console service call is awaited inline"
