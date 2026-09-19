"""Every action the task panel emits is registered, and cancelling is alertable.

`log_activity` falls through to `EventCategory.LXD_INFRA` and `MEDIUM` for an action it does
not know, silently, so an unregistered action is a record filed under a category nobody chose.
The other direction matters as much: a registered `task.*` action nothing emits is a control
wired to nothing, which this codebase has shipped before.

The cancel route and the follower are built beside this file; a source that is not on disk yet
is skipped by name rather than invented, and the orphan direction waits for all of them.
"""

import ast
from pathlib import Path

import pytest

from app.services.activity_logger import ACTION_CATEGORIES, HIGH_SEVERITY_ACTIONS

APP = Path(__file__).resolve().parents[1] / "app"
JOURNAL = APP / "services" / "task_journal"
# The named ones are where the emitters of task.cancel, task.journal.prune and the two
# follower actions live; the glob picks up whatever else the package holds.
SOURCES = sorted({
    APP / "routers" / "tasks.py",
    APP / "routers" / "image_uploads_dashboard.py",
    APP / "routers" / "iso_uploads_dashboard.py",
    JOURNAL / "follower.py",
    JOURNAL / "prune.py",
    *JOURNAL.glob("*.py"),
})
PREFIXES = ("task.", "image.upload.", "storage.iso.upload.")


def _present():
    return [path for path in SOURCES if path.exists()]


def _emitted_actions():
    found = set()
    for path in _present():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith(PREFIXES):
                    found.add(node.value)
    return found


def test_the_upload_routers_emit_their_two_records():
    emitted = _emitted_actions()
    assert "image.upload.import" in emitted
    assert "storage.iso.upload.import" in emitted


def test_every_action_this_feature_emits_is_registered():
    emitted = _emitted_actions()
    assert emitted, "no task actions were found, so this test is checking nothing"
    missing = sorted(a for a in emitted if a not in ACTION_CATEGORIES)
    assert not missing, (
        f"{missing} would be filed under LXD_INFRA/MEDIUM by accident rather than by decision."
    )


def test_cancelling_a_task_is_high_severity():
    """It ends work someone else started."""
    assert "task.cancel" in HIGH_SEVERITY_ACTIONS


def test_no_registered_task_action_is_emitted_by_nothing():
    absent = [path.name for path in SOURCES if not path.exists()]
    if absent:
        pytest.skip(f"orphan check waits for {absent}; the emitters live there")
    registered = {a for a in ACTION_CATEGORIES if a.startswith(PREFIXES)}
    emitted = _emitted_actions()
    orphans = sorted(registered - emitted)
    assert not orphans, f"{orphans} are registered but nothing emits them."
