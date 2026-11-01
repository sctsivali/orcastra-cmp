"""The terminal relays PTY bytes intact, bounds what is in flight, and says how a session ended.

- Output used to be decoded per LXD frame with errors="replace": a multibyte character split
  across two frames (htop's box drawing, CJK, emoji) reached xterm as U+FFFD.
- Nothing bounded output in flight, so `yes` grew the browser tab until it stalled.
- Every ending reached the browser as 1006, so a shell that had just exited was reconnected.
"""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from app.services import terminal_proxy
from app.services.interactive_relay import OutputWindow, close_reason
from app.services.lxd_ws import SessionSetupError
from tests._interactive_fakes import FakeBrowser, FakeLxdSocket, FakeTracker

SPLIT = "é─界🙂".encode("utf-8")


@pytest.fixture()
def lxd(monkeypatch):
    sockets = {}
    tracker = FakeTracker()
    state = {"refuse": None}

    @asynccontextmanager
    async def admitted(**_kwargs):
        yield

    async def run_inline(fn, *args, label, host, **kwargs):
        return fn(*args, **kwargs)

    class Service:
        def open_exec_operation(self, instance, host, project, width, height):
            if state["refuse"] is not None:
                raise state["refuse"]
            state["size"] = (width, height)
            return {"operation_id": "exec-1", "ws_url": "wss://c/data", "control_url": "wss://c/control"}

    async def open_socket(url, _ssl, *, timeout, max_size=None):
        sockets[url] = FakeLxdSocket(url)
        return sockets[url]

    monkeypatch.setattr(terminal_proxy, "guard_instance_mutation", admitted)
    monkeypatch.setattr(terminal_proxy, "session_io", run_inline)
    monkeypatch.setattr(terminal_proxy, "connection_material", lambda host: ("wss://c", object()))
    monkeypatch.setattr(terminal_proxy, "open_lxd_socket", open_socket)
    monkeypatch.setattr(terminal_proxy, "get_terminal_service", lambda: Service())
    monkeypatch.setattr(terminal_proxy, "get_session_tracker", lambda: tracker)
    return {"sockets": sockets, "tracker": tracker, "state": state}


def proxy(browser: FakeBrowser) -> terminal_proxy.TerminalProxy:
    return terminal_proxy.TerminalProxy(browser, "vm-a", "nuc2", "default", user_email="op@example.test")


async def wait_until(predicate, what: str) -> None:
    for _ in range(2000):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"never happened: {what}")


async def test_binary_output_reaches_the_browser_byte_for_byte(lxd):
    browser = FakeBrowser()
    browser.type_text(json.dumps({"width": 100, "height": 30, "binary": True, "flow": True}))
    task = asyncio.create_task(proxy(browser).run())
    await wait_until(lambda: "wss://c/data" in lxd["sockets"], "the data socket")
    data = lxd["sockets"]["wss://c/data"]
    data.feed(SPLIT[:3])  # "é" and the first byte of "─"
    data.feed(SPLIT[3:])
    await wait_until(lambda: browser.binary() == SPLIT, "the bytes")
    browser.leave()
    await task

    assert lxd["state"]["size"] == (100, 30)
    assert browser.json_types()[:2] == ["session", "ready"]


async def test_the_previous_protocol_still_gets_whole_characters(lxd):
    browser = FakeBrowser()
    browser.type_text(json.dumps({"width": 80, "height": 24}))
    task = asyncio.create_task(proxy(browser).run())
    await wait_until(lambda: "wss://c/data" in lxd["sockets"], "the data socket")
    data = lxd["sockets"]["wss://c/data"]
    for cut in (SPLIT[:1], SPLIT[1:5], SPLIT[5:]):
        data.feed(cut)
    await wait_until(
        lambda: "".join(p["data"] for k, p in browser.sent if k == "json" and p.get("type") == "output") == SPLIT.decode(),
        "the decoded text",
    )
    browser.leave()
    await task

    assert "�" not in json.dumps([p for k, p in browser.sent if k == "json"], ensure_ascii=False)


