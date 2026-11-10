"""Orcastra owns its own schedules and still never writes the four LXD config keys.

`snapshots.schedule` is the single key `_policy_from_config` derives `externally_managed` from.
Writing it would make an instance report itself as owned by the metered product outside this
console: its detail tab would say so, the operator's own snapshot actions on it would be refused
behind a billing acknowledgement, and each override would write a high severity audit row. LXD
also allows exactly one `snapshots.schedule` per instance, so the two products cannot share one.

So the refusal has two halves: nothing in this feature writes those keys (structural), and an
instance that acquires an upstream schedule AFTER ours was created is refused at fire time, which
a creation-time check cannot cover.
"""

import ast
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from app.core.lxd_names import POLICY_CONFIG_KEYS
from app.services.snapshots import schedule_service as svc
from app.services.snapshots.schedule_runner import run_one
from tests._schedule_fakes import FakeDB, FakeSchedule, ops_rows
from tests._snapshot_fakes import FakeClient, FakeInstanceNode, service

UTC = timezone.utc
SLOT = datetime(2026, 9, 9, 3, 0, tzinfo=UTC)

APP = Path(__file__).resolve().parents[1] / "app"
FEATURE = [
    APP / "services" / "snapshots",
    APP / "routers" / "snapshot_schedules.py",
    APP / "models" / "snapshot_schedule_models.py",
    APP / "models" / "snapshot_schedule_schemas.py",
]


def _sources():
    for target in FEATURE:
        if target.is_dir():
            yield from sorted(target.rglob("*.py"))
        else:
            yield target


def test_no_module_in_this_feature_writes_a_policy_key():
    """Structural: a comment promising never to write these keys enforces nothing."""
    offenders = []
    checked = 0
    for path in _sources():
        checked += 1
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in POLICY_CONFIG_KEYS:
                    offenders.append(f"{path.relative_to(APP.parent)}:{node.lineno} {node.value!r}")
    assert checked >= 6, "the scan found almost nothing, so it is not actually checking anything"
    assert not offenders, (
        "This feature must never name an LXD snapshot-policy key: writing one makes the instance "
        "report itself as owned by the metered product upstream.\n  " + "\n  ".join(offenders)
    )


def test_the_scan_would_notice_a_write():
    """Ablation: the scan flags a literal policy key."""
    tree = ast.parse('cfg = {"snapshots.schedule": "@daily"}')
    hits = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and n.value in POLICY_CONFIG_KEYS
    ]
    assert hits == ["snapshots.schedule"]


def test_a_schedule_pauses_when_an_upstream_one_appears_later():
    node = FakeInstanceNode(
        config={"snapshots.schedule": "@hourly", "snapshots.expiry": "3d"},
        snapshots=ops_rows(12, 1),
    )
    outcome = asyncio.run(
        run_one(
            FakeDB(),
            FakeSchedule(due_slot=SLOT),
            now=SLOT,
            lxd_service=service(FakeClient(node)),
        )
    )
    assert outcome.result == "refused"
    assert outcome.refusal.code == "upstream_schedule_appeared"
    assert outcome.terminal is True, "a schedule that must not run was left active"
    assert "@hourly" in outcome.refusal.message
    assert not any(e[0] == "create_snapshot" for e in node.recorded)


def test_an_unmanaged_instance_is_left_alone_by_that_check():
    """The control: without it the refusal above could be firing on every instance."""
    node = FakeInstanceNode(config={"limits.cpu": "2"}, snapshots=ops_rows(12, 1))
    outcome = asyncio.run(
        run_one(
            FakeDB(), FakeSchedule(due_slot=SLOT), now=SLOT, lxd_service=service(FakeClient(node))
        )
    )
    assert outcome.result == "success", outcome.error


def test_creating_a_schedule_on_a_managed_instance_is_a_conflict_not_a_gate():
    """409 with no acknowledgement field.

    Other refusals in this family name an acknowledgement, and the console offers whatever a
    refusal names. This is a conflict in the target's state that no acknowledgement can resolve,
    so it must not read as a gate.
    """
    conflict = svc.upstream_conflict(
        "nuc2", "default", "prod-db-01", {"externally_managed": True, "schedule": "@hourly", "expiry": "3d"}
    )
    assert conflict is not None
    assert conflict.status_code == 409
    body = conflict.detail
    assert body["error"] == "upstream_schedule_owns_this_instance"
    assert body["schedule"] == "@hourly"
    assert "acknowledge_field" not in body
    assert "acknowledge" not in body["message"].lower()
    assert body["remediation"]


def test_an_unmanaged_instance_creates_no_conflict():
    assert svc.upstream_conflict("nuc2", "default", "ct-x", {"externally_managed": False}) is None
