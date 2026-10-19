"""The browser event stream is a list of every project, so it owes the same bound the lists do.

The hub used to subscribe to `/1.0/events?type=operation,lifecycle`, which LXD answers with the
default project's events only. A create into any other project produced no frame, and the
"Creating..." placeholder sat until its ten-minute expiry with the instance long running.
Subscribing with `all-projects=true` closes that, and widens the stream to every project on the
host, so `frame_is_visible` applies the Partner project bound before the tenant ACL: on a
cluster two organisations share, a Partner must not start receiving the other organisation's
instance names, image aliases and error text the moment the subscription widened.
"""
import contextvars
import json
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.core import org_scope  # noqa: E402
from app.routers.events import frame_is_visible  # noqa: E402
from app.services.lxd import events_hub  # noqa: E402


class _FakeCertManager:
    def get_cluster_info(self, host):
        return {"metadata": {"endpoint": "https://10.0.0.1:8443/"}}


class _FakeTerminalService:
    cert_manager = _FakeCertManager()


def test_the_hub_subscribes_to_every_project(monkeypatch):
    monkeypatch.setattr(events_hub, "get_terminal_service", lambda: _FakeTerminalService())
    url = events_hub.LXDEventsHub()._build_ws_url("nuc2")
    assert url == "wss://10.0.0.1:8443/1.0/events?type=operation,lifecycle&all-projects=true"


@pytest.mark.parametrize(
    "raw,expected_project",
    [
        (
            {"type": "lifecycle", "project": "other-org",
             "metadata": {"action": "instance-created", "name": "web-01", "project": "other-org"}},
            "other-org",
        ),
        (
            {"type": "operation", "project": "other-org",
             "metadata": {"id": "op-1", "status": "Success", "status_code": 200,
                          "resources": {"instances": ["/1.0/instances/web-01"]}}},
            "other-org",
        ),
        # LXD writes the project into the resource URL outside the default project. The
        # name must come out clean, or the frame matches no placeholder and a refused create
        # never shows as failed.
        (
            {"type": "operation", "project": "other-org",
             "metadata": {"id": "op-2", "status": "Failure", "status_code": 400,
                          "resources": {"instances": ["/1.0/instances/web-01?project=other-org"]}}},
            "other-org",
        ),
        (
            {"type": "operation",
             "metadata": {"id": "op-3", "status": "Running", "status_code": 103,
                          "resources": {"instances": ["/1.0/instances/web-01?project=other-org"]}}},
            "other-org",
        ),
    ],
)
def test_a_frame_carries_its_project(raw, expected_project):
    payload = events_hub.LXDEventsHub._parse_event("nuc2", json.dumps(raw))
    assert payload is not None
    assert payload["project"] == expected_project
    assert payload["instance"] == "web-01"


def _decide(mapping, acl, host, payload):
    def check():
        org_scope.set_allowed_clusters(None)
        org_scope._visible_projects_var.set(mapping)
        org_scope._tenant_acl_var.set(acl)
        return frame_is_visible(host, payload)

    return contextvars.copy_context().run(check)


@pytest.mark.parametrize(
    "mapping,project,expected",
    [
        # A Partner bound to the default project on a shared cluster: the other org's frame drops.
        ({"nuc2": {"default"}}, "other-org", False),
        ({"nuc2": {"default"}}, "default", True),
        # A frame with no project is a default-project frame, as a record with no project is.
        ({"nuc2": {"default"}}, None, True),
        # The bound is per cluster: an unlisted cluster restricts nothing.
        ({"nuc2": {"default"}}, "other-org", False),
        # Nothing set (Admin, or a Partner on an unshared cluster): everything passes.
        (None, "other-org", True),
    ],
)
def test_the_partner_bound_holds_on_the_stream(mapping, project, expected):
    payload = {"type": "lifecycle", "host": "nuc2", "action": "instance-created",
               "instance": "web-01", "project": project}
    assert _decide(mapping, None, "nuc2", payload) is expected


def test_an_unlisted_cluster_restricts_nothing():
    payload = {"type": "operation", "host": "nuc1", "instance": "web-01", "project": "other-org"}
    assert _decide({"nuc2": {"default"}}, None, "nuc1", payload) is True


def test_a_tenant_still_sees_only_its_own_instances():
    acl = {"nuc2": {("customer-a", "*"): {"view"}}}
    mine = {"type": "lifecycle", "host": "nuc2", "instance": "web-01", "project": "customer-a"}
    theirs = {"type": "lifecycle", "host": "nuc2", "instance": "db-01", "project": "customer-b"}
    unattributed = {"type": "operation", "host": "nuc2", "instance": None, "project": "customer-a"}
    assert _decide(None, acl, "nuc2", mine) is True
    assert _decide(None, acl, "nuc2", theirs) is False
    # A frame this stream cannot attribute to an instance names nothing of the tenant's.
    assert _decide(None, acl, "nuc2", unattributed) is False


def test_a_partner_receives_the_cluster_wide_frames():
    unattributed = {"type": "operation", "host": "nuc2", "instance": None, "project": "default"}
    assert _decide({"nuc2": {"default"}}, None, "nuc2", unattributed) is True


def test_a_long_lived_socket_re_reads_its_scope():
    """The scope is resolved at connect; a socket that lives for hours must not keep it forever.

    Asserted on the sender's source, as the sibling stream tests do, because the sender is a
    closure over a live WebSocket.
    """
    import inspect

    from app.routers import events

    assert 0 < events.SCOPE_REFRESH_S <= 300
    source = inspect.getsource(events.events_websocket)
    sender = source.index("async def _sender():")
    refresh = source.index("await set_org_scope_for_ws(user)", sender)
    consult = source.index("if not frame_is_visible(host, payload):", sender)
    assert refresh < consult, "the scope must be re-read before the frame is judged"


async def test_a_queued_browser_frame_carries_no_requestor(monkeypatch):
    """The hub now parses the daemon's `requestor` for the journal follower; a browser queue
    receives the projection, which never carries it."""
    import asyncio

    async def _idle(self, host):
        return

    monkeypatch.setattr(events_hub.LXDEventsHub, "_run_node", _idle)
    hub = events_hub.LXDEventsHub()
    queue = await hub.subscribe("nuc2")
    raw = {"type": "operation", "project": "default",
           "metadata": {"id": "op-4", "status": "Running", "status_code": 103,
                        "resources": {"instances": ["/1.0/instances/web-01"]},
                        "requestor": {"protocol": "unix", "username": "root", "address": "@"}}}
    payload = events_hub.LXDEventsHub._parse_event("nuc2", json.dumps(raw))
    assert payload["requestor"] == {"protocol": "unix", "username": "root", "address": "@"}

    hub._fanout("nuc2", payload)

    frame = await asyncio.wait_for(queue.get(), 1)
    assert "requestor" not in frame
    assert set(frame) <= events_hub.BROWSER_FRAME_KEYS
