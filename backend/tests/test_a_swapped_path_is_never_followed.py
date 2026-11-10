"""The file service acts as root by path, and a user in the guest can change what a path names.

OPEN and SETSTAT follow links, and SFTP has neither O_NOFOLLOW nor lsetstat. A user who owns a
folder and watches it with inotify could replace an upload's temp with a link to /etc/shadow and
be handed that file by the next chown, or have it truncated by the next chunk. A chunked upload
needs no race, because the temp sits between requests. Each case here is one such swap.
"""

import asyncio
import stat

import pytest

from app.services.instance_files import delete_slice, operations, text_edit, uploads
from app.services.instance_files import sftp_codec as codec
from app.services.instance_files.errors import GuestFileError
from tests._fake_sftp_server import FakeGuest, connected

TARGET = {"user": "u1", "host": "c1", "project": "default", "instance": "vm1"}
SHADOW = b"root:secret\n"


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setattr(uploads, "_mac_key", lambda: b"test-only-upload-key")


def _guest():
    guest = FakeGuest()
    guest.add_dir(b"/etc")
    guest.add_file(b"/etc/shadow", SHADOW, mode=0o640, gid=42, mtime=1_000)
    guest.add_dir(b"/home")
    guest.add_dir(b"/home/lab", uid=1000, gid=1000)
    return guest


async def _stream(*pieces):
    for piece in pieces:
        yield piece


async def _session(client, size=4):
    created = await uploads.create(
        client, **TARGET, dest="/home/lab/f", size=size, on_conflict="refuse", max_bytes=1 << 30
    )
    return uploads.unseal(created["token"], **TARGET)


def _untouched(guest):
    node = guest.nodes[b"/etc/shadow"]
    assert node.data == SHADOW
    assert (node.uid, node.gid) == (0, 42)
    assert stat.S_IMODE(node.mode) == 0o640


async def test_a_temp_swapped_for_a_link_between_chunks_is_not_written_through():
    guest = _guest()
    client, task = await connected(guest)
    async with client:
        session = await _session(client)
        guest.add_link(session.temp.encode(), b"/etc/shadow")
        with pytest.raises(GuestFileError) as refused:
            await uploads.write_chunk(client, session, offset=0, length=4, body=_stream(b"evil"))
    assert refused.value.code == "upload_session_lost"
    _untouched(guest)
    await asyncio.wait_for(task, 1)


async def test_a_temp_swapped_for_a_link_before_commit_gets_no_owner():
    guest = _guest()
    client, task = await connected(guest)
    async with client:
        session = await _session(client, size=len(SHADOW))
        await uploads.write_chunk(client, session, offset=0, length=len(SHADOW), body=_stream(SHADOW))
        guest.nodes[session.temp.encode()].mode = stat.S_IFREG | 0o600
        guest.add_link(session.temp.encode(), b"/etc/shadow")
        with pytest.raises(GuestFileError):
            await uploads.complete(client, session)
    _untouched(guest)
    await asyncio.wait_for(task, 1)


async def test_a_temp_replaced_by_someone_elses_file_is_refused():
    guest = _guest()
    client, task = await connected(guest)
    async with client:
        session = await _session(client)
        guest.add_file(session.temp.encode(), b"mine", mode=0o644, uid=1000, gid=1000)
        with pytest.raises(GuestFileError) as refused:
            await uploads.write_chunk(client, session, offset=0, length=4, body=_stream(b"evil"))
    assert refused.value.code == "upload_session_lost"
    await asyncio.wait_for(task, 1)


async def test_a_swap_between_the_look_and_the_open_is_caught_by_the_handle():
    guest = _guest()

    async def script(packet_type, request_id, cursor):
        if packet_type == codec.FXP_OPEN and cursor.string(9000).endswith(b"/new"):
            guest.add_link(b"/home/lab/new", b"/etc/shadow")
        return None

    client, task = await connected(guest)
    async with client:
        await client.mkdir(b"/home/lab/keep")
        guest.script = script
        guest.add_dir(b"/home/lab/new")
        with pytest.raises(GuestFileError) as refused:
            await operations._inherit_owner(client, b"/home/lab/new", b"/home/lab", mode=0o755)
    assert refused.value.code == "path_changed"
    _untouched(guest)
    await asyncio.wait_for(task, 1)


async def test_a_folder_being_filled_is_not_mistaken_for_a_swap():
    """A parallel upload moves a new folder's mtime, and on ZFS its size, between the look and the open."""
    guest = _guest()
    guest.add_dir(b"/home/lab/new")
    filled = False

    async def script(packet_type, request_id, cursor):
        nonlocal filled
        if packet_type == codec.FXP_OPEN and not filled and cursor.string(9000).endswith(b"/new"):
            filled = True
            guest.nodes[b"/home/lab/new"].mtime += 1
        return None

    guest.script = script
    client, task = await connected(guest)
    async with client:
        await operations._inherit_owner(client, b"/home/lab/new", b"/home/lab", mode=0o755)
    assert guest.nodes[b"/home/lab/new"].uid == 1000
    assert guest.handles == {}
    await asyncio.wait_for(task, 1)


