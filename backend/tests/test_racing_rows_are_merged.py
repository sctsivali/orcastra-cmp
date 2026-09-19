"""The residual race leaves two rows for one click; the sweep folds them into one.

The by-key claim from either side covers every ordering except the one where the audit record
and the frame commit inside the same instant, each seeing no row to claim. That pair is
tolerated for up to a minute and then merged: the LXD row keeps the operation and takes the
actor, the dashboard row's log lines move over, and the dashboard row goes. Under
`FOR UPDATE ... SKIP LOCKED`, so a claim in flight on either row is left alone.
"""

import pytest

from app.services.task_journal.repository import merge_racing_pairs
from tests._journal_fakes import FakeDB, FakeResult

DASH = "55555555-5555-5555-5555-555555555555"
LXD = "66666666-6666-6666-6666-666666666666"
OTHER_LXD = "77777777-7777-7777-7777-777777777777"


def _with_pairs(pairs):
    return FakeDB(lambda sql, p: FakeResult(rows=pairs) if sql.startswith("SELECT d.id") else None)


@pytest.mark.asyncio
async def test_the_pair_query_names_both_shapes_and_skips_locked_rows():
    db = _with_pairs([])
    assert await merge_racing_pairs(db, window_s=900) == 0
    assert len(db.statements) == 1
    sql = db.statements[0]
    for clause in (
        "d.kind = 'dashboard_action' AND d.operation_id IS NULL",
        "l.kind = 'lxd_operation' AND l.operation_id IS NOT NULL",
        "l.actor_email IS NULL AND l.api_key_id IS NULL",
        "l.cluster_id = d.cluster_id AND l.project = d.project",
        "l.resource_type = d.resource_type AND l.resource_name = d.resource_name",
        "l.action = d.action",
        "FOR UPDATE OF d, l SKIP LOCKED",
    ):
        assert clause in sql, clause
    assert db.calls[0][1]["window_s"] == 900.0


@pytest.mark.asyncio
async def test_a_pair_is_merged_actor_onto_the_lxd_row_and_the_dashboard_row_removed():
    db = _with_pairs([(DASH, LXD)])
    assert await merge_racing_pairs(db) == 1
    statements = db.statements
    assert len(statements) == 4
    actor_copy, log_move, delete = statements[1], statements[2], statements[3]
    assert actor_copy.startswith("UPDATE task_journal AS l SET actor_email = d.actor_email")
    for column in ("actor_role", "actor_kind", "api_key_id", "source"):
        assert f"{column} = d.{column}" in actor_copy
    assert "metadata = d.metadata || l.metadata" in actor_copy
    assert "seq = nextval('task_journal_seq')" in actor_copy
    assert "WHERE l.id = CAST(:lxd AS uuid) AND d.id = CAST(:dash AS uuid)" in actor_copy
    assert log_move == (
        "UPDATE cluster_log SET task_id = CAST(:lxd AS uuid) WHERE task_id = CAST(:dash AS uuid)"
    )
    assert delete == "DELETE FROM task_journal WHERE id = CAST(:dash AS uuid)"
    for _sql, params in db.calls[1:]:
        assert params == {"dash": DASH, "lxd": LXD}


@pytest.mark.asyncio
async def test_a_dashboard_row_that_matches_two_stream_rows_is_merged_once():
    db = _with_pairs([(DASH, LXD), (DASH, OTHER_LXD)])
    assert await merge_racing_pairs(db) == 1
    deletes = [sql for sql in db.statements if sql.startswith("DELETE")]
    assert len(deletes) == 1, "deleting the dashboard row twice would be a no-op; merging twice would not"
    merged_into = [p["lxd"] for sql, p in db.calls if sql.startswith("UPDATE task_journal AS l")]
    assert merged_into == [LXD], "the first pair, in created_at order, wins"
