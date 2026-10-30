"""What a person can do to a guest's files, against a guest that behaves as the lab proved LXD does."""

import asyncio
import stat

import pytest

from app.services.instance_files import delete_slice, operations, text_edit
from app.services.instance_files import sftp_codec as codec
from app.services.instance_files.errors import GuestFileError
from app.services.instance_files.paths import clean_name, clean_path, from_guest, to_guest
from tests._fake_sftp_server import FakeGuest, connected, frame


def _name_reply(request_id: int, *names: bytes) -> bytes:
    import struct

    body = struct.pack(">BII", codec.FXP_NAME, request_id, len(names))
    for name in names:
        body += codec.string(name) + codec.string(b"-rw-r--r-- 1 root root 0 x " + name) + struct.pack(">I", 0)
    return frame(body)


async def test_a_listing_says_what_each_entry_is():
    guest = FakeGuest()
    guest.add_dir(b"/d")
    guest.add_file(b"/d/file", b"hello", uid=1000, gid=1000)
    guest.add_dir(b"/d/folder")
    guest.add_link(b"/d/good-link", b"/d/folder")
    guest.add_link(b"/d/broken-link", b"/nowhere")
    guest.add_link(b"/d/loop", b"/d/loop")
    guest.nodes[b"/d/zero"] = type(guest.nodes[b"/d"])(mode=stat.S_IFCHR | 0o666)
    guest.nodes[b"/d/pipe"] = type(guest.nodes[b"/d"])(mode=stat.S_IFIFO | 0o644)
    client, task = await connected(guest)
    async with client:
        listing = await operations.list_directory(client, "/d")
    by_name = {entry["name"]: entry for entry in listing["entries"]}
    assert by_name["file"]["type"] == "file"
    assert by_name["file"]["size"] == 5
    assert by_name["file"]["owner"] == "root"
    assert by_name["folder"]["type"] == "directory"
    assert by_name["good-link"]["target_type"] == "directory"
    assert by_name["good-link"]["link_target"] == "/d/folder"
    assert by_name["broken-link"]["target_type"] == "broken"
    assert by_name["loop"]["target_type"] == "broken"
    assert by_name["zero"]["type"] == "char"
    assert by_name["pipe"]["type"] == "fifo"
    assert listing["free_bytes"] == 4096 * 300
    assert listing["total_bytes"] == 4096 * 1000
    await asyncio.wait_for(task, 1)


@pytest.mark.parametrize("hostile", [b"..", b".", b"../../etc/shadow", b"a/b", b"nul\x00byte", b""])
async def test_a_name_that_is_not_a_child_never_reaches_the_interface(hostile):
    async def script(packet_type, request_id, cursor):
        if packet_type == codec.FXP_READDIR:
            if guest.sent:
                return None
            guest.sent = True
            return _name_reply(request_id, hostile, b"honest")
        return None

    guest = FakeGuest(script=script)
    guest.sent = False
    client, task = await connected(guest)
    async with client:
        listing = await operations.list_directory(client, "/")
    assert [entry["name"] for entry in listing["entries"]] == ["honest"]
    assert listing["dropped"] == 1
    await asyncio.wait_for(task, 1)


async def test_an_endless_directory_stops_at_the_entry_cap(monkeypatch):
    monkeypatch.setattr(operations, "MAX_LIST_ENTRIES", 250)
    counter = [0]

    async def script(packet_type, request_id, cursor):
        if packet_type == codec.FXP_READDIR:
            names = [b"f%08d" % (counter[0] + index) for index in range(100)]
            counter[0] += 100
            return _name_reply(request_id, *names)
        return None

    client, task = await connected(FakeGuest(script=script))
    async with client:
        listing = await operations.list_directory(client, "/")
    assert listing["truncated"] is True
    assert len(listing["entries"]) == 250
    await asyncio.wait_for(task, 1)


async def test_a_size_past_what_json_can_hold_is_clamped():
    guest = FakeGuest()
    guest.add_file(b"/big", stated_size=2**63)
    client, task = await connected(guest)
    async with client:
        listing = await operations.list_directory(client, "/")
    assert listing["entries"][0]["size"] == 2**53 - 1
    await asyncio.wait_for(task, 1)


