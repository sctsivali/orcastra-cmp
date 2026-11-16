"""Who may start a rebuild, and how often.

A rebuild is a full root disk write plus an image pull, so three limits hold on the server, read from
the job table that records every rebuild: one rebuild in flight per instance for anyone; for a Tenant,
a rolling hourly budget per instance and a cap on rebuilds running at once across its organization's
projects on that cluster. Partners and Administrators run their own hosts and keep only the first.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.models.db_models import RebuildJob
from app.services import rebuild_admission as admission
from tests._sqlite_session import sqlite_session, with_json_tables

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
HOST, PROJECT, NAME = "cluster-a", "cust-a", "vm1"


def _db():
    return with_json_tables(sqlite_session(), RebuildJob)


def _job(db, *, name=NAME, project=PROJECT, host=HOST, status="ready", age_minutes=10, updated_minutes=None, job_id=None):
    created = NOW - timedelta(minutes=age_minutes)
    updated = NOW - timedelta(minutes=updated_minutes if updated_minutes is not None else age_minutes)
    db.add(RebuildJob(
        job_id=job_id or f"j-{name}-{project}-{age_minutes}-{status}",
        cluster_id=host, project=project, instance_name=name, mode="image",
        status=status, created_at=created, updated_at=updated,
    ))


async def _admit(db, *, tenant, projects=None, per_hour=3, concurrent=2, name=NAME):
    await admission.admit_rebuild(
        db, host=HOST, project=PROJECT, instance_name=name, tenant=tenant,
        organization_projects=projects, per_hour=per_hour, concurrent=concurrent, now=NOW,
    )


async def test_a_first_rebuild_is_admitted_for_every_role():
    for tenant in (True, False):
        await _admit(_db(), tenant=tenant)


@pytest.mark.parametrize("tenant", [True, False])
async def test_a_second_rebuild_while_one_runs_is_refused_with_the_running_job(tenant):
    db = _db()
    _job(db, status="rebuilding", age_minutes=2, job_id="running-1")
    await db.commit()
    with pytest.raises(HTTPException) as caught:
        await _admit(db, tenant=tenant)
    assert caught.value.status_code == 409
    assert caught.value.detail["job_id"] == "running-1"


async def test_a_stranded_job_does_not_block_a_new_rebuild():
    db = _db()
    _job(db, status="rebuilding", age_minutes=200, updated_minutes=180)
    await db.commit()
    await _admit(db, tenant=False)


async def test_a_tenant_gets_three_rebuilds_an_hour_then_a_wait_it_can_read():
    db = _db()
    for age in (50, 30, 5):
        _job(db, age_minutes=age)
    await db.commit()
    with pytest.raises(HTTPException) as caught:
        await _admit(db, tenant=True)
    assert caught.value.status_code == 429
    # The oldest of the three leaves the window in 10 minutes.
    assert caught.value.headers["Retry-After"] == "600"
    assert caught.value.detail["retry_after_seconds"] == 600
    assert "10 minutes" in caught.value.detail["message"]


async def test_rebuilds_older_than_an_hour_do_not_count():
    db = _db()
    for age in (70, 65, 61, 5):
        _job(db, age_minutes=age)
    await db.commit()
    await _admit(db, tenant=True)


async def test_the_budget_is_per_instance():
    db = _db()
    for age in (50, 30, 5):
        _job(db, name="vm2", age_minutes=age)
    await db.commit()
    await _admit(db, tenant=True)


async def test_a_partner_has_no_hourly_budget():
    db = _db()
    for age in (50, 40, 30, 20, 5):
        _job(db, age_minutes=age)
    await db.commit()
    await _admit(db, tenant=False)


async def test_a_tenant_runs_at_most_two_rebuilds_at_once_across_its_projects():
    db = _db()
    _job(db, name="vm2", status="rebuilding", age_minutes=3)
    _job(db, name="vm3", project="cust-a-2", status="starting", age_minutes=4)
    await db.commit()
    with pytest.raises(HTTPException) as caught:
        await _admit(db, tenant=True, projects={PROJECT, "cust-a-2"})
    assert caught.value.status_code == 429
    assert "running" in caught.value.detail["message"].lower()


async def test_another_organizations_rebuilds_do_not_count_against_the_cap():
    db = _db()
    _job(db, name="x1", project="cust-b", status="rebuilding", age_minutes=3)
    _job(db, name="x2", project="cust-b", status="rebuilding", age_minutes=3)
    await db.commit()
    await _admit(db, tenant=True, projects={PROJECT})


async def test_a_tenant_covering_the_whole_cluster_counts_every_project_on_it():
    db = _db()
    _job(db, name="vm2", project="p1", status="rebuilding", age_minutes=3)
    _job(db, name="vm3", project="p2", status="pending", age_minutes=1)
    await db.commit()
    with pytest.raises(HTTPException) as caught:
        await _admit(db, tenant=True, projects=None)
    assert caught.value.status_code == 429


@pytest.mark.parametrize("per_hour,concurrent", [(0, 2), (3, 0)])
async def test_a_zero_limit_turns_tenant_rebuild_off(per_hour, concurrent):
    with pytest.raises(HTTPException) as caught:
        await _admit(_db(), tenant=True, per_hour=per_hour, concurrent=concurrent)
    assert caught.value.status_code == 403
    assert "turned off" in caught.value.detail["message"]


async def test_a_zero_limit_leaves_partners_alone():
    await _admit(_db(), tenant=False, per_hour=0, concurrent=0)


def test_the_wait_reads_in_plain_words():
    assert admission.plain_wait(30) == "1 minute"
    assert admission.plain_wait(60) == "1 minute"
    assert admission.plain_wait(61) == "2 minutes"
    assert admission.plain_wait(3600) == "60 minutes"
