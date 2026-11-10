"""The SFTP peer is a guest, and root in a guest can answer anything.

On a virtual machine the bytes come from lxd-agent, which the guest can replace, and LXD copies
them through unread. Every case here is an answer an honest server would never give: each must end
in a refusal within the operation's timeout, without allocating from a stated length and without
leaving a task behind.
"""

import asyncio
import struct
import tracemalloc

import pytest

from app.services.instance_files import sftp_codec as codec
from app.services.instance_files.errors import GuestFileError
from app.services.instance_files.sftp_client import Budget
from tests._fake_sftp_server import FakeGuest, connected, frame, status_body


def _ok(request_id: int) -> bytes:
    return frame(status_body(request_id, codec.FX_OK))


def _raw_frame_length(length: int) -> bytes:
    return struct.pack(">I", length)


def _reply(*parts: bytes) -> bytes:
    return frame(b"".join(parts))


HOSTILE_FRAMES = {
    "empty frame": lambda rid: _raw_frame_length(0),
    "frame of 4 GiB": lambda rid: _raw_frame_length(0xFFFFFFFF),
    "frame one past the ceiling": lambda rid: _raw_frame_length(codec.MAX_PACKET_BYTES + 1),
    "reply to an unknown id": lambda rid: _ok(rid + 7),
    "name count past the packet": lambda rid: _reply(struct.pack(">BII", codec.FXP_NAME, rid, 0xFFFFFFFF)),
    "string past the packet": lambda rid: _reply(
        struct.pack(">BII", codec.FXP_NAME, rid, 1), struct.pack(">I", 500), b"short", b"\0" * 8
    ),
    "name over its limit": lambda rid: _reply(
        struct.pack(">BII", codec.FXP_NAME, rid, 1),
        codec.string(b"x" * (codec.MAX_NAME_BYTES + 1)),
        codec.string(b""),
        struct.pack(">I", 0),
    ),
    "unknown attribute flag": lambda rid: _reply(
        struct.pack(">BII", codec.FXP_ATTRS, rid, 0x40)
    ),
    "huge attribute extension count": lambda rid: _reply(
        struct.pack(">BIII", codec.FXP_ATTRS, rid, codec.ATTR_EXTENDED, 0xFFFFFFFF)
    ),
    "truncated attrs": lambda rid: _reply(
        struct.pack(">BII", codec.FXP_ATTRS, rid, codec.ATTR_SIZE), b"\0\0"
    ),
    "wrong reply type": lambda rid: _reply(struct.pack(">BI", codec.FXP_HANDLE, rid), codec.string(b"h")),
}


@pytest.mark.parametrize("name", sorted(HOSTILE_FRAMES))
async def test_a_malformed_reply_ends_the_connection_with_a_refusal(name):
    hostile = HOSTILE_FRAMES[name]

    async def script(packet_type, request_id, cursor):
        return hostile(request_id)

    guest = FakeGuest(script=script)
    client, task = await connected(guest)
    async with client:
        with pytest.raises(GuestFileError) as refused:
            await client.lstat(b"/etc")
        assert refused.value.code in {"guest_protocol_error", "guest_disconnected"}
        # The connection is finished, so a second call cannot read a stale or forged reply.
        with pytest.raises(GuestFileError):
            await client.lstat(b"/etc")
    await asyncio.wait_for(task, 1)


async def test_a_stated_length_is_refused_before_it_allocates():
    async def script(packet_type, request_id, cursor):
        return _raw_frame_length(0xFFFFFFFF)

    client, task = await connected(FakeGuest(script=script))
    tracemalloc.start()
    async with client:
        with pytest.raises(GuestFileError):
            await client.lstat(b"/")
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 8 * 1024 * 1024
    await asyncio.wait_for(task, 1)


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(frame(struct.pack(">BI", codec.FXP_VERSION, 2)), id="version 2"),
        pytest.param(frame(struct.pack(">BI", codec.FXP_VERSION, 6)), id="version 6"),
        pytest.param(frame(struct.pack(">BI", codec.FXP_STATUS, 3)), id="not a version packet"),
        pytest.param(
            frame(
                struct.pack(">BI", codec.FXP_VERSION, 3)
                + b"".join(codec.string(b"e%d" % i) + codec.string(b"1") for i in range(100))
            ),
            id="too many extensions",
        ),
        pytest.param(
            frame(struct.pack(">BI", codec.FXP_VERSION, 3) + codec.string(b"x" * 300) + codec.string(b"1")),
            id="oversized extension name",
        ),
    ],
)
async def test_a_bad_version_answer_is_refused(reply):
    client, task = await connected(FakeGuest(version_reply=reply))
    with pytest.raises(GuestFileError) as refused:
        async with client:
            pass
    assert refused.value.code == "guest_protocol_error"
    await asyncio.wait_for(task, 1)


