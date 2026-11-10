"""A viewer whose display another viewer took ends with 4409, instead of taking it back in a loop.

QEMU serves one SPICE client per display; a new one takes the display from the last. LXD then
drops the earlier viewer's sockets the same way a broken network does, so a browser told 1011
reconnects and takes the display back, and two viewers trade it every second or two, each turn a
new LXD operation. The newest owner marks itself the instance's display before it connects, and
a relay whose LXD side ends under someone else's mark says so.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Dict, List

import pytest

from app.core.config import get_settings
from app.services import console_proxy
from app.services.spice_session import SpiceSessionStore, set_spice_session_store
from tests._interactive_fakes import FakeBrowser, FakeLxdSocket, FakeTracker


@pytest.fixture(autouse=True)
def secret_key():
    settings = get_settings()
    original = settings.secret_key
    object.__setattr__(settings, "secret_key", "test-secret-for-spice-sessions")
    yield
    object.__setattr__(settings, "secret_key", original)


class RecordingStore(SpiceSessionStore):
    def __init__(self, events: List[str]) -> None:
        super().__init__()
        self.events = events

    async def mark_display(self, host, project, instance, operation_id):
        self.events.append(f"mark {operation_id}")
        await super().mark_display(host, project, instance, operation_id)


class Cluster:
    def __init__(self) -> None:
        self.events: List[str] = []
        self.sockets: Dict[str, List[FakeLxdSocket]] = {}
        self.count = 0
        self.refuse_data = False

    def open_console_operation(self, instance, host, project, console_type, width, height):
        self.count += 1
        n = self.count
        return {
            "operation_id": f"op-{n}",
            "ws_url": f"wss://cluster/1.0/operations/op-{n}/websocket?secret=data-{n}",
            "control_url": f"wss://cluster/1.0/operations/op-{n}/websocket?secret=control-{n}",
        }

    async def open_socket(self, url: str, _ssl: Any, *, pin: str, timeout: float, max_size: Any = None) -> FakeLxdSocket:
        self.events.append(f"socket {url.rsplit('secret=', 1)[-1]}")
        if self.refuse_data and "secret=data" in url:
            raise ConnectionError("LXD refused the data socket")
        socket = FakeLxdSocket(url)
        self.sockets.setdefault(url, []).append(socket)
        return socket

    def drop_client(self, n: int) -> None:
        """QEMU let go of operation n's viewer: every data socket on it breaks."""
        for socket in self.sockets[f"wss://cluster/1.0/operations/op-{n}/websocket?secret=data-{n}"]:
            socket.break_off()


@pytest.fixture()
def cluster(monkeypatch):
    cluster = Cluster()

    @asynccontextmanager
    async def admitted(**_kwargs):
        yield

    async def run_inline(fn, *args, label, host, **kwargs):
        return fn(*args, **kwargs)

    class Service:
        open_console_operation = staticmethod(cluster.open_console_operation)

    class Activity:
        def log_activity(self, **kwargs):
            pass

    store = RecordingStore(cluster.events)
    set_spice_session_store(store)
    monkeypatch.setattr(console_proxy, "guard_instance_mutation", admitted)
    monkeypatch.setattr(console_proxy, "session_io", run_inline)
    monkeypatch.setattr(console_proxy, "socket_material", lambda host: ("wss://cluster", object(), "pin"))
    monkeypatch.setattr(console_proxy, "open_lxd_socket", cluster.open_socket)
    monkeypatch.setattr(console_proxy, "get_console_service", lambda: Service())
    monkeypatch.setattr(console_proxy, "get_session_tracker", lambda: FakeTracker())
    monkeypatch.setattr(console_proxy, "get_activity_logger", lambda: Activity())
    monkeypatch.setattr(console_proxy, "_MEMBER_SETTLE_SECONDS", 0.0)
    cluster.store = store
    yield cluster
    set_spice_session_store(None)


def viewer(browser: FakeBrowser, group: str) -> console_proxy.ConsoleProxy:
    return console_proxy.ConsoleProxy(browser, "vm-a", "nuc2", "default", "vga", group=group)


async def wait_until(predicate, what: str) -> None:
    for _ in range(2000):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"never happened: {what}")


async def test_the_owner_marks_the_display_before_it_connects(cluster):
    browser = FakeBrowser()
    task = asyncio.create_task(viewer(browser, "group-a").run())
    await wait_until(lambda: "socket data-1" in cluster.events, "the data socket")

    assert cluster.events.index("mark op-1") < cluster.events.index("socket control-1") < cluster.events.index("socket data-1")
    browser.leave()
    await task