async def test_an_owner_name_from_the_guest_is_shown_only_when_it_looks_like_one():
    assert operations._owner_names(b"-rw-r--r-- 1 lab users 0 x f") == ("lab", "users")
    assert operations._owner_names(b"-rw-r--r-- 1 <img> users 0 x f") == (None, "users")
    assert operations._owner_names(b"nonsense") == (None, None)
    assert operations._account(b"<img>") is None


def test_every_name_round_trips_exactly():
    samples = [
        b"plain.txt",
        "café.txt".encode(),
        b"latin1-\xe9.txt",
        b"\xff\xfe",
        "\U0010fc85".encode("utf-8"),
        b"mixed-\xe9-" + "é".encode(),
    ]
    for raw in samples:
        text = from_guest(raw)
        text.encode("utf-8")  # must be valid for JSON, with no lone surrogate
        assert to_guest(text) == raw


@pytest.mark.parametrize("path,code", [("relative", "invalid_path"), ("/a\x00b", "invalid_path"), ("/" + "x" * 5000, "invalid_path")])
def test_a_bad_path_is_refused(path, code):
    with pytest.raises(GuestFileError) as refused:
        clean_path(path)
    assert refused.value.code == code


def test_a_path_is_normalized_so_the_record_says_what_was_reached():
    assert clean_path("/a/../b//c/.") == "/b/c"
    assert clean_path("//etc") == "/etc"


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "line\nbreak", "tab\there", "x" * 256])
def test_a_bad_new_name_is_refused(name):
    with pytest.raises(GuestFileError):
        clean_name(name)


async def test_a_new_folder_gets_its_mode_and_its_parents_owner():
    guest = FakeGuest()
    guest.add_dir(b"/home")
    guest.add_dir(b"/home/lab", uid=1000, gid=1000)
    client, task = await connected(guest)
    async with client:
        await operations.make_directory(client, "/home/lab/new")
    node = guest.nodes[b"/home/lab/new"]
    assert (node.uid, node.gid) == (1000, 1000)
    assert stat.S_IMODE(node.mode) == 0o755
    await asyncio.wait_for(task, 1)


async def test_parents_are_made_and_an_existing_folder_is_not_an_error():
    guest = FakeGuest()
    guest.add_dir(b"/a")
    client, task = await connected(guest)
    async with client:
        await operations.make_directory(client, "/a/b/c/d", parents=True)
        with pytest.raises(GuestFileError) as refused:
            await operations.make_directory(client, "/a/b/c/d")
    assert b"/a/b/c/d" in guest.nodes
    assert refused.value.code == "already_exists"
    await asyncio.wait_for(task, 1)


async def test_nothing_is_created_inside_a_kernel_view():
    client, task = await connected(FakeGuest())
    async with client:
        with pytest.raises(GuestFileError):
            await operations.make_directory(client, "/proc/x")
        with pytest.raises(GuestFileError):
            await operations.create_file(client, "/sys/x")
    await asyncio.wait_for(task, 1)


async def test_a_move_refuses_to_replace_unless_told_to():
    guest = FakeGuest()
    guest.add_file(b"/a", b"new")
    guest.add_file(b"/b", b"old")
    client, task = await connected(guest)
    async with client:
        with pytest.raises(GuestFileError) as refused:
            await operations.move(client, "/a", "/b")
        assert refused.value.code == "already_exists"
        assert guest.nodes[b"/b"].data == b"old"
        await operations.move(client, "/a", "/b", replace=True)
    assert guest.nodes[b"/b"].data == b"new"
    assert b"/a" not in guest.nodes
    await asyncio.wait_for(task, 1)


async def test_a_move_leaves_no_second_name_behind():
    guest = FakeGuest()
    guest.add_file(b"/a", b"data")
    client, task = await connected(guest)
    async with client:
        await operations.move(client, "/a", "/b")
    assert b"/a" not in guest.nodes
    assert guest.nodes[b"/b"].data == b"data"
    await asyncio.wait_for(task, 1)


