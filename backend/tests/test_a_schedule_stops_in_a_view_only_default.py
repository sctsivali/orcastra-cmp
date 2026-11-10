"""A snapshot schedule stops in a view-only `default` unless its owner is an Admin.

Without this, a schedule a partner created before the view-only `default` rule would keep taking
snapshots in another party's `default`. The worker carries no request scope, so the rule is judged
from the schedule's owner: the party from the database, the role from Authentik. An Admin's
schedule keeps running, an unanswered identity provider defers the run without counting a failure,
and the owner can still pause or delete the schedule, which change only the dashboard's own row.
"""

import ast
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services import cluster_parties
from app.services.snapshots import schedule_authority
from app.services.snapshots.schedule_authority import (
    DEFAULT_VIEW_ONLY_FOR_THE_OWNER,
    OWNER_ROLE_UNREADABLE,
    default_view_only_refusal,
)
from app.services.snapshots.schedule_runner import run_one
from tests._schedule_fakes import FakeDB, FakeSchedule, ops_rows
from tests._snapshot_fakes import HOST, FakeClient, FakeInstanceNode, service

SLOT = datetime(2026, 9, 9, 3, 0, tzinfo=timezone.utc)
REAL_RULE = default_view_only_refusal


@pytest.fixture
def held(monkeypatch):
    """The real rule, with the cluster held by another party and a settable owner role."""
    state = {"held": {HOST}, "admin": False, "party_reads": 0, "role_reads": 0}

    async def parties(_db, _email, cluster_ids):
        state["party_reads"] += 1
        return set(state["held"]) & set(cluster_ids)

    async def is_admin(_email, *, roles=None):
        state["role_reads"] += 1
        return state["admin"]

    monkeypatch.setattr(schedule_authority, "default_view_only_refusal", REAL_RULE)
    monkeypatch.setattr(cluster_parties, "default_view_only_clusters", parties)
    monkeypatch.setattr("app.services.creator_role.creator_is_admin", is_admin)
    return state


def _run(schedule):
    node = FakeInstanceNode(snapshots=ops_rows(schedule.id, 1))
    outcome = asyncio.run(run_one(FakeDB(), schedule, now=SLOT, lxd_service=service(FakeClient(node))))
    return outcome, node


def test_a_partner_schedule_in_a_held_default_stops_before_lxd(held) -> None:
    outcome, node = _run(FakeSchedule(project="default", due_slot=SLOT))
    assert outcome.result == "refused" and outcome.terminal is True
    assert outcome.refusal.code == DEFAULT_VIEW_ONLY_FOR_THE_OWNER
    assert node.recorded == [], "the other party's default was written after the rule"


def test_an_admin_schedule_in_a_held_default_keeps_running(held) -> None:
    held["admin"] = True
    outcome, node = _run(FakeSchedule(project="default", due_slot=SLOT))
    assert outcome.result == "success", outcome.error
    assert any(entry[0] == "create_snapshot" for entry in node.recorded)


def test_an_unanswered_idp_defers_without_counting_a_failure(held) -> None:
    held["admin"] = None
    outcome, node = _run(FakeSchedule(project="default", due_slot=SLOT))
    assert outcome.result == "deferred"
    assert outcome.counts_as_failure is False
    assert node.recorded == []


def test_a_default_no_other_party_holds_runs_without_asking_the_idp(held) -> None:
    held["held"] = set()
    outcome, _node = _run(FakeSchedule(project="default", due_slot=SLOT))
    assert outcome.result == "success", outcome.error
    assert held["role_reads"] == 0


def test_a_schedule_in_its_own_project_is_not_judged(held) -> None:
    outcome, _node = _run(FakeSchedule(due_slot=SLOT))
    assert outcome.result == "success", outcome.error
    assert held["party_reads"] == 0 and held["role_reads"] == 0


async def test_the_rule_reads_the_owner_not_a_request(held) -> None:
    refusal = await REAL_RULE(object(), FakeSchedule(project=" default "))
    assert refusal is not None and refusal.terminal and "view only for whoever owns this schedule" in refusal.message
    held["admin"] = None
    refusal = await REAL_RULE(object(), FakeSchedule(project="default"))
    assert refusal.code == OWNER_ROLE_UNREADABLE and refusal.terminal is False


# ── the router: pause and delete stay open, resume keeps the rule ───────────────────────────────

def _router_functions():
    source = (Path(schedule_authority.__file__).parents[2] / "routers" / "snapshot_schedules.py").read_text()
    return {n.name: n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef)}


def _calls(function) -> set:
    return {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
            for c in ast.walk(function) if isinstance(c, ast.Call)}


def test_pause_and_delete_withdraw_without_the_view_only_refusal() -> None:
    functions = _router_functions()
    for name in ("pause_schedule", "delete_schedule"):
        calls = _calls(functions[name])
        assert "_verify_project_for_withdrawal" in calls and "verify_project_access" not in calls, name


def test_update_resume_and_run_now_keep_the_view_only_refusal() -> None:
    functions = _router_functions()
    for name in ("update_schedule", "resume_schedule", "run_schedule_now"):
        assert "verify_project_access" in _calls(functions[name]), name
    assert "default_view_only_refusal" in _calls(functions["resume_schedule"])


def test_the_withdrawal_check_skips_only_the_default_refusal() -> None:
    from fastapi import HTTPException

    from app.core import org_scope
    from app.routers.snapshot_schedules import _verify_project_for_withdrawal
    from tests.test_default_is_view_only_where_another_party_holds_the_cluster import HELD, _partner

    with _partner("POST", "/api/v1/snapshot-schedules/12/pause"):
        assert _verify_project_for_withdrawal(HELD, "default") == "default"
        with pytest.raises(HTTPException):
            org_scope.verify_project_access(HELD, "default")
        with pytest.raises(HTTPException) as hidden:
            _verify_project_for_withdrawal(HELD, "theirs")
        assert hidden.value.status_code == 403
