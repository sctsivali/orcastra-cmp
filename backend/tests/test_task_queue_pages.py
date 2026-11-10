"""Queue limits and counts apply after organization, project and Tenant ACL scope.

SQLite executes the same SQLAlchemy predicates and window query without a network service.
PostgreSQL-specific time arithmetic is replaced only in this in-memory test adapter.
"""

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import sqlite

from app.core import org_scope
from app.core.task_schedule_scope import schedule_conditions
from app.core.task_scope import task_conditions, task_is_visible
from app.models.snapshot_schedule_models import SnapshotSchedule
from app.models.task_journal_models import TaskJournal
from app.services.task_journal import reads
from tests._journal_fakes import FakeResult


class MemoryDB:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.statements = []
        columns = ", ".join(f'"{column.name}"' for column in TaskJournal.__table__.columns)
        self.connection.execute(f"CREATE TABLE task_journal ({columns})")
        self.connection.execute(
            "CREATE TABLE snapshot_schedules (id INTEGER, cluster_id TEXT, project TEXT, "
            "instance_name TEXT, state TEXT, next_run_at TEXT, label TEXT, run_state TEXT)"
        )

    async def execute(self, statement):
        sql = str(statement.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
        self.statements.append(sql)
        sql = sql.replace("now() + interval '1 hours'", "datetime('now', '+1 hour')")
        sql = sql.replace("now() - interval '15 minutes'", "datetime('now', '-15 minutes')")
        cursor = self.connection.execute(sql)
        rows = [dict(row) for row in cursor.fetchall()]
        scalar = next(iter(rows[0].values())) if rows else None
        return FakeResult(rows=rows, scalar=scalar)

    def task(
        self, seq, *, cluster="cluster-a", project="tenant-a", name="mine", status="pending",
        kind="dashboard_action", operation_class=None, actor_kind="user", finished_at=None,
    ):
        self.connection.execute(
            "INSERT INTO task_journal (id,seq,kind,cluster_id,project,resource_type,resource_name,"
            "operation_class,status,actor_kind,finished_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"task-{seq}", seq, kind, cluster, project, "instance", name, operation_class, status,
                actor_kind, finished_at, "2026-09-30T00:00:00Z",
            ),
        )

    def schedule(self, number, *, cluster="cluster-a", project="tenant-a", name="mine"):
        self.connection.execute(
            "INSERT INTO snapshot_schedules VALUES (?,?,?,?,?,?,?,?)",
            (number, cluster, project, name, "active", "2020-01-01T00:00:00Z", "daily", "idle"),
        )


@pytest.fixture
def scoped_memory():
    tokens = [
        (org_scope._allowed_clusters_var, org_scope._allowed_clusters_var.set(None)),
        (org_scope._tenant_acl_var, org_scope._tenant_acl_var.set(None)),
        (org_scope._visible_projects_var, org_scope._visible_projects_var.set(None)),
    ]
    db = MemoryDB()
    yield db
    db.connection.close()
    for variable, token in reversed(tokens):
        variable.reset(token)


def set_scope(role):
    org_scope._allowed_clusters_var.set(None if role == "admin" else {"cluster-a"})
    org_scope._visible_projects_var.set(None if role == "admin" else {"cluster-a": {"tenant-a"}})
    org_scope._tenant_acl_var.set(
        {"cluster-a": {("tenant-a", "mine"): {"view", "snapshot"}}} if role == "tenant" else None
    )


@pytest.mark.parametrize("role,expected", [("admin", [1, 2, 3, 4, 5, 6]), ("partner", [1, 2, 3, 4]), ("tenant", [1, 2, 4])])
async def test_queue_pages_and_pending_ranks_are_computed_inside_scope(scoped_memory, role, expected):
    db = scoped_memory
    set_scope(role)
    db.task(1, status="running")
    db.task(2)
    db.task(3, name="neighbour")
    db.task(4)
    db.task(5, project="tenant-b")
    db.task(6, cluster="cluster-b")
    received = []
    for offset in range(0, len(expected) + 2, 2):
        page, total = await reads.queue_rows(db, task_conditions(TaskJournal), 2, offset)
        assert total == len(expected)
        assert len(page) <= 2
        assert all(task_is_visible(row) for row in page)
        received.extend(page)
    assert [row["seq"] for row in received] == expected
    assert received[0]["queue_position"] is None
    pending_a = [row for row in received if row["status"] == "pending" and row["cluster_id"] == "cluster-a"]
    assert [row["queue_position"] for row in pending_a] == list(range(1, len(pending_a) + 1))
    queries = [sql for sql in db.statements if "row_number()" in sql]
    assert queries and all("LIMIT 2 OFFSET" in sql for sql in queries)


async def test_queue_empty_and_out_of_range_pages_keep_scoped_totals(scoped_memory):
    db = scoped_memory
    set_scope("partner")
    assert await reads.queue_rows(db, task_conditions(TaskJournal), 100, 0) == ([], 0)
    db.task(1)
    assert await reads.queue_rows(db, task_conditions(TaskJournal), 100, 100) == ([], 1)
    assert not any("row_number()" in sql for sql in db.statements)