async def test_a_folder_cannot_move_into_itself_or_over_anything():
    guest = FakeGuest()
    guest.add_dir(b"/d")
    guest.add_dir(b"/e")
    client, task = await connected(guest)
    async with client:
        with pytest.raises(GuestFileError) as into_itself:
            await operations.move(client, "/d", "/d/inner")
        with pytest.raises(GuestFileError) as over:
            await operations.move(client, "/d", "/e", replace=True)
    assert into_itself.value.code == "invalid_path"
    assert over.value.code == "already_exists"
    await asyncio.wait_for(task, 1)


@pytest.mark.parametrize("path", ["/", "/etc", "/usr", "/proc/1", "/sys/kernel", "/dev/null"])
async def test_a_system_root_is_never_removed_or_moved(path):
    client, task = await connected(FakeGuest())
    async with client:
        with pytest.raises(GuestFileError) as refused:
            await delete_slice.delete_path(client, path, recursive=True)
        with pytest.raises(GuestFileError) as moved:
            await operations.move(client, path, "/tmp/away")
    assert refused.value.code == "protected_path"
    assert moved.value.code == "protected_path"
    await asyncio.wait_for(task, 1)


async def test_chmod_never_goes_through_a_link():
    guest = FakeGuest()
    guest.add_dir(b"/tree")
    guest.add_file(b"/tree/inside", mode=0o600)
    guest.add_file(b"/outside", mode=0o600)
    guest.add_link(b"/tree/escape", b"/outside")
    client, task = await connected(guest)
    async with client:
        await operations.set_attributes(client, "/tree", recursive=True, mode=0o640)
        with pytest.raises(GuestFileError) as refused:
            await operations.set_attributes(client, "/tree/escape", recursive=False, mode=0o777)
    assert stat.S_IMODE(guest.nodes[b"/tree/inside"].mode) == 0o640
    assert stat.S_IMODE(guest.nodes[b"/outside"].mode) == 0o600
    assert refused.value.code == "is_symlink"
    await asyncio.wait_for(task, 1)


async def test_a_second_slice_skips_what_already_matches():
    guest = FakeGuest()
    guest.add_dir(b"/tree", mode=0o750)
    for index in range(10):
        guest.add_file(b"/tree/f%d" % index, mode=0o640)
    client, task = await connected(guest)
    async with client:
        first = await operations.set_attributes(client, "/tree", recursive=True, mode=0o750)
        second = await operations.set_attributes(client, "/tree", recursive=True, mode=0o750)
    assert first["changed"] == 10
    assert second["changed"] == 0
    await asyncio.wait_for(task, 1)


async def test_a_tree_is_deleted_and_a_link_only_as_a_link():
    guest = FakeGuest()
    guest.add_dir(b"/keep")
    guest.add_file(b"/keep/precious", b"x")
    guest.add_dir(b"/tree")
    guest.add_dir(b"/tree/a")
    guest.add_file(b"/tree/a/1")
    guest.add_link(b"/tree/link", b"/keep")
    client, task = await connected(guest)
    async with client:
        result = await delete_slice.delete_path(client, "/tree", recursive=True)
    assert result["remaining"] is False
    assert result["failed"] == []
    assert b"/tree" not in guest.nodes
    assert guest.nodes[b"/keep/precious"].data == b"x"
    await asyncio.wait_for(task, 1)


async def test_a_large_tree_is_deleted_a_slice_at_a_time():
    guest = FakeGuest()
    guest.add_dir(b"/tree")
    for folder in range(5):
        guest.add_dir(b"/tree/d%d" % folder)
        for index in range(100):
            guest.add_file(b"/tree/d%d/f%d" % (folder, index))
    client, task = await connected(guest)
    slices = 0
    async with client:
        while True:
            slices += 1
            result = await delete_slice.delete_path(client, "/tree", recursive=True, max_deletions=120)
            if not result["remaining"]:
                break
    assert slices > 1
    assert b"/tree" not in guest.nodes
    await asyncio.wait_for(task, 1)


