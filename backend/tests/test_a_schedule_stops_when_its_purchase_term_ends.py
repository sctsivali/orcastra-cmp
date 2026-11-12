"""A snapshot schedule its owner reaches the cluster for only by purchase follows the paid term.

The worker rebuilds a schedule's authority from database rows on every run, and the rows still bind
the seller's cluster to the buyer organization through retention, so a schedule a founder or a
buyer member made while the term ran would keep writing snapshots on the seller's cluster after
it. Past the term the owner may only look (core/sale_term.py), so the schedule pauses with a
reason the owner can read; a renewed term lets it be resumed.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.snapshots import schedule_authority as authority
from tests._purchase_world import ACTIVE_TERM, ENDED_TERM, FOUNDER, RETENTION_TERM, purchase_world

# Taken at import, before the conftest seam replaces it for every test.
REAL_REFUSAL = authority.paid_term_refusal


def _schedule(cluster_id: str = "sold") -> SimpleNamespace:
    return SimpleNamespace(owner_email=FOUNDER, cluster_id=cluster_id, project="shop", state="active")


@pytest.mark.parametrize("world", [RETENTION_TERM, ENDED_TERM], ids=["retention", "ended"])
async def test_past_the_term_the_schedule_pauses(world) -> None:
    refusal = await REAL_REFUSAL(await purchase_world(**world), _schedule())
    assert refusal is not None and refusal.terminal
    assert refusal.code == authority.PURCHASE_TERM_PASSED
    assert "sold" in refusal.message


@pytest.mark.parametrize("world, cluster_id", [
    (ACTIVE_TERM, "sold"),
    ({**RETENTION_TERM, "registered_elsewhere": True}, "sold"),
    (ENDED_TERM, "own"),
], ids=["term-running", "also-registered-elsewhere", "own-cluster"])
async def test_otherwise_the_schedule_runs(world, cluster_id) -> None:
    assert await REAL_REFUSAL(await purchase_world(**world), _schedule(cluster_id)) is None


async def test_the_term_switch_off_lets_it_run(monkeypatch) -> None:
    monkeypatch.setattr(authority, "get_settings", lambda: SimpleNamespace(tenant_term_enforcement_enabled=False))
    assert await REAL_REFUSAL(await purchase_world(**ENDED_TERM), _schedule()) is None


@pytest.mark.parametrize("relative, function", [
    ("services/snapshots/schedule_runner.py", "_run_one_guarded"),
    ("routers/snapshot_schedules.py", "resume_schedule"),
])
def test_the_worker_and_resume_both_ask(relative, function) -> None:
    source = (Path(authority.__file__).resolve().parents[2] / relative).read_text()
    fn = next(n for n in ast.walk(ast.parse(source))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == function)
    called = {getattr(c.func, "attr", None) for c in ast.walk(fn) if isinstance(c, ast.Call)}
    assert "paid_term_refusal" in called
