"""The ordinary paths of the client, against a guest that behaves as the lab proved LXD does."""

import asyncio
import hashlib
import stat

import pytest

from app.services.instance_files import sftp_codec as codec
from app.services.instance_files.errors import GuestFileError, from_sftp_status
from app.services.instance_files.sftp_client import is_directory, is_regular, is_symlink
from tests._fake_sftp_server import FakeGuest, connected


async def test_a_directory_is_read_in_batches_with_its_attributes():
    guest = FakeGuest()
    guest.add_dir(b"/big")
    for index in range(250):
        guest.add_file(b"/big/f%03d" % index, b"x" * index)
    client, task = await connected(guest)
    async with client:
        batches = [batch async for batch in client.iter_directory(b"/big")]
    names = [entry for batch in batches for entry in batch]
    assert len(batches) == 3
    assert len(names) == 250
    assert names[5].attrs.size == 5
    assert is_regular(names[5].attrs)
    await asyncio.wait_for(task, 1)


async def test_stopping_a_listing_early_closes_its_handle():
    guest = FakeGuest()
    guest.add_dir(b"/big")
    for index in range(250):
        guest.add_file(b"/big/f%03d" % index)
    client, task = await connected(guest)
    async with client:
        stream = client.iter_directory(b"/big")
        await stream.__anext__()
        await stream.aclose()
        attrs = await client.lstat(b"/big")
        assert is_directory(attrs)
    assert codec.FXP_CLOSE in guest.received
    await asyncio.wait_for(task, 1)


async def test_a_large_file_reads_back_identical_through_the_window():
    payload = hashlib.sha256(b"seed").digest() * 200_000
    guest = FakeGuest()
    guest.add_file(b"/big.bin", payload)
    client, task = await connected(guest)
    async with client:
        handle = await client.open(b"/big.bin", codec.PFLAG_READ)
        digest = hashlib.sha256()
        total = 0
        async for piece in client.read_stream(handle, limit=len(payload) + 1000):
            digest.update(piece)
            total += len(piece)
        await client.close(handle)
    assert total == len(payload)
    assert digest.digest() == hashlib.sha256(payload).digest()
    await asyncio.wait_for(task, 1)


async def test_a_short_read_is_filled_before_later_bytes_are_given_out():
    payload = bytes(range(256)) * 400

    async def script(packet_type, request_id, cursor):
        if packet_type == codec.FXP_READ:
            handle = cursor.string(256)
            offset = cursor.u64()
            length = min(cursor.u32(), 1000)
            piece = payload[offset : offset + length]
            import struct
            from tests._fake_sftp_server import frame, status_body

            if not piece:
                return frame(status_body(request_id, codec.FX_EOF))
            return frame(struct.pack(">BI", codec.FXP_DATA, request_id) + codec.string(piece))
        return None

    guest = FakeGuest(script=script)
    guest.add_file(b"/f", payload)
    client, task = await connected(guest)
    async with client:
        handle = await client.open(b"/f", codec.PFLAG_READ)
        out = b"".join([piece async for piece in client.read_stream(handle, limit=len(payload))])
    assert out == payload
    await asyncio.wait_for(task, 1)


async def test_a_large_write_lands_whole_and_in_order():
    payload = hashlib.sha256(b"write").digest() * 100_000
    guest = FakeGuest()
    client, task = await connected(guest)
    async with client:
        handle = await client.open(
            b"/out", codec.PFLAG_WRITE | codec.PFLAG_CREAT | codec.PFLAG_EXCL, mode=0o600
        )
        await client.write_all(handle, 0, payload)
        await client.close(handle)
    assert guest.nodes[b"/out"].data == payload
    assert stat.S_IMODE(guest.nodes[b"/out"].mode) == 0o600
    await asyncio.wait_for(task, 1)


async def test_exclusive_create_is_refused_when_the_file_exists():
    guest = FakeGuest()
    guest.add_file(b"/exists")
    client, task = await connected(guest)
    async with client:
        with pytest.raises(GuestFileError) as refused:
            await client.open(b"/exists", codec.PFLAG_WRITE | codec.PFLAG_CREAT | codec.PFLAG_EXCL)
    assert refused.value.code == "already_exists"
    await asyncio.wait_for(task, 1)


async def test_link_is_the_no_clobber_primitive_and_plain_rename_is_not():
    guest = FakeGuest()
    guest.add_file(b"/a", b"new")
    guest.add_file(b"/b", b"old")
    client, task = await connected(guest)
    async with client:
        with pytest.raises(GuestFileError) as refused:
            await client.link(b"/a", b"/b")
        assert refused.value.code == "already_exists"
        await client.rename(b"/a", b"/b")
    assert guest.nodes[b"/b"].data == b"new"
    await asyncio.wait_for(task, 1)


async def test_links_are_told_from_their_targets():
    guest = FakeGuest()
    guest.add_dir(b"/real")
    guest.add_link(b"/lnk", b"/real")
    client, task = await connected(guest)
    async with client:
        assert is_symlink(await client.lstat(b"/lnk"))
        assert is_directory(await client.stat(b"/lnk"))
        assert await client.readlink(b"/lnk") == b"/real"
        assert await client.realpath(b"/lnk") == b"/real"
    await asyncio.wait_for(task, 1)


async def test_free_space_is_read():
    client, task = await connected(FakeGuest())
    async with client:
        vfs = await client.statvfs(b"/")
    assert vfs.fragment_size * vfs.blocks_available == 4096 * 300
    await asyncio.wait_for(task, 1)


@pytest.mark.parametrize(
    "text,code",
    [
        (b"rename /a /b: file exists", "already_exists"),
        (b"remove /d: directory not empty", "directory_not_empty"),
        (b"rename /a /b: invalid cross-device link", "cross_device"),
        (b"open /x: no space left on device", "no_space"),
        (b"write /x: disk quota exceeded", "no_space"),
        (b"open /x: read-only file system", "read_only_fs"),
        (b"rename /x: device or resource busy", "resource_busy"),
        (b"open /x: operation not permitted", "operation_not_permitted"),
        (b"something new from a later version", "guest_io_error"),
    ],
)
def test_failure_text_becomes_a_stable_code(text, code):
    assert from_sftp_status(4, text).code == code


def test_the_standard_status_codes_map_directly():
    assert from_sftp_status(2).code == "not_found"
    assert from_sftp_status(3).code == "permission_denied"
    assert from_sftp_status(8).code == "unsupported_server"
