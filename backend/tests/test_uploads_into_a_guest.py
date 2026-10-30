"""An upload is a sequence of independent chunks, then one atomic commit."""

import asyncio
import json
import stat
import struct
import time

import pytest

from app.services.instance_files import bundle, uploads
from app.services.instance_files.errors import GuestFileError
from app.services.instance_files.uploads_temp import is_upload_temp, temp_name
from tests._fake_sftp_server import FakeGuest, connected

TARGET = {"user": "u1", "host": "c1", "project": "default", "instance": "vm1"}


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setattr(uploads, "_mac_key", lambda: b"test-only-upload-key")


async def _stream(*pieces):
    for piece in pieces:
        yield piece


async def _create(client, dest="/home/lab/file.bin", size=10, on_conflict="refuse", max_bytes=1 << 40):
    created = await uploads.create(
        client, **TARGET, dest=dest, size=size, on_conflict=on_conflict, max_bytes=max_bytes
    )
    return created, uploads.unseal(created["token"], **TARGET)


def _guest():
    guest = FakeGuest()
    guest.add_dir(b"/home")
    guest.add_dir(b"/home/lab", uid=1000, gid=1001)
    return guest


async def test_a_whole_upload_lands_with_the_folders_owner():
    guest = _guest()
    client, task = await connected(guest)
    async with client:
        _, session = await _create(client, size=10)
        assert await uploads.write_chunk(client, session, offset=0, length=4, body=_stream(b"ab", b"cd")) == 4
        assert await uploads.write_chunk(client, session, offset=4, length=6, body=_stream(b"efghij")) == 10
        done = await uploads.complete(client, session)
    node = guest.nodes[b"/home/lab/file.bin"]
    assert node.data == b"abcdefghij"
    assert (node.uid, node.gid) == (1000, 1001)
    assert stat.S_IMODE(node.mode) == 0o644
    assert done["already_committed"] is False
    assert [path for path in guest.nodes if b".part" in path] == []
    await asyncio.wait_for(task, 1)


async def test_nothing_appears_at_the_destination_until_commit():
    guest = _guest()
    client, task = await connected(guest)
    async with client:
        _, session = await _create(client, size=4)
        await uploads.write_chunk(client, session, offset=0, length=4, body=_stream(b"abcd"))
        assert b"/home/lab/file.bin" not in guest.nodes
        await uploads.abort(client, session)
    assert [path for path in guest.nodes if b".part" in path] == []
    await asyncio.wait_for(task, 1)


async def test_a_retried_chunk_replaces_what_the_failed_one_left():
    guest = _guest()
    client, task = await connected(guest)
    async with client:
        _, session = await _create(client, size=8)
        await uploads.write_chunk(client, session, offset=0, length=4, body=_stream(b"aaaa"))
        # A failed attempt wrote past the acknowledged offset, with a hole.
        guest.nodes[session.temp.encode()].data = b"aaaa\0\0zz"
        assert await uploads.write_chunk(client, session, offset=4, length=4, body=_stream(b"bbbb")) == 8
        await uploads.complete(client, session)
    assert guest.nodes[b"/home/lab/file.bin"].data == b"aaaabbbb"
    await asyncio.wait_for(task, 1)


async def test_an_offset_beyond_what_is_held_is_refused():
    guest = _guest()
    client, task = await connected(guest)
    async with client:
        _, session = await _create(client, size=8)
        with pytest.raises(GuestFileError) as refused:
            await uploads.write_chunk(client, session, offset=4, length=4, body=_stream(b"bbbb"))
    assert refused.value.code == "offset_conflict"
    assert refused.value.extra["temp_size"] == 0
    await asyncio.wait_for(task, 1)


@pytest.mark.parametrize("offset,length", [(-1, 1), (8, 1), (4, 5)])
async def test_a_chunk_outside_the_declared_size_is_refused(offset, length):
    guest = _guest()
    client, task = await connected(guest)
    async with client:
        _, session = await _create(client, size=8)
        with pytest.raises(GuestFileError) as refused:
            await uploads.write_chunk(client, session, offset=offset, length=length, body=_stream(b"x" * length))
    assert refused.value.code == "offset_conflict"
    await asyncio.wait_for(task, 1)


async def test_a_body_longer_than_declared_stops_at_once():
    guest = _guest()
    client, task = await connected(guest)
    async with client:
        _, session = await _create(client, size=8)
        with pytest.raises(GuestFileError) as refused:
            await uploads.write_chunk(client, session, offset=0, length=4, body=_stream(b"abcdefgh"))
    assert refused.value.code == "too_large"
    await asyncio.wait_for(task, 1)


async def test_a_short_upload_cannot_be_committed():
    guest = _guest()
    client, task = await connected(guest)
    async with client:
        _, session = await _create(client, size=8)
        await uploads.write_chunk(client, session, offset=0, length=4, body=_stream(b"abcd"))
        with pytest.raises(GuestFileError) as refused:
            await uploads.complete(client, session)
    assert refused.value.code == "size_mismatch"
    assert b"/home/lab/file.bin" not in guest.nodes
    await asyncio.wait_for(task, 1)


