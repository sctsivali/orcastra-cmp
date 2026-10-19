"""Every field the wire models declare is set by the presenter, for every tier.

The images Flags facet shipped over two fields the backend never sent, because a response
model's default hides a field nobody set. `present.task_row` and `present.log_row` build the
dict the models validate, so the key set is asserted equal to the declared field set, and the
dict is validated for real. A column the models gain and the presenter does not know fails
here, not in the panel.
"""

import ast
import inspect
import os
from datetime import datetime, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.models.task_journal_models import ClusterLog, TaskJournal  # noqa: E402
from app.models.task_schemas import ClusterLogEntry, TaskRecord  # noqa: E402
from app.services.task_journal import present  # noqa: E402

NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)

TASK_ROW = {
    "id": "8d1b2c1e-0000-4000-8000-000000000001", "seq": 1, "kind": "dashboard_action",
    "cluster_id": "nuc1", "project": "default", "resource_type": "instance",
    "resource_name": "web-01", "operation_id": None, "operation_class": None,
    "action": "instance.stop", "description": "Stopping instance", "status": "success",
    "status_code": 200, "progress": None, "error": None, "may_cancel": False,
    "actor_email": "ops@example.test", "actor_role": "admin", "actor_kind": "user",
    "api_key_id": None, "source": "dashboard", "request_id": None,
    "target_organization_id": None, "target_organization_slug": None, "metadata": {},
    "lxd_created_at": None, "lxd_updated_at": None, "started_at": NOW, "finished_at": NOW,
    "created_at": NOW, "updated_at": NOW,
}

LOG_ROW = {
    "id": 1, "ts": NOW, "kind": "lifecycle", "severity": "notice", "cluster_id": "nuc1",
    "project": "default", "resource_type": "instance", "resource_name": "web-01",
    "action": "instance-started", "message": "Instance web-01 started",
    "actor_email": None, "actor_role": None, "actor_kind": "external", "api_key_id": None,
    "result": None, "request_id": None, "event_id": "evt-1", "task_id": None,
    "target_organization_id": None, "details": None,
}


@pytest.mark.parametrize("tier", ["admin", "partner", "tenant"])
def test_a_task_row_sets_every_declared_field(tier: str) -> None:
    out = present.task_row(TASK_ROW, tier=tier, now=NOW)
    assert set(out) == set(TaskRecord.model_fields)
    record = TaskRecord(**out)
    assert record.id == TASK_ROW["id"]
    assert record.duration_ms == 0


@pytest.mark.parametrize("tier", ["admin", "partner", "tenant"])
def test_a_log_row_sets_every_declared_field(tier: str) -> None:
    out = present.log_row(LOG_ROW, tier=tier)
    assert set(out) == set(ClusterLogEntry.model_fields)
    ClusterLogEntry(**out)


def test_the_presenter_reads_every_journal_column_the_wire_declares() -> None:
    """A column that exists on the table and on the wire model but is never read by the
    presenter would be sent as its default forever."""
    task_columns = {c.name for c in TaskJournal.__table__.columns}
    log_columns = {c.name for c in ClusterLog.__table__.columns}
    source = inspect.getsource(present)
    read_keys = {
        node.args[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    } | {
        node.slice.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    }
    unread_task = (task_columns & set(TaskRecord.model_fields)) - read_keys
    unread_log = (log_columns & set(ClusterLogEntry.model_fields)) - read_keys
    assert not unread_task, f"task columns the presenter never reads: {sorted(unread_task)}"
    assert not unread_log, f"log columns the presenter never reads: {sorted(unread_log)}"