async def test_a_viewer_whose_display_was_taken_closes_with_4409(cluster):
    first_main, first_display = FakeBrowser(), FakeBrowser()
    owner = asyncio.create_task(viewer(first_main, "group-a").run())
    await wait_until(lambda: "socket data-1" in cluster.events, "the first viewer's operation")
    member = asyncio.create_task(viewer(first_display, "group-a").run())
    await wait_until(lambda: cluster.events.count("socket data-1") == 2, "the first viewer's display channel")

    second = FakeBrowser()
    newer = asyncio.create_task(viewer(second, "group-b").run())
    await wait_until(lambda: "socket data-2" in cluster.events, "the second viewer's operation")
    # QEMU drops the first client: LXD's sockets end the way a broken network ends them.
    cluster.drop_client(1)
    await asyncio.gather(owner, member)

    assert first_main.closed == (4409, "Another viewer opened this console")
    assert first_display.closed == (4409, "Another viewer opened this console")
    assert await cluster.store.display_of("nuc2", "default", "vm-a") == "op-2", "the first viewer's exit kept the newer mark"
    second.leave()
    await newer


async def test_a_lost_connection_on_its_own_display_is_still_1011(cluster):
    browser = FakeBrowser()
    task = asyncio.create_task(viewer(browser, "group-a").run())
    await wait_until(lambda: "socket data-1" in cluster.events, "the data socket")

    cluster.drop_client(1)
    await task

    assert browser.closed[0] == 1011


async def test_the_owner_forgets_its_mark_when_it_leaves(cluster):
    browser = FakeBrowser()
    task = asyncio.create_task(viewer(browser, "group-a").run())
    await wait_until(lambda: "socket data-1" in cluster.events, "the data socket")
    assert await cluster.store.display_of("nuc2", "default", "vm-a") == "op-1"

    browser.leave()
    await task

    assert await cluster.store.display_of("nuc2", "default", "vm-a") is None


async def test_a_viewer_without_a_display_group_never_claims_a_takeover(cluster):
    """A channel that opened its own operation (no group) still ends with 1011."""
    await cluster.store.mark_display("nuc2", "default", "vm-a", "op-someone-else")
    browser = FakeBrowser()
    task = asyncio.create_task(viewer(browser, group=None).run())
    await wait_until(lambda: "socket data-1" in cluster.events, "the data socket")

    cluster.drop_client(1)
    await task

    assert browser.closed[0] == 1011


async def test_an_owner_whose_setup_failed_leaves_no_mark(cluster):
    """The mark goes in before the sockets; a failed connect must not leave it behind for 60 s."""
    cluster.refuse_data = True
    browser = FakeBrowser()

    await viewer(browser, "group-a").run()

    assert "mark op-1" in cluster.events
    assert browser.closed[0] == 1011
    assert await cluster.store.display_of("nuc2", "default", "vm-a") is None


async def test_a_cancelled_relay_stops_its_power_watch(cluster):
    watching = asyncio.Event()
    stopped = asyncio.Event()

    async def watch(host, instance, project):
        watching.set()
        try:
            await asyncio.get_running_loop().create_future()
        finally:
            stopped.set()

    browser = FakeBrowser()
    proxy = console_proxy.ConsoleProxy(browser, "vm-a", "nuc2", "default", "vga", group="group-a", watch_power=watch)
    task = asyncio.create_task(proxy.run())
    await asyncio.wait_for(watching.wait(), 2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stopped.is_set(), "the power watch outlived its relay"


async def test_the_mark_is_renewed_and_forgotten_in_one_redis_step():
    """Separate GET and EXPIRE or DEL calls would let a leaver delete a newer viewer's mark."""

    class Client:
        def __init__(self) -> None:
            self.calls: List[tuple] = []

        async def eval(self, script, numkeys, *args):
            self.calls.append((script, numkeys, args))
            return 1

        def __getattr__(self, name):
            raise AssertionError(f"a separate {name} call is not atomic")

    store = SpiceSessionStore()
    store._client = Client()
    await store.keep_display("nuc2", "default", "vm-a", "op-1")
    await store.forget_display("nuc2", "default", "vm-a", "op-1")

    (keep, forget) = store._client.calls
    assert "GET" in keep[0] and "EXPIRE" in keep[0] and keep[1] == 1 and keep[2][1:] == ("op-1", 60)
    assert "GET" in forget[0] and "DEL" in forget[0] and forget[2][1:] == ("op-1",)
    assert keep[2][0] == forget[2][0] and keep[2][0].startswith("orcastra:spice:display:")

