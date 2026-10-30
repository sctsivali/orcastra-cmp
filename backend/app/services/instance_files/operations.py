"""What a person can do to a guest's files, each as one short conversation over one connection."""

import asyncio
import re
import stat as stat_module
import time
from contextlib import aclosing
from typing import Any, Dict, List, Optional, Tuple

from . import sftp_codec as codec
from .errors import GuestFileError, already_exists, invalid_path, not_regular_file
from .paths import MAX_PATH_BYTES, clean_name, from_guest, is_pseudo, is_safe_entry, join, refuse_protected, to_guest
from .sftp_client import Budget, BudgetSpent, SftpClient, is_directory, is_symlink
from .uploads_temp import is_expired_temp, is_upload_temp

# The interface renders 20,000 rows without strain; a directory past that is a dump, not a listing.
MAX_LIST_ENTRIES = 20_000
# Names are guest-controlled, so count their bytes too: 20,000 entries of 8 KiB each is 160 MB.
MAX_LIST_BYTES = 16 * 1024 * 1024
MAX_RESOLVED_LINKS = 2_000
MAX_STALE_LISTED = 256
LIST_SECONDS = 12.0
WALK_ENTRIES = 200_000
_PARALLEL = 32
# JSON numbers lose integers past 2^53, and a guest can state any 64-bit size.
_MAX_SAFE_INTEGER = 2**53 - 1
_LONGNAME = re.compile(rb"^\S+\s+\d+\s+(\S+)\s+(\S+)\s")
_ACCOUNT = re.compile(rb"^[A-Za-z0-9_.][A-Za-z0-9_.-]{0,31}$")

_TYPES = {
    stat_module.S_IFREG: "file",
    stat_module.S_IFDIR: "directory",
    stat_module.S_IFLNK: "symlink",
    stat_module.S_IFIFO: "fifo",
    stat_module.S_IFSOCK: "socket",
    stat_module.S_IFCHR: "char",
    stat_module.S_IFBLK: "block",
}


def entry_type(attrs: codec.Attrs) -> str:
    return _TYPES.get(stat_module.S_IFMT(attrs.mode or 0), "unknown")


def _account(value: bytes) -> Optional[str]:
    return value.decode("ascii") if _ACCOUNT.match(value) else None


def _owner_names(longname: bytes) -> Tuple[Optional[str], Optional[str]]:
    """Owner and group as the guest's own `ls -l` line spells them. Display only, never trusted."""
    match = _LONGNAME.match(longname[:256])
    return (_account(match.group(1)), _account(match.group(2))) if match else (None, None)


def _entry(name: str, attrs: codec.Attrs, longname: bytes = b"") -> Dict[str, Any]:
    owner, group = _owner_names(longname)
    return {
        "name": name,
        "type": entry_type(attrs),
        "size": min(attrs.size or 0, _MAX_SAFE_INTEGER),
        "mode": stat_module.S_IMODE(attrs.mode or 0),
        "uid": attrs.uid,
        "gid": attrs.gid,
        "owner": owner,
        "group": group,
        "mtime": attrs.mtime,
        "link_target": None,
        "target_type": None,
    }


async def _resolve_link(guest: SftpClient, parent: str, entry: Dict[str, Any], gate: asyncio.Semaphore) -> None:
    path = to_guest(join(parent, entry["name"]))
    async with gate:
        try:
            entry["link_target"] = from_guest(await guest.readlink(path))
            entry["target_type"] = entry_type(await guest.stat(path))
        except GuestFileError as failure:
            if failure.status >= 500:
                raise
            # A dangling link or a loop is a fact about the guest, and the row still shows it.
            entry["target_type"] = "broken"


async def _free_space(guest: SftpClient, path: bytes) -> Tuple[Optional[int], Optional[int]]:
    try:
        vfs = await guest.statvfs(path)
    except GuestFileError as failure:
        if failure.status >= 500 and failure.code != "unsupported_server":
            raise
        return None, None
    unit = vfs.fragment_size or vfs.block_size
    return min(vfs.blocks_available * unit, _MAX_SAFE_INTEGER), min(vfs.blocks * unit, _MAX_SAFE_INTEGER)


async def list_directory(guest: SftpClient, path: str) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    stale: List[str] = []
    truncated = False
    dropped = 0
    held = 0
    now = time.time()
    # Twice the rows, so names that are hidden or dropped cost what they cost and no more.
    budget = Budget(entries=MAX_LIST_ENTRIES * 2, seconds=LIST_SECONDS, held_bytes=MAX_LIST_BYTES * 2)
    try:
        async with aclosing(guest.iter_directory(to_guest(path), budget)) as batches:
            async for batch in batches:
                for item in batch:
                    if not is_safe_entry(item.filename):
                        dropped += 1
                        continue
                    name = from_guest(item.filename)
                    if is_upload_temp(name):
                        if is_expired_temp(name, now) and len(stale) < MAX_STALE_LISTED:
                            stale.append(name)
                        continue
                    # One past the cap before calling it truncated: a folder of exactly the limit is whole.
                    if len(entries) >= MAX_LIST_ENTRIES or held >= MAX_LIST_BYTES:
                        truncated = True
                        break
                    entries.append(_entry(name, item.attrs, item.longname))
                    held += len(item.filename) + len(item.longname)
                if truncated:
                    break
    except BudgetSpent:
        truncated = True
    gate = asyncio.Semaphore(_PARALLEL)
    links = [entry for entry in entries if entry["type"] == "symlink"][:MAX_RESOLVED_LINKS]
    await asyncio.gather(*(_resolve_link(guest, path, entry, gate) for entry in links))
    free, total = await _free_space(guest, to_guest(path))
    return {
        "path": path,
        "entries": entries,
        "truncated": truncated,
        "dropped": dropped,
        "stale_uploads": stale,
        "free_bytes": free,
        "total_bytes": total,
    }


