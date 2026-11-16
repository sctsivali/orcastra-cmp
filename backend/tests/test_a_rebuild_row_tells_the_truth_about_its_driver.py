"""A rebuild job row says whether a worker is still driving it.

One rebuild in flight per instance is the rule for every role, so a row left mid-flight by a worker
that is gone would refuse every retry on that instance, and Migration Center reads it as active
work too. Three things keep the row honest:

- the worker driving a rebuild refreshes the row on a heartbeat while LXD works, so a live rebuild
  never looks stranded however long the image pull takes;
- a backend that stops while a rebuild runs (a deploy, an upgrade, a restart) fails the row on its
  way down, naming what to check on the instance, unless LXD may still be running the rebuild it
  accepted: then the row keeps blocking until LXD's own wait is over;
- a row nobody refreshed for its window no longer blocks a new rebuild;
- a row the sweep already failed is never reopened by a worker that comes back.

An instance LXD reports in Error answers the stop with "already stopped"; the rebuild goes on, as
it did before the stop step learned to fail a job.
"""

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone

import pytest

from app.models.db_models import RebuildJob
from app.routers import instance_rebuild
from app.services import rebuild_admission as admission
from app.services.lxd_service import LXDOperationError
from tests._sqlite_session import sqlite_session, with_json_tables

HOST, PROJECT, NAME, JOB = "cluster-a", "cust-a", "vm1", "job-1"


class _Maker:
    def __init__(self, db):
        self.db = db

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *exc):
        return False


class _Lxd:
    """The LXD calls the driver makes; the rebuild waits until the test lets it finish."""

    def __init__(self, stop_error=None, hold=None):
        self.stop_error = stop_error
        self.hold = hold  # "stop" or "start": that call waits for `release` too
        self.release = asyncio.Event()
        self.rebuilding = asyncio.Event()
        self.reached = asyncio.Event()
        self.calls = []

    async def stop_vm(self, name, host, force=False, project=None):
        self.calls.append("stop")
        if self.hold == "stop":
            self.reached.set()
            await self.release.wait()
        if self.stop_error:
            raise self.stop_error

    async def rebuild_instance(self, name, host, source, project=None, cloud_init=None):
        self.calls.append("rebuild")
        self.rebuilding.set()
        await self.release.wait()
        return "op-1"

    async def start_vm(self, name, host, project=None):
        self.calls.append("start")
        if self.hold == "start":
            self.reached.set()
            await asyncio.Event().wait()


@contextlib.asynccontextmanager
async def _no_fence(**_kw):
    yield


def _world(monkeypatch, lxd, heartbeat=0.05):
    db = with_json_tables(sqlite_session(), RebuildJob)
    long_ago = datetime.now(timezone.utc) - timedelta(minutes=30)
    db.add(RebuildJob(
        job_id=JOB, cluster_id=HOST, project=PROJECT, instance_name=NAME, mode="image",
        status="pending", created_at=long_ago, updated_at=long_ago,
    ))
    audited = []
    monkeypatch.setattr(instance_rebuild, "async_session_maker", _Maker(db))
    monkeypatch.setattr(instance_rebuild, "get_lxd_service", lambda: lxd)
    monkeypatch.setattr(instance_rebuild, "guard_instance_mutation", _no_fence)
    monkeypatch.setattr(instance_rebuild.activity_logger, "log_activity", lambda **kw: audited.append(kw))
    monkeypatch.setattr(instance_rebuild, "_HEARTBEAT_SECONDS", heartbeat)
    return db, audited


def _row(db):
    db.sync.expire_all()
    return db.sync.query(RebuildJob).filter_by(job_id=JOB).one()


def _aware(moment):
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_a_running_rebuild_refreshes_its_row_until_it_ends(monkeypatch):
    lxd = _Lxd()
    db, audited = _world(monkeypatch, lxd)
    await db.commit()
    task = asyncio.create_task(instance_rebuild._run_rebuild(JOB, False))
    await asyncio.wait_for(lxd.rebuilding.wait(), 5)
    first = _aware(_row(db).updated_at)
    await asyncio.sleep(0.3)
    row = _row(db)
    assert row.status == "rebuilding"
    assert _aware(row.updated_at) > first, "the heartbeat moved the row while LXD worked"
    assert admission._live(row, datetime.now(timezone.utc))

    lxd.release.set()
    await asyncio.wait_for(task, 5)
    done = _row(db)
    assert done.status == "ready"
    settled = _aware(done.updated_at)
    await asyncio.sleep(0.3)
    assert _aware(_row(db).updated_at) == settled, "no heartbeat after the job ended"
    assert [a["result"] for a in audited] == ["success"]


