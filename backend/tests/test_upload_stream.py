"""
The resumable-upload staging path, `append_request_body`.

Reading the whole body before the size check protects the disk but not the process, and a sync
write per chunk blocks the event loop for the entire file. This module asserts:

  1. an oversized body is rejected without being fully read
  2. the write does not run on the event loop thread
  3. the loop keeps ticking while a slow write is outstanding
  4. a rejected PATCH leaves the staging file at the offset the caller started from,
     so the DB row and the file cannot disagree
"""

import asyncio
import os
import threading
import time
from typing import AsyncIterator, List

import pytest
from fastapi import HTTPException

from app.core import upload_stream
from app.core.upload_stream import append_request_body


class FakeRequest:
    """Minimal stand-in: the helper only calls `.stream()`.

    Counts the chunks pulled, which proves the body is not drained before the size check.
    """

    def __init__(self, chunks: List[bytes]) -> None:
        self._chunks = chunks
        self.consumed = 0

    async def stream(self) -> AsyncIterator[bytes]:
        for c in self._chunks:
            self.consumed += 1
            yield c


@pytest.fixture
def staging(tmp_path):
    p = tmp_path / "session.iso"
    p.write_bytes(b"")
    return str(p)


async def test_appends_and_returns_the_new_offset(staging) -> None:
    req = FakeRequest([b"abcd", b"efgh"])

    offset = await append_request_body(
        req, staging, start_offset=0, limit=1024, label="t"
    )

    assert offset == 8
    with open(staging, "rb") as f:
        assert f.read() == b"abcdefgh"


async def test_resumes_from_a_nonzero_offset(staging) -> None:
    with open(staging, "wb") as f:
        f.write(b"HEAD")
    req = FakeRequest([b"TAIL"])

    offset = await append_request_body(
        req, staging, start_offset=4, limit=1024, label="t"
    )

    assert offset == 8
    with open(staging, "rb") as f:
        assert f.read() == b"HEADTAIL"


async def test_oversized_body_is_rejected_without_being_fully_read(staging) -> None:
    """The property that separates a memory guard from a disk guard.

    100 chunks are offered and the limit is crossed at the third. A handler that reads the
    whole body first consumes all 100 and only then raises; this one must stop early.
    """
    chunks = [b"x" * 16] * 100
    req = FakeRequest(chunks)

    with pytest.raises(HTTPException) as caught:
        await append_request_body(req, staging, start_offset=0, limit=40, label="t")

    assert caught.value.status_code == 413
    assert req.consumed < len(chunks), (
        f"the whole body was drained ({req.consumed}/{len(chunks)} chunks) before the "
        "size check fired - peak memory is still attacker-controlled"
    )


async def test_rejected_upload_leaves_the_file_at_the_starting_offset(staging) -> None:
    """Otherwise the file grows past the offset the DB row records and resume is corrupt."""
    with open(staging, "wb") as f:
        f.write(b"KEEP")

    # Force at least one flush to land before the limit is crossed.
    monkey_flush = 8
    original = upload_stream.FLUSH_BYTES
    upload_stream.FLUSH_BYTES = monkey_flush
    try:
        req = FakeRequest([b"y" * 8, b"y" * 8, b"y" * 8])
        with pytest.raises(HTTPException) as caught:
            await append_request_body(
                req, staging, start_offset=4, limit=4 + 20, label="t"
            )
        assert caught.value.status_code == 413
    finally:
        upload_stream.FLUSH_BYTES = original

    assert os.path.getsize(staging) == 4, (
        "staging file was not rolled back; the row says 4 bytes but the file is "
        f"{os.path.getsize(staging)}"
    )
    with open(staging, "rb") as f:
        assert f.read() == b"KEEP"


async def test_write_does_not_run_on_the_event_loop_thread(staging, monkeypatch) -> None:
    seen: List[int] = []
    real_write = upload_stream._write_at

    def recording_write(path: str, pos: int, data: bytes) -> None:
        seen.append(threading.get_ident())
        real_write(path, pos, data)

    monkeypatch.setattr(upload_stream, "_write_at", recording_write)

    loop_thread = threading.get_ident()
    await append_request_body(
        FakeRequest([b"payload"]), staging, start_offset=0, limit=1024, label="t"
    )

    assert seen, "the write never ran"
    assert all(t != loop_thread for t in seen), "the write ran on the event loop thread"


async def test_the_loop_keeps_running_while_a_slow_write_is_outstanding(
    staging, monkeypatch
) -> None:
    real_write = upload_stream._write_at

    def slow_write(path: str, pos: int, data: bytes) -> None:
        time.sleep(0.3)
        real_write(path, pos, data)

    monkeypatch.setattr(upload_stream, "_write_at", slow_write)

    ticks: List[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        while not stop.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    before = len(ticks)

    await append_request_body(
        FakeRequest([b"payload"]), staging, start_offset=0, limit=1024, label="t"
    )

    stop.set()
    await beat
    ticked = len(ticks) - before

    # A nominal loop ticks ~100x/s; requiring 10x/s over the 0.3 s write leaves room for a busy
    # host and still fails when the loop is blocked.
    assert ticked >= 3, f"event loop starved during the write: only {ticked} ticks"


async def test_a_file_longer_than_the_offset_is_healed_not_appended_past(staging) -> None:
    """A resumed upload overwrites stray bytes past the committed offset.

    The session row is the authority on committed bytes. The file can be longer: a rollback
    that could not run, or a write whose caller already timed out, leaves extra bytes behind.
    Appending past them would leave a gap nothing catches, since expected_sha256 is optional.
    """
    with open(staging, "wb") as f:
        f.write(b"HEAD" + b"XXXXXX")
    req = FakeRequest([b"TAIL"])

    offset = await append_request_body(
        req, staging, start_offset=4, limit=1024, label="t"
    )

    assert offset == 8
    with open(staging, "rb") as f:
        assert f.read() == b"HEADTAIL"
    assert os.path.getsize(staging) == offset, "file size must match the returned offset"


async def test_a_missing_staging_file_is_recreated_rather_than_crashing(tmp_path) -> None:
    """Recovery paths can delete the staging file; a resumed PATCH must not 500 on it."""
    path = str(tmp_path / "gone.iso")
    req = FakeRequest([b"abcd"])

    offset = await append_request_body(req, path, start_offset=0, limit=1024, label="t")

    assert offset == 4
    with open(path, "rb") as f:
        assert f.read() == b"abcd"


async def test_a_session_leaves_no_staging_file_until_the_first_write(tmp_path) -> None:
    """No file may exist before there are bytes to put in it.

    Nothing reclaims a staging file without a session row (the recovery loop only re-drives
    stuck imports), so the file is created on first write and a failure before then leaks none.
    """
    path = str(tmp_path / "not-yet.iso")
    assert not os.path.exists(path)

    await append_request_body(FakeRequest([b"first"]), path, start_offset=0, limit=64, label="t")

    assert os.path.exists(path), "the first write must create the file"
    assert os.path.getsize(path) == 5