async def test_complete_is_idempotent_after_a_lost_answer():
    guest = _guest()
    client, task = await connected(guest)
    async with client:
        _, session = await _create(client, size=4)
        await uploads.write_chunk(client, session, offset=0, length=4, body=_stream(b"abcd"))
        await uploads.complete(client, session)
        again = await uploads.complete(client, session)
    assert again["already_committed"] is True
    await asyncio.wait_for(task, 1)


async def test_a_conflict_at_create_and_at_commit_is_refused_unless_replace():
    guest = _guest()
    guest.add_file(b"/home/lab/file.bin", b"old", mode=0o600, uid=33, gid=33)
    client, task = await connected(guest)
    async with client:
        with pytest.raises(GuestFileError) as refused:
            await _create(client, size=3)
        assert refused.value.code == "already_exists"
        _, session = await _create(client, size=3, on_conflict="replace")
        await uploads.write_chunk(client, session, offset=0, length=3, body=_stream(b"new"))
        await uploads.complete(client, session)
    node = guest.nodes[b"/home/lab/file.bin"]
    assert node.data == b"new"
    assert stat.S_IMODE(node.mode) == 0o600
    assert (node.uid, node.gid) == (33, 33)
    await asyncio.wait_for(task, 1)


async def test_a_file_that_appeared_during_the_upload_is_not_overwritten():
    guest = _guest()
    client, task = await connected(guest)
    async with client:
        _, session = await _create(client, size=3)
        await uploads.write_chunk(client, session, offset=0, length=3, body=_stream(b"new"))
        guest.add_file(b"/home/lab/file.bin", b"theirs")
        with pytest.raises(GuestFileError) as refused:
            await uploads.complete(client, session)
        assert refused.value.code == "already_exists"
        assert guest.nodes[b"/home/lab/file.bin"].data == b"theirs"
        await uploads.complete(client, session, on_conflict="replace")
    assert guest.nodes[b"/home/lab/file.bin"].data == b"new"
    await asyncio.wait_for(task, 1)


async def test_a_folder_is_never_replaced_by_a_file():
    guest = _guest()
    guest.add_dir(b"/home/lab/file.bin")
    client, task = await connected(guest)
    async with client:
        with pytest.raises(GuestFileError) as refused:
            await _create(client, size=3, on_conflict="replace")
    assert refused.value.code == "is_a_directory"
    await asyncio.wait_for(task, 1)


async def test_an_upload_past_the_free_space_is_refused_before_a_byte_is_sent():
    guest = _guest()
    client, task = await connected(guest)
    async with client:
        with pytest.raises(GuestFileError) as refused:
            await _create(client, size=4096 * 301)
    assert refused.value.code == "no_space"
    assert refused.value.status == 507
    assert [path for path in guest.nodes if b".part" in path] == []
    await asyncio.wait_for(task, 1)


async def test_an_upload_past_the_limit_is_refused():
    guest = _guest()
    client, task = await connected(guest)
    async with client:
        with pytest.raises(GuestFileError) as refused:
            await _create(client, size=100, max_bytes=99)
    assert refused.value.code == "too_large"
    await asyncio.wait_for(task, 1)


async def test_a_kernel_view_takes_no_upload():
    client, task = await connected(_guest())
    async with client:
        with pytest.raises(GuestFileError) as refused:
            await _create(client, dest="/proc/x")
    assert refused.value.code == "protected_path"
    await asyncio.wait_for(task, 1)


async def test_leftovers_are_swept_and_active_uploads_are_counted():
    guest = _guest()
    old = temp_name(int(time.time()) - 10)
    guest.add_file(b"/home/lab/" + old.encode())
    for _ in range(uploads.MAX_SESSIONS_PER_FOLDER):
        guest.add_file(b"/home/lab/" + temp_name(int(time.time()) + 3600).encode())
    client, task = await connected(guest)
    async with client:
        with pytest.raises(GuestFileError) as refused:
            await _create(client, size=1)
    assert refused.value.code == "busy"
    assert b"/home/lab/" + old.encode() not in guest.nodes
    await asyncio.wait_for(task, 1)


async def test_expired_leftovers_can_be_purged():
    guest = _guest()
    guest.add_file(b"/home/lab/" + temp_name(int(time.time()) - 10).encode())
    keeper = temp_name(int(time.time()) + 3600)
    guest.add_file(b"/home/lab/" + keeper.encode())
    client, task = await connected(guest)
    async with client:
        assert await uploads.purge_temps(client, "/home/lab") == {"removed": 1}
    assert b"/home/lab/" + keeper.encode() in guest.nodes
    await asyncio.wait_for(task, 1)


