"""The task journal is read under the instance list's rule, and the rule exists twice.

`app.core.task_scope` pushes the caller's scope into the WHERE clause so a page is filtered
before it is loaded, and judges every row again in Python after the load (and before a feed
frame is queued). Two copies of one rule drift, so this holds them equal over a corpus of 200
synthetic rows per tier: the set the SQL selects, the set `task_is_visible` accepts, and the
set the scope helpers themselves accept row by row.

The SQL is executed for real against an in-memory SQLite table carrying the journal's columns,
so an operator precedence slip or a NULL comparison that silently drops a row shows here
rather than on the first live page.
"""

import contextvars
import itertools
import os
import random
from typing import Any, Callable, Dict, List, Optional

import pytest
from sqlalchemy import create_engine, literal_column, select, text

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.core import org_scope  # noqa: E402
from app.core import task_scope  # noqa: E402
from app.core.tenant_visibility import tenant_acl_active, tenant_may_see_project  # noqa: E402
from app.models.task_journal_models import ClusterLog, TaskJournal  # noqa: E402

CLUSTERS = ("nuc1", "nuc2", "c3")
PROJECTS = ("default", "cust-a", "cust-b", "cust-c")
TYPES = ("instance", "project", "network", "image", "warning")
NAMES = ("web-01", "db-01", "cust-a", "cust-b", "br0", None)

COLUMNS = (
    "id", "seq", "kind", "cluster_id", "project", "resource_type", "resource_name",
    "operation_id", "status", "actor_email", "created_at", "updated_at",
)


def _corpus() -> List[Dict[str, Any]]:
    rng = random.Random(20260919)
    rows: List[Dict[str, Any]] = []
    combos = list(itertools.product(CLUSTERS, PROJECTS, TYPES, NAMES))
    rng.shuffle(combos)
    for index, (cluster, project, rtype, name) in enumerate(combos[:200]):
        rows.append(
            {
                "id": f"row-{index:03d}",
                "seq": index,
                "kind": "lxd_operation",
                "cluster_id": cluster,
                "project": project,
                "resource_type": rtype,
                "resource_name": name,
                "operation_id": f"op-{index}",
                "status": "running",
                "actor_email": "ops@example.test" if index % 3 else "other@example.test",
                "created_at": "2026-09-19T00:00:00+00:00",
                "updated_at": "2026-09-19T00:00:00+00:00",
            }
        )
    assert len(rows) == 200
    return rows


CORPUS = _corpus()


@pytest.fixture(scope="module")
def engine():
    engine = create_engine("sqlite://")
    columns = ", ".join(f"{c} TEXT" if c != "seq" else "seq INTEGER" for c in COLUMNS)
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE task_journal ({columns})"))
        conn.execute(text(f"CREATE TABLE cluster_log ({columns})"))
        for table in ("task_journal", "cluster_log"):
            conn.execute(
                text(
                    f"INSERT INTO {table} ({', '.join(COLUMNS)}) VALUES "
                    f"({', '.join(':' + c for c in COLUMNS)})"
                ),
                CORPUS,
            )
        # Host-less log rows: identity and settings changes, an access denial.
        conn.execute(
            text(
                "INSERT INTO cluster_log (id, seq, kind, cluster_id, project, resource_type, "
                "resource_name, status, actor_email) VALUES (:id, :seq, 'audit', NULL, NULL, "
                ":rtype, :name, 'success', :actor)"
            ),
            [
                {"id": "hostless-mine", "seq": 900, "rtype": "api_key", "name": "k1", "actor": "ops@example.test"},
                {"id": "hostless-theirs", "seq": 901, "rtype": "endpoint", "name": "/x", "actor": "other@example.test"},
            ],
        )
    return engine


# ---------------------------------------------------------------------------------------------
# contexts: (allowed clusters, tenant acl, partner project bound)
# ---------------------------------------------------------------------------------------------

CONTEXTS = {
    "admin": (None, None, None),
    "partner_bound": ({"nuc1", "nuc2"}, None, {"nuc2": {"default", "cust-a"}}),
    "partner_empty": (set(), None, None),
    "tenant_wildcard": ({"nuc1"}, {"nuc1": {("*", "*"): {"view"}}}, None),
    "tenant_instance": (
        {"nuc1", "nuc2"},
        {
            "nuc1": {("cust-a", "web-01"): {"view"}, ("cust-b", "*"): {"snapshot"}},
            "nuc2": {("*", "db-01"): {"view", "start"}},
        },
        None,
    ),
    "tenant_no_view": ({"nuc1"}, {"nuc1": {("*", "*"): {"start", "stop"}}}, None),
    "tenant_empty": (set(), {}, None),
}


