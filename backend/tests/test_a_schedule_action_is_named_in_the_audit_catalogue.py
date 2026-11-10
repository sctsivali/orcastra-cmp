"""Every action this feature emits is registered, and the destructive ones are alertable.

`log_activity` files an action it does not know under `EventCategory.LXD_INFRA` at `MEDIUM` with no
warning, so an unregistered action produces a record that looks fine, under a category and severity
no one chose.

The actor matters as much as the action. A background task has no RequestContext, and the fallback
writes an all-null actor with no warning, which loses all attribution on the product's only
unattended write path.
"""

import ast
from pathlib import Path

from app.core.request_context import get_request_context
from app.services.activity_logger import ACTION_CATEGORIES, HIGH_SEVERITY_ACTIONS
from tests._schedule_fakes import FakeSchedule

APP = Path(__file__).resolve().parents[1] / "app"
SOURCES = [
    APP / "services" / "snapshots" / "schedule_runner.py",
    APP / "services" / "snapshots" / "schedule_scheduler.py",
    APP / "routers" / "snapshot_schedules.py",
]

DESTRUCTIVE = {
    "instance.snapshot.schedule.delete",
    "instance.snapshot.schedule.pruned",
    "instance.snapshot.schedule.paused",
}


def _emitted_actions():
    """Every schedule action literal that appears in this feature's sources."""
    found = set()
    for path in SOURCES:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("instance.snapshot.schedule."):
                    found.add(node.value)
    return found


def test_every_action_this_feature_emits_is_registered():
    emitted = _emitted_actions()
    assert emitted, "no schedule actions were found, so this test is checking nothing"
    missing = sorted(a for a in emitted if a not in ACTION_CATEGORIES)
    assert not missing, (
        f"{missing} would be filed under LXD_INFRA/MEDIUM by accident rather than by decision."
    )


def test_the_destructive_actions_are_high_severity():
    """`.pruned` destroys a restore point, so it ranks with instance.snapshot.delete beside it.
    `.paused` and `.delete` are how an operator learns their backups stopped."""
    for action in sorted(DESTRUCTIVE):
        assert action in HIGH_SEVERITY_ACTIONS, action


def test_no_registered_schedule_action_is_emitted_by_nothing():
    """The reverse check: a registered action that nothing emits is a control wired to nothing."""
    registered = {a for a in ACTION_CATEGORIES if a.startswith("instance.snapshot.schedule.")}
    emitted = _emitted_actions()
    orphans = sorted(registered - emitted)
    assert not orphans, f"{orphans} are registered but nothing emits them."


def test_the_actor_is_the_operator_who_authorized_the_schedule():
    """The schedule's owner, never a synthetic system identity that hides who authorized it."""
    from app.services.snapshots.schedule_runner import adopt_schedule_context

    schedule = FakeSchedule(owner_email="partner@example.test", id=7)
    adopt_schedule_context(schedule)
    ctx = get_request_context()
    assert ctx is not None, "a scheduled action would land with an all-null actor"
    assert ctx.user_id == "partner@example.test"
    assert ctx.extra["automated"] is True
    assert ctx.extra["schedule_id"] == 7
    assert ctx.extra["actor_source"] == "schedule_owner"


def test_the_context_is_adopted_inside_the_run_rather_than_once_at_start_up():
    """A ContextVar written inside one asyncio Task does not reach another, and there is no reset
    helper, so this has to be overwrite-per-run rather than a set/clear pair at start-up."""
    source = (APP / "services" / "snapshots" / "schedule_runner.py").read_text()
    tree = ast.parse(source)
    run_one = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_one"
    )
    # The FIRST statement of the body, so nothing can be audited before the actor is known.
    body = [n for n in run_one.body if not isinstance(n, ast.Expr) or not isinstance(n.value, ast.Constant)]
    first = body[0]
    assert isinstance(first, ast.Expr), ast.dump(first)[:120]
    assert isinstance(first.value, ast.Call)
    assert first.value.func.id == "adopt_schedule_context"