async def test_input_and_resize_reach_their_sockets(lxd):
    browser = FakeBrowser()
    browser.type_text(json.dumps({"width": 80, "height": 24, "binary": True}))
    task = asyncio.create_task(proxy(browser).run())
    await wait_until(lambda: "wss://c/control" in lxd["sockets"], "the sockets")
    browser.type_bytes(b"ls\r")
    browser.type_text(json.dumps({"type": "input", "data": "é"}))
    browser.type_text(json.dumps({"type": "resize", "width": 132, "height": 43}))
    data, control = lxd["sockets"]["wss://c/data"], lxd["sockets"]["wss://c/control"]
    await wait_until(lambda: len(data.received) == 2 and len(control.received) == 1, "input and resize")
    browser.leave()
    await task

    assert data.received == [b"ls\r", "é".encode()]
    assert json.loads(control.received[0]) == {"command": "window-resize", "args": {"width": "132", "height": "43"}}


async def test_a_shell_that_exits_closes_with_1000_and_says_so(lxd):
    browser = FakeBrowser()
    browser.type_text(json.dumps({"width": 80, "height": 24, "binary": True}))
    task = asyncio.create_task(proxy(browser).run())
    await wait_until(lambda: "wss://c/data" in lxd["sockets"], "the data socket")
    lxd["sockets"]["wss://c/data"].end()
    await task

    assert browser.closed == (1000, "The shell exited")
    assert "closed" in browser.json_types()
    assert lxd["tracker"].unregistered == ["exec-1"]


async def test_a_dropped_cluster_connection_closes_with_1011(lxd):
    browser = FakeBrowser()
    browser.type_text(json.dumps({"width": 80, "height": 24, "binary": True}))
    task = asyncio.create_task(proxy(browser).run())
    await wait_until(lambda: "wss://c/data" in lxd["sockets"], "the data socket")
    lxd["sockets"]["wss://c/data"].break_off()
    await task

    assert browser.closed[0] == 1011


@pytest.mark.parametrize("retryable,code", [(False, 4404), (True, 1011)])
async def test_a_session_that_cannot_start_says_why_and_whether_to_retry(lxd, retryable, code):
    lxd["state"]["refuse"] = SessionSetupError("The instance is not running (status: Stopped)", retryable=retryable)
    browser = FakeBrowser()
    browser.type_text(json.dumps({"width": 80, "height": 24, "binary": True}))
    await proxy(browser).run()

    assert browser.closed == (code, "The instance is not running (status: Stopped)")
    assert {
        "type": "error", "message": "The instance is not running (status: Stopped)", "code": code, "retryable": retryable,
    } in [p for k, p in browser.sent if k == "json"]


async def test_a_fenced_instance_closes_with_4423_and_keeps_the_sentence_in_the_frame(lxd):
    """The page reads the code; the sentence rides in the frame, where 123 bytes do not cut it."""
    from app.services.migration_fence import MigrationFenceConflict

    lxd["state"]["refuse"] = MigrationFenceConflict("Migration Center is moving this instance")
    browser = FakeBrowser()
    browser.type_text(json.dumps({"width": 80, "height": 24, "binary": True}))
    await proxy(browser).run()

    assert browser.closed == (4423, "migration_fenced")
    assert {
        "type": "error", "message": "Migration Center is moving this instance", "code": 4423, "retryable": False,
    } in [p for k, p in browser.sent if k == "json"]


async def test_output_waits_for_acks_past_the_high_water_mark():
    window = OutputWindow(enabled=True)
    window.sent(OutputWindow.HIGH_WATER)
    waiting = asyncio.create_task(window.before_send())
    await asyncio.sleep(0)
    assert not waiting.done()

    window.ack(OutputWindow.HIGH_WATER - OutputWindow.LOW_WATER + 1)
    await asyncio.wait_for(waiting, 1)


async def test_a_client_that_never_acks_is_never_paused():
    window = OutputWindow(enabled=False)
    window.sent(OutputWindow.HIGH_WATER * 4)
    await asyncio.wait_for(window.before_send(), 1)


def test_a_close_reason_fits_the_frame_on_a_character_boundary():
    reason = close_reason("界" * 100)
    assert len(reason.encode("utf-8")) <= 123
    assert reason.endswith("...")
    reason.encode("utf-8").decode("utf-8")
