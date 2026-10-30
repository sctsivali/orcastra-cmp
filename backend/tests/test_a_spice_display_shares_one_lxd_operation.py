"""The channels of one SPICE display share one LXD console operation.

spice-html5 opens a socket per channel (main, display, inputs, cursor). Each used to create its own
LXD operation, control socket and tracked session, which cost four rounds of cluster setup per
console and showed four sessions where the operator opened one. The channels now meet in the
SPICE session store: the first creates the operation, the rest join it with a data socket only.
"""

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Dict, List

import pytest

from app.core.config import get_settings
from app.services import console_proxy
from app.services.lxd_ws import SessionSetupError
from app.services.spice_session import SpiceSessionStore, set_spice_session_store
from tests._interactive_fakes import FakeBrowser, FakeLxdSocket, FakeTracker

GROUP = "display-group-1"


@pytest.fixture(autouse=True)
def secret_key():
    settings = get_settings()
    original = settings.secret_key
    object.__setattr__(settings, "secret_key", "test-secret-for-spice-sessions")
    yield
    object.__setattr__(settings, "secret_key", original)


@pytest.fixture()
def store():
    store = SpiceSessionStore()
    set_spice_session_store(store)
    yield store
    set_spice_session_store(None)


class Cluster:
    """The LXD side: counts operations and hands out sockets the test can drive."""

    def __init__(self) -> None:
        self.operations: List[Dict[str, Any]] = []
        self.sockets: Dict[str, List[FakeLxdSocket]] = {}
        self.refuse: SessionSetupError | None = None

    def open_console_operation(self, instance, host, project, console_type, width, height):
        if self.refuse is not None:
            raise self.refuse
        number = len(self.operations) + 1
        info = {
            "operation_id": f"op-{number}",
            "ws_url": f"wss://cluster/1.0/operations/op-{number}/websocket?secret=data-{number}",
            "control_url": f"wss://cluster/1.0/operations/op-{number}/websocket?secret=control-{number}",
        }
        self.operations.append(info)
        return info

    async def open_socket(self, url, _ssl, *, timeout, max_size=None):
        socket = FakeLxdSocket(url)
        self.sockets.setdefault(url, []).append(socket)
        return socket


@pytest.fixture()
def cluster(monkeypatch):
    cluster = Cluster()
    tracker = FakeTracker()

    @asynccontextmanager
    async def admitted(**_kwargs):
        yield

    async def run_inline(fn, *args, label, host, **kwargs):
        return fn(*args, **kwargs)

    class Service:
        open_console_operation = staticmethod(cluster.open_console_operation)

    class Activity:
        def __init__(self) -> None:
            self.records: List[dict] = []

        def log_activity(self, **kwargs):
            self.records.append(kwargs)

    activity = Activity()
    monkeypatch.setattr(console_proxy, "guard_instance_mutation", admitted)
    monkeypatch.setattr(console_proxy, "session_io", run_inline)
    monkeypatch.setattr(console_proxy, "connection_material", lambda host: ("wss://cluster", object()))
    monkeypatch.setattr(console_proxy, "open_lxd_socket", cluster.open_socket)
    monkeypatch.setattr(console_proxy, "get_console_service", lambda: Service())
    monkeypatch.setattr(console_proxy, "get_session_tracker", lambda: tracker)
    monkeypatch.setattr(console_proxy, "get_activity_logger", lambda: activity)
    monkeypatch.setattr(console_proxy, "_MEMBER_SETTLE_SECONDS", 0.0)
    cluster.tracker = tracker
    cluster.activity = activity
    return cluster


def channel(browser: FakeBrowser, group: str | None = GROUP) -> console_proxy.ConsoleProxy:
    return console_proxy.ConsoleProxy(browser, "vm-a", "nuc2", "default", "vga", user_email="op@example.test", group=group)


async def wait_until(predicate, what: str) -> None:
    for _ in range(2000):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"never happened: {what}")


async def test_four_channels_create_one_operation_and_one_tracked_session(cluster, store):
    browsers = [FakeBrowser() for _ in range(4)]
    main = asyncio.create_task(channel(browsers[0]).run())
    await wait_until(lambda: len(cluster.operations) == 1 and len(cluster.sockets) == 2, "the main channel's sockets")
    others = [asyncio.create_task(channel(b).run()) for b in browsers[1:]]
    data_url = cluster.operations[0]["ws_url"]
    await wait_until(lambda: len(cluster.sockets.get(data_url, [])) == 4, "every channel's data socket")

    assert len(cluster.operations) == 1
    assert len(cluster.tracker.registered) == 1
    assert [r["action"] for r in cluster.activity.records] == ["console.session.create"]

    for b in browsers:
        b.leave()
    await asyncio.gather(main, *others)