def test_only_this_services_names_count_as_temps():
    assert is_upload_temp(temp_name(1_800_000_000))
    for name in (".orcastra-upload-x.part", ".orcastra-upload-1-2.part", "report.part", "a.orcastra-upload-1800000000-0123456789abcdef.part"):
        assert not is_upload_temp(name)


def _session(**changes):
    values = {
        "user": "u1", "host": "c1", "project": "default", "instance": "vm1", "temp": "/t", "dest": "/d",
        "size": 1, "on_conflict": "refuse", "expires_at": int(time.time()) + 100,
    }
    return uploads.UploadSession(**{**values, **changes})


@pytest.mark.parametrize(
    "override",
    [{"user": "u2"}, {"host": "c2"}, {"project": "other"}, {"instance": "vm2"}],
)
def test_a_token_is_bound_to_who_and_where(override):
    token = uploads.seal(_session())
    with pytest.raises(GuestFileError) as refused:
        uploads.unseal(token, **{**TARGET, **override})
    assert refused.value.code == "upload_session_lost"


def test_a_token_cannot_be_edited_or_outlive_its_session():
    token = uploads.seal(_session())
    body, _, mac = token.partition(".")
    for forged in (body[:-2] + "aa." + mac, body + "." + "0" * 64, "garbage", ""):
        with pytest.raises(GuestFileError):
            uploads.unseal(forged, **TARGET)
    with pytest.raises(GuestFileError):
        uploads.unseal(uploads.seal(_session(expires_at=int(time.time()) - 1)), **TARGET)


def _bundle(manifest, *files):
    raw = json.dumps(manifest).encode()
    return struct.pack(">I", len(raw)) + raw + b"".join(files)


async def test_a_bundle_lands_every_file_and_makes_its_folders():
    guest = _guest()
    body = _bundle(
        {
            "base": "/home/lab",
            "folders": ["empty"],
            "files": [{"path": "a/one.txt", "size": 3}, {"path": "a/b/two.txt", "size": 4}, {"path": "three", "size": 0}],
        },
        b"one",
        b"twoo",
    )
    client, task = await connected(guest)
    async with client:
        result = await bundle.receive_bundle(client, _stream(body[:9], body[9:]), now=int(time.time()))
    assert [item["error"] for item in result["results"]] == [None, None, None]
    assert guest.nodes[b"/home/lab/a/b/two.txt"].data == b"twoo"
    assert guest.nodes[b"/home/lab/three"].data == b""
    assert b"/home/lab/empty" in guest.nodes
    assert guest.nodes[b"/home/lab/a"].uid == 1000
    assert [path for path in guest.nodes if b".part" in path] == []
    await asyncio.wait_for(task, 1)


async def test_a_refused_file_does_not_shift_the_files_after_it():
    guest = _guest()
    guest.add_file(b"/home/lab/exists", b"old")
    body = _bundle(
        {"base": "/home/lab", "files": [{"path": "exists", "size": 3}, {"path": "new", "size": 3}]},
        b"AAA",
        b"BBB",
    )
    client, task = await connected(guest)
    async with client:
        result = await bundle.receive_bundle(client, _stream(body), now=int(time.time()))
    assert [item["error"] for item in result["results"]] == ["already_exists", None]
    assert guest.nodes[b"/home/lab/exists"].data == b"old"
    assert guest.nodes[b"/home/lab/new"].data == b"BBB"
    await asyncio.wait_for(task, 1)


@pytest.mark.parametrize("name", ["../escape", "a/../../escape", "/absolute", "a//b", "line\nbreak"])
async def test_a_bundle_path_cannot_leave_its_folder(name):
    guest = _guest()
    body = _bundle({"base": "/home/lab", "files": [{"path": name, "size": 1}]}, b"x")
    client, task = await connected(guest)
    async with client:
        result = await bundle.receive_bundle(client, _stream(body), now=int(time.time()))
    assert result["results"][0]["error"] == "invalid_path"
    assert not any(b"escape" in path for path in guest.nodes)
    await asyncio.wait_for(task, 1)


@pytest.mark.parametrize(
    "manifest",
    [
        {"base": "/home/lab", "files": [{"path": "f", "size": bundle.MAX_BUNDLE_BYTES + 1}]},
        {"base": "/home/lab", "files": [{"path": "f", "size": 1}] * (bundle.MAX_BUNDLE_FILES + 1)},
        {"base": "/home/lab", "files": [{"path": "f", "size": -1}]},
        {"base": "/home/lab", "files": [{"path": "f"}]},
        {"base": "/proc", "files": []},
        {"files": []},
    ],
)
async def test_a_bad_manifest_is_refused_before_any_file(manifest):
    guest = _guest()
    client, task = await connected(guest)
    async with client:
        with pytest.raises(GuestFileError):
            await bundle.receive_bundle(client, _stream(_bundle(manifest)), now=int(time.time()))
    assert len(guest.nodes) == 3
    await asyncio.wait_for(task, 1)