def _under(name: str, fn: Callable[[], Any]) -> Any:
    allowed, acl, mapping = CONTEXTS[name]

    def run():
        org_scope._allowed_clusters_var.set(allowed)
        org_scope._tenant_acl_var.set(acl)
        org_scope._visible_projects_var.set(mapping)
        return fn()

    return contextvars.copy_context().run(run)


def _reference(row: Dict[str, Any]) -> bool:
    """The helpers themselves, called the way the instance list calls them for an instance row
    and the way the projects list calls them for a project row: a project create or delete runs
    in `default` but is about the project it names, so the bound reads the name."""
    allowed = org_scope.get_current_allowed_clusters()
    if allowed is not None and row["cluster_id"] not in allowed:
        return False
    bound_project = (row["resource_name"] or "default") if row["resource_type"] == "project" else row["project"]
    if not org_scope.project_is_visible(row["cluster_id"], bound_project):
        return False
    if not tenant_acl_active():
        return True
    if row["resource_type"] == "instance":
        if not row["resource_name"]:
            return False
        return org_scope.check_tenant_permission(
            row["cluster_id"], row["project"], row["resource_name"], "view"
        )
    if row["resource_type"] == "project":
        return tenant_may_see_project(row["cluster_id"], row["resource_name"])
    return False


def _sql_ids(engine, model, conditions: List[Any]) -> set:
    # note: `id` is a UUID column and the corpus uses plain labels, so the column is read as
    # bare text rather than through the UUID result processor.
    stmt = select(literal_column("id")).select_from(model.__table__).where(*conditions)
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(stmt).all()}


@pytest.mark.parametrize("name", sorted(CONTEXTS))
def test_sql_python_and_helpers_select_the_same_tasks(engine, name: str) -> None:
    by_sql = _under(name, lambda: _sql_ids(engine, TaskJournal, task_scope.task_conditions(TaskJournal)))
    by_row = _under(name, lambda: {r["id"] for r in CORPUS if task_scope.task_is_visible(r)})
    by_helpers = _under(name, lambda: {r["id"] for r in CORPUS if _reference(r)})
    assert by_sql == by_row, f"{name}: SQL and task_is_visible disagree on {by_sql ^ by_row}"
    assert by_row == by_helpers, f"{name}: task_is_visible and the helpers disagree on {by_row ^ by_helpers}"


def test_admin_sees_the_whole_corpus(engine) -> None:
    assert _under("admin", lambda: task_scope.task_conditions(TaskJournal)) == []
    assert len(_under("admin", lambda: _sql_ids(engine, TaskJournal, []))) == 200


def test_the_partner_bound_drops_the_other_organisations_projects(engine) -> None:
    ids = _under("partner_bound", lambda: _sql_ids(engine, TaskJournal, task_scope.task_conditions(TaskJournal)))
    rows = {r["id"]: r for r in CORPUS}
    for row_id in ids:
        row = rows[row_id]
        assert row["cluster_id"] in ("nuc1", "nuc2")
        if row["cluster_id"] == "nuc2":
            judged = (row["resource_name"] or "default") if row["resource_type"] == "project" else row["project"]
            assert judged in ("default", "cust-a"), row
    # nuc1 carries no bound, so every project passes there.
    assert any(rows[i]["cluster_id"] == "nuc1" and rows[i]["project"] == "cust-c" for i in ids)
    # A project row is judged by the project it names: the Partner's own `cust-a` create (run
    # in default) is theirs, another organisation's `cust-b` create (also run in default) is not.
    own = [r for r in CORPUS if r["cluster_id"] == "nuc2" and r["resource_type"] == "project" and r["resource_name"] == "cust-a" and r["project"] == "default"]
    foreign = [r for r in CORPUS if r["cluster_id"] == "nuc2" and r["resource_type"] == "project" and r["resource_name"] == "cust-b" and r["project"] == "default"]
    assert own and foreign, "the corpus holds both shapes"
    assert all(r["id"] in ids for r in own)
    assert not any(r["id"] in ids for r in foreign)


@pytest.mark.parametrize("name", ["tenant_wildcard", "tenant_instance", "tenant_no_view", "tenant_empty"])
def test_a_tenant_never_receives_a_network_image_or_warning_row(engine, name: str) -> None:
    ids = _under(name, lambda: _sql_ids(engine, TaskJournal, task_scope.task_conditions(TaskJournal)))
    rows = {r["id"]: r for r in CORPUS}
    for row_id in ids:
        assert rows[row_id]["resource_type"] in ("instance", "project"), rows[row_id]
        assert rows[row_id]["resource_type"] != "instance" or rows[row_id]["resource_name"]


