"""A folder as a streamed .tar.gz, built from a walk over SFTP.

A person extracts this archive on their own machine, and a guest that may be hostile wrote its
contents. Symlinks kept as they are make a tar that writes outside its directory on extract: a link
`x -> /etc` followed by `x/passwd`. So a name is validated part by part, a duplicate is dropped,
ownership and special bits are removed, a link is kept only when it stays inside the archive, and
what was left out is listed in SKIPPED.txt.
"""

import itertools
import logging
import posixpath
import stat as stat_module
import re
import tarfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import aclosing
from typing import AsyncIterator, List, Optional, Set, Tuple

from app.core.blocking import run_blocking

from . import sftp_codec as codec
from .errors import GuestFileError, too_large
from .paths import MAX_PATH_BYTES, from_guest, is_pseudo, is_safe_entry, to_guest
from .sftp_client import Budget, BudgetSpent, SftpClient, is_directory, is_symlink

logger = logging.getLogger(__name__)

_BLOCK = 512
_COMPRESS_BYTES = 1024 * 1024
_MAX_SKIPPED_LINES = 1_000
_MAX_ENTRIES = 200_000
_MAX_WALK_SECONDS = 6 * 3600.0
_MAX_HELD_BYTES = 128 * 1024 * 1024
# An extractor on Windows splits on a backslash, so `..\\..\\evil.bat` is a traversal there.
_UNSAFE_NAME = re.compile(rb"[\x00-\x1f\x7f\\]")
_compress_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="file_archive")


class _Gzip:
    """gzip level 1, off the event loop: a level-1 stream still costs a core per transfer."""

    def __init__(self) -> None:
        self._engine = zlib.compressobj(1, zlib.DEFLATED, zlib.MAX_WBITS | 16)
        self._pending = bytearray()

    async def add(self, data: bytes) -> Optional[bytes]:
        self._pending += data
        if len(self._pending) < _COMPRESS_BYTES:
            return None
        block = bytes(self._pending)
        self._pending.clear()
        return await run_blocking(
            self._engine.compress, block, timeout=30, label="file_archive_gzip", executor=_compress_pool
        )

    async def finish(self) -> bytes:
        block = bytes(self._pending)
        self._pending.clear()
        head = await run_blocking(
            self._engine.compress, block, timeout=30, label="file_archive_gzip", executor=_compress_pool
        )
        return head + self._engine.flush()


def _header(name: bytes, *, kind: bytes, size: int, mode: int, mtime: int, link: bytes = b"") -> bytes:
    info = tarfile.TarInfo(name.decode("utf-8", "surrogateescape"))
    info.type = kind
    info.size = size
    info.mode = mode & 0o777
    info.mtime = mtime
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.linkname = link.decode("utf-8", "surrogateescape")
    return info.tobuf(tarfile.GNU_FORMAT, "utf-8", "surrogateescape")


def _link_stays_inside(entry: bytes, target: bytes) -> bool:
    """Whether a link at archive path `entry` resolves inside the archive's own top folder.

    important: `..` only as leading components. The check is lexical, and two links that each pass
    it can chain out: `up -> ../../..` and then `esc -> up/../../../../secret` walks through the
    first one's real location. With `..` only at the front, a link cannot step back through another
    link. A NUL or a backslash is refused because a reader may cut or split on them.
    """
    if target.startswith(b"/") or not target or b"\x00" in target or b"\\" in target:
        return False
    parts = target.split(b"/")
    climbing = list(itertools.takewhile(lambda part: part == b"..", parts))
    if b".." in parts[len(climbing):]:
        return False
    top = entry.split(b"/", 1)[0]
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(entry), target))
    return resolved == top or resolved.startswith(top + b"/")


