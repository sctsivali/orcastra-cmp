"""The hub carries every lifecycle action and the full operation record for the journal, and
exactly what it carried before for a browser.

A browser subscriber's frame is a projection restricted to `BROWSER_FRAME_KEYS`, the key set the
instance list has always received, and it never sees a lifecycle action outside `instance-*`.
A firehose subscriber (the journal follower) gets the whole payload plus the hub's own
connect/disconnect frames. Nothing in either shape ever carries an exec file descriptor or a
command line.
"""

import asyncio
import json
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.services.lxd import events_hub, health  # noqa: E402

TODAYS_BROWSER_KEYS = {
    "type", "host", "action", "instance", "project", "source", "operation_id", "instances",
    "description", "status", "status_code", "progress", "err",
}
# What each frame type carried before the firehose, key for key.
TODAYS_OPERATION_KEYS = {
    "type", "host", "operation_id", "instances", "instance", "project", "description",
    "status", "status_code", "progress", "err",
}
TODAYS_LIFECYCLE_KEYS = {"type", "host", "action", "instance", "project", "source"}


async def _idle(self, host):
    return


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.setattr(events_hub.LXDEventsHub, "_run_node", _idle)
    return events_hub.LXDEventsHub()


def _operation_event(**overrides):
    md = {
        "id": "op-9",
        "class": "task",
        "description": "Creating instance",
        "created_at": "2026-09-19T10:00:00.123456789Z",
        "updated_at": "2026-09-19T10:00:01Z",
        "status": "Running",
        "status_code": 103,
        "may_cancel": True,
        "location": "nuc2-a",
        "resources": {"instances": ["/1.0/instances/web-02?project=cust-a"]},
        "requestor": {"protocol": "unix", "username": "root", "address": "@"},
        "metadata": {
            "download_progress": "rootfs: 48%",
            "fds": {"0": "secret-fd"},
            "command": ["sh", "-c", "echo secret"],
        },
        "err": "",
    }
    md.update(overrides)
    return json.dumps({"type": "operation", "timestamp": "2026-09-19T10:00:01Z", "metadata": md})


def _lifecycle_event(action, name="web-02", **extra):
    md = {
        "action": action,
        "name": name,
        "project": "cust-a",
        "source": "/1.0/instances/web-02",
        "requestor": {"protocol": "unix", "username": "root", "address": "@"},
        "context": {"command": ["bash"], "type": "container"},
    }
    md.update(extra)
    return json.dumps({"type": "lifecycle", "timestamp": "2026-09-19T10:00:02Z", "metadata": md})


def test_the_browser_key_set_is_exactly_what_it_was():
    assert set(events_hub.BROWSER_FRAME_KEYS) == TODAYS_BROWSER_KEYS


def test_parse_keeps_a_non_instance_lifecycle_action():
    payload = events_hub.LXDEventsHub._parse_event("nuc2", _lifecycle_event("network-created", "br0"))
    assert payload is not None
    assert payload["action"] == "network-created"
    assert payload["name"] == "br0"
    assert payload["instance"] is None, "instance names an instance and nothing else"
    assert payload["requestor"] == {"protocol": "unix", "username": "root", "address": "@"}
    assert payload["context"] == {"type": "container"}, "the exec command line never leaves the daemon"


def test_parse_extends_the_operation_payload_additively():
    payload = events_hub.LXDEventsHub._parse_event("nuc2", _operation_event())
    assert payload is not None
    assert TODAYS_OPERATION_KEYS <= set(payload)
    assert payload["class"] == "task"
    assert payload["may_cancel"] is True
    assert payload["created_at"] == "2026-09-19T10:00:00.123456789Z"
    assert payload["updated_at"] == "2026-09-19T10:00:01Z"
    assert payload["location"] == "nuc2-a"
    assert payload["resources"] == {"instances": ["web-02"]}, "?project= is stripped"
    assert payload["project"] == "cust-a"
    assert payload["requestor"] == {"protocol": "unix", "username": "root", "address": "@"}
    assert payload["progress"] == "rootfs: 48%"
    flat = json.dumps(payload)
    assert "secret-fd" not in flat and "echo secret" not in flat and "fds" not in flat
    assert "command" not in flat


async def test_a_non_instance_lifecycle_reaches_only_the_firehose(hub):
    browser = await hub.subscribe("nuc2")
    firehose = await hub.subscribe("nuc2", firehose=True, maxsize=2000)
    payload = events_hub.LXDEventsHub._parse_event("nuc2", _lifecycle_event("network-created", "br0"))

    hub._fanout("nuc2", payload)

    assert browser.qsize() == 0
    assert firehose.qsize() == 1
    assert firehose.get_nowait() is payload, "the firehose receives the full payload"
    assert firehose.maxsize == 2000


