"""A task an API key ran is attributed to the key, and never to a person who was not there.

An integration request carries no user; its request context has a null actor and the audit
record names the key in `details.api_key_id`, the public half of the credential. The journal
row says `api_key` and names that key, with `actor_email` empty: an operator's address on an
action their integration took would misattribute it.
"""

from app.services.task_journal.sink_mapping import record_to_log_row, record_to_task, source_for
from tests._journal_fakes import audit_record


def _api_key_record(action="integration.instance.delete", **overrides):
    return audit_record(
        action, user_id=None, role=None, details={"api_key_id": "ak_public_1", "force": True},
        **overrides,
    )


def test_the_task_names_the_key():
    task = record_to_task(_api_key_record())
    assert task is not None
    assert task.actor.actor_kind == "api_key"
    assert task.actor.source == "integration"
    assert task.actor.api_key_id == "ak_public_1"
    assert task.actor.actor_email is None
    assert task.actor.actor_role is None
    assert task.kind == "dashboard_action" and task.key.resource_type == "instance"


def test_the_log_line_names_the_key():
    row = record_to_log_row(_api_key_record())
    assert row is not None
    assert row["actor_kind"] == "api_key"
    assert row["api_key_id"] == "ak_public_1"
    assert row["actor_email"] is None
    assert row["details"]["api_key_id"] == "ak_public_1", "the public id is kept in details too"


def test_a_key_id_in_details_makes_any_action_an_api_key_action():
    """The integration router is not the only path a key can take; the id is the signal."""
    task = record_to_task(audit_record("instance.start", user_id=None, details={"api_key_id": "ak_2"}))
    assert task is not None
    assert task.actor.actor_kind == "api_key"
    assert task.actor.source == "dashboard"


def test_an_mcp_tool_call_is_a_log_line_from_the_mcp_source_and_not_a_task():
    record = _api_key_record("integration.mcp.tool_call", target_type="tool", target_id="list_instances")
    row = record_to_log_row(record)
    assert row is not None
    assert row["actor_kind"] == "api_key"
    assert source_for(record) == "mcp"
    assert record_to_task(record) is None


def test_a_person_is_named_by_email_only():
    task = record_to_task(audit_record("instance.start", user_id="ops@example.test", role="partner"))
    assert task is not None
    assert task.actor.actor_kind == "user"
    assert task.actor.actor_email == "ops@example.test"
    assert task.actor.actor_role == "partner"
    assert task.actor.api_key_id is None
    opaque = record_to_task(audit_record("instance.start", user_id="subject-without-at"))
    assert opaque is not None
    assert opaque.actor.actor_kind == "user"
    assert opaque.actor.actor_email is None, "an opaque subject is not an email"


def test_a_scheduled_run_names_the_schedule_owner_from_the_schedule_source():
    record = audit_record(
        "instance.snapshot.schedule.run", user_id="owner@example.test", role="scheduler",
        actor_extra={"schedule_id": 7, "automated": True, "actor_source": "schedule_owner"},
    )
    task = record_to_task(record)
    assert task is not None
    assert task.kind == "schedule"
    assert task.actor.source == "schedule"
    assert task.actor.actor_email == "owner@example.test"
    assert task.extra == {"schedule_id": 7}
