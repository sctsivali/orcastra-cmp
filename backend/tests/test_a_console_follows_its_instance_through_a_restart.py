"""A console whose instance powers down closes with 1012, so the browser comes back after a restart.

On LXD 5.21, after a restart LXD keeps the serial console's websocket open and answering pings, but
the new boot goes to a new QEMU process, so the page would say "Connected" over a console that
never prints again. Every restart path (the API, a forced restart, `reboot` inside the guest) emits
instance-shutdown or instance-stopped as the outgoing process exits; the relay ends on that event.
"""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from app.services import console_proxy, instance_power_watch
from tests._interactive_fakes import FakeBrowser, FakeLxdSocket, FakeTracker


@pytest.fixture()
def lxd(monkeypatch):
    sockets = {}

    @asynccontextmanager
    async def admitted(**_kwargs):
        yield

    async def run_inline(fn, *args, label, host, **kwargs):
        return fn(*args, **kwargs)

    class Service:
        def open_console_operation(self, instance, host, project, console_type, width, height):
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
    monkeypatch.setattr(console_proxy, "get_session_tracker", lambda: FakeTracker())
    monkeypatch.setattr(console_proxy, "get_activity_logger", lambda: Activity())
    return sockets


class PowerSwitch:
    """Stands in for the events hub: the test says when the instance goes down."""

    def __init__(self) -> None:
        self.down: "asyncio.Future[str]" = asyncio.get_running_loop().create_future()
        self.watched = []

    async def __call__(self, host: str, instance: str, project: str) -> str:
        self.watched.append((host, instance, project))
        return await self.down


async def wait_until(predicate, what: str) -> None:
    for _ in range(2000):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"never happened: {what}")


@pytest.mark.parametrize(
    "console_type,action,reason",
    [
        ("console", "instance-shutdown", "The instance shut down"),
        ("console", "instance-stopped", "The instance stopped"),
        ("vga", "instance-shutdown", "The instance shut down"),
    ],
)
async def test_a_console_closes_with_1012_when_its_instance_powers_down(lxd, console_type, action, reason):
    power = PowerSwitch()
    browser = FakeBrowser()
    if console_type == "console":
        browser.type_text(json.dumps({"width": 80, "height": 24}))
    proxy = console_proxy.ConsoleProxy(browser, "vm-a", "nuc2", "lab", console_type, watch_power=power)
    task = asyncio.create_task(proxy.run())
    await wait_until(lambda: "wss://c/data" in lxd and power.watched, "the relay and its watch")

    power.down.set_result(action)
    await task

    assert power.watched == [("nuc2", "vm-a", "lab")]
    assert browser.closed == (1012, reason)
    assert lxd["wss://c/data"].closed, "the LXD socket is let go"


async def test_a_failed_watch_never_ends_the_console(lxd):
    async def broken(host, instance, project):
        raise ConnectionError("events hub unreachable")

    browser = FakeBrowser()
    browser.type_text(json.dumps({"width": 80, "height": 24}))
    task = asyncio.create_task(
        console_proxy.ConsoleProxy(browser, "vm-a", "nuc2", "default", "console", watch_power=broken).run()
    )
    await wait_until(lambda: "wss://c/data" in lxd, "the data socket")
    await asyncio.sleep(0.01)
    assert browser.closed is None, "the console stayed open without its watch"

    lxd["wss://c/data"].feed(b"still here")
    await wait_until(lambda: browser.binary() == b"still here", "output after the watch failed")
    browser.leave()
    await task


@pytest.mark.parametrize(
    "frame,expected",
    [
        ({"type": "lifecycle", "action": "instance-shutdown", "instance": "vm-a", "project": "lab"}, True),
        ({"type": "lifecycle", "action": "instance-stopped", "instance": "vm-a", "project": "lab"}, True),
        # The new process coming up is not a reason to end the session that just attached to it.
        ({"type": "lifecycle", "action": "instance-restarted", "instance": "vm-a", "project": "lab"}, False),
        ({"type": "lifecycle", "action": "instance-started", "instance": "vm-a", "project": "lab"}, False),
        ({"type": "lifecycle", "action": "instance-console", "instance": "vm-a", "project": "lab"}, False),
        ({"type": "lifecycle", "action": "instance-shutdown", "instance": "vm-b", "project": "lab"}, False),
        ({"type": "lifecycle", "action": "instance-shutdown", "instance": "vm-a", "project": "other"}, False),
        ({"type": "operation", "action": "instance-shutdown", "instance": "vm-a", "project": "lab"}, False),
    ],
)
def test_only_this_instance_going_down_counts(frame, expected):
    assert instance_power_watch.is_power_down(frame, instance="vm-a", project="lab") is expected


def test_the_default_project_matches_a_frame_without_one():
    frame = {"type": "lifecycle", "action": "instance-stopped", "instance": "vm-a", "project": None}
    assert instance_power_watch.is_power_down(frame, instance="vm-a", project="default") is True


async def test_the_watch_returns_on_the_event_and_leaves_the_hub(monkeypatch):
    class Hub:
        def __init__(self) -> None:
            self.queue: "asyncio.Queue[dict]" = asyncio.Queue()
            self.accept = None
            self.unsubscribed = False

        async def subscribe(self, host, accept=None):
            self.accept = accept
            return self.queue

        async def unsubscribe(self, host, queue):
            self.unsubscribed = True

    hub = Hub()
    monkeypatch.setattr(instance_power_watch, "get_events_hub", lambda: hub)
    watch = asyncio.create_task(instance_power_watch.wait_for_power_down("nuc2", "vm-a", "lab"))
    await wait_until(lambda: hub.accept is not None, "the subscription")

    other = {"type": "lifecycle", "action": "instance-shutdown", "instance": "vm-b", "project": "lab"}
    mine = {"type": "lifecycle", "action": "instance-shutdown", "instance": "vm-a", "project": "lab"}
    assert hub.accept(other) is False and hub.accept(mine) is True
    hub.queue.put_nowait(mine)

    assert await watch == "instance-shutdown"
    assert hub.unsubscribed
