"""The console relay: how a session ends, the serial console's control frames, and kill scope."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from app.routers.console import _is_console_operation
from app.services import console_proxy
from app.services.session_models import SessionProtocol
from tests._interactive_fakes import FakeBrowser, FakeLxdSocket, FakeTracker


@pytest.fixture()
def lxd(monkeypatch):
    sockets = {}
    tracker = FakeTracker()
    calls = []

    @asynccontextmanager
    async def admitted(**_kwargs):
        yield

    async def run_inline(fn, *args, label, host, **kwargs):
        return fn(*args, **kwargs)

    class Service:
        def open_console_operation(self, instance, host, project, console_type, width, height):
            calls.append((console_type, width, height))
            return {"operation_id": "op-1", "ws_url": "wss://c/data", "control_url": "wss://c/control"}

    class Activity:
        def log_activity(self, **kwargs):
            pass

    async def open_socket(url, _ssl, *, pin, timeout, max_size=None):
        sockets[url] = FakeLxdSocket(url)
        return sockets[url]

    monkeypatch.setattr(console_proxy, "guard_instance_mutation", admitted)
    monkeypatch.setattr(console_proxy, "session_io", run_inline)
    monkeypatch.setattr(console_proxy, "socket_material", lambda host: ("wss://c", object(), "pin"))
    monkeypatch.setattr(console_proxy, "open_lxd_socket", open_socket)
    monkeypatch.setattr(console_proxy, "get_console_service", lambda: Service())
    monkeypatch.setattr(console_proxy, "get_session_tracker", lambda: tracker)
    monkeypatch.setattr(console_proxy, "get_activity_logger", lambda: Activity())
    return {"sockets": sockets, "tracker": tracker, "calls": calls}


async def wait_until(predicate, what: str) -> None:
    for _ in range(2000):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"never happened: {what}")


async def test_a_console_the_instance_ends_closes_with_1000(lxd):
    browser = FakeBrowser()
    task = asyncio.create_task(console_proxy.ConsoleProxy(browser, "vm-a", "nuc2", "default", "vga").run())
    await wait_until(lambda: "wss://c/data" in lxd["sockets"], "the data socket")
    lxd["sockets"]["wss://c/data"].end()
    await task

    assert browser.closed == (1000, "The console session ended")


async def test_a_dropped_console_closes_with_1011(lxd):
    browser = FakeBrowser()
    task = asyncio.create_task(console_proxy.ConsoleProxy(browser, "vm-a", "nuc2", "default", "vga").run())
    await wait_until(lambda: "wss://c/data" in lxd["sockets"], "the data socket")
    lxd["sockets"]["wss://c/data"].break_off()
    await task

    assert browser.closed[0] == 1011


async def test_text_sent_to_a_spice_channel_is_not_relayed_into_the_protocol(lxd):
    browser = FakeBrowser()
    task = asyncio.create_task(console_proxy.ConsoleProxy(browser, "vm-a", "nuc2", "default", "vga").run())
    await wait_until(lambda: "wss://c/data" in lxd["sockets"], "the data socket")
    browser.type_text("not spice")
    browser.type_bytes(b"REDQ")
    data = lxd["sockets"]["wss://c/data"]
    await wait_until(lambda: data.received == [b"REDQ"], "the binary frame")
    browser.leave()
    await task

    assert data.received == [b"REDQ"]


async def test_a_serial_console_sizes_resizes_and_tracks_as_serial(lxd):
    browser = FakeBrowser()
    browser.type_text(json.dumps({"width": 132, "height": 40, "flow": True}))
    task = asyncio.create_task(console_proxy.ConsoleProxy(browser, "ct-a", "nuc2", "default", "console").run())
    await wait_until(lambda: "wss://c/control" in lxd["sockets"], "the control socket")
    browser.type_bytes(b"root\n")
    browser.type_text(json.dumps({"type": "resize", "width": 100, "height": 30}))
    data, control = lxd["sockets"]["wss://c/data"], lxd["sockets"]["wss://c/control"]
    await wait_until(lambda: data.received and control.received, "input and resize")
    data.feed(b"login: ")
    await wait_until(lambda: browser.binary() == b"login: ", "the serial output")
    browser.leave()
    await task

    assert ("text", '{"type": "ready"}') in browser.sent
    assert lxd["calls"] == [("console", 132, 40)]
    assert json.loads(control.received[0])["args"] == {"width": "100", "height": "30"}
    assert lxd["tracker"].registered[0]["protocol"] == SessionProtocol.SERIAL


@pytest.mark.parametrize(
    "description,is_console",
    [("Showing console", True), ("Showing console log", True), ("Executing command", False), ("", False)],
)
def test_the_console_conflict_and_kill_paths_only_see_consoles(description, is_console):
    operation = {"class": "websocket", "description": description}
    assert _is_console_operation(operation) is is_console
