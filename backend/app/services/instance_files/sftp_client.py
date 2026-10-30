"""An async SFTP v3 client for one short-lived connection into one guest.

One reader task answers every in-flight request by id, which is what lets a transfer keep a window
of requests outstanding: on LXD a read returns at most 32 KiB, so waiting for each reply gives
35 MB/s where a window of 64 gives 126 MB/s (measured on 5.21.8).

important: any breach of the protocol or any timeout ends the whole connection. A connection that
timed out might still deliver a late reply for a request nobody is waiting for, and telling that
from a hostile reply is not worth the code. Callers open one per request, so losing one costs
nothing.
"""

import asyncio
import logging
import stat as stat_module
import struct
from collections import deque
from types import TracebackType
from typing import AsyncIterator, Deque, Dict, List, Optional, Set, Tuple, Type

from . import sftp_codec as codec
from .errors import GuestFileError, from_sftp_status, guest_disconnected, guest_timeout, protocol_error

logger = logging.getLogger(__name__)

# What LXD actually returns for one READ, so a larger ask only produces short reads.
READ_CHUNK_BYTES = 32 * 1024
READ_WINDOW = 64
# Accepted by LXD up to its 256 KiB message ceiling; past it the connection resets (probed).
WRITE_CHUNK_BYTES = 128 * 1024
WRITE_WINDOW = 32

DEFAULT_OP_TIMEOUT = 15.0


