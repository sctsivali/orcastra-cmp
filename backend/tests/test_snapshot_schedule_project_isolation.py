"""Snapshot schedules keep the same project boundary as interactive instance actions."""

import asyncio

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.models.snapshot_schedule_schemas import (
    SnapshotScheduleCreateRequest,
    SnapshotScheduleUpdateRequest,
)
from app.routers import snapshot_schedules
from app.services.snapshots import schedule_authority, schedule_service
from tests._schedule_fakes import FakeSchedule
from tests._snapshot_fakes import HOST, INSTANCE


class _NoIO:
    def __init__(self):
        self.commits = 0
        self.deletes = 0

    async def execute(self, _statement):
        raise AssertionError("database I/O ran after a request-scope project denial")

    async def commit(self):
        self.commits += 1

    async def delete(self, _row):
        self.deletes += 1


class _NoLXD:
    def __getattr__(self, name):
        raise AssertionError(f"LXD I/O ran after a request-scope project denial: {name}")


class _Rows:
    def __init__(self, rows):
        self.rows = list(rows)

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None


class _TwoOrgAuthorityDB:
    """One actor belongs to both orgs, but only the second owns the named project."""

    async def execute(self, statement):
        sql = str(statement)
        if "tenant_policy_rules" in sql:
            assert "tenant_access_policies.organization_id IN" in sql
            return _Rows([(7,)])
        if "organization_clusters.organization_id, organization_clusters.id" in sql:
            return _Rows([(6, 1), (7, 2)])
        if "SELECT DISTINCT organizations.id" in sql:
            return _Rows([(6,), (7,)])
        raise AssertionError(sql)


def _partner():
    return AuthenticatedUser(
        user_id="partner-1",
        email="partner@example.test",
        groups=["role_partner"],
        roles=["role_partner"],
    )


def _foreign_scope(call):
    clusters = org_scope._allowed_clusters_var.set({HOST})
    projects = org_scope._visible_projects_var.set({HOST: {"default"}})
    try:
        return call()
    finally:
        org_scope._visible_projects_var.reset(projects)
        org_scope._allowed_clusters_var.reset(clusters)


def _create_body():
    return SnapshotScheduleCreateRequest(
        host=HOST,
        project="foreign-project",
        instance_name=INSTANCE,
        label="nightly",
        frequency="daily",
        hour_utc=3,
        minute_utc=0,
        keep_last=3,
        expiry_days=7,
    )


def test_create_refuses_a_foreign_project_before_database_or_lxd_io():
    db = _NoIO()

    async def invoke():
        return await snapshot_schedules.create_schedule(
            body=_create_body(),
            _=None,
            user=_partner(),
            db=db,
            lxd_service=_NoLXD(),
        )

    with pytest.raises(HTTPException) as denied:
        _foreign_scope(lambda: asyncio.run(invoke()))

    assert denied.value.status_code == 403
    assert db.commits == 0


def test_list_refuses_an_explicit_foreign_project_before_schedule_query(monkeypatch):
    called = []

    async def list_for_actor(*args, **kwargs):
        called.append((args, kwargs))
        return []

    monkeypatch.setattr(snapshot_schedules.svc, "list_for_actor", list_for_actor)

    async def invoke():
        return await snapshot_schedules.list_schedules(
            host=HOST,
            project="foreign-project",
            instance=None,
            user=_partner(),
            db=_NoIO(),
        )

    with pytest.raises(HTTPException) as denied:
        _foreign_scope(lambda: asyncio.run(invoke()))

    assert denied.value.status_code == 403
    assert called == []


def test_create_authority_selects_the_org_that_owns_the_project_not_the_first_binding():
    organization_id = asyncio.run(
        schedule_authority.authorizing_organization_id(
            _TwoOrgAuthorityDB(),
            actor_email="partner@example.test",
            cluster_id=HOST,
            project="org-b-project",
        )
    )

    assert organization_id == 7


def test_schedule_management_authorizes_the_current_colleague_not_the_original_owner(monkeypatch):
    schedule = FakeSchedule(owner_email="former@example.test")
    actors = []

    class ScheduleDB:
        async def execute(self, _statement):
            return _Rows([schedule])

    async def standing(*_args, **kwargs):
        actors.append(kwargs["actor_email"])
        return None

    monkeypatch.setattr(schedule_service.schedule_authority, "standing_authority_refusal", standing)

    row = asyncio.run(
        schedule_service.get_owned(ScheduleDB(), 12, "colleague@example.test")
    )

    assert row is schedule
    assert actors == ["colleague@example.test"]


async def _invoke_action(action, db, user):
    if action == "update":
        return await snapshot_schedules.update_schedule(
            body=SnapshotScheduleUpdateRequest(label="renamed"),
            schedule_id=12,
            _=None,
            user=user,
            db=db,
            lxd_service=_NoLXD(),
        )
    if action == "pause":
        return await snapshot_schedules.pause_schedule(12, None, user, db)
    if action == "resume":
        return await snapshot_schedules.resume_schedule(12, None, user, db, _NoLXD())
    if action == "delete":
        return await snapshot_schedules.delete_schedule(12, None, user, db)
    if action == "run-now":
        return await snapshot_schedules.run_schedule_now(12, None, user, db, _NoLXD())
    raise AssertionError(action)


@pytest.mark.parametrize("action", ["update", "pause", "resume", "delete", "run-now"])
def test_existing_schedule_actions_refuse_foreign_project_before_mutation_or_lxd(
    monkeypatch,
    action,
):
    schedule = FakeSchedule(project="foreign-project")

    async def get_owned(*_args, **_kwargs):
        return schedule

    monkeypatch.setattr(snapshot_schedules.svc, "get_owned", get_owned)
    db = _NoIO()

    with pytest.raises(HTTPException) as denied:
        _foreign_scope(lambda: asyncio.run(_invoke_action(action, db, _partner())))

    assert denied.value.status_code == 403
    assert db.commits == 0
    assert db.deletes == 0


def test_resume_requires_the_original_owner_even_when_a_colleague_can_manage_the_row(monkeypatch):
    schedule = FakeSchedule(project="default", owner_email="former@example.test")

    async def get_owned(*_args, **_kwargs):
        return schedule

    async def revoked(*_args, **_kwargs):
        return schedule_authority.Refusal(
            code="schedule_owner_left_the_organization",
            message="owner left",
            terminal=True,
        )

    monkeypatch.setattr(snapshot_schedules.svc, "get_owned", get_owned)
    monkeypatch.setattr(snapshot_schedules.schedule_authority, "standing_authority_refusal", revoked)
    db = _NoIO()

    with pytest.raises(HTTPException) as denied:
        asyncio.run(snapshot_schedules.resume_schedule(12, None, _partner(), db, _NoLXD()))

    assert denied.value.status_code == 409
    assert denied.value.detail["error"] == "schedule_owner_left_the_organization"
    assert db.commits == 0
