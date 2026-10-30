"""The archive of a guest's folder is opened by a person, on their machine, from a hostile source."""

import asyncio
import io
import os
import stat
import tarfile

import pytest

from app.services.instance_files import sftp_codec as codec
from app.services.instance_files.tar_stream import _link_stays_inside, tar_gz_stream
from tests._fake_sftp_server import FakeGuest, Node, connected


async def _archive(guest, roots, max_bytes=1 << 40):
    client, task = await connected(guest)
    async with client:
        blob = b"".join([piece async for piece in tar_gz_stream(client, roots, max_bytes=max_bytes)])
    await asyncio.wait_for(task, 1)
    return tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz")


async def test_a_folder_comes_back_whole():
    guest = FakeGuest()
    guest.add_dir(b"/app")
    guest.add_dir(b"/app/sub")
    guest.add_file(b"/app/a.txt", b"hello", mode=0o640)
    guest.add_file(b"/app/sub/big.bin", b"x" * 100_000)
    guest.add_file(b"/app/sub/empty")
    guest.add_link(b"/app/rel", b"sub/big.bin")
    archive = await _archive(guest, ["/app"])
    names = archive.getnames()
    assert {"app", "app/a.txt", "app/sub", "app/sub/big.bin", "app/sub/empty", "app/rel"} <= set(names)
    assert "SKIPPED.txt" not in names
    assert archive.extractfile("app/a.txt").read() == b"hello"
    assert archive.extractfile("app/sub/big.bin").read() == b"x" * 100_000
    assert archive.getmember("app/a.txt").mode == 0o640
    assert archive.getmember("app/rel").linkname == "sub/big.bin"


async def test_ownership_and_special_bits_stay_behind():
    guest = FakeGuest()
    guest.add_dir(b"/app")
    guest.add_file(b"/app/suid", b"x", mode=0o4755, uid=1000, gid=1000)
    archive = await _archive(guest, ["/app"])
    member = archive.getmember("app/suid")
    assert member.mode == 0o755
    assert (member.uid, member.gid) == (0, 0)


@pytest.mark.parametrize("target", [b"/etc/shadow", b"../../../etc/passwd", b"../outside", b"sub/../../x"])
async def test_a_link_that_leaves_the_archive_is_left_out_and_named(target):
    guest = FakeGuest()
    guest.add_dir(b"/app")
    guest.add_link(b"/app/escape", target)
    archive = await _archive(guest, ["/app"])
    assert "app/escape" not in archive.getnames()
    listing = archive.extractfile("SKIPPED.txt").read().decode()
    assert "/app/escape: link points outside this archive" in listing


def test_the_link_rule():
    assert _link_stays_inside(b"app/a/link", b"../b")
    assert _link_stays_inside(b"app/link", b"sub/file")
    assert not _link_stays_inside(b"app/link", b"../x")
    assert not _link_stays_inside(b"app/link", b"/abs")
    assert not _link_stays_inside(b"app/link", b"")


async def test_extraction_writes_nothing_outside(tmp_path):
    guest = FakeGuest()
    guest.add_dir(b"/app")
    guest.add_link(b"/app/x", b"../../../../tmp/owned")
    guest.add_file(b"/app/ok", b"fine")
    archive = await _archive(guest, ["/app"])
    archive.extractall(tmp_path, filter="data")
    written = {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")}
    assert written == {"app", "app/ok", "SKIPPED.txt"}


async def test_special_files_are_listed_and_never_opened():
    guest = FakeGuest()
    guest.add_dir(b"/app")
    guest.nodes[b"/app/pipe"] = Node(stat.S_IFIFO | 0o644)
    guest.nodes[b"/app/zero"] = Node(stat.S_IFCHR | 0o666)
    guest.nodes[b"/app/sock"] = Node(stat.S_IFSOCK | 0o644)
    archive = await _archive(guest, ["/app"])
    assert archive.getnames() == ["app", "SKIPPED.txt"]
    listing = archive.extractfile("SKIPPED.txt").read().decode()
    assert listing.count("not a regular file") == 3
    assert codec.FXP_OPEN not in guest.received


@pytest.mark.parametrize("root", ["/proc", "/sys", "/dev"])
async def test_a_kernel_view_is_never_walked(root):
    guest = FakeGuest()
    guest.add_dir(root.encode())
    guest.add_file(root.encode() + b"/kcore", b"x")
    archive = await _archive(guest, [root])
    assert archive.getnames() == ["SKIPPED.txt"]


async def test_two_roots_with_one_name_keep_the_first():
    guest = FakeGuest()
    guest.add_dir(b"/a")
    guest.add_dir(b"/b")
    guest.add_file(b"/a/same", b"first")
    guest.add_file(b"/b/same", b"second")
    archive = await _archive(guest, ["/a/same", "/b/same"])
    assert archive.extractfile("same").read() == b"first"
    assert "duplicate name" in archive.extractfile("SKIPPED.txt").read().decode()


async def test_a_file_that_shrinks_while_read_keeps_later_entries_aligned():
    guest = FakeGuest()
    guest.add_dir(b"/app")
    node = guest.add_file(b"/app/a-shrinks", b"x" * 10_000)
    guest.add_file(b"/app/b-after", b"intact")

    async def script(packet_type, request_id, cursor):
        if packet_type == codec.FXP_OPEN and cursor.string(9000).endswith(b"a-shrinks"):
            node.data = b"x" * 100
        return None

    guest.script = script
    archive = await _archive(guest, ["/app"])
    assert archive.extractfile("app/b-after").read() == b"intact"
    assert "changed while it was read" in archive.extractfile("SKIPPED.txt").read().decode()


async def test_an_archive_past_the_limit_stops():
    from app.services.instance_files.errors import GuestFileError

    guest = FakeGuest()
    guest.add_dir(b"/app")
    guest.add_file(b"/app/big", b"x" * 1000)
    with pytest.raises(GuestFileError) as refused:
        await _archive(guest, ["/app"], max_bytes=999)
    assert refused.value.code == "too_large"


async def test_a_non_utf8_name_survives_the_archive():
    guest = FakeGuest()
    guest.add_dir(b"/app")
    guest.add_file(b"/app/caf\xe9", b"x")
    archive = await _archive(guest, ["/app"])
    assert os.fsencode(archive.getnames()[1]) == b"app/caf\xe9"
