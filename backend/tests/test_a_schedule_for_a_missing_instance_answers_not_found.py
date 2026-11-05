"""A schedule asked for an instance the cluster does not have answers 404, not 500.

Live on 2026-11-05 a partner creating a schedule for a name that does not exist got a 500: the
instance read raised LXD's not-found and nothing mapped it. The instance snapshot routes already map
the same read through lxd_http_error; the schedule create now does too.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.models.snapshot_schedule_schemas import SnapshotScheduleCreateRequest
from app.routers import snapshot_schedules
from app.services.snapshots import schedule_runner


def _create(monkeypatch, failure: Exception) -> HTTPException:
    async def owner(*_a, **_k):
        return 6

    @asynccontextmanager
    async def no_guard(**_k):
        yield

    async def client(*_a, **_k):
        return object()

    async def read(*_a, **_k):
        raise failure

    monkeypatch.setattr(snapshot_schedules.svc, "assert_actor_can_schedule_target", owner)
    monkeypatch.setattr(snapshot_schedules, "_guard_schedule_mutation", no_guard)
    monkeypatch.setattr(snapshot_schedules, "_client", client)
    monkeypatch.setattr(schedule_runner, "_blocking", read)
    body = SnapshotScheduleCreateRequest(host="c1", project="p", instance_name="nope", label="nightly",
                                         frequency="daily", hour_utc=3, minute_utc=0, keep_last=3, expiry_days=7)
    user = AuthenticatedUser(user_id="p", email="p@example.test", groups=["role_partner"], roles=["role_partner"])
    token = org_scope._allowed_clusters_var.set(None)
    try:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(snapshot_schedules.create_schedule(body=body, _=None, user=user, db=object(), lxd_service=object()))
    finally:
        org_scope._allowed_clusters_var.reset(token)
    return caught.value


def test_a_missing_instance_is_not_found(monkeypatch) -> None:
    assert _create(monkeypatch, Exception("Instance not found")).status_code == 404


def test_an_unreachable_cluster_is_unavailable(monkeypatch) -> None:
    assert _create(monkeypatch, Exception("Max retries exceeded: connection refused")).status_code == 503
