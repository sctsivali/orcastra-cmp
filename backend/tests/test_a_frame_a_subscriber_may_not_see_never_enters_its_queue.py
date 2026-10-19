"""A frame a subscriber may not see is judged before it is queued, under that subscriber's scope.

The hub fans a node's frames out from the node's reader task, which was created under the
FIRST subscriber's context. Judging a frame there with the scope contextvars would apply one
subscriber's rights to everyone, so each subscriber carries a copy of its own context taken
when it subscribed, and the predicate runs under that copy. A frame that fails the check
never costs the subscriber a queue slot, which is what kept a tenant's queue on a busy shared
host from filling with neighbours' frames and evicting its own.
"""
import asyncio
import contextvars
import logging
import os
from functools import partial

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.core import org_scope  # noqa: E402
from app.routers import events  # noqa: E402
from app.routers.events import frame_is_visible  # noqa: E402
from app.services.lxd import events_hub  # noqa: E402


async def _idle(self, host):
    return


@pytest.fixture
def hub(monkeypatch):
    # The upstream reader is not under test; a subscribe must not try to reach a daemon.
    monkeypatch.setattr(events_hub.LXDEventsHub, "_run_node", _idle)
    return events_hub.LXDEventsHub()


async def _subscribe_under(hub, mapping, accept=None):
    """Subscribe from a task whose context carries the given project bound."""
    ctx = contextvars.copy_context()
    ctx.run(org_scope.set_allowed_clusters, None)
    ctx.run(org_scope._visible_projects_var.set, mapping)
    ctx.run(org_scope._tenant_acl_var.set, None)
    predicate = accept if accept is not None else partial(frame_is_visible, "nuc2")
    task = asyncio.create_task(hub.subscribe("nuc2", accept=predicate), context=ctx)
    return await task


def _frame(project: str) -> dict:
    return {"type": "lifecycle", "host": "nuc2", "action": "instance-created",
            "instance": "web-01", "project": project}


async def test_each_subscriber_is_judged_under_its_own_scope(hub):
    partner_a = await _subscribe_under(hub, {"nuc2": {"default", "cust-a"}})
    partner_b = await _subscribe_under(hub, {"nuc2": {"default", "cust-b"}})
    admin = await _subscribe_under(hub, None)

    hub._fanout("nuc2", _frame("cust-a"))

    assert partner_a.qsize() == 1
    assert partner_b.qsize() == 0
    assert admin.qsize() == 1


async def test_a_raising_predicate_drops_its_frames_and_keeps_the_loop_alive(hub, caplog):
    def broken(_payload):
        raise RuntimeError("predicate exploded")

    broken_queue = await _subscribe_under(hub, None, accept=broken)
    healthy = await _subscribe_under(hub, None)

    with caplog.at_level(logging.ERROR, logger="app.services.lxd.events_hub"):
        hub._fanout("nuc2", _frame("default"))
        hub._fanout("nuc2", _frame("default"))

    assert broken_queue.qsize() == 0
    assert healthy.qsize() == 2, "the subscriber after the broken one still receives"
    assert sum("predicate raised" in r.getMessage() for r in caplog.records) == 1


async def test_rescope_widens_what_a_subscriber_receives(hub):
    queue = await _subscribe_under(hub, {"nuc2": {"default"}})
    hub._fanout("nuc2", _frame("cust-a"))
    assert queue.qsize() == 0

    wider = contextvars.copy_context()
    wider.run(org_scope._visible_projects_var.set, {"nuc2": {"default", "cust-a"}})
    wider.run(org_scope._tenant_acl_var.set, None)
    wider.run(hub.rescope, "nuc2", queue)

    hub._fanout("nuc2", _frame("cust-a"))
    assert queue.qsize() == 1


async def test_a_subscriber_without_a_predicate_receives_everything(hub):
    queue = await hub.subscribe("nuc2")
    hub._fanout("nuc2", _frame("cust-a"))
    assert queue.qsize() == 1


def test_the_socket_wires_the_predicate_and_rescopes_after_a_refresh():
    """Asserted on the handler's source, as the sibling stream tests are."""
    import inspect

    source = inspect.getsource(events.events_websocket)
    sender = source.index("async def _sender():")
    assert source.index("accept=partial(frame_is_visible, host)") < sender
    refresh = source.index("await set_org_scope_for_ws(user)", sender)
    rescope = source.index("hub.rescope(host, queue)", sender)
    consult = source.index("if not frame_is_visible(host, payload):", sender)
    assert refresh < rescope < consult
    assert "asyncio.wait_for(" in source[sender:], "the wait must be bounded by the refresh"
