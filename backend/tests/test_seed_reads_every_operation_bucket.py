"""A seed reads every bucket of `/1.0/operations` across projects, off the loop, with both
timeouts, and marks what the daemon no longer reports as lost.

The hub only carries what happens while it is connected. The seed on every connect (and the
reconcile every two minutes) is what closes the gap: `running`, `pending`, `success`, `failure`
and `cancelled` are all upserted, a daemon that refuses `all-projects` is walked project by
project, and an open row whose id is not in the answer is marked lost. A cluster the breaker
has tripped is never read.
"""

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.services.lxd import health  # noqa: E402
from app.services.task_journal import follower as follower_module  # noqa: E402
from app.services.task_journal import seed  # noqa: E402
from tests._follower_fakes import FakeHub, LockStore, NoIdentities  # noqa: E402
from tests._journal_fakes import FakeResult, FakeSessionMaker  # noqa: E402
from tests._snapshot_fakes import FakeClient, FakeInstanceNode, service  # noqa: E402

HOST = "nuc1"


@pytest.fixture(autouse=True)
def _clean_breaker():
    # The breaker is process-wide; an unreachable read here must not trip the next test's host.
    for host in (HOST, "pandora"):
        health.clear(host)
    yield
    for host in (HOST, "pandora"):
        health.clear(host)


def _op(op_id, status, code, description="Starting instance", name="web-01", project=None):
    url = f"/1.0/instances/{name}" + (f"?project={project}" if project else "")
    return {
        "id": op_id,
        "class": "task",
        "description": description,
        "created_at": "2026-09-19T10:00:00Z",
        "updated_at": "2026-09-19T10:00:05Z",
        "status": status,
        "status_code": code,
        "resources": {"instances": [url]},
        "metadata": {"fds": {"0": "never-stored"}, "download_progress": "rootfs: 10%"},
        "may_cancel": False,
        "err": "boom" if status == "Failure" else "",
        "location": "nuc1",
    }


def _buckets():
    return {
        "running": [_op("op-run", "Running", 103, project="cust-a")],
        "pending": [_op("op-pend", "Pending", 105)],
        "success": [_op("op-ok", "Success", 200)],
        "failure": [_op("op-bad", "Failure", 400)],
        "cancelled": [_op("op-cxl", "Cancelled", 401)],
    }


def _upserts(maker):
    return [
        (sql, params) for db in maker.sessions for sql, params in db.calls
        if "INSERT INTO task_journal" in sql or "UPDATE task_journal" in sql
    ]


async def test_every_bucket_is_read_once_with_recursion_all_projects_and_the_socket_timeout():
    node = FakeInstanceNode()
    node.operation_buckets = _buckets()
    client = FakeClient(node)
    maker = FakeSessionMaker()

    count = await seed.reconcile_cluster(service(client, hosts=(HOST,)), HOST, maker)

    assert count == 5
    reads = [r for r in node.recorded if r[0] == "list_operations"]
    assert reads == [("list_operations", {"recursion": "1", "all-projects": "true"}, (5, 30))]
    ids = {p["operation_id"] for _, p in _upserts(maker) if p and "operation_id" in p}
    assert ids == {"op-run", "op-pend", "op-ok", "op-bad", "op-cxl"}


async def test_the_seed_carries_status_project_and_error_and_never_the_inner_metadata():
    node = FakeInstanceNode()
    node.operation_buckets = _buckets()
    maker = FakeSessionMaker()

    await seed.reconcile_cluster(service(FakeClient(node), hosts=(HOST,)), HOST, maker)

    by_id = {p["operation_id"]: p for _, p in _upserts(maker) if p and "operation_id" in p}
    assert by_id["op-run"]["new_status"] == "running"
    assert by_id["op-run"]["project"] == "cust-a", "the project comes out of the resource URL"
    assert by_id["op-run"]["resource_name"] == "web-01", "and the name comes out clean"
    assert by_id["op-pend"]["new_status"] == "pending"
    assert by_id["op-ok"]["new_status"] == "success"
    assert by_id["op-bad"]["new_status"] == "failure"
    assert by_id["op-bad"]["error"] == "boom"
    assert by_id["op-cxl"]["new_status"] == "cancelled"
    assert by_id["op-run"]["progress"] == "rootfs: 10%"
    everything = " ".join(str(p) for _, p in _upserts(maker))
    assert "never-stored" not in everything and "fds" not in everything