async def test_a_file_that_refuses_to_go_is_reported_not_retried_forever():
    async def script(packet_type, request_id, cursor):
        from tests._fake_sftp_server import status_body

        if packet_type == codec.FXP_REMOVE and cursor.string(9000).endswith(b"immutable"):
            return frame(status_body(request_id, 4, b"remove x: operation not permitted"))
        return None

    guest = FakeGuest(script=script)
    guest.add_dir(b"/tree")
    guest.add_file(b"/tree/immutable")
    guest.add_file(b"/tree/normal")
    client, task = await connected(guest)
    async with client:
        result = await delete_slice.delete_path(client, "/tree", recursive=True)
    assert result["failed"] == ["/tree/immutable"]
    assert result["failed_count"] == 1
    assert result["remaining"] is False
    assert b"/tree/normal" not in guest.nodes
    await asyncio.wait_for(task, 1)


async def test_deleting_a_non_empty_folder_needs_the_recursive_flag():
    guest = FakeGuest()
    guest.add_dir(b"/tree")
    guest.add_file(b"/tree/f")
    client, task = await connected(guest)
    async with client:
        with pytest.raises(GuestFileError) as refused:
            await delete_slice.delete_path(client, "/tree", recursive=False)
    assert refused.value.code == "directory_not_empty"
    await asyncio.wait_for(task, 1)


async def test_a_summary_counts_what_a_selection_holds():
    guest = FakeGuest()
    guest.add_dir(b"/tree")
    guest.add_dir(b"/tree/a")
    guest.add_file(b"/tree/a/1", b"xx")
    guest.add_file(b"/tree/2", b"xxx")
    client, task = await connected(guest)
    async with client:
        summary = await operations.summarize(client, ["/tree"])
    assert summary == {"files": 2, "folders": 2, "bytes": 5, "complete": True}
    await asyncio.wait_for(task, 1)


async def test_read_text_goes_to_end_of_file_whatever_size_is_stated():
    guest = FakeGuest()
    guest.add_file(b"/proc-like", b"model name: x\n", stated_size=0)
    client, task = await connected(guest)
    async with client:
        read = await text_edit.read_text(client, "/proc-like")
    assert read["text"] == "model name: x\n"
    await asyncio.wait_for(task, 1)


@pytest.mark.parametrize(
    "mode",
    [stat.S_IFCHR | 0o666, stat.S_IFIFO | 0o644, stat.S_IFSOCK | 0o644, stat.S_IFBLK | 0o600, stat.S_IFDIR | 0o755],
)
async def test_only_a_regular_file_is_opened(mode):
    guest = FakeGuest()
    guest.nodes[b"/special"] = type(guest.nodes[b"/"])(mode=mode)
    client, task = await connected(guest)
    async with client:
        with pytest.raises(GuestFileError) as refused:
            await text_edit.read_text(client, "/special")
    assert refused.value.code == "not_regular_file"
    assert codec.FXP_OPEN not in guest.received
    await asyncio.wait_for(task, 1)


async def test_a_file_stated_small_but_endless_stops_at_the_cap(monkeypatch):
    monkeypatch.setattr(text_edit, "MAX_TEXT_BYTES", 100_000)
    guest = FakeGuest()
    guest.add_file(b"/huge", b"x" * 300_000)
    client, task = await connected(guest)
    async with client:
        with pytest.raises(GuestFileError) as refused:
            await text_edit.read_text(client, "/huge")
    assert refused.value.code == "too_large"
    await asyncio.wait_for(task, 1)


async def test_binary_is_not_text_and_latin1_opens_read_only():
    guest = FakeGuest()
    guest.add_file(b"/bin", b"ab\x00cd")
    guest.add_file(b"/latin", b"caf\xe9\n")
    client, task = await connected(guest)
    async with client:
        with pytest.raises(GuestFileError) as refused:
            await text_edit.read_text(client, "/bin")
        read = await text_edit.read_text(client, "/latin")
    assert refused.value.code == "not_text"
    assert read["read_only"] is True
    await asyncio.wait_for(task, 1)


