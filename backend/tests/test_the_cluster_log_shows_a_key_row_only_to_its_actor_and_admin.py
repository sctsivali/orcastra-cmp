"""A My Keys audit row reaches an Administrator and the person who acted, nobody else.

The rows carry `cluster_id=<the key's node>` and `resource_name=<key id>`, so the cluster and
project scope let every partner on a shared cluster read another partner's key ids from the
Cluster Log. A key id was the one thing standing between a partner and another owner's PFX
download, so the log handed out the missing half of that exploit. The rule exists twice (the
SQL and its Python twin), and both are held to the same answer here.
"""

import contextvars
import os
from typing import Any, Callable, Dict, List, Optional

import pytest
from sqlalchemy import create_engine, literal_column, select, text

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.core import org_scope  # noqa: E402
from app.core import task_scope  # noqa: E402
from app.models.task_journal_models import ClusterLog  # noqa: E402

ME = "partner-a@example.test"
OTHER = "partner-b@example.test"

ROWS: List[Dict[str, Any]] = [
    {"id": "key-mine", "cluster_id": "nuc1", "project": "default", "resource_type": "ssh_key", "resource_name": "key_a", "actor_email": ME},
    {"id": "key-mine-cased", "cluster_id": "nuc1", "project": "default", "resource_type": "ssh_key", "resource_name": "key_c", "actor_email": ME.upper()},
    {"id": "key-theirs", "cluster_id": "nuc1", "project": "default", "resource_type": "ssh_key", "resource_name": "key_b", "actor_email": OTHER},
    {"id": "cleanup-theirs", "cluster_id": None, "project": None, "resource_type": "ssh_key_cleanup", "resource_name": None, "actor_email": OTHER},
    {"id": "instance-row", "cluster_id": "nuc1", "project": "default", "resource_type": "instance", "resource_name": "web-01", "actor_email": OTHER},
]
COLUMNS = ("id", "cluster_id", "project", "resource_type", "resource_name", "actor_email")


@pytest.fixture(scope="module")
def engine():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(f"CREATE TABLE cluster_log ({', '.join(c + ' TEXT' for c in COLUMNS)})"))
        conn.execute(
            text(f"INSERT INTO cluster_log ({', '.join(COLUMNS)}) VALUES ({', '.join(':' + c for c in COLUMNS)})"),
            ROWS,
        )
    return engine


CONTEXTS = {
    "admin": None,
    "partner": {"nuc1"},
}


def _under(name: str, fn: Callable[[], Any]) -> Any:
    def run():
        org_scope._allowed_clusters_var.set(CONTEXTS[name])
        org_scope._tenant_acl_var.set(None)
        org_scope._visible_projects_var.set(None)
        return fn()

    return contextvars.copy_context().run(run)


def _sql_ids(engine, caller: Optional[str]) -> set:
    conditions = task_scope.log_conditions(ClusterLog, caller_email=caller)
    stmt = select(literal_column("id")).select_from(ClusterLog.__table__).where(*conditions)
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(stmt).all()}


def _row_ids(caller: Optional[str]) -> set:
    return {r["id"] for r in ROWS if task_scope.log_is_visible(r, caller_email=caller)}


def test_an_administrator_reads_every_key_row(engine) -> None:
    assert _under("admin", lambda: _sql_ids(engine, ME)) == {r["id"] for r in ROWS}


def test_a_partner_reads_only_the_key_rows_they_acted_in(engine) -> None:
    by_sql = _under("partner", lambda: _sql_ids(engine, ME))
    assert {"key-mine", "key-mine-cased"} <= by_sql
    assert "key-theirs" not in by_sql
    assert "cleanup-theirs" not in by_sql
    # Ordinary cluster rows keep the cluster scope they always had.
    assert "instance-row" in by_sql


@pytest.mark.parametrize("name", sorted(CONTEXTS))
@pytest.mark.parametrize("caller", [ME, OTHER, None])
def test_the_sql_and_its_python_twin_agree(engine, name: str, caller: Optional[str]) -> None:
    by_sql = _under(name, lambda: _sql_ids(engine, caller))
    by_row = _under(name, lambda: _row_ids(caller))
    assert by_sql == by_row, f"{name}/{caller}: {by_sql ^ by_row}"


def test_a_caller_without_an_email_sees_no_key_row(engine) -> None:
    assert not {"key-mine", "key-theirs"} & _under("partner", lambda: _sql_ids(engine, None))