async def test_vanished_open_rows_are_marked_lost_with_the_seen_ids():
    node = FakeInstanceNode()
    node.operation_buckets = _buckets()
    maker = FakeSessionMaker()

    await seed.reconcile_cluster(service(FakeClient(node), hosts=(HOST,)), HOST, maker)

    lost = [(sql, p) for db in maker.sessions for sql, p in db.calls if "SET status = 'lost'" in sql]
    assert len(lost) == 1
    sql, params = lost[0]
    assert "operation_id <> ALL(CAST(:seen AS text[]))" in sql
    assert set(params["seen"]) == {"op-run", "op-pend", "op-ok", "op-bad", "op-cxl"}
    assert params["cluster_id"] == HOST
    assert params["older_than_s"] == 120.0
    assert maker.sessions[-1].commits == 1


async def test_a_daemon_that_refuses_all_projects_is_walked_project_by_project():
    node = FakeInstanceNode()
    node.projects = ["default", "cust-a"]
    node.operation_buckets_by_project = {
        "default": {"running": [_op("op-d", "Running", 103)]},
        "cust-a": {"success": [_op("op-a", "Success", 200, project="cust-a")]},
    }

    class _Refused(Exception):
        class response:  # noqa: N801 - the shape pylxd's LXDAPIException carries
            status_code = 400

    node.operations_all_projects_error = _Refused()
    maker = FakeSessionMaker()

    count = await seed.reconcile_cluster(service(FakeClient(node), hosts=(HOST,)), HOST, maker)

    assert count == 2
    reads = [r for r in node.recorded if r[0] == "list_operations"]
    assert reads[0][1] == {"recursion": "1", "all-projects": "true"}
    assert [r[1] for r in reads[1:]] == [
        {"recursion": "1", "project": "default"}, {"recursion": "1", "project": "cust-a"},
    ]
    assert all(r[2] == (5, 30) for r in reads)
    assert ("list_project_names", {"recursion": "0"}) in node.recorded
    ids = {p["operation_id"] for _, p in _upserts(maker) if p and "operation_id" in p}
    assert ids == {"op-d", "op-a"}


async def test_a_partial_walk_marks_nothing_lost_and_still_writes_connected():
    """One project refused mid-walk: the rows it holds were not seen, so nothing may be flipped
    to lost this pass (a review finding), and the REST path answering still records the
    cluster connected."""
    node = FakeInstanceNode()
    node.projects = ["default", "cust-a"]
    node.operation_buckets_by_project = {"default": {"running": [_op("op-d", "Running", 103)]}}
    node.operations_project_errors = {"cust-a": RuntimeError("timed out")}

    class _Refused(Exception):
        class response:  # noqa: N801
            status_code = 400

    node.operations_all_projects_error = _Refused()
    maker = FakeSessionMaker()

    count = await seed.reconcile_cluster(service(FakeClient(node), hosts=(HOST,)), HOST, maker)

    assert count == 1
    lost = [sql for db in maker.sessions for sql, _ in db.calls if "SET status = 'lost'" in sql]
    assert lost == []
    states = [p for db in maker.sessions for sql, p in db.calls if "INSERT INTO task_follower_clusters" in sql]
    assert states and states[-1]["state"] == "connected"


async def test_any_other_refusal_propagates_rather_than_walking():
    node = FakeInstanceNode()

    class _Down(Exception):
        class response:  # noqa: N801
            status_code = 503

    node.operations_all_projects_error = _Down()
    with pytest.raises(_Down):
        await seed.reconcile_cluster(service(FakeClient(node), hosts=(HOST,)), HOST, FakeSessionMaker())


async def test_an_unreachable_cluster_is_recorded_disconnected_and_nothing_is_marked_lost():
    node = FakeInstanceNode()
    node.operation_buckets = _buckets()
    maker = FakeSessionMaker()
    svc = service(FakeClient(node), hosts=(HOST,), clients={})

    assert await seed.reconcile_cluster(svc, HOST, maker) is None

    statements = maker.statements
    assert any("INSERT INTO task_follower_clusters" in sql for sql in statements)
    params = [p for db in maker.sessions for sql, p in db.calls if "task_follower_clusters" in sql]
    assert params[0]["state"] == "disconnected"
    assert params[0]["error"].startswith(f"Could not connect to {HOST}")
    assert not any("SET status = 'lost'" in sql for sql in statements)


