"""A guest for the SFTP client: an in-memory filesystem speaking v3 over a socket pair.

It behaves like LXD's server where the lab proved it (32 KiB reads, 100 names a batch, RENAME
replacing a file, MKDIR ignoring its mode, Go's error text). `script` lets a test replace the
answer to any packet, which is how a hostile guest is written.
"""

import asyncio
import socket
import stat
import struct
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, Optional, Tuple

from app.services.instance_files import sftp_codec as codec
from app.services.instance_files.sftp_client import SftpClient

FAILURE = 4


@dataclass
class Node:
    mode: int
    data: bytes = b""
    uid: int = 0
    gid: int = 0
    mtime: int = 1_700_000_000
    target: bytes = b""
    # What a stat says, when that is not the truth: /proc/cpuinfo states 0 and has content.
    stated_size: Optional[int] = None


def status_body(request_id: int, code: int, text: bytes = b"") -> bytes:
    return (
        struct.pack(">BII", codec.FXP_STATUS, request_id, code)
        + codec.string(text)
        + codec.string(b"")
    )


def attrs_bytes(node: Node) -> bytes:
    return struct.pack(
        ">IQIIIII",
        codec.ATTR_SIZE | codec.ATTR_UIDGID | codec.ATTR_PERMISSIONS | codec.ATTR_ACMODTIME,
        len(node.data) if node.stated_size is None else node.stated_size,
        node.uid,
        node.gid,
        node.mode,
        node.mtime,
        node.mtime,
    )


