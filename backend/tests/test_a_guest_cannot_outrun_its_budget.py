"""A guest's READDIR need never end. Every consumer of a walk has to stop anyway.

Reported by the security review with a proof: a tenant who replaced lxd-agent in their VM could
serve an endless directory and grow a worker past 800 MB in four seconds. `iter_directory` stopped
only on end of file and each consumer checked its limit between directories, so a worker died and
took every other tenant's consoles with it. Below that it held a task and a concurrency slot
forever, because nothing cancelled a handler whose client had left.
"""

import asyncio
import stat
import struct
import tracemalloc

import pytest
from fastapi import HTTPException

from app.services.instance_files import delete_slice, limits, operations, session, tar_stream, uploads
from app.services.instance_files import sftp_codec as codec
from app.services.instance_files.errors import GuestFileError
from app.services.instance_files.uploads import stalled_after
from tests._fake_sftp_server import FakeGuest, connected, frame

NAME_BYTES = 200
SETTLE_SECONDS = 8


def _names_reply(request_id: int, start: int, directory: bool = False) -> bytes:
    mode = stat.S_IFDIR | 0o755 if directory else stat.S_IFREG | 0o644
    attrs = struct.pack(">IQIIIII", 0x0F, 0, 0, 0, mode, 1, 1)
    body = struct.pack(">BII", codec.FXP_NAME, request_id, 100)
    for index in range(100):
        name = b"n%010d" % (start + index) + b"x" * NAME_BYTES
        body += codec.string(name) + codec.string(b"-rw-r--r-- 1 root root 0 x " + name) + attrs
    return frame(body)


def _endless_guest(directory: bool = False) -> FakeGuest:
    counter = [0]

    async def script(packet_type, request_id, cursor):
        if packet_type == codec.FXP_READDIR:
            counter[0] += 100
            return _names_reply(request_id, counter[0], directory)
        return None

    guest = FakeGuest(script=script)
    guest.add_dir(b"/flood")
    return guest


async def _archive(client):
    async for _ in tar_stream.tar_gz_stream(client, ["/flood"], max_bytes=1 << 40):
        pass


CONSUMERS = {
    "listing": lambda client: operations.list_directory(client, "/flood"),
    "summary": lambda client: operations.summarize(client, ["/flood"], budget_seconds=0.5),
    "recursive chmod": lambda client: operations.set_attributes(client, "/flood", recursive=True, mode=0o700, budget_seconds=0.5),
    "recursive delete": lambda client: delete_slice.delete_path(client, "/flood", recursive=True, budget_seconds=0.5),
    "purge": lambda client: uploads.purge_temps(client, "/flood"),
    "sweep": lambda client: uploads.sweep_temps(client, b"/flood"),
    "archive": _archive,
}


@pytest.mark.parametrize("name", sorted(CONSUMERS))
@pytest.mark.parametrize("directory", [False, True], ids=["files", "folders"])
async def test_an_endless_directory_ends(name, directory, monkeypatch):
    monkeypatch.setattr(tar_stream, "_MAX_ENTRIES", 5_000)
    monkeypatch.setattr(uploads, "_SWEEP_ENTRIES", 5_000)
    monkeypatch.setattr(operations, "MAX_LIST_ENTRIES", 2_000)
    monkeypatch.setattr(operations, "WALK_ENTRIES", 5_000)
    client, task = await connected(_endless_guest(directory), op_timeout=2)
    tracemalloc.start()
    async with client:
        try:
            await asyncio.wait_for(CONSUMERS[name](client), SETTLE_SECONDS)
        except GuestFileError as refused:
            assert refused.code in {"too_large", "delete_stuck"}
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 64 * 1024 * 1024
    task.cancel()


async def test_a_listing_says_it_was_cut_short():
    listing = None

    async def run(client):
        nonlocal listing
        listing = await operations.list_directory(client, "/flood")

    client, task = await connected(_endless_guest(), op_timeout=2)
    import app.services.instance_files.operations as ops

    original = ops.MAX_LIST_ENTRIES
    ops.MAX_LIST_ENTRIES = 300
    try:
        async with client:
            await asyncio.wait_for(run(client), SETTLE_SECONDS)
    finally:
        ops.MAX_LIST_ENTRIES = original
    assert listing["truncated"] is True
    assert len(listing["entries"]) == 300
    task.cancel()


async def test_stale_temp_names_cannot_fill_a_response():
    from app.services.instance_files.uploads_temp import temp_name

    counter = [0]

    async def script(packet_type, request_id, cursor):
        if packet_type != codec.FXP_READDIR:
            return None
        attrs = struct.pack(">I", 0)
        body = struct.pack(">BII", codec.FXP_NAME, request_id, 100)
        for _ in range(100):
            counter[0] += 1
            name = temp_name(1_000_000_000).encode()
            body += codec.string(name) + codec.string(b"") + attrs
        return frame(body)

    guest = FakeGuest(script=script)
    guest.add_dir(b"/flood")
    client, task = await connected(guest, op_timeout=2)
    async with client:
        listing = await asyncio.wait_for(operations.list_directory(client, "/flood"), SETTLE_SECONDS)
    assert len(listing["stale_uploads"]) == operations.MAX_STALE_LISTED
    assert listing["truncated"] is True
    task.cancel()


async def test_a_request_has_a_wall_clock_whatever_its_guest_does(monkeypatch):
    class _Guest:
        pass

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_guest(*args, **kwargs):
        yield _Guest()

    async def no_gate(*args):
        return None

    monkeypatch.setattr(session, "REQUEST_SECONDS", 0.05)
    monkeypatch.setattr(session, "open_guest", fake_guest)
    monkeypatch.setattr(session, "sensitive_gate", no_gate)
    monkeypatch.setattr(session, "fence_gate", no_gate)
    with pytest.raises(HTTPException) as refused:
        async with session.guest_session(None, "c1", "default", "vm"):
            await asyncio.sleep(5)
    assert refused.value.status_code == 504
    assert refused.value.detail["error"] == "guest_timeout"
    assert limits.in_use() == 0


async def test_a_request_body_may_not_go_quiet():
    async def dribble():
        yield b"a"
        await asyncio.Event().wait()

    with pytest.raises(GuestFileError) as refused:
        async for _ in stalled_after(dribble(), 0.02):
            pass
    assert refused.value.code == "upload_stalled"


async def test_short_operations_are_bounded_per_user_cluster_and_worker(monkeypatch):
    monkeypatch.setattr(limits, "SHORT_OPS_PER_WORKER", 4)
    held = []
    for index in range(4):
        slot = limits.short_slot(f"c{index}", "default", "vm", f"u{index}")
        await slot.__aenter__()
        held.append(slot)
    with pytest.raises(GuestFileError) as refused:
        async with limits.short_slot("c9", "default", "vm", "u9"):
            pass
    assert refused.value.code == "busy"
    for slot in held:
        await slot.__aexit__(None, None, None)
    assert limits.in_use() == 0