def test_a_tenant_with_an_instance_rule_sees_that_instance_and_the_default_project(engine) -> None:
    ids = _under("tenant_instance", lambda: _sql_ids(engine, TaskJournal, task_scope.task_conditions(TaskJournal)))
    rows = {r["id"]: r for r in CORPUS}
    seen = {(r["cluster_id"], r["project"], r["resource_type"], r["resource_name"]) for r in (rows[i] for i in ids)}
    assert ("nuc1", "cust-a", "instance", "web-01") in seen
    assert ("nuc1", "cust-a", "instance", "db-01") not in seen
    # The snapshot-only rule on cust-b grants no view.
    assert not any(c == "nuc1" and p == "cust-b" and t == "instance" for c, p, t, _n in seen)
    # A project row for the rule's project, and `default` unconditionally.
    assert ("nuc1", "default", "project", "cust-a") in seen
    default_row = {"cluster_id": "nuc1", "project": "default", "resource_type": "project", "resource_name": None}
    assert _under("tenant_instance", lambda: task_scope.task_is_visible(default_row)) is True
    # The instance wildcard on nuc2 names db-01 in every project.
    assert ("nuc2", "cust-c", "instance", "db-01") in seen
    assert ("nuc2", "cust-c", "instance", "web-01") not in seen


@pytest.mark.parametrize("name", ["tenant_no_view", "tenant_empty", "partner_empty"])
def test_no_grant_means_no_rows(engine, name: str) -> None:
    assert _under(name, lambda: _sql_ids(engine, TaskJournal, task_scope.task_conditions(TaskJournal))) == set()
    assert _under(name, lambda: [r for r in CORPUS if task_scope.task_is_visible(r)]) == []


def _log_reference(row: Dict[str, Any], caller: Optional[str]) -> bool:
    if org_scope.get_current_allowed_clusters() is None:
        return True
    if row["cluster_id"] is None:
        return bool(caller) and row["actor_email"] == caller
    return _reference(row)


def _log_rows(engine) -> List[Dict[str, Any]]:
    with engine.connect() as conn:
        result = conn.execute(
            select(literal_column("id"), literal_column("cluster_id"), literal_column("actor_email"))
            .select_from(ClusterLog.__table__)
        )
        found = {r[0]: (r[1], r[2]) for r in result.all()}
    rows = [dict(r) for r in CORPUS]
    for row_id in ("hostless-mine", "hostless-theirs"):
        rows.append(
            {
                "id": row_id, "cluster_id": None, "project": None, "resource_type": "api_key",
                "resource_name": "k1", "actor_email": found[row_id][1],
            }
        )
    return rows


@pytest.mark.parametrize("name", sorted(CONTEXTS))
@pytest.mark.parametrize("caller", ["ops@example.test", None])
def test_the_log_rule_matches_its_python_twin(engine, name: str, caller: Optional[str]) -> None:
    rows = _log_rows(engine)
    by_sql = _under(
        name,
        lambda: _sql_ids(engine, ClusterLog, task_scope.log_conditions(ClusterLog, caller_email=caller)),
    )
    by_row = _under(name, lambda: {r["id"] for r in rows if task_scope.log_is_visible(r, caller_email=caller)})
    by_ref = _under(name, lambda: {r["id"] for r in rows if _log_reference(r, caller)})
    assert by_sql == by_row == by_ref, f"{name}/{caller}: {by_sql ^ by_row} {by_row ^ by_ref}"
    if name == "admin":
        assert {"hostless-mine", "hostless-theirs"} <= by_sql
    elif caller:
        assert "hostless-mine" in by_sql and "hostless-theirs" not in by_sql
    else:
        assert not ({"hostless-mine", "hostless-theirs"} & by_sql)


def test_caller_tier_reads_the_context() -> None:
    assert _under("admin", task_scope.caller_tier) == "admin"
    assert _under("partner_bound", task_scope.caller_tier) == "partner"
    assert _under("partner_empty", task_scope.caller_tier) == "partner"
    assert _under("tenant_wildcard", task_scope.caller_tier) == "tenant"
    assert _under("tenant_empty", task_scope.caller_tier) == "tenant"


def test_disconnected_clusters_are_bounded_to_the_caller() -> None:
    states = [
        {"cluster_id": "nuc1", "state": "disconnected"},
        {"cluster_id": "nuc2", "state": "connected"},
        {"cluster_id": "c3", "state": "disconnected"},
        {"cluster_id": "gone", "state": "unregistered"},
    ]
    assert _under("admin", lambda: task_scope.disconnected_for_caller(states)) == ["nuc1", "c3"]
    assert _under("partner_bound", lambda: task_scope.disconnected_for_caller(states)) == ["nuc1"]
    assert _under("tenant_empty", lambda: task_scope.disconnected_for_caller(states)) == []
