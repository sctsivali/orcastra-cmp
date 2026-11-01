"""The browser-to-LXD relay behind ``/terminal/ws/{instance}``: an interactive exec shell.

Wire protocol (the client's first text frame chooses it):

- ``{"width", "height", "binary": true, "flow": true}``: PTY output goes out as raw binary frames,
  input comes in as binary frames, and the client ACKs output it has drawn. Text frames carry
  JSON control only: resize, ack, ping, and the server's session / ready / error notices.
- ``{"width", "height"}`` alone: the previous protocol, kept for a tab still running the last
  build across a deploy. Output goes out as ``{"type": "output", "data": "..."}``.

important: output used to be decoded per LXD frame with errors="replace", so a multibyte
character split across two frames (box drawing in htop, CJK, emoji, systemd's bullet) arrived as
U+FFFD. Binary frames hand xterm the bytes and its streaming decoder stitches them. The legacy
path keeps an incremental decoder for the same reason.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import time
from typing import Any, Optional

from fastapi import WebSocket

from app.core import ws_close
from app.services.interactive_relay import (
    CLOSE_NORMAL,
    CLOSE_UNAVAILABLE,
    CLOSE_UPSTREAM,
    OutputWindow,
    close_browser,
)
from app.services.lxd_ws import SessionSetupError, connection_material, open_lxd_socket, redact, session_io
from app.services.migration_fence import MigrationFenceConflict, guard_instance_mutation
from app.services.session_models import new_session_id
from app.services.session_tracker import SessionProtocol, get_session_tracker
from app.services.terminal_service import get_terminal_service

logger = logging.getLogger(__name__)

_INIT_TIMEOUT_SECONDS = 5.0
_CONNECT_TIMEOUT_SECONDS = 15.0


class TerminalProxy:
    def __init__(
        self,
        browser_ws: WebSocket,
        instance_name: str,
        host: str,
        project: str,
        user_id: str = "anonymous",
        user_email: str = "",
        user_role: str = "unknown",
        target_organization_id: Optional[int] = None,
        target_org_slug: Optional[str] = None,
        break_glass_grant_id: Optional[int] = None,
    ) -> None:
        self.browser_ws = browser_ws
        self.instance_name = instance_name
        self.host = host
        self.project = project
        self.user_id = user_id
        self.user_email = user_email
        self.user_role = user_role
        self.target_organization_id = target_organization_id
        self.target_org_slug = target_org_slug
        self.break_glass_grant_id = break_glass_grant_id

        self.data_ws: Any = None
        self.control_ws: Any = None
        self._session: Any = None
        self._binary = False
        self._window = OutputWindow(enabled=False)
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    @property
    def _label(self) -> str:
        return f"{self.instance_name}@{self.host}"

    async def run(self) -> None:
        started = time.monotonic()
        try:
            width, height = await self._read_init()
            info = await self._open_operation(width, height)
            await self.browser_ws.send_json({
                "type": "session",
                "session_id": self._session.session_id if self._session else info.get("operation_id"),
                "instance": self.instance_name,
                "host": self.host,
            })
            _, ssl_context = await session_io(
                connection_material, self.host, label=f"terminal.material:{self.host}", host=self.host,
            )
            self.data_ws, self.control_ws = await asyncio.gather(
                open_lxd_socket(info["ws_url"], ssl_context, timeout=_CONNECT_TIMEOUT_SECONDS),
                open_lxd_socket(info["control_url"], ssl_context, timeout=_CONNECT_TIMEOUT_SECONDS, max_size=1024 * 1024),
            )
            if self._session:
                get_session_tracker().mark_connected(self._session.session_id)
            await self.browser_ws.send_json({"type": "ready"})
            logger.info("Terminal ready for %s in %d ms", self._label, int((time.monotonic() - started) * 1000))
            await self._relay()
        except SessionSetupError as exc:
            logger.info("Terminal for %s did not start: %s", self._label, exc)
            await self._refuse(str(exc), CLOSE_UPSTREAM if exc.retryable else CLOSE_UNAVAILABLE)
        except MigrationFenceConflict as exc:
            await self._refuse(str(exc.detail), ws_close.FENCED, reason="migration_fenced")
        except Exception as exc:  # noqa: BLE001 - the browser is told; the log keeps the type
            logger.error("Terminal proxy for %s failed: %s: %s", self._label, type(exc).__name__, redact(exc))
            await self._refuse("The terminal connection failed", CLOSE_UPSTREAM)
        finally:
            await self._cleanup()

    async def _read_init(self) -> tuple[int, int]:
        try:
            message = await asyncio.wait_for(self.browser_ws.receive(), timeout=_INIT_TIMEOUT_SECONDS)
            text = message.get("text") if isinstance(message, dict) else None
            body = json.loads(text) if text else {}
        except (asyncio.TimeoutError, ValueError, TypeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        self._binary = bool(body.get("binary"))
        self._window = OutputWindow(enabled=self._binary and bool(body.get("flow")))
        return _bounded(body.get("width"), 120, 20, 1000), _bounded(body.get("height"), 40, 5, 500)

    async def _open_operation(self, width: int, height: int) -> dict:
        service = get_terminal_service()
        tracker = get_session_tracker()
        # The admission lock covers the exec submission and the tracker registration only.
        async with guard_instance_mutation(
            cluster_id=self.host,
            project=self.project,
            instance_name=self.instance_name,
        ):
            info = await session_io(
                service.open_exec_operation,
                self.instance_name,
                self.host,
                self.project,
                width,
                height,
                label=f"terminal.open:{self.host}",
                host=self.host,
            )
            session_id = new_session_id(info.get("operation_id"), "terminal")
            self._session = tracker.register(
                session_id=session_id,
                instance_name=self.instance_name,
                host=self.host,
                project=self.project,
                protocol=SessionProtocol.TERMINAL,
                user_id=self.user_id,
                user_email=self.user_email,
                user_role=self.user_role,
                target_organization_id=self.target_organization_id,
                target_org_slug=self.target_org_slug,
                break_glass_grant_id=self.break_glass_grant_id,
            )
        return info

    async def _refuse(self, message: str, code: int, *, reason: str = "") -> None:
        """Say why in an error frame, then close with `code` and `reason` (the message when unset).

        note: the frame carries the full sentence and the code, since a close reason is cut at 123
        bytes. A code of core/ws_close.py closes with its token instead of the sentence.
        """
        try:
            await self.browser_ws.send_json({
                "type": "error", "message": message, "code": code, "retryable": code == CLOSE_UPSTREAM,
            })
        except Exception:  # noqa: BLE001 - already gone
            pass
        await close_browser(self.browser_ws, code, reason or message)

    # ── relay ────────────────────────────────────────────────────────────────────────────────

    async def _relay(self) -> None:
        tasks = {
            asyncio.create_task(self._browser_to_lxd(), name="terminal.browser_to_lxd"),
            asyncio.create_task(self._lxd_to_browser(), name="terminal.lxd_to_browser"),
            asyncio.create_task(self._drain_control(), name="terminal.control"),
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        self._window.release()
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        ended = next(iter(done))
        error = ended.exception() if not ended.cancelled() else None
        if error is None and ended.result() == "browser":
            return
        try:
            await self.browser_ws.send_json({"type": "closed"})
        except Exception:  # noqa: BLE001 - already gone
            pass
        if error is None:
            await close_browser(self.browser_ws, CLOSE_NORMAL, "The shell exited")
        else:
            logger.info("Terminal relay for %s ended: %s", self._label, type(error).__name__)
            await close_browser(self.browser_ws, CLOSE_UPSTREAM, "Lost the connection to the shell")

    async def _browser_to_lxd(self) -> str:
        while True:
            message = await self.browser_ws.receive()
            if message.get("type") == "websocket.disconnect":
                return "browser"
            data = message.get("bytes")
            if data:
                await self.data_ws.send(data)
                self._count(up=len(data))
                continue
            text = message.get("text")
            if text:
                await self._handle_text(text)

    async def _handle_text(self, text: str) -> None:
        try:
            body = json.loads(text)
        except ValueError:
            return
        if not isinstance(body, dict):
            return
        kind = body.get("type")
        if kind == "input":
            # The previous protocol: keystrokes as JSON text.
            payload = str(body.get("data") or "").encode("utf-8")
            if payload:
                await self.data_ws.send(payload)
                self._count(up=len(payload))
        elif kind == "resize":
            await self.control_ws.send(json.dumps({
                "command": "window-resize",
                "args": {
                    "width": str(_bounded(body.get("width"), 120, 20, 1000)),
                    "height": str(_bounded(body.get("height"), 40, 5, 500)),
                },
            }))
        elif kind == "ack":
            self._window.ack(body.get("bytes"))
        elif kind == "ping":
            await self.browser_ws.send_json({"type": "pong"})

    async def _lxd_to_browser(self) -> str:
        async for message in self.data_ws:
            payload = message if isinstance(message, (bytes, bytearray)) else str(message).encode("utf-8")
            self._count(down=len(payload))
            if self._binary:
                await self._window.before_send()
                await self.browser_ws.send_bytes(bytes(payload))
                self._window.sent(len(payload))
            else:
                text = self._decoder.decode(bytes(payload))
                if text:
                    await self.browser_ws.send_json({"type": "output", "data": text})
        return "lxd"

    async def _drain_control(self) -> str:
        async for _message in self.control_ws:
            pass
        return "control"

    def _count(self, down: int = 0, up: int = 0) -> None:
        if self._session is None:
            return
        if down:
            self._session.add_download_bytes(down)
        if up:
            self._session.add_upload_bytes(up)

    async def _cleanup(self) -> None:
        self._window.release()
        for socket in (self.data_ws, self.control_ws):
            if socket is None:
                continue
            try:
                await socket.close()
            except Exception:  # noqa: BLE001 - already closed
                pass
        if self._session is not None:
            get_session_tracker().unregister(self._session.session_id)
        logger.info("Terminal session ended: %s", self._label)


def _bounded(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))