async def tar_gz_stream(
    guest: SftpClient, roots: List[str], *, max_bytes: int
) -> AsyncIterator[bytes]:
    gzip = _Gzip()
    seen: Set[bytes] = set()
    skipped: List[str] = []
    remaining_bytes = max_bytes
    walk = Budget(entries=_MAX_ENTRIES, seconds=_MAX_WALK_SECONDS, held_bytes=_MAX_HELD_BYTES)

    def skip(path: bytes, why: str) -> None:
        if len(skipped) < _MAX_SKIPPED_LINES:
            skipped.append(f"{from_guest(path)}: {why}")

    async def emit(data: bytes) -> AsyncIterator[bytes]:
        out = await gzip.add(data)
        if out:
            yield out

    stack: List[Tuple[bytes, bytes, codec.Attrs]] = []
    for root in reversed(roots):
        raw = to_guest(root)
        name = raw.rstrip(b"/").rsplit(b"/", 1)[-1] or b"root"
        attrs = await guest.lstat(raw)
        # A link chosen for download is followed; a link inside the tree is not.
        if is_symlink(attrs):
            raw = await guest.realpath(raw)
            attrs = await guest.lstat(raw)
        stack.append((raw, name, attrs))

    while stack:
        path, arcname, attrs = stack.pop()
        if arcname in seen:
            skip(path, "duplicate name")
            continue
        seen.add(arcname)
        if is_pseudo(from_guest(path)):
            skip(path, "system path")
            continue
        mode = attrs.mode or 0
        mtime = attrs.mtime or 0
        if is_directory(attrs):
            async for out in emit(_header(arcname + b"/", kind=tarfile.DIRTYPE, size=0, mode=mode, mtime=mtime)):
                yield out
            try:
                async with aclosing(guest.iter_directory(path, walk)) as batches:
                    async for batch in batches:
                        for item in sorted(batch, key=lambda entry: entry.filename, reverse=True):
                            child = path.rstrip(b"/") + b"/" + item.filename
                            if not is_safe_entry(item.filename) or _UNSAFE_NAME.search(item.filename):
                                skip(path, "unsafe entry name")
                                continue
                            if len(child) > MAX_PATH_BYTES:
                                skip(path, "path too long")
                                continue
                            walk.hold(len(child) + len(arcname) + len(item.filename) + 1)
                            stack.append((child, arcname + b"/" + item.filename, item.attrs))
            except GuestFileError as failure:
                if failure.status >= 500:
                    raise
                skip(path, "could not be read")
            except BudgetSpent:
                raise too_large(max_bytes) from None
        elif is_symlink(attrs):
            try:
                target = await guest.readlink(path)
            except GuestFileError as failure:
                if failure.status >= 500:
                    raise
                skip(path, "link could not be read")
                continue
            if not _link_stays_inside(arcname, target):
                skip(path, "link points outside this archive")
                continue
            async for out in emit(_header(arcname, kind=tarfile.SYMTYPE, size=0, mode=0o777, mtime=mtime, link=target)):
                yield out
        elif stat_module.S_ISREG(mode):
            stated = attrs.size or 0
            if stated > remaining_bytes:
                raise too_large(max_bytes)
            try:
                handle = await guest.open(path, codec.PFLAG_READ)
            except GuestFileError as failure:
                if failure.status >= 500:
                    raise
                skip(path, "could not be read")
                continue
            async for out in emit(_header(arcname, kind=tarfile.REGTYPE, size=stated, mode=mode, mtime=mtime)):
                yield out
            written = 0
            async with aclosing(guest.read_stream(handle, limit=stated)) as pieces:
                async for piece in pieces:
                    written += len(piece)
                    async for out in emit(piece):
                        yield out
            await guest.close(handle)
            # A file that shrank mid-read still owes its header the bytes it promised, or every
            # entry after it would be read at the wrong offset.
            if written < stated:
                skip(path, "changed while it was read")
                async for out in _pad_zeroes(gzip, stated - written):
                    yield out
            padding = -stated % _BLOCK
            if padding:
                async for out in emit(b"\0" * padding):
                    yield out
            remaining_bytes -= stated
        else:
            skip(path, "not a regular file")

    if skipped:
        listing = ("\n".join(skipped) + "\n").encode("utf-8", "surrogateescape")
        async for out in emit(_header(b"SKIPPED.txt", kind=tarfile.REGTYPE, size=len(listing), mode=0o644, mtime=0)):
            yield out
        async for out in emit(listing + b"\0" * (-len(listing) % _BLOCK)):
            yield out
    async for out in emit(b"\0" * (_BLOCK * 2)):
        yield out
    tail = await gzip.finish()
    if tail:
        yield tail


async def _pad_zeroes(gzip: _Gzip, count: int) -> AsyncIterator[bytes]:
    zeroes = b"\0" * 65536
    while count > 0:
        step = min(count, len(zeroes))
        out = await gzip.add(zeroes[:step])
        count -= step
        if out:
            yield out