class SftpClient:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        op_timeout: float = DEFAULT_OP_TIMEOUT,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._op_timeout = op_timeout
        self._next_id = 0
        self._pending: Dict[int, "asyncio.Future[Tuple[int, bytes]]"] = {}
        self._abandoned: Set[int] = set()
        self._pump_task: Optional["asyncio.Task[None]"] = None
        self._failure: Optional[GuestFileError] = None
        self.extensions: Dict[bytes, bytes] = {}

    async def __aenter__(self) -> "SftpClient":
        try:
            await self.handshake()
        except BaseException:
            self.abort()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.abort()

    async def handshake(self) -> None:
        self._writer.write(codec.encode_init())
        try:
            async with asyncio.timeout(self._op_timeout):
                await self._writer.drain()
                packet_type, body = await self._read_frame()
        except TimeoutError:
            raise guest_timeout() from None
        if packet_type != codec.FXP_VERSION:
            raise protocol_error("expected VERSION")
        self.extensions = codec.decode_version(body)
        self._pump_task = asyncio.create_task(self._pump(), name="sftp-pump")

    def abort(self) -> None:
        """End the connection now. Graceful TLS close would wait on a guest that may be stalling."""
        if self._pump_task is not None:
            self._pump_task.cancel()
        self._fail(guest_disconnected())
        try:
            self._writer.transport.abort()
        except Exception:  # noqa: BLE001 - already gone, which is the goal
            pass

    def _fail(self, failure: GuestFileError) -> None:
        if self._failure is None:
            self._failure = failure
        for future in self._pending.values():
            if not future.done():
                future.set_exception(self._failure)
        self._pending.clear()

    async def _read_frame(self) -> Tuple[int, bytes]:
        prefix = await self._reader.readexactly(4)
        length = codec.parse_frame_length(prefix)
        body = await self._reader.readexactly(length)
        return body[0], body[1:]

    async def _pump(self) -> None:
        try:
            while True:
                packet_type, body = await self._read_frame()
                cursor = codec.Cursor(body)
                request_id = cursor.u32()
                if request_id in self._abandoned:
                    self._abandoned.discard(request_id)
                    continue
                future = self._pending.pop(request_id, None)
                if future is None:
                    raise protocol_error("reply to no request")
                if not future.done():
                    future.set_result((packet_type, body[4:]))
        except asyncio.CancelledError:
            raise
        except GuestFileError as failure:
            logger.warning("SFTP peer broke the protocol: %s", failure.extra.get("reason"))
            self._fail(failure)
            self._close_quietly()
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            self._fail(guest_disconnected())
            self._close_quietly()

    def _close_quietly(self) -> None:
        try:
            self._writer.transport.abort()
        except Exception:  # noqa: BLE001 - already gone, which is the goal
            pass

    def _send(self, packet_type: int, payload: bytes = b"") -> Tuple[int, "asyncio.Future[Tuple[int, bytes]]"]:
        if self._failure is not None:
            raise self._failure
        self._next_id = (self._next_id + 1) & 0xFFFFFFFF
        request_id = self._next_id
        future: "asyncio.Future[Tuple[int, bytes]]" = asyncio.get_running_loop().create_future()
        # A window can fail with requests nobody will await, and that must not log as unhandled.
        future.add_done_callback(lambda done: done.cancelled() or done.exception())
        self._pending[request_id] = future
        self._writer.write(codec.encode_request(packet_type, request_id, payload))
        return request_id, future

    async def _drain(self) -> None:
        try:
            async with asyncio.timeout(self._op_timeout):
                await self._writer.drain()
        except TimeoutError:
            self.abort()
            raise guest_timeout() from None
        except ConnectionError:
            self.abort()
            raise guest_disconnected() from None

    async def _await(self, future: "asyncio.Future[Tuple[int, bytes]]") -> Tuple[int, bytes]:
        try:
            async with asyncio.timeout(self._op_timeout):
                return await future
        except TimeoutError:
            self.abort()
            raise guest_timeout() from None

    async def _request(self, packet_type: int, payload: bytes = b"") -> Tuple[int, bytes]:
        _, future = self._send(packet_type, payload)
        await self._drain()
        return await self._await(future)

    @staticmethod
    def _raise_status(body: bytes) -> None:
        code, message = codec.decode_status(body)
        if code != codec.FX_OK:
            raise from_sftp_status(code, message)

    async def _expect_ok(self, packet_type: int, payload: bytes) -> None:
        reply, body = await self._request(packet_type, payload)
        if reply != codec.FXP_STATUS:
            raise protocol_error("expected STATUS")
        self._raise_status(body)

    async def _expect(self, wanted: int, packet_type: int, payload: bytes) -> bytes:
        reply, body = await self._request(packet_type, payload)
        if reply == codec.FXP_STATUS:
            self._raise_status(body)
            raise protocol_error("STATUS OK where a result was due")
        if reply != wanted:
            raise protocol_error("wrong reply type")
        return body

    async def lstat(self, path: bytes) -> codec.Attrs:
        body = await self._expect(codec.FXP_ATTRS, codec.FXP_LSTAT, codec.string(path))
        return codec.decode_attrs(codec.Cursor(body))

    async def stat(self, path: bytes) -> codec.Attrs:
        body = await self._expect(codec.FXP_ATTRS, codec.FXP_STAT, codec.string(path))
        return codec.decode_attrs(codec.Cursor(body))

    async def fstat(self, handle: bytes) -> codec.Attrs:
        body = await self._expect(codec.FXP_ATTRS, codec.FXP_FSTAT, codec.string(handle))
        return codec.decode_attrs(codec.Cursor(body))

    async def realpath(self, path: bytes) -> bytes:
        body = await self._expect(codec.FXP_NAME, codec.FXP_REALPATH, codec.string(path))
        return codec.decode_single_name(body)

    async def readlink(self, path: bytes) -> bytes:
        body = await self._expect(codec.FXP_NAME, codec.FXP_READLINK, codec.string(path))
        return codec.decode_single_name(body)

    async def setstat(self, path: bytes, attrs: bytes) -> None:
        await self._expect_ok(codec.FXP_SETSTAT, codec.string(path) + attrs)

    async def fsetstat(self, handle: bytes, attrs: bytes) -> None:
        await self._expect_ok(codec.FXP_FSETSTAT, codec.string(handle) + attrs)

    async def mkdir(self, path: bytes) -> None:
        await self._expect_ok(codec.FXP_MKDIR, codec.string(path) + codec.encode_attrs())

    async def rmdir(self, path: bytes) -> None:
        await self._expect_ok(codec.FXP_RMDIR, codec.string(path))

    async def remove(self, path: bytes) -> None:
        await self._expect_ok(codec.FXP_REMOVE, codec.string(path))

    async def replace(self, source: bytes, target: bytes) -> None:
        """posix-rename: atomically replaces an existing file at `target`."""
        await self._expect_ok(
            codec.FXP_EXTENDED,
            codec.string(b"posix-rename@openssh.com") + codec.string(source) + codec.string(target),
        )

    async def rename(self, source: bytes, target: bytes) -> None:
        """Plain RENAME.

        important: unlike OpenSSH, LXD's server replaces an existing FILE here without a word
        (probed on 5.21.8). A folder is refused over anything that exists. So this is safe for
        moving a folder, and a file that must not overwrite goes through `link`.
        """
        await self._expect_ok(codec.FXP_RENAME, codec.string(source) + codec.string(target))

    async def link(self, source: bytes, target: bytes) -> None:
        """A hard link, which fails atomically when `target` exists: the no-clobber primitive."""
        await self._expect_ok(
            codec.FXP_EXTENDED,
            codec.string(b"hardlink@openssh.com") + codec.string(source) + codec.string(target),
        )

    async def statvfs(self, path: bytes) -> codec.StatVfs:
        body = await self._expect(
            codec.FXP_EXTENDED_REPLY,
            codec.FXP_EXTENDED,
            codec.string(b"statvfs@openssh.com") + codec.string(path),
        )
        return codec.decode_statvfs(body)

    async def open(self, path: bytes, flags: int, *, mode: Optional[int] = None) -> bytes:
        attrs = codec.encode_attrs(mode=mode)
        body = await self._expect(
            codec.FXP_HANDLE, codec.FXP_OPEN, codec.string(path) + struct.pack(">I", flags) + attrs
        )
        return codec.decode_handle(body)

    async def close(self, handle: bytes) -> None:
        await self._expect_ok(codec.FXP_CLOSE, codec.string(handle))

    async def iter_directory(self, path: bytes) -> AsyncIterator[List[codec.Name]]:
        """Batches of entries. The caller decides when enough is enough and stops iterating."""
        body = await self._expect(codec.FXP_HANDLE, codec.FXP_OPENDIR, codec.string(path))
        handle = codec.decode_handle(body)
        try:
            while True:
                reply, payload = await self._request(codec.FXP_READDIR, codec.string(handle))
                if reply == codec.FXP_STATUS:
                    code, message = codec.decode_status(payload)
                    if code == codec.FX_EOF:
                        return
                    if code == codec.FX_OK:
                        raise protocol_error("STATUS OK in a directory read")
                    raise from_sftp_status(code, message)
                if reply != codec.FXP_NAME:
                    raise protocol_error("wrong reply type")
                names = codec.decode_names(payload)
                if not names:
                    raise protocol_error("empty directory batch")
                yield names
        finally:
            if self._failure is None:
                self._forget(self._send(codec.FXP_CLOSE, codec.string(handle))[0])

    def _forget(self, request_id: int) -> None:
        """Stop waiting for a reply that no longer matters, without treating it as a breach."""
        future = self._pending.pop(request_id, None)
        if future is not None:
            future.cancel()
        self._abandoned.add(request_id)

    async def read(self, handle: bytes, offset: int, length: int) -> bytes:
        """One read. Empty at end of file."""
        length = min(length, READ_CHUNK_BYTES)
        reply, body = await self._request(
            codec.FXP_READ, codec.string(handle) + struct.pack(">QI", offset, length)
        )
        if reply == codec.FXP_DATA:
            return codec.decode_data(body, length)
        if reply == codec.FXP_STATUS:
            code, message = codec.decode_status(body)
            if code == codec.FX_EOF:
                return b""
            raise from_sftp_status(code, message)
        raise protocol_error("wrong reply type")

    async def read_stream(
        self, handle: bytes, *, offset: int = 0, limit: int
    ) -> AsyncIterator[bytes]:
        """In-order bytes from `offset`, at most `limit` of them, with a window of reads in flight.

        A short reply is legal, so the gap it leaves is read again in order before moving on.
        An empty read means end of file, however much `limit` allows.
        """
        window: Deque[Tuple[int, int, int, "asyncio.Future[Tuple[int, bytes]]"]] = deque()
        next_offset = offset
        end = offset + limit
        try:
            while True:
                while next_offset < end and len(window) < READ_WINDOW:
                    length = min(READ_CHUNK_BYTES, end - next_offset)
                    request_id, future = self._send(
                        codec.FXP_READ, codec.string(handle) + struct.pack(">QI", next_offset, length)
                    )
                    window.append((request_id, next_offset, length, future))
                    next_offset += length
                if not window:
                    return
                await self._drain()
                _, at, asked, future = window.popleft()
                reply, body = await self._await(future)
                if reply == codec.FXP_STATUS:
                    code, message = codec.decode_status(body)
                    if code == codec.FX_EOF:
                        return
                    raise from_sftp_status(code, message)
                if reply != codec.FXP_DATA:
                    raise protocol_error("wrong reply type")
                data = codec.decode_data(body, asked)
                if not data:
                    return
                yield data
                if len(data) < asked:
                    async for piece in self._read_gap(handle, at + len(data), asked - len(data)):
                        yield piece
        finally:
            for request_id, _, _, _ in window:
                self._forget(request_id)

    async def _read_gap(self, handle: bytes, offset: int, length: int) -> AsyncIterator[bytes]:
        while length > 0:
            data = await self.read(handle, offset, length)
            if not data:
                return
            yield data
            offset += len(data)
            length -= len(data)

    async def write_all(self, handle: bytes, offset: int, data: bytes) -> None:
        """Write `data` at `offset`, returning only after every piece has been acknowledged."""
        window: Deque["asyncio.Future[Tuple[int, bytes]]"] = deque()
        view = memoryview(data)
        at = 0
        while at < len(view) or window:
            while at < len(view) and len(window) < WRITE_WINDOW:
                piece = bytes(view[at : at + WRITE_CHUNK_BYTES])
                _, future = self._send(
                    codec.FXP_WRITE,
                    codec.string(handle) + struct.pack(">Q", offset + at) + codec.string(piece),
                )
                window.append(future)
                at += len(piece)
            await self._drain()
            reply, body = await self._await(window.popleft())
            if reply != codec.FXP_STATUS:
                raise protocol_error("expected STATUS")
            self._raise_status(body)


def is_regular(attrs: codec.Attrs) -> bool:
    return attrs.mode is not None and stat_module.S_ISREG(attrs.mode)


def is_directory(attrs: codec.Attrs) -> bool:
    return attrs.mode is not None and stat_module.S_ISDIR(attrs.mode)


def is_symlink(attrs: codec.Attrs) -> bool:
    return attrs.mode is not None and stat_module.S_ISLNK(attrs.mode)
