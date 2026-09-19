"""What the journal stores is read by every tenant the scope lets in; secrets never get there.

The audit logger redacts values under credential-shaped names but keeps the names. The journal
is stricter: the name goes too, and the keys that carry command lines, file descriptors and
before/after snapshots are dropped outright, because a stored `command` for an exec is a
credential the moment someone pastes a token onto a command line. Error text is passed through
the sanitiser so a cluster URL or a filesystem path never reaches a row either.
"""

import json

import pytest

from app.services.task_journal.repository import insert_action, insert_log_rows
from app.services.task_journal.sink_mapping import record_to_log_row, record_to_task, scrub
from tests._journal_fakes import FakeDB, audit_record

FORBIDDEN = (
    "hunter2", "tok_secret_value", "/dev/pts/3", "rm -rf /data", "old-config-value",
    "new-config-value", "client-secret-value", "SECRET_ENV_VALUE", "https://10.0.0.5:8443",
    "/var/lib/orcastra/private",
)


def _record():
    record = audit_record(
        "instance.start",
        result="failure",
        error_message="connect to https://10.0.0.5:8443 failed, see /var/lib/orcastra/private/log",
        details={
            "token": "tok_secret_value",
            "password": "hunter2",
            "fds": {"0": "/dev/pts/3"},
            "command": ["rm -rf /data"],
            "environment": {"API": "SECRET_ENV_VALUE"},
            "before": {"config": "old-config-value"},
            "after": {"config": "new-config-value"},
            "nested": {"client_secret": "client-secret-value", "keep": "visible"},
            "api_key_id": "ak_public",
            "operation_id": "op-1",
        },
    )
    record["metadata"] = {
        "duration_ms": 12.0,
        "before": {"config": "old-config-value"},
        "after": {"config": "new-config-value"},
    }
    return record


def _assert_clean(blob: str) -> None:
    for needle in FORBIDDEN:
        assert needle not in blob, needle
    for key in ("token", "password", "fds", "command", "environment", "before", "after", "client_secret"):
        assert f'"{key}"' not in blob, key


def test_the_log_row_carries_none_of_it():
    row = record_to_log_row(_record())
    assert row is not None
    _assert_clean(json.dumps(row, default=str))
    assert row["details"]["api_key_id"] == "ak_public", "the public id survives the scrub"
    assert row["details"]["nested"] == {"keep": "visible"}
    assert "[redacted]" in row["message"], "the failure reason is kept, its URL and path are not"


def test_the_task_write_carries_none_of_it():
    task = record_to_task(_record())
    assert task is not None
    _assert_clean(json.dumps(task.__dict__, default=str))
    assert task.audit_error is not None and "[redacted]" in task.audit_error


@pytest.mark.asyncio
async def test_the_compiled_parameters_carry_none_of_it():
    """Two layers: the mapping and the repository's own sanitiser on every error and message."""
    db = FakeDB()
    row = record_to_log_row(_record())
    task = record_to_task(_record())
    await insert_log_rows(db, [row])
    await insert_action(
        db, kind=task.kind, key=task.key, actor=task.actor, result=task.result,
        duration_ms=task.duration_ms, description=task.description,
        audit_error="raw https://10.0.0.5:8443 /var/lib/orcastra/private", extra=task.extra,
    )
    for _sql, params in db.calls:
        _assert_clean(json.dumps(params, default=str))


def test_the_scrub_is_recursive_and_keeps_public_identifiers():
    scrubbed = scrub({
        "api_key_id": "ak_1",
        "list": [{"token": "x", "name": "ok"}],
        "Authorization": "Bearer abc",
        "cookie_jar": "c=1",
        "plain": 1,
    })
    assert scrubbed == {"api_key_id": "ak_1", "list": [{"name": "ok"}], "plain": 1}


def test_the_metadata_allowlist_holds_for_task_extra():
    task = record_to_task(audit_record("instance.rebuild", details={
        "job_id": "job-1", "mode": "image", "source": "ubuntu", "cloud_init": "#cloud-config\npassword: x",
    }))
    assert task is not None
    assert task.extra == {"job_id": "job-1"}, "only allowlisted keys reach metadata"
