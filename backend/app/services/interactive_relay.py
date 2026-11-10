"""What the console and terminal proxies share: how a browser socket is closed, and output flow.

A proxy that just returns lets uvicorn drop TCP with no close frame; the browser sees 1006, which
the clients read as "network dropped, reconnect", so an exited shell would reconnect into a new
one. Every ending closes with a code the client acts on: 1000 ended on the instance side (no
automatic reconnect), 1011 the link to the instance broke (reconnect may work), 1012 the instance
powered down (reconnect once it runs), 4404 the session cannot start (reconnecting will not help),
4409 another viewer took the display, which QEMU serves to one (taking it back is the operator's
call). ws_session_guard sends 4403 when access ends mid-session; 4423 means Migration Center
fenced the instance while the session was starting. Access refusals (4401, 4403, 4428, 4423)
carry a reason token instead of a sentence; core/ws_close.py lists them.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

CLOSE_NORMAL = 1000
CLOSE_UPSTREAM = 1011
CLOSE_POWERED_DOWN = 1012
CLOSE_TAKEN_OVER = 4409
CLOSE_UNAVAILABLE = 4404

# RFC 6455 caps a close reason at 123 bytes; a longer one makes the close frame itself fail, and
# the browser then sees 1006.
_MAX_REASON_BYTES = 120


# C0, DEL and C1, then the format characters that reorder what a reader sees.
_UNPRINTABLE = re.compile("[\\x00-\\x1f\\x7f-\\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def plain_text(text: str) -> str:
    """``text`` with nothing a terminal would execute.

    important: a refusal echoes what the browser asked for, and the browser writes it to xterm, so
    a crafted instance name must not arrive as an escape sequence. The client strips this too.
    """
    return _UNPRINTABLE.sub(" ", text or "").strip()


def close_reason(text: str) -> str:
    """``text`` cut to fit a close frame, on a character boundary."""
    raw = plain_text(text).encode("utf-8")
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

    TCP backpressure bounds the socket but not the browser: xterm queues every write it is handed,
    so `yes` or a large `cat` would grow the tab until it stalls. The client ACKs bytes once xterm
    has parsed them; past HIGH_WATER unacknowledged bytes the forwarder waits until ACKs bring it
    under LOW_WATER. Interactive output never comes near either mark.

    A client that never said it ACKs (ex; a tab running an older build) is never paused.
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