async def test_a_joining_channel_relays_its_own_frames_on_the_shared_operation(cluster, store):
    main_browser, display_browser = FakeBrowser(), FakeBrowser()
    main = asyncio.create_task(channel(main_browser).run())
    await wait_until(lambda: len(cluster.operations) == 1 and len(cluster.sockets) == 2, "the main channel")
    display = asyncio.create_task(channel(display_browser).run())
    data_url = cluster.operations[0]["ws_url"]
    await wait_until(lambda: len(cluster.sockets[data_url]) == 2, "the display channel's data socket")

    display_socket = cluster.sockets[data_url][1]
    display_socket.feed(b"\x01\x02frame")
    display_browser.type_bytes(b"ack")
    await wait_until(lambda: display_browser.binary() == b"\x01\x02frame", "the frame reaching the display channel")
    await wait_until(lambda: display_socket.received == [b"ack"], "the ack reaching LXD")

    display_browser.leave()
    main_browser.leave()
    await asyncio.gather(main, display)


async def test_the_owner_reports_the_bytes_of_every_channel(cluster, store):
    main_browser, display_browser = FakeBrowser(), FakeBrowser()
    owner = channel(main_browser)
    main = asyncio.create_task(owner.run())
    await wait_until(lambda: len(cluster.operations) == 1 and len(cluster.sockets) == 2, "the main channel")
    display = asyncio.create_task(channel(display_browser).run())
    data_url = cluster.operations[0]["ws_url"]
    await wait_until(lambda: len(cluster.sockets[data_url]) == 2, "the display channel")

    cluster.sockets[data_url][1].feed(b"x" * 5000)
    await wait_until(lambda: len(display_browser.binary()) == 5000, "the display frame")
    display_browser.leave()
    await display
    main_browser.leave()
    await main

    assert cluster.tracker.sessions[0].bytes_downloaded == 5000
    assert cluster.tracker.unregistered == [cluster.tracker.sessions[0].session_id]


async def test_closing_the_display_retracts_its_record(cluster, store):
    browser = FakeBrowser()
    main = asyncio.create_task(channel(browser).run())
    await wait_until(lambda: len(cluster.sockets) == 2, "the main channel")
    browser.leave()
    await main

    assert await store.wait_for(GROUP, timeout=0) is None


async def test_the_record_never_holds_the_secret_in_the_clear(cluster, store):
    browser = FakeBrowser()
    main = asyncio.create_task(channel(browser).run())
    await wait_until(lambda: len(cluster.sockets) == 2, "the main channel")

    stored = [value for _expiry, value in store._local.values() if isinstance(value, str)]
    assert stored, "the owner published a record"
    assert not any("secret=" in value or "data-1" in value for value in stored)
    browser.leave()
    await main


async def test_a_tampered_record_attaches_nothing(store):
    await store.publish(GROUP, {"data_url": "wss://cluster/op/data?secret=s"})
    key = next(k for k in store._local if k.endswith(":op"))
    expiry, sealed = store._local[key]
    store._local[key] = (expiry, sealed[:-4] + ("AAAA" if not sealed.endswith("AAAA") else "BBBB"))

    assert await store.wait_for(GROUP, timeout=0) is None


async def test_a_channel_opens_its_own_operation_when_no_record_appears(cluster, store, monkeypatch):
    await store.claim(GROUP)  # an owner that claimed and then vanished
    original = store.wait_for

    async def quick(group, timeout=10.0):
        return await original(group, timeout=0.05)

    monkeypatch.setattr(store, "wait_for", quick)
    browser = FakeBrowser()
    task = asyncio.create_task(channel(browser).run())
    await wait_until(lambda: len(cluster.operations) == 1 and len(cluster.sockets) == 2, "the fallback operation")
    browser.leave()
    await task

    assert len(cluster.tracker.registered) == 1


async def test_a_refused_display_tells_its_waiting_channels_at_once(cluster, store):
    cluster.refuse = SessionSetupError("The instance is not running (status: Stopped)", retryable=False)
    main_browser, display_browser = FakeBrowser(), FakeBrowser()
    await channel(main_browser).run()
    await channel(display_browser).run()

    assert main_browser.closed == (4404, "The instance is not running (status: Stopped)")
    assert display_browser.closed == (4404, "The instance is not running (status: Stopped)")
    assert cluster.operations == []


