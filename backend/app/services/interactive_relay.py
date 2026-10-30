"""What the console and terminal proxies share: how a browser socket is closed, and output flow.

important: a proxy that simply returned left uvicorn to drop the TCP connection with no close
frame, so every ending (a shell's `exit`, a VM powering off, an LXD error) reached the browser as
1006, which the clients read as "network dropped, reconnect". A shell that had just exited
reconnected into a new one. Every ending now closes with a code that says which it was:

- 1000: the session ended on the instance side; do not reconnect on your own.
- 1011: the connection to the instance broke; reconnecting may work.
- 1012: the instance powered down (a stop, or a restart on its way); reconnect once it runs again.
- 4404: the session cannot start (not running, not found, not a VM); reconnecting will not help.
- 4403: access ended mid-session (sent by ws_session_guard).
- 4409: another viewer opened this display (QEMU serves one); taking it back is the operator's call.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

CLOSE_NORMAL = 1000
CLOSE_UPSTREAM = 1011
CLOSE_POWERED_DOWN = 1012
CLOSE_TAKEN_OVER = 4409
CLOSE_UNAVAILABLE = 4404

# RFC 6455 caps a close reason at 123 bytes; a longer one makes the close frame itself fail,
# which used to be swallowed and turned back into a 1006.
_MAX_REASON_BYTES = 120


def close_reason(text: str) -> str:
    """``text`` cut to fit a close frame, on a character boundary."""
    raw = (text or "").replace("\n", " ").strip().encode("utf-8")
    if len(raw) <= _MAX_REASON_BYTES:
        return raw.decode("utf-8")
    return raw[: _MAX_REASON_BYTES - 3].decode("utf-8", errors="ignore").rstrip() + "..."


async def close_browser(websocket: Any, code: int, reason: str = "") -> None:
    """Close the browser's socket with ``code``; a socket that is already gone is fine."""
    try:
        await websocket.close(code=code, reason=close_reason(reason))
    except Exception:  # noqa: BLE001 - already closed or never accepted
        pass


class OutputWindow:
    """Bounds how much PTY output is in flight to a browser that ACKs what it has drawn.

    note: TCP backpressure bounds the socket, not the browser: xterm queues every write it is
    handed, so `yes` or a large `cat` grew the tab until it stalled. The client ACKs bytes once
    xterm has parsed them; past HIGH_WATER unacknowledged bytes the forwarder waits until ACKs
    bring it under LOW_WATER. Interactive output never comes near either mark.

    A client that never said it ACKs (a tab still running the previous build) is never paused.
    """

    HIGH_WATER = 2 * 1024 * 1024
    LOW_WATER = 512 * 1024

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._unacked = 0
        self._open = asyncio.Event()
        self._open.set()

    async def before_send(self) -> None:
        if self.enabled and self._unacked >= self.HIGH_WATER:
            self._open.clear()
            await self._open.wait()

    def sent(self, count: int) -> None:
        if self.enabled:
            self._unacked += count

    def ack(self, count: Any) -> None:
        try:
            acked = int(count)
        except (TypeError, ValueError):
            return
        if acked <= 0:
            return
        self._unacked = max(0, self._unacked - acked)
        if self._unacked < self.LOW_WATER:
            self._open.set()

    def release(self) -> None:
        """Let a paused forwarder go, so shutdown never waits on an ACK that will not come."""
        self._open.set()