async def stat_path(guest: SftpClient, path: str) -> Dict[str, Any]:
    raw = to_guest(path)
    attrs = await guest.lstat(raw)
    entry = _entry(path.rsplit("/", 1)[-1] or "/", attrs)
    realpath = path
    if is_symlink(attrs):
        entry["link_target"] = from_guest(await guest.readlink(raw))
        try:
            entry["target_type"] = entry_type(await guest.stat(raw))
            realpath = from_guest(await guest.realpath(raw))
        except GuestFileError as failure:
            if failure.status >= 500:
                raise
            entry["target_type"] = "broken"
    return {"entry": entry, "realpath": realpath}


async def _inherit_owner(guest: SftpClient, path: bytes, parent: bytes, *, mode: int) -> None:
    """Give a new object its folder's owner: this service writes as root, and the person does not."""
    folder = await guest.lstat(parent)
    handle, _ = await guest.open_unchanged(path, codec.PFLAG_READ)
    try:
        await guest.fsetstat(handle, codec.encode_attrs(uid=folder.uid, gid=folder.gid, mode=mode))
    finally:
        await guest.close(handle)


async def make_directory(guest: SftpClient, path: str, *, parents: bool = False) -> Dict[str, Any]:
    if is_pseudo(path):
        raise invalid_path("Nothing can be created under this system path.")
    pending: List[str] = [path]
    if parents:
        cursor = path.rsplit("/", 1)[0]
        while cursor:
            try:
                attrs = await guest.lstat(to_guest(cursor))
            except GuestFileError as failure:
                if failure.code != "not_found":
                    raise
                pending.insert(0, cursor)
                cursor = cursor.rsplit("/", 1)[0]
                continue
            if not is_directory(attrs) and not is_symlink(attrs):
                raise GuestFileError("not_a_directory", 409, "This path is not a folder.")
            break
    for folder in pending:
        raw = to_guest(folder)
        try:
            await guest.mkdir(raw)
        except GuestFileError as failure:
            if not (parents and failure.code == "already_exists" and folder != path):
                raise
            continue
        # note: LXD's MKDIR ignores the mode it is given (0750 arrived as 0755 in the lab).
        await _inherit_owner(guest, raw, to_guest(folder.rsplit("/", 1)[0] or "/"), mode=0o755)
    return await stat_path(guest, path)


async def create_file(guest: SftpClient, path: str) -> Dict[str, Any]:
    if is_pseudo(path):
        raise invalid_path("Nothing can be created under this system path.")
    raw = to_guest(path)
    folder = await guest.lstat(to_guest(path.rsplit("/", 1)[0] or "/"))
    handle = await guest.open(
        raw, codec.PFLAG_WRITE | codec.PFLAG_CREAT | codec.PFLAG_EXCL, mode=0o644
    )
    try:
        await guest.fsetstat(handle, codec.encode_attrs(uid=folder.uid, gid=folder.gid, mode=0o644))
    finally:
        await guest.close(handle)
    return await stat_path(guest, path)


async def _move_without_replacing(guest: SftpClient, source: bytes, target: bytes) -> None:
    """Fails with already_exists when `target` exists, without a window where it could be lost.

    A hard link either appears or fails, which plain RENAME does not promise on LXD: it replaces a
    file silently. A filesystem without hard links falls back to look-then-rename, whose race is
    only against another writer of the same name.
    """
    try:
        await guest.link(source, target)
    except GuestFileError as failure:
        if failure.code not in ("operation_not_permitted", "unsupported_server", "guest_io_error"):
            raise
        try:
            await guest.lstat(target)
        except GuestFileError as probe:
            if probe.code != "not_found":
                raise
        else:
            raise already_exists() from None
        await guest.rename(source, target)
        return
    await guest.remove(source)


async def move(guest: SftpClient, source: str, target: str, *, replace: bool = False) -> Dict[str, Any]:
    refuse_protected([source])
    if is_pseudo(target):
        raise invalid_path("Nothing can be moved under this system path.")
    if source == target:
        raise already_exists()
    if target.startswith(source + "/"):
        raise invalid_path("A folder cannot be moved into itself.")
    clean_name(target.rsplit("/", 1)[-1])
    raw_source, raw_target = to_guest(source), to_guest(target)
    attrs = await guest.lstat(raw_source)
    if is_directory(attrs):
        # Refused by LXD over anything that exists, so `replace` cannot lose a folder's contents.
        await guest.rename(raw_source, raw_target)
    elif replace:
        await guest.replace(raw_source, raw_target)
    else:
        await _move_without_replacing(guest, raw_source, raw_target)
    return await stat_path(guest, target)