@pytest.mark.parametrize("role", ["admin", "partner", "tenant"])
def test_schedule_sql_scope_matches_snapshot_permission_for_every_row(scoped_memory, role):
    db = scoped_memory
    set_scope(role)
    candidates = []
    for cluster in ("cluster-a", "cluster-b"):
        for project in ("default", "tenant-a", "tenant-b"):
            for name in ("mine", "neighbour"):
                number = len(candidates) + 1
                db.schedule(number, cluster=cluster, project=project, name=name)
                candidates.append((number, cluster, project, name))
    statement = select(SnapshotSchedule.id).where(*schedule_conditions(SnapshotSchedule))
    sql = str(statement.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
    ids = [row["id"] for row in db.connection.execute(sql)]
    expected = [
        number for number, cluster, project, name in candidates
        if (org_scope.get_current_allowed_clusters() is None or cluster in org_scope.get_current_allowed_clusters())
        and org_scope.project_is_visible(cluster, project)
        and org_scope.check_tenant_permission(cluster, project, name, "snapshot")
    ]
    assert ids == expected


async def test_due_schedule_limit_and_total_follow_acl_then_revocation(scoped_memory, monkeypatch):
    db = scoped_memory
    set_scope("tenant")
    monkeypatch.setattr(reads, "get_settings", lambda: SimpleNamespace(snapshot_schedules_enabled=True))
    for number in range(1, 106):
        db.schedule(number)
    for number in range(106, 221):
        db.schedule(number, name="neighbour")
    conditions = schedule_conditions(SnapshotSchedule)
    page, total = await reads.due_schedules(db, ["cluster-a"], conditions=conditions, limit=100)
    assert total == 105 and len(page) == 100
    assert all(row["instance_name"] == "mine" for row in page)
    count_only, total = await reads.due_schedules(db, ["cluster-a"], conditions=conditions, limit=0)
    assert count_only == [] and total == 105
    org_scope._tenant_acl_var.set({"cluster-a": {("tenant-a", "mine"): {"view"}}})
    assert await reads.due_schedules(
        db, ["cluster-a"], conditions=schedule_conditions(SnapshotSchedule), limit=100
    ) == ([], 0)


def test_wildcard_snapshot_grant_stays_inside_shared_cluster_project_bound(scoped_memory):
    db = scoped_memory
    set_scope("tenant")
    org_scope._tenant_acl_var.set({"cluster-a": {("*", "*"): {"snapshot"}}})
    db.schedule(1)
    db.schedule(2, project="tenant-b")
    statement = select(SnapshotSchedule.id).where(*schedule_conditions(SnapshotSchedule))
    sql = str(statement.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
    assert [row["id"] for row in db.connection.execute(sql)] == [1]


# LXD's own background work, as the events follower records it: an LXD task operation that no
# outside requestor started ("Updating images", "Cleaning up expired backups"). A sick daemon can
# hold these open for weeks, so the dock counts leave them out.
HOUSEKEEPING = {"kind": "lxd_operation", "operation_class": "task", "actor_kind": "system"}


def _just_now() -> str:
    # SQLite's datetime('now') shape, so the window comparison is a like-for-like string compare.
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def test_the_dock_counts_leave_out_lxd_housekeeping_and_the_queue_keeps_it(scoped_memory):
    db = scoped_memory
    set_scope("admin")
    for seq in (1, 2, 3):
        db.task(seq, status="running", **HOUSEKEEPING)
    # Everything else open is somebody's action and counts, whoever started it.
    db.task(4, status="running", kind="lxd_operation", operation_class="task", actor_kind="user")
    db.task(5, kind="lxd_operation", operation_class="task", actor_kind="external")
    db.task(6, status="running", kind="lxd_operation", operation_class="websocket", actor_kind="system")
    # A frame with no class is not housekeeping: NOT (a AND NULL) is NULL, which would drop it.
    db.task(7, status="running", kind="lxd_operation", operation_class=None, actor_kind="system")
    db.task(8, kind="dashboard_action", actor_kind="system")
    scope = task_conditions(TaskJournal)

    assert await reads.count_running(db, scope) == 5
    page, total = await reads.queue_rows(db, scope, 100, 0)
    assert total == 8
    assert sorted(row["seq"] for row in page) == [1, 2, 3, 4, 5, 6, 7, 8]


async def test_a_failed_housekeeping_task_is_not_a_recent_failure(scoped_memory):
    db = scoped_memory
    set_scope("admin")
    db.task(1, status="failure", finished_at=_just_now(), **HOUSEKEEPING)
    db.task(
        2, status="failure", finished_at=_just_now(),
        kind="lxd_operation", operation_class="task", actor_kind="user",
    )
    db.task(3, status="failure", finished_at="2020-01-01 00:00:00", kind="dashboard_action")

    assert await reads.count_failed_recent(db, task_conditions(TaskJournal)) == 1


@pytest.mark.parametrize("role,expected", [("admin", 3), ("partner", 2), ("tenant", 1)])
async def test_the_dock_count_keeps_the_role_scope_it_had(scoped_memory, role, expected):
    db = scoped_memory
    set_scope(role)
    db.task(1, status="running")
    db.task(2, name="neighbour")
    db.task(3, cluster="cluster-b")
    db.task(4, status="running", **HOUSEKEEPING)
    db.task(5, cluster="cluster-b", **HOUSEKEEPING)

    assert await reads.count_running(db, task_conditions(TaskJournal)) == expected