async def test_an_instance_lifecycle_reaches_both_as_different_shapes(hub):
    browser = await hub.subscribe("nuc2")
    firehose = await hub.subscribe("nuc2", firehose=True)
    payload = events_hub.LXDEventsHub._parse_event("nuc2", _lifecycle_event("instance-started"))

    hub._fanout("nuc2", payload)

    browser_frame = browser.get_nowait()
    assert set(browser_frame) == TODAYS_LIFECYCLE_KEYS
    assert "requestor" not in browser_frame and "context" not in browser_frame
    assert browser_frame["instance"] == "web-02"
    assert firehose.get_nowait()["requestor"]["protocol"] == "unix"


async def test_a_browser_operation_frame_has_exactly_the_browser_keys(hub):
    browser = await hub.subscribe("nuc2")
    payload = events_hub.LXDEventsHub._parse_event("nuc2", _operation_event())

    hub._fanout("nuc2", payload)

    frame = browser.get_nowait()
    assert set(frame) == TODAYS_OPERATION_KEYS
    assert set(frame) <= TODAYS_BROWSER_KEYS
    for forbidden in ("requestor", "class", "location", "resources", "may_cancel", "timestamp"):
        assert forbidden not in frame


async def test_the_predicate_judges_the_projected_frame(hub):
    seen = []

    def accept(frame):
        seen.append(frame)
        return True

    await hub.subscribe("nuc2", accept=accept)
    hub._fanout("nuc2", events_hub.LXDEventsHub._parse_event("nuc2", _operation_event()))
    assert seen and "requestor" not in seen[0]


async def test_hub_state_frames_reach_only_firehose_queues(hub):
    browser = await hub.subscribe("nuc2")
    firehose = await hub.subscribe("nuc2", firehose=True)

    hub._notify_firehose("nuc2", {"type": "hub", "host": "nuc2", "state": "connected", "error": None})
    hub._notify_firehose(
        "nuc2", {"type": "hub", "host": "nuc2", "state": "disconnected", "error": "reset"}
    )

    assert browser.qsize() == 0
    assert [f["state"] for f in (firehose.get_nowait(), firehose.get_nowait())] == [
        "connected", "disconnected",
    ]


def test_run_node_emits_state_frames_and_honours_the_breaker():
    """Pinned on the source: the upstream loop is not driven against a daemon here."""
    import inspect

    source = inspect.getsource(events_hub.LXDEventsHub._run_node)
    tripped = source.index("health.is_tripped(host)")
    dial = source.index("websockets.connect(")
    assert tripped < dial, "the breaker is consulted before dialing"
    assert "_TRIPPED_SLEEP_S" in source[tripped:dial]
    assert events_hub._TRIPPED_SLEEP_S == 60
    # Live: blitz001 and central1-argon had tripped before the follower subscribed and were
    # never marked disconnected, because the tripped branch slept without saying so.
    tripped_frame = source.index('"state": "disconnected"', tripped)
    assert tripped < tripped_frame < source.index("_TRIPPED_SLEEP_S", tripped), (
        "a tripped host is reported disconnected before the sleep"
    )
    connected = source.index('"state": "connected"')
    disconnected = source.index('"state": "disconnected"', connected)
    assert dial < connected < disconnected
    assert source.index("backoff = 1", dial) < connected, "connected is emitted once the socket is up"
    assert health.is_tripped("never-registered") is False


async def test_subscribe_keeps_its_positional_shape(hub):
    """`hub.subscribe(host)` and `hub.subscribe(host, accept=...)` are the browser call sites."""
    queue = await hub.subscribe("nuc2")
    assert queue.maxsize == events_hub._QUEUE_MAXSIZE
    with pytest.raises(TypeError):
        await hub.subscribe("nuc2", None, True)  # firehose is keyword-only
    assert isinstance(queue, asyncio.Queue)


async def test_a_firehose_subscriber_joining_an_open_node_is_told_connected_at_once(hub):
    """A browser held nuc2 open before the follower was elected on this worker: the upstream
    loop emitted its `connected` frame long ago, so without this the follower never seeds.
    A browser subscriber gets nothing: the frame is not in BROWSER_FRAME_KEYS territory."""
    browser = await hub.subscribe("nuc2")
    node = hub._nodes["nuc2"]
    node.connected = True
    late_firehose = await hub.subscribe("nuc2", firehose=True)
    frame = late_firehose.get_nowait()
    assert frame == {"type": "hub", "host": "nuc2", "state": "connected", "error": None}
    assert browser.empty()
    late_browser = await hub.subscribe("nuc2")
    assert late_browser.empty()
    # A node whose socket is not open says nothing; the upstream loop will.
    node.connected = False
    silent = await hub.subscribe("nuc2", firehose=True)
    assert silent.empty()
