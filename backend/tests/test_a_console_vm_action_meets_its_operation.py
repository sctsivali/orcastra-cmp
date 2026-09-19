"""The console's own start/stop path audits `vm.*`; the daemon's frame maps to `instance.*`.

The key the audit claims with and the key the frame writes must be one key, or every console
start lists twice (the review's finding, confirmed twice). The alias table gives the task key
its canonical name; the stdout record and the cluster log line keep the name the router
emitted, so nothing downstream of Fluent Bit changes.
"""

from app.services.task_journal.follower_frames import operation_frame
from app.services.task_journal.repository_types import CorrelationKey
from app.services.task_journal.sink_mapping import record_to_log_row, record_to_task
from app.services.task_journal.vocabulary import TASK_ACTION_ALIASES, TASK_ACTIONS, canonical_task_action
from tests._journal_fakes import audit_record


def _vm_audit(action: str):
    record = audit_record(action, target_type="vm", target_id="web-01", host="nuc2", project="tenant-b")
    return record


def test_a_vm_start_audit_and_the_daemons_frame_build_the_same_key():
    task = record_to_task(_vm_audit("vm.start"))
    assert task is not None
    frame = operation_frame({
        "type": "operation", "host": "nuc2", "operation_id": "op-1", "class": "task",
        "description": "Starting instance", "status": "Running", "status_code": 103,
        "resources": {"instances": ["web-01"]}, "project": "tenant-b",
    })
    assert frame is not None
    assert task.key == CorrelationKey("nuc2", "tenant-b", "instance", "web-01", "instance.start")
    assert (frame.cluster_id, frame.project, frame.resource_type, frame.resource_name, frame.action) == (
        "nuc2", "tenant-b", "instance", "web-01", "instance.start",
    )


def test_every_alias_names_a_task_action_on_both_sides():
    for alias, canonical in TASK_ACTION_ALIASES.items():
        assert alias in TASK_ACTIONS, alias
        assert canonical in TASK_ACTIONS, canonical
        assert TASK_ACTIONS[alias][1] == TASK_ACTIONS[canonical][1], (alias, "resource type")
    assert canonical_task_action("instance.stop") == "instance.stop"


def test_the_log_line_keeps_the_action_the_router_emitted():
    row = record_to_log_row(_vm_audit("vm.stop"))
    assert row is not None and row["action"] == "vm.stop"
    task = record_to_task(_vm_audit("vm.stop"))
    assert task is not None and task.key.action == "instance.stop"