async def test_a_folder_that_never_holds_still_is_given_up_on():
    guest = _guest()
    guest.add_dir(b"/home/lab/new")
    opened = 0

    async def script(packet_type, request_id, cursor):
        nonlocal opened
        if packet_type == codec.FXP_OPEN and cursor.string(9000).endswith(b"/new"):
            opened += 1
            guest.nodes[b"/home/lab/new"].mtime += 1
        return None

    guest.script = script
    client, task = await connected(guest)
    async with client:
        with pytest.raises(GuestFileError) as refused:
            await operations._inherit_owner(client, b"/home/lab/new", b"/home/lab", mode=0o755)
    assert refused.value.code == "path_changed"
    assert guest.nodes[b"/home/lab/new"].uid == 0
    assert 1 < opened < 10
    assert guest.handles == {}
    await asyncio.wait_for(task, 1)


async def test_a_file_that_moves_is_not_looked_at_twice():
    guest = _guest()
    guest.add_file(b"/home/lab/f", b"x", mode=0o644)
    opened = 0

    async def script(packet_type, request_id, cursor):
        nonlocal opened
        if packet_type == codec.FXP_OPEN and cursor.string(9000).endswith(b"/f"):
            opened += 1
            guest.nodes[b"/home/lab/f"].mtime += 1
        return None

    guest.script = script
    client, task = await connected(guest)
    async with client:
        with pytest.raises(GuestFileError) as refused:
            await client.open_unchanged(b"/home/lab/f", codec.PFLAG_READ)
    assert refused.value.code == "path_changed"
    assert opened == 1
    await asyncio.wait_for(task, 1)


async def test_a_new_folder_swapped_for_a_link_gives_nothing_away():
    guest = _guest()

    async def script(packet_type, request_id, cursor):
        if packet_type == codec.FXP_MKDIR:
            path = cursor.string(9000)
            guest.add_link(path, b"/etc")
            return __import__("tests._fake_sftp_server", fromlist=["frame"]).frame(
                __import__("tests._fake_sftp_server", fromlist=["status_body"]).status_body(request_id, codec.FX_OK)
            )
        return None

    guest.script = script
    client, task = await connected(guest)
    async with client:
        with pytest.raises(GuestFileError):
            await operations.make_directory(client, "/home/lab/new")
    assert guest.nodes[b"/etc"].uid == 0
    await asyncio.wait_for(task, 1)


async def test_recursive_chown_skips_what_changed_under_it():
    guest = _guest()
    guest.add_dir(b"/home/lab/tree", uid=1000, gid=1000)
    guest.add_file(b"/home/lab/tree/ok", uid=1000, gid=1000)
    guest.add_file(b"/home/lab/tree/swapped", uid=1000, gid=1000)

    async def script(packet_type, request_id, cursor):
        if packet_type == codec.FXP_LSTAT and cursor.string(9000).endswith(b"/swapped"):
            guest.add_link(b"/home/lab/tree/swapped", b"/etc/shadow")
        return None

    guest.script = script
    client, task = await connected(guest)
    async with client:
        result = await operations.set_attributes(client, "/home/lab/tree", recursive=True, uid=2000, gid=2000)
    assert result["skipped"] == 1
    assert guest.nodes[b"/home/lab/tree/ok"].uid == 2000
    _untouched(guest)
    await asyncio.wait_for(task, 1)


async def test_writing_through_a_file_does_not_follow_a_swap():
    guest = _guest()
    guest.add_file(b"/home/lab/mounted", b"old", uid=1000, gid=1000)

    async def script(packet_type, request_id, cursor):
        if packet_type == codec.FXP_OPEN and cursor.string(9000).endswith(b"/mounted"):
            flags = cursor.u32()
            if flags & codec.PFLAG_WRITE:
                guest.add_link(b"/home/lab/mounted", b"/etc/shadow")
        return None

    client, task = await connected(guest)
    async with client:
        read = await text_edit.read_text(client, "/home/lab/mounted")
        guest.script = script
        with pytest.raises(GuestFileError) as refused:
            await text_edit.write_text(client, "/home/lab/mounted", "x", read["etag"], allow_in_place=True)
    assert refused.value.code == "path_changed"
    _untouched(guest)
    await asyncio.wait_for(task, 1)


async def test_a_folder_swapped_for_a_link_is_not_emptied():
    guest = _guest()
    guest.add_dir(b"/home/lab/tree")
    guest.add_dir(b"/home/lab/tree/sub")
    guest.add_file(b"/home/lab/tree/sub/x")
    guest.add_dir(b"/home/lab/outside")
    guest.add_file(b"/home/lab/outside/precious", b"keep")

    async def script(packet_type, request_id, cursor):
        if packet_type == codec.FXP_REALPATH and cursor.string(9000).endswith(b"/tree/sub"):
            for path in [p for p in guest.nodes if p.startswith(b"/home/lab/tree/sub")]:
                del guest.nodes[path]
            guest.add_link(b"/home/lab/tree/sub", b"/home/lab/outside")
        return None

    guest.script = script
    client, task = await connected(guest)
    async with client:
        result = await delete_slice.delete_path(client, "/home/lab/tree", recursive=True)
    assert guest.nodes[b"/home/lab/outside/precious"].data == b"keep"
    assert result["failed_count"] >= 1
    await asyncio.wait_for(task, 1)


async def test_a_file_created_here_is_given_away_through_its_own_handle():
    guest = _guest()
    client, task = await connected(guest)
    async with client:
        await operations.create_file(client, "/home/lab/new.txt")
    assert codec.FXP_FSETSTAT in guest.received
    assert codec.FXP_SETSTAT not in guest.received
    assert guest.nodes[b"/home/lab/new.txt"].uid == 1000
    await asyncio.wait_for(task, 1)