async def test_a_guest_that_never_answers_the_handshake_times_out():
    guest = FakeGuest(version_reply=b"")
    client, task = await connected(guest, op_timeout=0.05)
    with pytest.raises(GuestFileError) as refused:
        async with client:
            pass
    assert refused.value.code == "guest_timeout"
    task.cancel()


@pytest.mark.parametrize(
    "stalled",
    [codec.FXP_LSTAT, codec.FXP_OPENDIR, codec.FXP_READ, codec.FXP_WRITE, codec.FXP_RENAME],
)
async def test_a_stall_at_any_operation_times_out_and_ends_the_connection(stalled):
    async def script(packet_type, request_id, cursor):
        return b"" if packet_type == stalled else None

    guest = FakeGuest(script=script)
    guest.add_file(b"/f", b"data")
    client, task = await connected(guest, op_timeout=0.05)
    async with client:
        handle = None
        if stalled in (codec.FXP_READ, codec.FXP_WRITE):
            handle = await client.open(b"/f", codec.PFLAG_READ | codec.PFLAG_WRITE)
        with pytest.raises(GuestFileError) as refused:
            if stalled == codec.FXP_LSTAT:
                await client.lstat(b"/f")
            elif stalled == codec.FXP_OPENDIR:
                async for _ in client.iter_directory(b"/", Budget(entries=100, seconds=30)):
                    pass
            elif stalled == codec.FXP_READ:
                async for _ in client.read_stream(handle, limit=10):
                    pass
            elif stalled == codec.FXP_WRITE:
                await client.write_all(handle, 0, b"x")
            else:
                await client.rename(b"/f", b"/g")
        assert refused.value.code == "guest_timeout"
        # A timeout ends the connection, so a late reply cannot reach a later call.
        with pytest.raises(GuestFileError):
            await client.lstat(b"/f")
    task.cancel()


async def test_a_read_answered_with_more_than_asked_is_refused():
    async def script(packet_type, request_id, cursor):
        if packet_type == codec.FXP_READ:
            return frame(struct.pack(">BI", codec.FXP_DATA, request_id) + codec.string(b"x" * 40000))
        return None

    guest = FakeGuest(script=script)
    guest.add_file(b"/f", b"data")
    client, task = await connected(guest)
    async with client:
        handle = await client.open(b"/f", codec.PFLAG_READ)
        with pytest.raises(GuestFileError) as refused:
            async for _ in client.read_stream(handle, limit=100):
                pass
        assert refused.value.code == "guest_protocol_error"
    await asyncio.wait_for(task, 1)


async def test_endless_empty_data_stops_rather_than_spinning():
    async def script(packet_type, request_id, cursor):
        if packet_type == codec.FXP_READ:
            return frame(struct.pack(">BI", codec.FXP_DATA, request_id) + codec.string(b""))
        return None

    guest = FakeGuest(script=script)
    guest.add_file(b"/f", b"data")
    client, task = await connected(guest)
    async with client:
        handle = await client.open(b"/f", codec.PFLAG_READ)
        pieces = [piece async for piece in client.read_stream(handle, limit=100_000)]
        assert pieces == []
    await asyncio.wait_for(task, 1)


async def test_an_empty_directory_batch_is_refused_so_a_listing_cannot_spin():
    async def script(packet_type, request_id, cursor):
        if packet_type == codec.FXP_READDIR:
            return frame(struct.pack(">BII", codec.FXP_NAME, request_id, 0))
        return None

    client, task = await connected(FakeGuest(script=script))
    async with client:
        with pytest.raises(GuestFileError) as refused:
            async for _ in client.iter_directory(b"/", Budget(entries=100, seconds=30)):
                pass
        assert refused.value.code == "guest_protocol_error"
    await asyncio.wait_for(task, 1)


async def test_a_failure_text_never_leaves_the_error():
    async def script(packet_type, request_id, cursor):
        return frame(status_body(request_id, 4, b"<img src=x onerror=alert(1)> secret-token"))

    client, task = await connected(FakeGuest(script=script))
    async with client:
        with pytest.raises(GuestFileError) as refused:
            await client.lstat(b"/etc")
        assert refused.value.code == "guest_io_error"
        assert "secret-token" not in refused.value.message
        assert "onerror" not in repr(refused.value.to_http().detail)
    await asyncio.wait_for(task, 1)


async def test_a_guest_that_closes_mid_operation_is_a_disconnect():
    async def script(packet_type, request_id, cursor):
        raise ConnectionError

    client, task = await connected(FakeGuest(script=script))
    async with client:
        with pytest.raises(GuestFileError) as refused:
            await client.lstat(b"/etc")
        assert refused.value.code == "guest_disconnected"
    await asyncio.wait_for(task, 1)
