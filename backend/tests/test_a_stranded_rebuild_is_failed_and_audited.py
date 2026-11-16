"""A rebuild no worker can still drive is failed at startup, and the failure is audited.

A rebuild left in flight by a backend restart would block that instance for everyone (one rebuild
in flight per instance). The startup sweep fails jobs whose last update is older than the stranded
threshold, leaves younger in-flight jobs and finished jobs alone, and writes one failure audit row
per job it fails, so an operator reading the activity log learns the instance may hold a half
written root disk. This runs the sweep against real rows.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.models.db_models import RebuildJob
from app.routers import instance_rebuild
from app.services import rebuild_admission
from tests._sqlite_session import sqlite_session, with_json_tables


class _Maker:
    """async_session_maker's shape: a call that returns an async context manager."""

    def __init__(self, db):
        self.db = db

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *exc):
        return False


def _job(db, job_id, status, updated_ago):
    now = datetime.now(timezone.utc)
    db.add(RebuildJob(
        job_id=job_id, cluster_id="cluster-a", project="cust-a", instance_name=job_id,
        mode="image", status=status, created_at=now - updated_ago, updated_at=now - updated_ago,
    ))


@pytest.mark.asyncio
async def test_only_jobs_past_the_threshold_are_failed_and_each_one_is_audited(monkeypatch):
    from app.core import database

    db = with_json_tables(sqlite_session(), RebuildJob)
    past = rebuild_admission.STRANDED_AFTER + timedelta(minutes=5)
    _job(db, "stranded-rebuilding", "rebuilding", rebuild_admission.REBUILD_OP_WINDOW + timedelta(minutes=1))
    _job(db, "stranded-pending", "pending", past)
    _job(db, "young-rebuilding", "rebuilding", timedelta(seconds=20))
    # LXD may still be running the rebuild a silent `rebuilding` row names, until its wait is over.
    _job(db, "rebuilding-within-lxd-wait", "rebuilding", past)
    _job(db, "old-ready", "ready", past)
    await db.commit()

    audited = []
    monkeypatch.setattr(database, "async_session_maker", _Maker(db))
    monkeypatch.setattr(instance_rebuild.activity_logger, "log_activity", lambda **kw: audited.append(kw))

    assert await instance_rebuild.recover_stranded_rebuilds() == 2

    db.sync.expire_all()
    status = {row.job_id: row for row in db.sync.query(RebuildJob).all()}
    assert status["stranded-rebuilding"].status == "failed"
    assert status["stranded-pending"].status == "failed"
    assert "partially written root disk" in status["stranded-rebuilding"].error
    assert status["young-rebuilding"].status == "rebuilding"
    assert status["rebuilding-within-lxd-wait"].status == "rebuilding"
    assert status["old-ready"].status == "ready"

    assert sorted(a["details"]["job_id"] for a in audited) == ["stranded-pending", "stranded-rebuilding"]
    for entry in audited:
        assert entry["action"] == "instance.rebuild"
        assert entry["result"] == "failure"
        assert entry["details"]["stranded"] is True
        assert entry["target_host"] == "cluster-a" and entry["target_project"] == "cust-a"
        assert entry["error_message"] == status[entry["details"]["job_id"]].error


@pytest.mark.asyncio
async def test_a_second_sweep_finds_nothing_left_to_fail(monkeypatch):
    from app.core import database

    db = with_json_tables(sqlite_session(), RebuildJob)
    _job(db, "stranded", "starting", rebuild_admission.STRANDED_AFTER + timedelta(minutes=1))
    await db.commit()
    audited = []
    monkeypatch.setattr(database, "async_session_maker", _Maker(db))
    monkeypatch.setattr(instance_rebuild.activity_logger, "log_activity", lambda **kw: audited.append(kw))

    assert await instance_rebuild.recover_stranded_rebuilds() == 1
    assert await instance_rebuild.recover_stranded_rebuilds() == 0
    assert len(audited) == 1
