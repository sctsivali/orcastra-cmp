"""SFTP version 3 packets, with every length checked before it is trusted.

important: the peer of this codec is a guest. On a virtual machine the bytes come from lxd-agent,
which root inside the guest can replace, and LXD copies them to us unread. So a length field is an
attacker's number. Nothing here allocates from one before comparing it to the bytes actually held,
and the packet ceiling is enforced by the caller before the body is read at all. That is why this
is not paramiko: its `_read_packet` reads whatever 32-bit size the peer states.

Pure functions over bytes, no I/O, so the hostile cases can be tested as a table.
"""

import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .errors import protocol_error

SFTP_VERSION = 3

# Largest reply accepted. A READ answers at most 32 KiB and LXD batches 100 names, so honest traffic
# sits far below this, and an entry of 4 KiB name plus 4 KiB longname still fits.
MAX_PACKET_BYTES = 1024 * 1024
MAX_HANDLE_BYTES = 256
MAX_NAME_BYTES = 4096
MAX_EXTENSIONS = 16

FXP_INIT = 1
FXP_VERSION = 2
FXP_OPEN = 3
FXP_CLOSE = 4
FXP_READ = 5
FXP_WRITE = 6
FXP_LSTAT = 7
FXP_FSTAT = 8
FXP_SETSTAT = 9
FXP_FSETSTAT = 10
FXP_OPENDIR = 11
FXP_READDIR = 12
FXP_REMOVE = 13
FXP_MKDIR = 14
FXP_RMDIR = 15
FXP_REALPATH = 16
FXP_STAT = 17
FXP_RENAME = 18
FXP_READLINK = 19
FXP_STATUS = 101
FXP_HANDLE = 102
FXP_DATA = 103
FXP_NAME = 104
FXP_ATTRS = 105
FXP_EXTENDED = 200
FXP_EXTENDED_REPLY = 201

FX_OK = 0
FX_EOF = 1

PFLAG_READ = 0x01
PFLAG_WRITE = 0x02
PFLAG_CREAT = 0x08
PFLAG_TRUNC = 0x10
PFLAG_EXCL = 0x20

ATTR_SIZE = 0x00000001
ATTR_UIDGID = 0x00000002
ATTR_PERMISSIONS = 0x00000004
ATTR_ACMODTIME = 0x00000008
ATTR_EXTENDED = 0x80000000
_KNOWN_ATTRS = ATTR_SIZE | ATTR_UIDGID | ATTR_PERMISSIONS | ATTR_ACMODTIME | ATTR_EXTENDED


@dataclass(frozen=True)
class Attrs:
    size: Optional[int] = None
    uid: Optional[int] = None
    gid: Optional[int] = None
    mode: Optional[int] = None
    atime: Optional[int] = None
    mtime: Optional[int] = None


@dataclass(frozen=True)
class Name:
    filename: bytes
    longname: bytes
    attrs: Attrs


@dataclass(frozen=True)
class StatVfs:
    block_size: int
    fragment_size: int
    blocks: int
    blocks_free: int
    blocks_available: int


