"""Orcastra owns its own schedules and still never writes the four LXD config keys.

`snapshots.schedule` is the single key `_policy_from_config` derives `externally_managed` from, and
eighteen consumers read that. Writing it would make an instance report itself as owned by the
metered product outside this console: its detail tab would say so, the operator's own snapshot
actions on it would be refused behind a billing acknowledgement, and each override would write a
high severity audit row. LXD also allows exactly one `snapshots.schedule` per instance, so the two
products can never share one even if that were acceptable.

So the refusal has two halves. A structural one, that nothing in this feature writes those keys at
all, and a behavioural one, that an instance which acquires an upstream schedule AFTER ours was
created is refused at fire time rather than only at creation. The second is the half a
creation-time check cannot cover, and it is the one that would otherwise run a second schedule
beside a customer's paid one for months.
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
    """Structural, because a comment saying "we never write these" is not a mechanism.

    No such test existed for any path before this one: `POLICY_KEYS` had zero test references.
    """
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
    """Ablation, inline. A ratchet nobody has seen fail is a ratchet nobody should trust."""
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
    """The control. Without it the refusal above could be firing on everything."""
    node = FakeInstanceNode(config={"limits.cpu": "2"}, snapshots=ops_rows(12, 1))
    outcome = asyncio.run(
        run_one(
            FakeDB(), FakeSchedule(due_slot=SLOT), now=SLOT, lxd_service=service(FakeClient(node))
        )
    )
    assert outcome.result == "success", outcome.error


def test_creating_a_schedule_on_a_managed_instance_is_a_conflict_not_a_gate():
    """409 with no acknowledgement field, deliberately.

    Every other refusal in this family carries one naming the way through, and the console is
    built to grow the affordance a refusal names. A 403 with no such field would read, to that
    console and to the next person writing against it, as a gate whose acknowledgement they failed
    to find. This is a conflict in the target's state, which no acknowledgement can resolve.
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
