"""Least disclosure below Admin, on every row and every frame.

`present.task_row` and `present.log_row` are the only path from a journal row to the wire,
REST and WebSocket alike, so the policy is asserted there once: organisation ids and the
requestor's address reach Admin only, the cluster member reaches Admin and Partner, and a
tenant's metadata and details are cut to the allowlist. What was never stored cannot be
presented; the "nothing secret is journaled" test covers the write side.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app.models.task_schemas import ClusterLogEntry, TaskRecord  # noqa: E402
from app.services.task_journal import present  # noqa: E402

NOW = datetime(2026, 9, 19, 2, 10, 11, tzinfo=timezone.utc)


def _task(**overrides):
    row = {
        "id": "8d1b2c1e-0000-4000-8000-000000000001",
        "seq": 42,
        "kind": "lxd_operation",
        "cluster_id": "nuc1",
        "project": "cust-a",
        "resource_type": "instance",
        "resource_name": "web-01",
        "operation_id": "op-1",
        "operation_class": "task",
        "action": "instance.start",
        "description": "Starting instance",
        "status": "running",
        "status_code": 103,
        "progress": "50%",
        "error": None,
        "may_cancel": True,
        "actor_email": "ops@example.test",
        "actor_role": "partner",
        "actor_kind": "user",
        "api_key_id": None,
        "source": "dashboard",
        "request_id": "req-1",
        "target_organization_id": 6,
        "target_organization_slug": "sivali",
        "metadata": {
            "requestor": {"username": "ops", "protocol": "tls", "address": "10.0.0.9:51234"},
            "location": "nuc1-member-2",
            "resources": {"instances": ["/1.0/instances/web-01"]},
            "job_id": "job-9",
            "schedule_id": 3,
            "snapshot": "snap0",
        },
        "lxd_created_at": NOW - timedelta(seconds=30),
        "lxd_updated_at": NOW - timedelta(seconds=10),
        "started_at": NOW - timedelta(seconds=30),
        "finished_at": None,
        "created_at": NOW - timedelta(seconds=31),
        "updated_at": NOW - timedelta(seconds=10),
    }
    row.update(overrides)
    return row


def _log(**overrides):
    row = {
        "id": 7,
        "ts": NOW,
        "kind": "audit",
        "severity": "info",
        "cluster_id": "nuc1",
        "project": "cust-a",
        "resource_type": "instance",
        "resource_name": "web-01",
        "action": "instance.create",
        "message": "Created instance web-01",
        "actor_email": "ops@example.test",
        "actor_role": "partner",
        "actor_kind": "user",
        "api_key_id": None,
        "result": "success",
        "request_id": "req-1",
        "event_id": "evt-1",
        "task_id": "8d1b2c1e-0000-4000-8000-000000000001",
        "target_organization_id": 6,
        "details": {
            "operation_id": "op-1",
            "image": "ubuntu/24.04",
            "instance_type": "container",
            "profiles": ["default"],
            "project": "cust-a",
            "automated": False,
            "schedule_id": 3,
            "snapshot": "snap0",
            "audit_error": "boom",
            "config_keys": ["limits.cpu"],
            "layer": "router",
        },
    }
    row.update(overrides)
    return row


def test_admin_sees_everything_that_is_stored() -> None:
    out = present.task_row(_task(), tier="admin", now=NOW)
    TaskRecord(**out)
    assert out["target_organization_id"] == 6
    assert out["target_organization_slug"] == "sivali"
    assert out["requestor"] == {"username": "ops", "protocol": "tls", "address": "10.0.0.9:51234"}
    assert out["location"] == "nuc1-member-2"
    assert out["metadata"] == {
        "resources": {"instances": ["/1.0/instances/web-01"]},
        "job_id": "job-9",
        "schedule_id": 3,
        "snapshot": "snap0",
    }
    assert out["duration_ms"] == 30_000


def test_a_partner_loses_the_organisation_and_the_requestor_address() -> None:
    out = present.task_row(_task(), tier="partner", now=NOW)
    TaskRecord(**out)
    assert out["target_organization_id"] is None
    assert out["target_organization_slug"] is None
    assert out["requestor"] == {"username": "ops", "protocol": "tls", "address": None}
    assert out["location"] == "nuc1-member-2"
    assert "job_id" in out["metadata"]
    # Every tier still sees who acted and through which key, as the session audit does.
    assert out["actor_email"] == "ops@example.test"
    assert out["operation_class"] == "task"


def test_a_tenant_also_loses_the_location_and_keeps_only_the_allowlisted_metadata() -> None:
    out = present.task_row(_task(), tier="tenant", now=NOW)
    TaskRecord(**out)
    assert out["target_organization_id"] is None
    assert out["requestor"]["address"] is None
    assert out["location"] is None
    assert out["metadata"] == {"schedule_id": 3, "snapshot": "snap0"}
    assert set(out["metadata"]) <= present.TENANT_DETAIL_KEYS


@pytest.mark.parametrize("tier", ["admin", "partner", "tenant"])
def test_the_requestor_and_location_never_leak_through_metadata(tier: str) -> None:
    out = present.task_row(_task(), tier=tier, now=NOW)
    assert "requestor" not in out["metadata"]
    assert "location" not in out["metadata"]
    assert "address" not in str(out["metadata"])


def test_a_row_without_a_requestor_presents_none() -> None:
    out = present.task_row(_task(metadata={}), tier="admin", now=NOW)
    assert out["requestor"] is None
    assert out["location"] is None
    assert out["metadata"] == {}


def test_log_details_follow_the_same_allowlist_for_a_tenant() -> None:
    admin = present.log_row(_log(), tier="admin")
    partner = present.log_row(_log(), tier="partner")
    tenant = present.log_row(_log(), tier="tenant")
    for out in (admin, partner, tenant):
        ClusterLogEntry(**out)
    assert admin["target_organization_id"] == 6
    assert partner["target_organization_id"] is None
    assert tenant["target_organization_id"] is None
    assert "audit_error" in admin["details"] and "config_keys" in partner["details"]
    assert set(tenant["details"]) == present.TENANT_DETAIL_KEYS
    assert "audit_error" not in tenant["details"]


def test_absent_details_stay_absent() -> None:
    assert present.log_row(_log(details=None), tier="tenant")["details"] is None


def test_duration_is_finished_minus_started_once_finished() -> None:
    row = _task(finished_at=NOW - timedelta(seconds=5))
    assert present.task_row(row, tier="admin", now=NOW)["duration_ms"] == 25_000
    never = _task(started_at=None)
    assert present.task_row(never, tier="admin", now=NOW)["duration_ms"] is None