async def test_a_channel_without_a_group_opens_its_own_operation(cluster, store):
    first, second = FakeBrowser(), FakeBrowser()
    a = asyncio.create_task(channel(first, group=None).run())
    b = asyncio.create_task(channel(second, group=None).run())
    await wait_until(lambda: len(cluster.operations) == 2, "two operations")
    first.leave()
    second.leave()
    await asyncio.gather(a, b)


def test_the_vga_tickets_of_one_display_carry_one_group():
    from app.core.ws_ticket import TicketClaims, WsTarget, claims_for
    from dataclasses import replace
    from app.services.spice_session import new_group

    claims = replace(claims_for("console", WsTarget(host="nuc2", instance="vm-a", console_type="vga"), None), group=new_group())
    again = TicketClaims.from_json(claims.to_json())

    assert again.group == claims.group and claims.group
    assert TicketClaims.from_json(claims_for("terminal", WsTarget(), None).to_json()).group is None


# ── the shared record is bound to its group and its target ─────────────────────────────────────


async def test_a_record_copied_into_another_group_is_refused(store):
    """Fernet has no associated data: a sealed record moved to another group's key used to decrypt
    and attach a stranger's channels to the victim's display. The record now names its own key."""
    await store.publish("victim-group", {"data_url": "wss://cluster/1.0/operations/op-1/websocket?secret=s",
                                         "operation_id": "op-1", "host": "nuc2", "project": "default", "instance": "vm-a"})
    victim_key = next(k for k in store._local if k.endswith(":op"))
    expiry, sealed = store._local[victim_key]
    from app.services.spice_session import _keys
    store._local[_keys("attacker-group")[1]] = (expiry, sealed)

    assert await store.wait_for("attacker-group", timeout=0) is None


async def test_a_member_does_not_join_a_display_of_another_instance(cluster, store):
    await store.claim(GROUP)
    await store.publish(GROUP, {"data_url": "wss://cluster/1.0/operations/op-9/websocket?secret=s",
                                "operation_id": "op-9", "host": "nuc2", "project": "default", "instance": "other-vm"})
    browser = FakeBrowser()
    task = asyncio.create_task(channel(browser).run())
    # It opens its own operation rather than the other instance's display.
    await wait_until(lambda: len(cluster.operations) == 1, "a fallback operation of its own")
    assert "wss://cluster/1.0/operations/op-9/websocket?secret=s" not in cluster.sockets
    browser.leave()
    await task


async def test_a_member_does_not_follow_a_url_outside_the_clusters_operations(cluster, store):
    await store.claim(GROUP)
    await store.publish(GROUP, {"data_url": "wss://elsewhere.example/1.0/operations/op-9/websocket?secret=s",
                                "operation_id": "op-9", "host": "nuc2", "project": "default", "instance": "vm-a"})
    browser = FakeBrowser()
    task = asyncio.create_task(channel(browser).run())
    await wait_until(lambda: len(cluster.operations) == 1, "a fallback operation of its own")
    assert not any(url.startswith("wss://elsewhere.example") for url in cluster.sockets)
    browser.leave()
    await task


def test_the_local_store_forgets_expired_records(store):
    for i in range(50):
        store._local[f"stale-{i}"] = (0.0, "x")
    store._local_set("fresh", "y", 60)
    assert list(store._local) == ["fresh"]


def test_a_failure_log_never_carries_an_operation_secret():
    from app.services.lxd_ws import redact

    text = "wss://10.0.0.5:8443/1.0/operations/abc/websocket?secret=3f9a3f9a isn't a valid URI"
    assert "3f9a3f9a" not in redact(text)


async def test_a_refused_record_ends_the_wait_at_once(store, caplog):
    """A record that is there but refused (another display's, or one sealed by an older build)
    is not waited out for ten seconds, nor logged twenty times a second."""
    import logging
    import time as _time

    await store.publish("other-group", {"data_url": "wss://cluster/1.0/operations/op-1/websocket?secret=s",
                                        "operation_id": "op-1", "host": "nuc2", "project": "default", "instance": "vm-a"})
    from app.services.spice_session import _keys
    expiry, sealed = store._local[_keys("other-group")[1]]
    store._local[_keys(GROUP)[1]] = (expiry, sealed)

    started = _time.monotonic()
    with caplog.at_level(logging.WARNING, logger="app.services.spice_session"):
        assert await store.wait_for(GROUP, timeout=5) is None
    assert _time.monotonic() - started < 1
    assert len([r for r in caplog.records if "another display" in r.getMessage()]) == 1