async def resolve_regular(guest: SftpClient, path: str) -> Tuple[bytes, codec.Attrs]:
    """Where a read of `path` lands, refusing anything that is not a regular file.

    important: `lstat` of the resolved path, before any open. Opening a fifo can hang the agent and
    a character device such as /dev/zero never ends, and LXD's own Files API calls both `file`.
    """
    raw = to_guest(path)
    attrs = await guest.lstat(raw)
    if is_symlink(attrs):
        raw = await guest.realpath(raw)
        attrs = await guest.lstat(raw)
    if not stat_module.S_ISREG(attrs.mode or 0):
        raise not_regular_file()
    return raw, attrs


async def set_attributes(
    guest: SftpClient,
    path: str,
    *,
    recursive: bool,
    mode: Optional[int] = None,
    uid: Optional[int] = None,
    gid: Optional[int] = None,
    budget_seconds: float = 20.0,
) -> Dict[str, Any]:
    """chmod or chown. A slice stops at its budget, and running it again skips what already matches.

    important: every change goes through `open_unchanged`, so a link is never followed, whether the
    tree held it from the start or somebody swapped it in during the walk. Pipes, sockets and
    devices are skipped: opening a pipe can hang the agent.
    """
    refuse_protected([path])
    change = codec.encode_attrs(mode=mode, uid=uid, gid=gid)
    raw = to_guest(path)
    root = await guest.lstat(raw)
    if is_symlink(root):
        raise GuestFileError("is_symlink", 409, "A link has no permissions of its own. Change its target.")

    def matches(attrs: codec.Attrs) -> bool:
        same_mode = mode is None or stat_module.S_IMODE(attrs.mode or 0) == mode
        same_owner = uid is None or (attrs.uid == uid and attrs.gid == gid)
        return same_mode and same_owner

    changed = skipped = 0
    remaining = False
    gate = asyncio.Semaphore(_PARALLEL)

    async def apply(target: bytes) -> None:
        nonlocal changed, skipped
        async with gate:
            try:
                handle, _ = await guest.open_unchanged(target, codec.PFLAG_READ)
                try:
                    await guest.fsetstat(handle, change)
                finally:
                    await guest.close(handle)
                changed += 1
            except GuestFileError as failure:
                if failure.status >= 500:
                    raise
                skipped += 1

    if not matches(root):
        await apply(raw)
    if recursive and is_directory(root):
        budget = Budget(entries=WALK_ENTRIES, seconds=budget_seconds)
        stack = [raw]
        try:
            while stack:
                folder = stack.pop()
                todo: List[bytes] = []
                async with aclosing(guest.iter_directory(folder, budget)) as batches:
                    async for batch in batches:
                        for item in batch:
                            if not is_safe_entry(item.filename) or is_symlink(item.attrs):
                                continue
                            child = folder.rstrip(b"/") + b"/" + item.filename
                            if len(child) > MAX_PATH_BYTES:
                                skipped += 1
                                continue
                            budget.hold(len(child))
                            if is_directory(item.attrs):
                                stack.append(child)
                            elif not stat_module.S_ISREG(item.attrs.mode or 0):
                                skipped += 1
                                continue
                            if not matches(item.attrs):
                                todo.append(child)
                await asyncio.gather(*(apply(child) for child in todo))
        except BudgetSpent:
            remaining = True
    return {"changed": changed, "skipped": skipped, "remaining": remaining}


async def summarize(
    guest: SftpClient, paths: List[str], *, budget_seconds: float = 12.0, max_entries: int = 200_000
) -> Dict[str, Any]:
    """How much a selection holds, for a delete prompt and for refusing an oversized archive."""
    files = folders = 0
    total = 0
    complete = True
    budget = Budget(entries=max_entries, seconds=budget_seconds)
    stack: List[bytes] = []
    for path in paths:
        raw = to_guest(path)
        attrs = await guest.lstat(raw)
        if is_directory(attrs):
            folders += 1
            stack.append(raw)
        else:
            files += 1
            total += attrs.size or 0
    try:
        while stack:
            folder = stack.pop()
            try:
                async with aclosing(guest.iter_directory(folder, budget)) as batches:
                    async for batch in batches:
                        for item in batch:
                            if not is_safe_entry(item.filename):
                                continue
                            if is_directory(item.attrs):
                                folders += 1
                                child = folder.rstrip(b"/") + b"/" + item.filename
                                if len(child) > MAX_PATH_BYTES:
                                    complete = False
                                    continue
                                budget.hold(len(child))
                                stack.append(child)
                            else:
                                files += 1
                                if stat_module.S_ISREG(item.attrs.mode or 0):
                                    total += min(item.attrs.size or 0, _MAX_SAFE_INTEGER)
            except GuestFileError as failure:
                if failure.status >= 500:
                    raise
                complete = False
    except BudgetSpent:
        complete = False
    return {"files": files, "folders": folders, "bytes": total, "complete": complete}