async def _admit(db, now=None):
    await admission.admit_rebuild(
        db, host=HOST, project=PROJECT, instance_name=NAME, tenant=False,
        organization_projects=None, per_hour=3, concurrent=2, now=now or datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("hold,text", [
    ("stop", "before the root disk was rebuilt"),
    ("start", "after the root disk was rebuilt"),
])
async def test_a_backend_that_stops_outside_the_daemon_rebuild_frees_the_instance_at_once(monkeypatch, hold, text):
    lxd = _Lxd(hold=hold)
    if hold == "start":
        lxd.release.set()
    db, audited = _world(monkeypatch, lxd)
    await db.commit()
    task = asyncio.create_task(instance_rebuild._run_rebuild(JOB, False))
    await asyncio.wait_for(lxd.reached.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = _row(db)
    assert row.status == "failed"
    assert text in row.error
    assert [a["result"] for a in audited] == ["failure"]
    assert audited[0]["details"]["interrupted"] is True
    await _admit(db)  # the row no longer blocks a retry


@pytest.mark.asyncio
async def test_a_backend_that_stops_while_lxd_rebuilds_keeps_the_instance_blocked_until_lxd_is_done(monkeypatch):
    lxd = _Lxd()
    db, audited = _world(monkeypatch, lxd)
    await db.commit()
    task = asyncio.create_task(instance_rebuild._run_rebuild(JOB, False))
    await asyncio.wait_for(lxd.rebuilding.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = _row(db)
    # LXD may still be running the rebuild it accepted: neither a second rebuild nor the sweep
    # may act until its wait is over.
    assert row.status == "rebuilding"
    assert audited == []
    with pytest.raises(Exception) as caught:
        await _admit(db)
    assert getattr(caught.value, "status_code", None) == 409
    silent = _aware(row.updated_at) + admission.STRANDED_AFTER + timedelta(minutes=1)
    with pytest.raises(Exception):
        await _admit(db, now=silent)
    over = _aware(row.updated_at) + admission.REBUILD_OP_WINDOW + timedelta(minutes=1)
    await _admit(db, now=over)


@pytest.mark.asyncio
async def test_an_accepted_rebuild_whose_result_is_unknown_keeps_the_instance_blocked(monkeypatch):
    from app.services.lxd.exceptions import LXDRebuildOutcomeUnknown

    lxd = _Lxd()

    async def wait_broke(name, host, source, project=None, cloud_init=None):
        lxd.calls.append("rebuild")
        raise LXDRebuildOutcomeUnknown("The result of LXD operation op-9 could not be read", "op-9")

    lxd.rebuild_instance = wait_broke
    db, audited = _world(monkeypatch, lxd)
    await db.commit()
    await asyncio.wait_for(instance_rebuild._run_rebuild(JOB, False), 5)
    row = _row(db)
    assert row.status == "rebuilding", "LXD may still be rewriting the disk"
    assert row.operation_id == "op-9" and "could not be read" in row.error
    assert lxd.calls == ["stop", "rebuild"] and audited == []
    with pytest.raises(Exception) as caught:
        await _admit(db)
    assert getattr(caught.value, "status_code", None) == 409
    await _admit(db, now=_aware(row.updated_at) + admission.REBUILD_OP_WINDOW + timedelta(minutes=1))


@pytest.mark.asyncio
async def test_a_row_the_sweep_failed_is_not_reopened_by_a_worker_that_comes_back(monkeypatch):
    lxd = _Lxd()
    db, audited = _world(monkeypatch, lxd)
    await db.commit()
    task = asyncio.create_task(instance_rebuild._run_rebuild(JOB, False))
    await asyncio.wait_for(lxd.rebuilding.wait(), 5)
    # Another worker's sweep claims the row while this one is cut off from the database.
    row = _row(db)
    row.status, row.error = "failed", instance_rebuild._STRANDED_ERROR
    db.sync.commit()
    lxd.release.set()
    await asyncio.wait_for(task, 5)
    row = _row(db)
    assert row.status == "failed" and row.error == instance_rebuild._STRANDED_ERROR
    assert lxd.calls == ["stop", "rebuild"], "a settled job starts nothing more"
    assert audited == []


@pytest.mark.asyncio
async def test_an_instance_in_error_that_reads_already_stopped_is_rebuilt(monkeypatch):
    lxd = _Lxd(stop_error=LXDOperationError("Could not stop VM: The instance is already stopped"))
    lxd.release.set()
    db, audited = _world(monkeypatch, lxd)
    await db.commit()
    await asyncio.wait_for(instance_rebuild._run_rebuild(JOB, False), 5)
    assert _row(db).status == "ready"
    assert lxd.calls == ["stop", "rebuild", "start"]


@pytest.mark.asyncio
async def test_any_other_stop_failure_still_fails_the_job_before_the_disk_is_touched(monkeypatch):
    lxd = _Lxd(stop_error=LXDOperationError("Could not stop VM: Failed to stop the instance"))
    lxd.release.set()
    db, audited = _world(monkeypatch, lxd)
    await db.commit()
    await asyncio.wait_for(instance_rebuild._run_rebuild(JOB, False), 5)
    row = _row(db)
    assert row.status == "failed"
    assert "Could not stop the instance before the rebuild" in row.error
    assert lxd.calls == ["stop"]


def _admission_db(status, silent):
    db = with_json_tables(sqlite_session(), RebuildJob)
    now = datetime.now(timezone.utc)
    db.add(RebuildJob(
        job_id="other", cluster_id=HOST, project=PROJECT, instance_name=NAME, mode="image",
        status=status, created_at=now - silent - timedelta(minutes=1), updated_at=now - silent,
    ))
    return db, now


async def _blocks(status, silent):
    db, now = _admission_db(status, silent)
    await db.commit()
    try:
        await _admit(db, now=now)
    except Exception as caught:  # noqa: BLE001
        assert getattr(caught, "status_code", None) == 409
        return True
    return False


@pytest.mark.asyncio
async def test_each_row_blocks_for_its_own_window_and_no_longer():
    assert admission.STRANDED_AFTER <= timedelta(minutes=10), "a dead worker blocks retries for minutes, not hours"
    assert timedelta(seconds=instance_rebuild._HEARTBEAT_SECONDS) * 5 <= admission.STRANDED_AFTER
    assert admission.REBUILD_OP_WINDOW > timedelta(seconds=1800), "longer than LXD's own rebuild wait"
    minute = timedelta(minutes=1)

    for status in ("pending", "stopping", "starting", "provisioning"):
        assert await _blocks(status, minute), status
        assert not await _blocks(status, admission.STRANDED_AFTER + minute), status
    assert await _blocks("rebuilding", admission.STRANDED_AFTER + minute)
    assert not await _blocks("rebuilding", admission.REBUILD_OP_WINDOW + minute)