def frame(body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + body


Script = Callable[[int, int, "codec.Cursor"], Awaitable[Optional[bytes]]]


@dataclass
class FakeGuest:
    nodes: Dict[bytes, Node] = field(default_factory=dict)
    script: Optional[Script] = None
    version_reply: Optional[bytes] = None
    handles: Dict[bytes, Tuple[bytes, int]] = field(default_factory=dict)
    received: list = field(default_factory=list)
    _next_handle: int = 0

    def __post_init__(self) -> None:
        self.nodes.setdefault(b"/", Node(stat.S_IFDIR | 0o755))

    def add_file(self, path: bytes, data: bytes = b"", mode: int = 0o644, **kw) -> Node:
        node = Node(stat.S_IFREG | mode, data, **kw)
        self.nodes[path] = node
        return node

    def add_dir(self, path: bytes, mode: int = 0o755, **kw) -> Node:
        node = Node(stat.S_IFDIR | mode, **kw)
        self.nodes[path] = node
        return node

    def add_link(self, path: bytes, target: bytes) -> Node:
        node = Node(stat.S_IFLNK | 0o777, target, target=target)
        self.nodes[path] = node
        return node

    def children(self, path: bytes):
        prefix = path.rstrip(b"/") + b"/"
        for candidate in sorted(self.nodes):
            if candidate != b"/" and candidate.startswith(prefix) and b"/" not in candidate[len(prefix):]:
                yield candidate[len(prefix):], self.nodes[candidate]

    def resolve(self, path: bytes, depth: int = 0) -> Optional[bytes]:
        node = self.nodes.get(path)
        if node is None or not stat.S_ISLNK(node.mode):
            return path if node is not None else None
        if depth > 8:
            return None
        target = node.target if node.target.startswith(b"/") else path.rsplit(b"/", 1)[0] + b"/" + node.target
        return self.resolve(target, depth + 1)

    def _go_error(self, request_id: int, verb: bytes, path: bytes, text: bytes) -> bytes:
        return status_body(request_id, FAILURE, verb + b" " + path + b": " + text)

    def _missing(self, request_id: int) -> bytes:
        return status_body(request_id, 2, b"no such file or directory")

    def answer(self, packet_type: int, request_id: int, cursor: codec.Cursor) -> bytes:
        ok = status_body(request_id, codec.FX_OK)
        if packet_type in (codec.FXP_LSTAT, codec.FXP_STAT):
            path = cursor.string(9000)
            if packet_type == codec.FXP_STAT:
                resolved = self.resolve(path)
                node = self.nodes.get(resolved) if resolved else None
            else:
                node = self.nodes.get(path)
            if node is None:
                return self._missing(request_id)
            return struct.pack(">BI", codec.FXP_ATTRS, request_id) + attrs_bytes(node)
        if packet_type == codec.FXP_FSTAT:
            node = self.nodes[self.handles[cursor.string(256)][0]]
            return struct.pack(">BI", codec.FXP_ATTRS, request_id) + attrs_bytes(node)
        if packet_type == codec.FXP_OPENDIR:
            path = cursor.string(9000)
            resolved = self.resolve(path)
            node = self.nodes.get(resolved) if resolved else None
            if node is None:
                return self._missing(request_id)
            if not stat.S_ISDIR(node.mode):
                return self._go_error(request_id, b"open", path, b"not a directory")
            handle = self._new_handle(resolved, 0)
            return struct.pack(">BI", codec.FXP_HANDLE, request_id) + codec.string(handle)
        if packet_type == codec.FXP_READDIR:
            handle = cursor.string(256)
            path, at = self.handles[handle]
            entries = list(self.children(path))
            batch = entries[at : at + 100]
            if not batch:
                return status_body(request_id, codec.FX_EOF, b"EOF")
            self.handles[handle] = (path, at + len(batch))
            body = struct.pack(">BII", codec.FXP_NAME, request_id, len(batch))
            for name, node in batch:
                long = b"-rw-r--r--    1 root     root  %d Oct 1 00:00 %s" % (len(node.data), name)
                body += codec.string(name) + codec.string(long) + attrs_bytes(node)
            return body
        if packet_type == codec.FXP_CLOSE:
            self.handles.pop(cursor.string(256), None)
            return ok
        if packet_type == codec.FXP_OPEN:
            path = cursor.string(9000)
            flags = cursor.u32()
            attrs = codec.decode_attrs(cursor)
            node = self.nodes.get(path)
            if node is None:
                if not flags & codec.PFLAG_CREAT:
                    return self._missing(request_id)
                parent = path.rsplit(b"/", 1)[0] or b"/"
                if parent not in self.nodes:
                    return self._missing(request_id)
                node = self.add_file(path, mode=(attrs.mode if attrs.mode is not None else 0o644))
            elif flags & codec.PFLAG_EXCL:
                return self._go_error(request_id, b"open", path, b"file exists")
            if flags & codec.PFLAG_TRUNC:
                node.data = b""
            handle = self._new_handle(path, 0)
            return struct.pack(">BI", codec.FXP_HANDLE, request_id) + codec.string(handle)
        if packet_type == codec.FXP_READ:
            handle = cursor.string(256)
            offset = cursor.u64()
            length = min(cursor.u32(), 32768)
            node = self.nodes[self.handles[handle][0]]
            piece = node.data[offset : offset + length]
            if not piece:
                return status_body(request_id, codec.FX_EOF, b"EOF")
            return struct.pack(">BI", codec.FXP_DATA, request_id) + codec.string(piece)
        if packet_type == codec.FXP_WRITE:
            handle = cursor.string(256)
            offset = cursor.u64()
            data = cursor.string(1 << 20)
            node = self.nodes[self.handles[handle][0]]
            if offset > len(node.data):
                node.data += b"\0" * (offset - len(node.data))
            node.data = node.data[:offset] + data + node.data[offset + len(data):]
            return ok
        if packet_type in (codec.FXP_SETSTAT, codec.FXP_FSETSTAT):
            key = cursor.string(9000)
            path = self.handles[key][0] if packet_type == codec.FXP_FSETSTAT else self.resolve(key)
            node = self.nodes.get(path) if path else None
            if node is None:
                return self._missing(request_id)
            attrs = codec.decode_attrs(cursor)
            if attrs.mode is not None:
                node.mode = stat.S_IFMT(node.mode) | attrs.mode
            if attrs.uid is not None:
                node.uid, node.gid = attrs.uid, attrs.gid
            if attrs.size is not None:
                node.data = node.data[: attrs.size].ljust(attrs.size, b"\0")
            return ok
        if packet_type == codec.FXP_MKDIR:
            path = cursor.string(9000)
            if path in self.nodes:
                return self._go_error(request_id, b"mkdir", path, b"file exists")
            if (path.rsplit(b"/", 1)[0] or b"/") not in self.nodes:
                return self._missing(request_id)
            self.add_dir(path)
            return ok
        if packet_type in (codec.FXP_RMDIR, codec.FXP_REMOVE):
            path = cursor.string(9000)
            node = self.nodes.get(path)
            if node is None:
                return self._missing(request_id)
            if stat.S_ISDIR(node.mode) and any(True for _ in self.children(path)):
                return self._go_error(request_id, b"remove", path, b"directory not empty")
            del self.nodes[path]
            return ok
        if packet_type == codec.FXP_REALPATH:
            path = cursor.string(9000)
            resolved = self.resolve(path) or path
            return (
                struct.pack(">BII", codec.FXP_NAME, request_id, 1)
                + codec.string(resolved) + codec.string(resolved) + struct.pack(">I", 0)
            )
        if packet_type == codec.FXP_READLINK:
            path = cursor.string(9000)
            node = self.nodes.get(path)
            if node is None or not stat.S_ISLNK(node.mode):
                return self._go_error(request_id, b"readlink", path, b"invalid argument")
            return (
                struct.pack(">BII", codec.FXP_NAME, request_id, 1)
                + codec.string(node.target) + codec.string(node.target) + struct.pack(">I", 0)
            )
        if packet_type == codec.FXP_RENAME:
            return self._rename(request_id, cursor.string(9000), cursor.string(9000))
        if packet_type == codec.FXP_EXTENDED:
            name = cursor.string(256)
            if name == b"posix-rename@openssh.com":
                return self._rename(request_id, cursor.string(9000), cursor.string(9000))
            if name == b"hardlink@openssh.com":
                source, target = cursor.string(9000), cursor.string(9000)
                if source not in self.nodes:
                    return self._missing(request_id)
                if target in self.nodes:
                    return self._go_error(request_id, b"link", source + b" " + target, b"file exists")
                self.nodes[target] = self.nodes[source]
                return ok
            if name == b"statvfs@openssh.com":
                return struct.pack(">BI11Q", codec.FXP_EXTENDED_REPLY, request_id, 4096, 4096, 1000, 400, 300, 0, 0, 0, 0, 0, 255)
            return status_body(request_id, 8, b"operation unsupported")
        return status_body(request_id, 8, b"operation unsupported")

    def _rename(self, request_id: int, source: bytes, target: bytes) -> bytes:
        node = self.nodes.get(source)
        if node is None:
            return self._missing(request_id)
        existing = self.nodes.get(target)
        if existing is not None:
            if stat.S_ISDIR(existing.mode):
                return self._go_error(request_id, b"rename", source + b" " + target, b"file exists")
            if stat.S_ISDIR(node.mode):
                return self._go_error(request_id, b"rename", source + b" " + target, b"not a directory")
        prefix = source + b"/"
        for candidate in [c for c in self.nodes if c.startswith(prefix)]:
            self.nodes[target + candidate[len(source):]] = self.nodes.pop(candidate)
        del self.nodes[source]
        self.nodes[target] = node
        return status_body(request_id, codec.FX_OK)

    def _new_handle(self, path: bytes, at: int) -> bytes:
        self._next_handle += 1
        handle = str(self._next_handle).encode()
        self.handles[handle] = (path, at)
        return handle

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            prefix = await reader.readexactly(4)
            await reader.readexactly(struct.unpack(">I", prefix)[0])
            if self.version_reply is not None:
                writer.write(self.version_reply)
            else:
                writer.write(frame(struct.pack(">BI", codec.FXP_VERSION, 3)))
            await writer.drain()
            while True:
                prefix = await reader.readexactly(4)
                body = await reader.readexactly(struct.unpack(">I", prefix)[0])
                cursor = codec.Cursor(body[1:])
                request_id = cursor.u32()
                self.received.append(body[0])
                reply: Optional[bytes] = None
                if self.script is not None:
                    # Its own cursor, so a script can look at a packet and still let it through.
                    peek = codec.Cursor(body[1:])
                    peek.u32()
                    reply = await self.script(body[0], request_id, peek)
                if reply is None:
                    reply = frame(self.answer(body[0], request_id, cursor))
                if reply:
                    writer.write(reply)
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


async def connected(guest: FakeGuest, *, op_timeout: float = 1.0) -> Tuple[SftpClient, "asyncio.Task[None]"]:
    """A client talking to `guest`, not yet handshaken. The caller enters it with `async with`."""
    ours, theirs = socket.socketpair()
    ours.setblocking(False)
    theirs.setblocking(False)
    reader, writer = await asyncio.open_connection(sock=ours)
    peer_reader, peer_writer = await asyncio.open_connection(sock=theirs)
    task = asyncio.create_task(guest.serve(peer_reader, peer_writer))
    return SftpClient(reader, writer, op_timeout=op_timeout), task
