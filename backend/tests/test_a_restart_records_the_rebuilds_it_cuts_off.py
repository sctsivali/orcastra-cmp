"""A backend that stops records the rebuilds it cuts off before its process ends.

uvicorn catches SIGTERM, shuts the app down, then raises the same signal again with the default
handler, so the process ends without cancelling the tasks still on its event loop. A rebuild left
to that teardown never reached its interruption handler: on a real restart its row stayed in flight
and refused every retry until the stranded sweep, minutes later. The app's shutdown cancels the
rebuilds this worker drives while the loop and the database are still there.
"""

import asyncio
import inspect
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from app import main
from app.routers import instance_rebuild
from tests.test_a_rebuild_row_tells_the_truth_about_its_driver import JOB, _Lxd, _admit, _row, _world

BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("hold,text", [
    ("stop", "before the root disk was rebuilt"),
    ("start", "after the root disk was rebuilt"),
])
async def test_a_shutdown_fails_the_rebuilds_it_cuts_off_outside_the_daemon_rebuild(monkeypatch, hold, text):
    lxd = _Lxd(hold=hold)
    if hold == "start":
        lxd.release.set()
    db, audited = _world(monkeypatch, lxd)
    await db.commit()
    task = instance_rebuild._spawn_rebuild(JOB, False)
    await asyncio.wait_for(lxd.reached.wait(), 5)

    assert await instance_rebuild.interrupt_running_rebuilds() == 1

    assert task.cancelled()
    row = _row(db)
    assert row.status == "failed"
    assert text in row.error
    assert [a["result"] for a in audited] == ["failure"]
    assert audited[0]["details"]["interrupted"] is True
    await _admit(db)  # the instance takes a new rebuild at once
    assert instance_rebuild._RUNNING_JOBS == set()


@pytest.mark.asyncio
async def test_a_shutdown_while_lxd_rebuilds_leaves_the_row_blocking(monkeypatch):
    lxd = _Lxd()
    db, audited = _world(monkeypatch, lxd)
    await db.commit()
    instance_rebuild._spawn_rebuild(JOB, False)
    await asyncio.wait_for(lxd.rebuilding.wait(), 5)

    assert await instance_rebuild.interrupt_running_rebuilds() == 1

    assert _row(db).status == "rebuilding"
    assert audited == []


@pytest.mark.asyncio
async def test_a_shutdown_with_no_rebuild_running_returns_at_once():
    started = time.monotonic()
    assert await instance_rebuild.interrupt_running_rebuilds() == 0
    assert time.monotonic() - started < 1


def test_the_app_interrupts_its_rebuilds_before_the_database_closes():
    shutdown = inspect.getsource(main.lifespan).split("yield", 1)[1]
    assert "interrupt_running_rebuilds" in shutdown
    assert shutdown.index("interrupt_running_rebuilds") < shutdown.index("close_db")


_SERVER = textwrap.dedent("""
    import asyncio, contextlib, sys
    import uvicorn
    from fastapi import FastAPI
    from app.routers import instance_rebuild

    async def rebuild():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            with open(sys.argv[1], "w") as marker:
                marker.write("interrupted")
            raise

    @contextlib.asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(rebuild())
        instance_rebuild._RUNNING_JOBS.add(task)
        task.add_done_callback(instance_rebuild._RUNNING_JOBS.discard)
        print("READY", flush=True)
        yield
        await instance_rebuild.interrupt_running_rebuilds()

    uvicorn.run(FastAPI(lifespan=lifespan), host="127.0.0.1", port=0, log_level="warning")
""")


def test_under_a_real_uvicorn_sigterm_the_cut_off_rebuild_is_recorded(tmp_path):
    marker = tmp_path / "marker"
    env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    proc = subprocess.Popen(
        [sys.executable, "-c", _SERVER, str(marker)], cwd=BACKEND, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        line = proc.stdout.readline()
        assert line.strip() == "READY", proc.stderr.read()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert marker.read_text() == "interrupted", proc.stderr.read()