async def test_seed_stamps_the_cluster_seeded_after_reconciling():
    node = FakeInstanceNode()
    node.operation_buckets = {"running": [_op("op-run", "Running", 103)]}
    maker = FakeSessionMaker()

    assert await seed.seed_cluster(service(FakeClient(node), hosts=(HOST,)), HOST, maker) == 1

    stamps = [p for db in maker.sessions for sql, p in db.calls if "task_follower_clusters" in sql]
    assert stamps[-1] == {"cluster_id": HOST, "state": "connected", "error": None, "seeded": True}


async def test_the_reconcile_sweep_skips_a_tripped_cluster(monkeypatch):
    calls = []

    async def fake_reconcile(lxd_service, host, session_maker, identities=None):
        calls.append(host)
        return 0

    monkeypatch.setattr(seed, "reconcile_cluster", fake_reconcile)
    follower = follower_module.JournalFollower(
        FakeSessionMaker(), lambda: None, retry_seconds=15, database_url="x",
        hub=FakeHub(), engine_factory=lambda url: None, identities=NoIdentities(), excluded=frozenset(),
    )
    follower._cluster_state = {"nuc1": "connected", "pandora": "connected", "dead": "disconnected"}
    health.record_failure("pandora", "refused")
    try:
        await follower.run_sweep("reconcile")
    finally:
        health.clear("pandora")

    assert calls == ["nuc1"], "connected and not tripped is the only shape that is read"


def test_the_reads_run_off_the_loop_on_a_bounded_pool():
    import inspect

    source = inspect.getsource(seed._read)
    assert "run_blocking(" in source
    assert "functools.partial(node.get, params=params, timeout=READ_TIMEOUT)" in source
    assert "executor=_JOURNAL_EXECUTOR" in source and "max_in_flight=_MAX_IN_FLIGHT" in source
    assert seed.READ_TIMEOUT == (5, 30) and seed.WALL_TIMEOUT_S == 35.0
    assert seed._JOURNAL_EXECUTOR._max_workers == 4 and seed._MAX_IN_FLIGHT == 4
    assert seed.BUCKETS == ("running", "pending", "success", "failure", "cancelled")
    assert "client_for(lxd_service, host" in inspect.getsource(seed.reconcile_cluster)
    _ = LockStore  # the shared fakes module is importable beside the snapshot fakes


async def test_the_warnings_sweep_writes_one_row_per_warning_and_last_seen():
    node = FakeInstanceNode()
    node.warnings = [
        {
            "uuid": "9c1d", "type": "Couldn't find the CGroup network priority controller",
            "status": "new", "severity": "low", "location": "nuc1", "project": "",
            "entity_url": "", "count": 3, "first_seen_at": "2026-09-19T09:00:00Z",
            "last_seen_at": "2026-09-19T10:00:00Z", "last_message": "cgroup missing",
        },
        {
            "uuid": "7f2a", "type": "Instance type not operational", "status": "acknowledged",
            "severity": "high", "location": "nuc1", "project": "cust-a",
            "entity_url": "/1.0/instances/web-01?project=cust-a", "count": 1,
            "first_seen_at": "2026-09-19T09:30:00Z", "last_seen_at": "2026-09-19T09:30:00Z",
            "last_message": "kvm unavailable",
        },
        {"uuid": "", "type": "no uuid"},
    ]

    def respond(sql, params):
        if "INSERT INTO cluster_log" in sql:
            return FakeResult(rowcount=len(params))
        return None

    maker = FakeSessionMaker(respond)

    inserted = await seed.sweep_warnings(service(FakeClient(node), hosts=(HOST,)), HOST, maker)

    assert inserted == 2
    reads = [r for r in node.recorded if r[0] == "list_warnings"]
    assert reads == [("list_warnings", {"recursion": "1", "all-projects": "true"}, (5, 30))]
    sql, rows = next((s, p) for db in maker.sessions for s, p in db.calls if "INSERT INTO cluster_log" in s)
    assert "ON CONFLICT (event_id)" in sql
    assert [r["event_id"] for r in rows] == [
        "warning:9c1d:2026-09-19T10:00:00Z", "warning:7f2a:2026-09-19T09:30:00Z",
    ]
    assert [r["severity"] for r in rows] == ["info", "error"]
    assert [r["kind"] for r in rows] == ["warning", "warning"]
    assert rows[0]["action"] == "warning.couldn-t-find-the-cgroup-network-priority-controller"
    assert rows[0]["message"] == "cgroup missing"
    assert rows[1]["resource_type"] == "instance" and rows[1]["resource_name"] == "web-01"
    assert rows[1]["project"] == "cust-a" and rows[1]["cluster_id"] == HOST
    assert seed.SEVERITY_FROM_LXD == {"low": "info", "moderate": "warning", "high": "error"}
