"""One click is one log line and one task, not two of each.

The router records `instance.start` and the service layer records `lxd.instance.start` for
the same click. The service twin also fires with no click at all (a rebuild's stop and start,
start-on-create), under whichever actor created the task, so journaling it would mint phantom
rows beside the operation rows the follower already writes. The twin is dropped; the router
record is the one QA asserts on. `lxd.connection.*` has no router twin and is the only record
of a cluster (re)connect, so it stays, as a log line with a system actor and never a task.
"""

import pytest

from app.services.task_journal.sink_mapping import record_to_log_row, record_to_task
from app.services.task_journal.vocabulary import SUPPRESSED_ACTIONS
from tests._journal_fakes import audit_record


@pytest.mark.parametrize("twin", sorted(a for a in SUPPRESSED_ACTIONS if a.startswith("lxd.")))
def test_a_service_layer_twin_maps_to_nothing(twin):
    record = audit_record(twin, user_id="ops@example.test")
    assert record_to_log_row(record) is None
    assert record_to_task(record) is None


def test_the_router_record_for_the_same_click_is_a_log_line_and_a_task():
    record = audit_record("instance.start")
    assert record_to_log_row(record) is not None
    task = record_to_task(record)
    assert task is not None and task.kind == "dashboard_action"
    assert task.key.action == "instance.start"


def test_a_cluster_connection_record_is_a_system_log_line_and_never_a_task():
    record = audit_record(
        "lxd.connection.establish", user_id=None, role=None, target_type="cluster",
        target_id="nuc1", host="nuc1", project=None,
    )
    row = record_to_log_row(record)
    assert row is not None
    assert row["kind"] == "audit"
    assert row["actor_kind"] == "system"
    assert row["actor_email"] is None
    assert row["cluster_id"] == "nuc1"
    assert row["resource_type"] == "cluster"
    assert record_to_task(record) is None


def test_the_audit_viewer_itself_is_not_logged():
    """Reading the log must not write the log, or every refresh of the panel is a line in it."""
    assert record_to_log_row(audit_record("audit.view", target_type="audit", target_id="page")) is None