class Cursor:
    """Reads one packet body. Every read refuses to pass the end of the bytes held."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._at = 0

    @property
    def remaining(self) -> int:
        return len(self._data) - self._at

    def u32(self) -> int:
        if self.remaining < 4:
            raise protocol_error("short u32")
        value = struct.unpack_from(">I", self._data, self._at)[0]
        self._at += 4
        return value

    def u64(self) -> int:
        if self.remaining < 8:
            raise protocol_error("short u64")
        value = struct.unpack_from(">Q", self._data, self._at)[0]
        self._at += 8
        return value

    def string(self, limit: int) -> bytes:
        length = self.u32()
        if length > limit:
            raise protocol_error("string over its limit")
        if length > self.remaining:
            raise protocol_error("string past the packet")
        value = self._data[self._at : self._at + length]
        self._at += length
        return value


def string(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


def encode_init() -> bytes:
    body = struct.pack(">BI", FXP_INIT, SFTP_VERSION)
    return struct.pack(">I", len(body)) + body


def encode_request(packet_type: int, request_id: int, payload: bytes = b"") -> bytes:
    body = struct.pack(">BI", packet_type, request_id) + payload
    return struct.pack(">I", len(body)) + body


def encode_attrs(
    *,
    size: Optional[int] = None,
    uid: Optional[int] = None,
    gid: Optional[int] = None,
    mode: Optional[int] = None,
) -> bytes:
    flags = 0
    tail = b""
    if size is not None:
        flags |= ATTR_SIZE
        tail += struct.pack(">Q", size)
    if uid is not None and gid is not None:
        flags |= ATTR_UIDGID
        tail += struct.pack(">II", uid, gid)
    if mode is not None:
        flags |= ATTR_PERMISSIONS
        tail += struct.pack(">I", mode)
    return struct.pack(">I", flags) + tail


def parse_frame_length(prefix: bytes) -> int:
    """The body length a frame states, refused before any of the body is read."""
    length = struct.unpack(">I", prefix)[0]
    if length < 1:
        raise protocol_error("empty frame")
    if length > MAX_PACKET_BYTES:
        raise protocol_error("frame over the ceiling")
    return length


def decode_version(body: bytes) -> Dict[bytes, bytes]:
    """The extensions a server offers. `body` is the VERSION packet after its type byte."""
    cursor = Cursor(body)
    version = cursor.u32()
    if version != SFTP_VERSION:
        raise protocol_error("version is not 3")
    extensions: Dict[bytes, bytes] = {}
    while cursor.remaining:
        if len(extensions) >= MAX_EXTENSIONS:
            raise protocol_error("too many extensions")
        name = cursor.string(256)
        extensions[name] = cursor.string(256)
    return extensions


def _skip_attr_extensions(cursor: Cursor) -> None:
    count = cursor.u32()
    if count > MAX_EXTENSIONS:
        raise protocol_error("too many attribute extensions")
    for _ in range(count):
        cursor.string(256)
        cursor.string(4096)


def decode_attrs(cursor: Cursor) -> Attrs:
    flags = cursor.u32()
    if flags & ~_KNOWN_ATTRS:
        raise protocol_error("unknown attribute flag")
    size = cursor.u64() if flags & ATTR_SIZE else None
    uid = gid = None
    if flags & ATTR_UIDGID:
        uid, gid = cursor.u32(), cursor.u32()
    mode = cursor.u32() if flags & ATTR_PERMISSIONS else None
    atime = mtime = None
    if flags & ATTR_ACMODTIME:
        atime, mtime = cursor.u32(), cursor.u32()
    if flags & ATTR_EXTENDED:
        _skip_attr_extensions(cursor)
    return Attrs(size=size, uid=uid, gid=gid, mode=mode, atime=atime, mtime=mtime)


def decode_status(body: bytes) -> Tuple[int, bytes]:
    cursor = Cursor(body)
    code = cursor.u32()
    # Some servers omit the message and language, which v3 tolerates.
    message = cursor.string(1024) if cursor.remaining else b""
    return code, message


def decode_handle(body: bytes) -> bytes:
    return Cursor(body).string(MAX_HANDLE_BYTES)


def decode_data(body: bytes, requested: int) -> bytes:
    cursor = Cursor(body)
    data = cursor.string(MAX_PACKET_BYTES)
    if len(data) > requested:
        raise protocol_error("more data than asked for")
    return data


def decode_names(body: bytes) -> List[Name]:
    cursor = Cursor(body)
    count = cursor.u32()
    # An entry costs at least 12 bytes (two empty strings and flags), so a count the packet could
    # not hold is refused here rather than discovered after building a list for it.
    if count * 12 > cursor.remaining:
        raise protocol_error("name count past the packet")
    names: List[Name] = []
    for _ in range(count):
        filename = cursor.string(MAX_NAME_BYTES)
        longname = cursor.string(MAX_NAME_BYTES * 2)
        names.append(Name(filename, longname, decode_attrs(cursor)))
    return names


def decode_single_name(body: bytes) -> bytes:
    names = decode_names(body)
    if len(names) != 1:
        raise protocol_error("expected exactly one name")
    return names[0].filename


def decode_statvfs(body: bytes) -> StatVfs:
    cursor = Cursor(body)
    block_size, fragment_size, blocks, blocks_free, blocks_available = (
        cursor.u64(), cursor.u64(), cursor.u64(), cursor.u64(), cursor.u64(),
    )
    return StatVfs(block_size, fragment_size, blocks, blocks_free, blocks_available)
