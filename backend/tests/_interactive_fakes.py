"""Fake browser and LXD sockets for the console and terminal proxies.

The proxies only need a handful of methods from each side; these record what was sent and let a
test script what arrives, with every wait ending on an event rather than a sleep.
"""

import asyncio
from typing import Any, List, Optional, Tuple

from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close


class FakeBrowser:
    """The browser end of a proxied socket (a Starlette WebSocket, as far as the proxy can tell)."""

    def __init__(self) -> None:
        self.incoming: "asyncio.Queue[dict]" = asyncio.Queue()
        self.sent: List[Tuple[str, Any]] = []
        self.closed: Optional[Tuple[int, str]] = None
        self.close_event = asyncio.Event()

    # what the test drives
    def type_bytes(self, data: bytes) -> None:
        self.incoming.put_nowait({"type": "websocket.receive", "bytes": data})

    def type_text(self, text: str) -> None:
        self.incoming.put_nowait({"type": "websocket.receive", "text": text})

    def leave(self) -> None:
        self.incoming.put_nowait({"type": "websocket.disconnect", "code": 1001})

    # what the proxy calls
    async def receive(self) -> dict:
        return await self.incoming.get()

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(("bytes", bytes(data)))

    async def send_text(self, text: str) -> None:
        self.sent.append(("text", text))

    async def send_json(self, body: Any) -> None:
        self.sent.append(("json", body))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed is None:
            self.closed = (code, reason)
        self.close_event.set()

    def binary(self) -> bytes:
        return b"".join(payload for kind, payload in self.sent if kind == "bytes")

    def json_types(self) -> List[str]:
        return [payload.get("type") for kind, payload in self.sent if kind == "json"]


class FakeLxdSocket:
    """One WebSocket to an LXD operation: frames the test feeds come out of ``async for``."""

    def __init__(self, url: str = "") -> None:
        self.url = url
        self.frames: "asyncio.Queue[Any]" = asyncio.Queue()
        self.received: List[Any] = []
        self.closed = asyncio.Event()

    def feed(self, frame: Any) -> None:
        self.frames.put_nowait(frame)

    def end(self) -> None:
        """LXD closes the socket normally (the shell exited, the VM stopped)."""
        self.frames.put_nowait(None)

    def break_off(self) -> None:
        """The connection to LXD drops."""
        self.frames.put_nowait(ConnectionClosedError(Close(1006, ""), None))

    async def send(self, data: Any) -> None:
        self.received.append(data)

    async def close(self) -> None:
        self.closed.set()

    def __aiter__(self) -> "FakeLxdSocket":
        return self

    async def __anext__(self) -> Any:
        frame = await self.frames.get()
        if frame is None:
            raise StopAsyncIteration
        if isinstance(frame, BaseException):
            raise frame
        return frame


class FakeSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.bytes_downloaded = 0
        self.bytes_uploaded = 0

    def add_download_bytes(self, count: int) -> None:
        self.bytes_downloaded += count

    def add_upload_bytes(self, count: int) -> None:
        self.bytes_uploaded += count


class FakeTracker:
    def __init__(self) -> None:
        self.sessions: List[FakeSession] = []
        self.registered: List[dict] = []
        self.connected: List[str] = []
        self.unregistered: List[str] = []

    def register(self, *, session_id: str, **kwargs: Any) -> FakeSession:
        session = FakeSession(session_id)
        self.sessions.append(session)
        self.registered.append({"session_id": session_id, **kwargs})
        return session

    def mark_connected(self, session_id: str) -> None:
        self.connected.append(session_id)

    def unregister(self, session_id: str) -> None:
        self.unregistered.append(session_id)
