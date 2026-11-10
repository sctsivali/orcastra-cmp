"""A frame a subscriber may not see never enters its queue, and the socket judges it twice.

The tailer fans one frame out to every subscriber on the worker. Each predicate runs under the
context captured at subscribe (or at the last rescope), so an Admin, a Partner and a tenant on one
worker each receive their own view of the same row. A predicate that raises fails closed and is
logged once. The WebSocket handler's source is pinned like the events stream: the predicate goes
to subscribe before the sender exists, and after a scope refresh the rescope precedes the
re-judgement, which precedes the send.

The tick runs against a fake session: the first tick only sets the watermark, the next emits each
change once, a full page asks the client to resync, and a follower change is announced once.
"""

import asyncio
import contextvars
import inspect
import logging
import os
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Dict, List, Optional

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from _journal_fakes import FakeResult, FakeSessionMaker  # noqa: E402
from app.core import org_scope  # noqa: E402
from app.routers import tasks_ws  # noqa: E402
from app.services.task_journal import tailer as tailer_module  # noqa: E402
from app.services.task_journal.tailer import PAGE, TaskFeedTailer  # noqa: E402

NOW = datetime(2026, 9, 19, 2, 0, 0, tzinfo=timezone.utc)


def _task_row(row_id: str, seq: int, **overrides: Any) -> Dict[str, Any]:
    row = {
        "id": row_id, "seq": seq, "kind": "lxd_operation", "cluster_id": "nuc1", "project": "cust-a",
        "resource_type": "instance", "resource_name": "web-01", "operation_id": f"op-{seq}",
        "operation_class": "task", "action": "instance.start", "description": "Starting",
        "status": "running", "status_code": 103, "progress": None, "error": None, "may_cancel": True,
        "actor_email": None, "actor_role": None, "actor_kind": "external", "api_key_id": None,
        "source": "cli", "request_id": None, "target_organization_id": None,
        "target_organization_slug": None, "metadata": {}, "lxd_created_at": None,
        "lxd_updated_at": None, "started_at": None, "finished_at": None, "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


def _subscribe_under(tailer: TaskFeedTailer, allowed: Optional[set], acl: Optional[dict], accept):
    async def run() -> asyncio.Queue:
        org_scope._allowed_clusters_var.set(allowed)
        org_scope._tenant_acl_var.set(acl)
        org_scope._visible_projects_var.set(None)
        return await tailer.subscribe(accept=accept)

    return contextvars.copy_context().run(lambda: asyncio.ensure_future(run()))


async def test_three_subscribers_on_one_worker_each_get_their_own_view() -> None:
    tailer = TaskFeedTailer(run_loop=False)
    accept = partial(tasks_ws.feed_frame_is_visible, caller_email="ops@example.test")
    admin = await _subscribe_under(tailer, None, None, accept)
    foreign = await _subscribe_under(tailer, {"vdc-hetzner"}, None, accept)
    tenant = await _subscribe_under(tailer, {"nuc1"}, {"nuc1": {("cust-a", "*"): {"view"}}}, accept)
    assert tailer.subscriber_count == 3

    tailer._fanout({"type": "task", "row": _task_row("t1", 1)})
    assert (admin.qsize(), foreign.qsize(), tenant.qsize()) == (1, 0, 1)

    # A network row on the tenant's cluster reaches Admin only.
    tailer._fanout({"type": "task", "row": _task_row("t2", 2, resource_type="network", resource_name="br0")})
    assert (admin.qsize(), foreign.qsize(), tenant.qsize()) == (2, 0, 1)

    # A host-less log line reaches Admin and its own actor.
    hostless = {"id": 9, "cluster_id": None, "actor_email": "ops@example.test"}
    tailer._fanout({"type": "log", "row": hostless})
    assert (admin.qsize(), foreign.qsize(), tenant.qsize()) == (3, 1, 2)

    # Follower and resync frames carry no data and reach everyone.
    tailer._fanout({"type": "follower", "active": False})
    tailer._fanout({"type": "resync", "reason": "lag"})
    assert (admin.qsize(), foreign.qsize(), tenant.qsize()) == (5, 3, 4)

    # Each predicate ran under its captured context; the process default (Admin) is untouched.
    assert org_scope.get_current_allowed_clusters() is None
    for queue in (admin, foreign, tenant):
        await tailer.unsubscribe(queue)
    assert tailer.subscriber_count == 0


async def test_rescope_replaces_the_context_a_subscriber_is_judged_under() -> None:
    tailer = TaskFeedTailer(run_loop=False)
    accept = partial(tasks_ws.feed_frame_is_visible, caller_email=None)
    queue = await _subscribe_under(tailer, {"nuc1"}, None, accept)
    tailer._fanout({"type": "task", "row": _task_row("t1", 1)})
    assert queue.qsize() == 1

    def narrow() -> None:
        org_scope._allowed_clusters_var.set(set())
        tailer.rescope(queue)

    contextvars.copy_context().run(narrow)
    tailer._fanout({"type": "task", "row": _task_row("t1", 2)})
    assert queue.qsize() == 1, "after the narrowing the same row no longer enters the queue"


async def test_a_raising_predicate_fails_closed_and_logs_once(caplog) -> None:
    tailer = TaskFeedTailer(run_loop=False)

    def boom(_frame: Dict[str, Any]) -> bool:
        raise RuntimeError("predicate exploded")

    bad = await tailer.subscribe(accept=boom)
    good = await tailer.subscribe(accept=lambda _frame: True)
    with caplog.at_level(logging.ERROR, logger=tailer_module.__name__):
        for seq in range(3):
            tailer._fanout({"type": "task", "row": _task_row("t1", seq)})
    assert bad.qsize() == 0
    assert good.qsize() == 3
    assert sum("predicate raised" in r.getMessage() for r in caplog.records) == 1


async def test_a_slow_consumer_drops_its_oldest_frame() -> None:
    tailer = TaskFeedTailer(run_loop=False)
    queue = await tailer.subscribe(accept=lambda _frame: True)
    for seq in range(tailer_module.QUEUE_MAXSIZE + 5):
        tailer._fanout({"type": "task", "row": _task_row("t1", seq)})
    assert queue.qsize() == tailer_module.QUEUE_MAXSIZE
    assert queue.get_nowait()["row"]["seq"] == 5


# --- the tick against a fake session ------------------------------------------------------------


class _Journal:
    """Plays the database: a clock, a task page, a log page and the follower lock."""

    def __init__(self) -> None:
        self.now = NOW
        self.tasks: List[Dict[str, Any]] = []
        self.logs: List[Dict[str, Any]] = []
        self.follower = True
        self.params: List[Any] = []

    def respond(self, sql: str, params: Any) -> Optional[FakeResult]:
        if "now()" in sql and "SELECT now()" in sql:
            return FakeResult(scalar=self.now)
        if "pg_locks" in sql:
            return FakeResult(scalar=self.follower)
        if "FROM task_journal" in sql:
            self.params.append(params)
            return FakeResult(rows=list(self.tasks))
        if "FROM cluster_log" in sql:
            return FakeResult(rows=list(self.logs))
        return None


async def _tailer_with(journal: _Journal) -> tuple:
    tailer = TaskFeedTailer(session_maker=FakeSessionMaker(journal.respond), run_loop=False)
    queue = await tailer.subscribe(accept=lambda _frame: True)
    return tailer, queue


def _drain(queue: asyncio.Queue) -> List[Dict[str, Any]]:
    frames = []
    while not queue.empty():
        frames.append(queue.get_nowait())
    return frames


async def test_the_first_tick_sets_the_watermark_without_replaying_history() -> None:
    journal = _Journal()
    journal.tasks = [_task_row("old", 1)]
    tailer, queue = await _tailer_with(journal)
    await tailer.tick()
    assert _drain(queue) == [], "history is refetched on hello, never replayed"
    assert journal.params == [], "the first tick reads the clock only"
    assert tailer.follower_state is True


async def test_a_change_is_emitted_once_and_the_lookback_is_five_seconds() -> None:
    journal = _Journal()
    tailer, queue = await _tailer_with(journal)
    await tailer.tick()
    journal.now = NOW + timedelta(seconds=1)
    journal.tasks = [_task_row("t1", 7)]
    journal.logs = [{"id": 3, "ts": NOW, "cluster_id": "nuc1"}]
    await tailer.tick()
    frames = _drain(queue)
    assert [f["type"] for f in frames] == ["task", "log"]
    assert frames[0]["row"]["seq"] == 7 and frames[1]["row"]["id"] == 3
    assert journal.params[-1]["since"] == NOW - timedelta(seconds=5)
    assert journal.params[-1]["page"] == PAGE
    # The same rows inside the lookback window are not emitted twice; a bumped seq is.
    journal.now = NOW + timedelta(seconds=2)
    await tailer.tick()
    assert _drain(queue) == []
    journal.tasks = [_task_row("t1", 8)]
    await tailer.tick()
    assert [f["row"]["seq"] for f in _drain(queue)] == [8]


async def test_a_full_page_asks_the_client_to_resync() -> None:
    journal = _Journal()
    tailer, queue = await _tailer_with(journal)
    await tailer.tick()
    journal.tasks = [_task_row(f"t{i}", i) for i in range(PAGE)]
    await tailer.tick()
    frames = _drain(queue)
    assert frames[-1] == {"type": "resync", "reason": "lag"}
    # The page filled the queue and the resync frame displaced the oldest task frame, so the
    # client refetches instead of trusting a stream with a gap.
    assert len(frames) == tailer_module.QUEUE_MAXSIZE
    assert sum(f["type"] == "task" for f in frames) == PAGE - 1


async def test_a_follower_change_is_announced_once() -> None:
    journal = _Journal()
    tailer, queue = await _tailer_with(journal)
    await tailer.tick()
    tailer._follower_checked_at = 0.0
    journal.follower = False
    await tailer.tick()
    assert _drain(queue) == [{"type": "follower", "active": False}]
    tailer._follower_checked_at = 0.0
    await tailer.tick()
    assert _drain(queue) == [], "no change, no frame"
    assert tailer.follower_state is False


async def test_the_loop_starts_on_the_first_subscriber_and_stops_on_the_last() -> None:
    journal = _Journal()
    tailer = TaskFeedTailer(session_maker=FakeSessionMaker(journal.respond))
    queue = await tailer.subscribe(accept=lambda _frame: True)
    assert tailer._task is not None and not tailer._task.done()
    await asyncio.sleep(0)
    await tailer.unsubscribe(queue)
    await asyncio.sleep(0)
    assert tailer._task is None


# --- the socket ---------------------------------------------------------------------------------


def test_the_socket_judges_before_enqueue_and_again_after_the_rescope() -> None:
    source = inspect.getsource(tasks_ws.tasks_websocket)
    sender = source.index("async def _sender():")
    assert source.index("accept=partial(feed_frame_is_visible, caller_email=caller_email)") < sender
    refresh = source.index("await set_org_scope_for_ws(user)", sender)
    rescope = source.index("tailer.rescope(queue)", sender)
    judge = source.index("if not feed_frame_is_visible(frame, caller_email=caller_email):", sender)
    send = source.index("await websocket.send_json(_present(frame, caller_tier()))", sender)
    assert refresh < rescope < judge < send
    assert "asyncio.wait_for(" in source[sender:], "the wait must be bounded by the refresh"
    assert 'await websocket.close(code=1008, reason="Authentication required")' in source
    assert source.index("await set_org_scope_for_ws(user)") < source.index("await websocket.accept()")
    assert "await tailer.unsubscribe(queue)" in source[source.index("finally:"):]


def test_the_socket_reuses_the_events_stream_refresh_period() -> None:
    from app.routers import events

    assert tasks_ws.SCOPE_REFRESH_S is events.SCOPE_REFRESH_S


@pytest.mark.parametrize("kind", ["follower", "resync"])
def test_data_free_frames_pass_every_scope(kind: str) -> None:
    def judge() -> bool:
        org_scope._allowed_clusters_var.set(set())
        org_scope._tenant_acl_var.set({})
        return tasks_ws.feed_frame_is_visible({"type": kind}, caller_email=None)

    assert contextvars.copy_context().run(judge) is True
    assert tasks_ws.feed_frame_is_visible({"type": "unknown"}, caller_email=None) is False


def test_a_presented_frame_is_json_ready_and_carries_the_tier_policy() -> None:
    row = _task_row("t1", 1, target_organization_id=6, metadata={"location": "m1"})
    admin = tasks_ws._present({"type": "task", "row": row}, "admin")
    tenant = tasks_ws._present({"type": "task", "row": row}, "tenant")
    assert admin["type"] == "task" and admin["task"]["target_organization_id"] == 6
    assert tenant["task"]["target_organization_id"] is None and tenant["task"]["location"] is None
    assert isinstance(admin["task"]["created_at"], str), "datetimes are serialised for send_json"
    log = {
        "id": 1, "ts": NOW, "kind": "audit", "severity": "info", "cluster_id": "nuc1",
        "action": "x", "message": "m", "actor_kind": "user", "target_organization_id": 6,
    }
    assert tasks_ws._present({"type": "log", "row": log}, "partner")["entry"]["target_organization_id"] is None
    assert tasks_ws._present({"type": "resync", "reason": "lag"}, "tenant") == {"type": "resync", "reason": "lag"}