async def test_saving_keeps_mode_and_owner_and_leaves_no_temp():
    guest = FakeGuest()
    guest.add_dir(b"/etc")
    guest.add_file(b"/etc/app.conf", b"old\n", mode=0o640, uid=33, gid=33)
    client, task = await connected(guest)
    async with client:
        read = await text_edit.read_text(client, "/etc/app.conf")
        saved = await text_edit.write_text(client, "/etc/app.conf", "new\n", read["etag"])
    node = guest.nodes[b"/etc/app.conf"]
    assert node.data == b"new\n"
    assert stat.S_IMODE(node.mode) == 0o640
    assert (node.uid, node.gid) == (33, 33)
    assert saved["etag"] == text_edit.etag_of(b"new\n")
    assert [path for path in guest.nodes if b".part" in path] == []
    await asyncio.wait_for(task, 1)


async def test_saving_over_a_change_is_refused_even_at_the_same_size():
    guest = FakeGuest()
    guest.add_file(b"/f", b"aaaa")
    client, task = await connected(guest)
    async with client:
        read = await text_edit.read_text(client, "/f")
        guest.nodes[b"/f"].data = b"bbbb"
        with pytest.raises(GuestFileError) as refused:
            await text_edit.write_text(client, "/f", "cccc", read["etag"])
    assert refused.value.code == "etag_mismatch"
    assert guest.nodes[b"/f"].data == b"bbbb"
    await asyncio.wait_for(task, 1)


async def test_saving_through_a_link_edits_the_target_and_keeps_the_link():
    guest = FakeGuest()
    guest.add_dir(b"/etc")
    guest.add_file(b"/etc/real.conf", b"old")
    guest.add_link(b"/etc/link.conf", b"/etc/real.conf")
    client, task = await connected(guest)
    async with client:
        read = await text_edit.read_text(client, "/etc/link.conf")
        await text_edit.write_text(client, "/etc/link.conf", "new", read["etag"])
    assert stat.S_ISLNK(guest.nodes[b"/etc/link.conf"].mode)
    assert guest.nodes[b"/etc/real.conf"].data == b"new"
    await asyncio.wait_for(task, 1)


async def test_a_mounted_file_asks_before_writing_through_it():
    async def script(packet_type, request_id, cursor):
        from tests._fake_sftp_server import status_body

        if packet_type == codec.FXP_EXTENDED and cursor.string(256) == b"posix-rename@openssh.com":
            return frame(status_body(request_id, 4, b"rename x: device or resource busy"))
        return None

    guest = FakeGuest(script=script)
    guest.add_dir(b"/etc")
    guest.add_file(b"/etc/hosts", b"old")
    client, task = await connected(guest)
    async with client:
        read = await text_edit.read_text(client, "/etc/hosts")
        with pytest.raises(GuestFileError) as refused:
            await text_edit.write_text(client, "/etc/hosts", "new", read["etag"])
        assert refused.value.code == "atomic_replace_unavailable"
        assert guest.nodes[b"/etc/hosts"].data == b"old"
        assert [path for path in guest.nodes if b".part" in path] == []
        await text_edit.write_text(client, "/etc/hosts", "new", read["etag"], allow_in_place=True)
    assert guest.nodes[b"/etc/hosts"].data == b"new"
    await asyncio.wait_for(task, 1)


@pytest.mark.parametrize(
    "payload,mime",
    [
        (b"\x89PNG\r\n\x1a\nrest", "image/png"),
        (b"\xff\xd8\xff\xe0rest", "image/jpeg"),
        (b"GIF89arest", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBPrest", "image/webp"),
    ],
)
def test_an_image_is_named_by_its_bytes(payload, mime):
    assert text_edit.sniff_image(payload) == mime


@pytest.mark.parametrize("payload", [b"<svg onload=alert(1)>", b"<html><script>", b"", b"plain text"])
def test_anything_else_is_not_an_image(payload):
    assert text_edit.sniff_image(payload) is None
