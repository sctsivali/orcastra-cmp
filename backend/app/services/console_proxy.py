"""The browser-to-LXD relay behind ``/console/{instance}/ws``: the SPICE display and the serial console.

Roles of one socket:

- A SPICE channel whose ticket carries a display group either owns the display's LXD operation
  (the first channel, normally main) or joins it (display, inputs, cursor). See spice_session.
- A SPICE channel without a group (a ``?token=`` socket, or a store that failed) opens its own
  operation, as every channel did before.
- A serial console (``console_type=console``) is one operation with a control socket for resize.

Every role also watches its instance's power (``watch_power``, see instance_power_watch): a
restart left the serial console attached to a QEMU process that no longer existed.

important: setup runs through ``session_io`` and never on the event loop, and the migration
admission lock is held only around the operation POST and the tracker registration, exactly as
before. The relay is event driven (no polling timeouts), and whichever side ends first tears the
other down at once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi import WebSocket

from app.core import ws_close
from app.services.activity_logger import get_activity_logger
from app.services.console_service import get_console_service
from app.services.interactive_relay import (
    CLOSE_NORMAL,
    CLOSE_POWERED_DOWN,
    CLOSE_TAKEN_OVER,
    CLOSE_UNAVAILABLE,
    CLOSE_UPSTREAM,
    OutputWindow,
    close_browser,
)
from app.services.lxd_ws import SessionSetupError, connection_material, open_lxd_socket, redact, session_io
from app.services.migration_fence import MigrationFenceConflict, guard_instance_mutation
from app.services.session_models import new_session_id
from app.services.session_tracker import SessionProtocol, get_session_tracker
from app.services.spice_session import RENEW_EVERY_SECONDS, get_spice_session_store

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_SECONDS = 15.0
_MEMBER_FLUSH_SECONDS = 2.0
# How long the owner waits, after closing the operation, for the other channels' last byte counts.
_MEMBER_SETTLE_SECONDS = 1.0
_SERIAL_INIT_TIMEOUT_SECONDS = 5.0
_POWER_DOWN_REASONS = {"instance-stopped": "The instance stopped"}

PowerWatch = Callable[[str, str, str], Awaitable[str]]


class ConsoleProxy:
    def __init__(
        self,
        browser_ws: WebSocket,
        instance_name: str,
        host: str,
        project: str,
        console_type: str = "vga",
        user_id: str = "anonymous",
        user_email: str = "",
        user_role: str = "unknown",
        target_organization_id: Optional[int] = None,
        target_org_slug: Optional[str] = None,
        break_glass_grant_id: Optional[int] = None,
        group: Optional[str] = None,
        share_operation: bool = True,
        watch_power: Optional[PowerWatch] = None,
    ) -> None:
        self.browser_ws = browser_ws
        self.instance_name = instance_name
        self.host = host
        self.project = project
        self.console_type = console_type
        self.user_id = user_id
        self.user_email = user_email
        self.user_role = user_role
        self.target_organization_id = target_organization_id
        self.target_org_slug = target_org_slug
        self.break_glass_grant_id = break_glass_grant_id
        self.group = group if (share_operation and console_type == "vga") else None
        self._watch_power = watch_power
        self._operation_id: Optional[str] = None

        self.role = "solo"
        self._published = False
        self.data_ws: Any = None
        self.control_ws: Any = None
        self._session: Any = None
        self._window = OutputWindow(enabled=False)
        self._member_bytes = [0, 0]  # down, up not yet flushed (members only)
        self._member_seen = (0, 0)  # what the owner already folded into its session
        self._width = 0
        self._height = 0

    @property
    def _label(self) -> str:
        return f"{self.instance_name}@{self.host}"

    # ── entry ────────────────────────────────────────────────────────────────────────────────

    async def run(self) -> None:
        started = time.monotonic()
        try:
            if self.console_type == "console":
                await self._read_serial_init()
            if self.group:
                await self._run_shared(started)
            else:
                await self._run_own_operation(started)
        except SessionSetupError as exc:
            logger.info("Console %s for %s did not start: %s", self.console_type, self._label, exc)
            await close_browser(self.browser_ws, CLOSE_UPSTREAM if exc.retryable else CLOSE_UNAVAILABLE, str(exc))
        except MigrationFenceConflict:
            await close_browser(self.browser_ws, ws_close.FENCED, "migration_fenced")
        except Exception as exc:  # noqa: BLE001 - the browser is told; the log keeps the type
            logger.error("Console proxy for %s failed: %s: %s", self._label, type(exc).__name__, redact(exc))
            await close_browser(self.browser_ws, CLOSE_UPSTREAM, "The console connection failed")
        finally:
            await self._cleanup()

    async def _read_serial_init(self) -> None:
        """A serial client says its terminal size (and that it ACKs output) before anything else."""
        try:
            message = await asyncio.wait_for(self.browser_ws.receive(), timeout=_SERIAL_INIT_TIMEOUT_SECONDS)
            text = message.get("text") if isinstance(message, dict) else None
            body = json.loads(text) if text else {}
        except (asyncio.TimeoutError, ValueError, TypeError):
            body = {}
        self._width = _bounded(body.get("width"), 80, 20, 1000)
        self._height = _bounded(body.get("height"), 24, 5, 500)
        self._window = OutputWindow(enabled=bool(body.get("flow")))

    # ── SPICE display group ──────────────────────────────────────────────────────────────────

    async def _run_shared(self, started: float) -> None:
        store = get_spice_session_store()
        group = self.group or ""
        try:
            owner = await store.claim(group)
        except Exception as exc:  # noqa: BLE001 - share nothing, but still open the console
            logger.warning("SPICE session store unavailable (%s); %s opens its own operation", type(exc).__name__, self._label)
            await self._run_own_operation(started)
            return
        if owner:
            self.role = "owner"
            try:
                info = await self._open_operation()
            except Exception as exc:
                await store.fail(group, str(exc) if isinstance(exc, SessionSetupError) else "The console could not start")
                raise
            await store.publish(group, {
                "data_url": info["ws_url"],
                "operation_id": info["operation_id"],
                "session_id": self._session.session_id if self._session else "",
                "host": self.host,
                "project": self.project,
                "instance": self.instance_name,
            })
            self._published = True
            self._log_ready(started)
            renew = asyncio.create_task(self._renew_while_open(store, group))
            try:
                await self._relay()
            finally:
                renew.cancel()
            return

        record = await store.wait_for(group)
        if record and record.get("data_url"):
            base, ssl_context = await session_io(connection_material, self.host, label=f"console.material:{self.host}", host=self.host)
            if not self._is_own_display(record, base):
                logger.warning("A SPICE record for %s named another display; this channel opens its own", self._label)
                await self._run_own_operation(started)
                return
            self.role = "member"
            self._operation_id = str(record["operation_id"])
            self.data_ws = await open_lxd_socket(record["data_url"], ssl_context, timeout=_CONNECT_TIMEOUT_SECONDS)
            self._log_ready(started)
            flush = asyncio.create_task(self._flush_member_bytes(store, group))
            try:
                await self._relay()
            finally:
                flush.cancel()
                await self._flush_once(store, group)
            return
        if record and record.get("failed"):
            raise SessionSetupError(str(record["failed"]), retryable=False)
        logger.warning("No shared SPICE operation appeared for %s; this channel opens its own", self._label)
        await self._run_own_operation(started)

    def _is_own_display(self, record: Dict[str, Any], base: str) -> bool:
        """Whether the shared record is this socket's own display on this cluster's LXD."""
        operation_id = str(record.get("operation_id") or "")
        data_url = str(record.get("data_url") or "")
        return bool(
            operation_id
            and record.get("host") == self.host
            and record.get("project") == self.project
            and record.get("instance") == self.instance_name
            and data_url.startswith(f"{base}/1.0/operations/{operation_id}/websocket?secret=")
        )

    async def _renew_while_open(self, store: Any, group: str) -> None:
        while True:
            await asyncio.sleep(RENEW_EVERY_SECONDS)
            try:
                await store.renew(group)
                if self._operation_id:
                    await store.keep_display(self.host, self.project, self.instance_name, self._operation_id)
                await self._fold_member_bytes(store, group)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a missed renewal is retried next beat
                logger.debug("SPICE session renew failed: %s", type(exc).__name__)

    async def _fold_member_bytes(self, store: Any, group: str) -> None:
        down, up = await store.member_bytes(group)
        seen_down, seen_up = self._member_seen
        if self._session and (down > seen_down or up > seen_up):
            if down > seen_down:
                self._session.add_download_bytes(down - seen_down)
            if up > seen_up:
                self._session.add_upload_bytes(up - seen_up)
        self._member_seen = (max(down, seen_down), max(up, seen_up))

    async def _flush_member_bytes(self, store: Any, group: str) -> None:
        while True:
            await asyncio.sleep(_MEMBER_FLUSH_SECONDS)
            await self._flush_once(store, group)

    async def _flush_once(self, store: Any, group: str) -> None:
        down, up = self._member_bytes
        if not down and not up:
            return
        self._member_bytes = [0, 0]
        try:
            await store.add_bytes(group, down, up)
        except Exception as exc:  # noqa: BLE001 - stats only
            logger.debug("SPICE member byte flush failed: %s", type(exc).__name__)

    # ── one operation ────────────────────────────────────────────────────────────────────────

    async def _run_own_operation(self, started: float) -> None:
        self.role = "solo"
        await self._open_operation()
        self._log_ready(started)
        if self.console_type == "console":
            # A serial client shows "connected" on this; a SPICE stream must stay binary only.
            if self._session:
                await self.browser_ws.send_text(
                    json.dumps({"type": "session", "session_id": self._session.session_id})
                )
            await self.browser_ws.send_text(json.dumps({"type": "ready"}))
        await self._relay()

    async def _open_operation(self) -> Dict[str, Any]:
        """Create the LXD operation, track it, and connect its control and data sockets."""
        service = get_console_service()
        tracker = get_session_tracker()
        # The admission lock covers the daemon submission and the tracker registration only.
        async with guard_instance_mutation(
            cluster_id=self.host,
            project=self.project,
            instance_name=self.instance_name,
        ):
            try:
                info = await session_io(
                    service.open_console_operation,
                    self.instance_name,
                    self.host,
                    self.project,
                    self.console_type,
                    self._width,
                    self._height,
                    label=f"console.open:{self.host}",
                    host=self.host,
                )
            except SessionSetupError as exc:
                self._audit("failure", error=str(exc))
                raise
            session_id = new_session_id(info.get("operation_id"), "console")
            self._session = tracker.register(
                session_id=session_id,
                instance_name=self.instance_name,
                host=self.host,
                project=self.project,
                protocol=SessionProtocol.SERIAL if self.console_type == "console" else SessionProtocol.GRAPHICS,
                user_id=self.user_id,
                user_email=self.user_email,
                user_role=self.user_role,
                target_organization_id=self.target_organization_id,
                target_org_slug=self.target_org_slug,
                break_glass_grant_id=self.break_glass_grant_id,
            )
        self._audit("success")
        self._operation_id = str(info.get("operation_id") or "") or None
        if self.role == "owner" and self._operation_id:
            # note: before the sockets. The data socket is what makes QEMU drop the previous
            # viewer, whose relay then reads this mark to tell a takeover from a lost connection.
            try:
                await get_spice_session_store().mark_display(self.host, self.project, self.instance_name, self._operation_id)
            except Exception as exc:  # noqa: BLE001 - without it a takeover reads as a drop
                logger.debug("SPICE display mark failed: %s", type(exc).__name__)

        _, ssl_context = await session_io(connection_material, self.host, label=f"console.material:{self.host}", host=self.host)
        # note: control first. LXD ends a console operation whose control socket never arrives,
        # and a VGA data socket is what makes LXD dial QEMU's SPICE socket.
        self.control_ws = await open_lxd_socket(info["control_url"], ssl_context, timeout=_CONNECT_TIMEOUT_SECONDS, max_size=1024 * 1024)
        self.data_ws = await open_lxd_socket(info["ws_url"], ssl_context, timeout=_CONNECT_TIMEOUT_SECONDS)
        if self._session:
            tracker.mark_connected(self._session.session_id)
        return info

    def _audit(self, result: str, error: Optional[str] = None) -> None:
        details: Dict[str, Any] = {
            "transport": "websocket",
            "console_type": self.console_type,
            "shared_operation": self.role == "owner",
            "break_glass_grant_id": self.break_glass_grant_id,
        }
        kwargs: Dict[str, Any] = dict(
            action="console.session.create",
            target_type="instance",
            target_id=self.instance_name,
            target_host=self.host,
            target_project=self.project,
            result=result,
            details=details,
            target_organization_id=self.target_organization_id,
            target_organization_slug=self.target_org_slug,
        )
        if error:
            kwargs["error_message"] = error
        try:
            get_activity_logger().log_activity(**kwargs)
        except Exception as exc:  # noqa: BLE001 - the audit write never blocks a console
            logger.warning("Console activity record failed: %s", type(exc).__name__)

    def _log_ready(self, started: float) -> None:
        logger.info(
            "Console %s ready for %s as %s in %d ms",
            self.console_type, self._label, self.role, int((time.monotonic() - started) * 1000),
        )

    # ── relay ────────────────────────────────────────────────────────────────────────────────

    async def _relay(self) -> None:
        tasks = {
            asyncio.create_task(self._browser_to_lxd(), name="console.browser_to_lxd"),
            asyncio.create_task(self._lxd_to_browser(), name="console.lxd_to_browser"),
        }
        if self.control_ws is not None:
            tasks.add(asyncio.create_task(self._drain_control(), name="console.control"))
        if self._watch_power is not None:
            tasks.add(asyncio.create_task(self._power_down(self._watch_power), name="console.power"))
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # note: asyncio.wait leaves its tasks running when the relay itself is cancelled, and
            # the power watch would then hold its events subscription until the instance next
            # powered down.
            self._window.release()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        ended = next(iter(done))
        error = ended.exception() if not ended.cancelled() else None
        outcome = None if error else ended.result()
        if outcome == "browser":
            return
        if isinstance(outcome, str) and outcome.startswith("power:"):
            action = outcome.partition(":")[2]
            await close_browser(self.browser_ws, CLOSE_POWERED_DOWN, _POWER_DOWN_REASONS.get(action, "The instance shut down"))
            return
        if await self._taken_over():
            await close_browser(self.browser_ws, CLOSE_TAKEN_OVER, "Another viewer opened this console")
            return
        if error is None:
            await close_browser(self.browser_ws, CLOSE_NORMAL, "The console session ended")
        else:
            logger.info("Console relay for %s ended: %s", self._label, type(error).__name__)
            await close_browser(self.browser_ws, CLOSE_UPSTREAM, "Lost the connection to the console")

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
            if text and self.console_type == "console":
                await self._serial_control(text)
            # note: a SPICE client sends nothing but binary; stray text is not relayed into the
            # protocol stream, where it would corrupt the session.

    async def _serial_control(self, text: str) -> None:
        try:
            body = json.loads(text)
        except ValueError:
            return
        kind = body.get("type") if isinstance(body, dict) else None
        if kind == "resize" and self.control_ws is not None:
            width = _bounded(body.get("width"), 80, 20, 1000)
            height = _bounded(body.get("height"), 24, 5, 500)
            await self.control_ws.send(json.dumps({
                "command": "window-resize",
                "args": {"width": str(width), "height": str(height)},
            }))
        elif kind == "ack":
            self._window.ack(body.get("bytes"))
        elif kind == "ping":
            await self.browser_ws.send_text(json.dumps({"type": "pong"}))

    async def _lxd_to_browser(self) -> str:
        async for message in self.data_ws:
            payload = message if isinstance(message, (bytes, bytearray)) else str(message).encode("utf-8")
            await self._window.before_send()
            await self.browser_ws.send_bytes(bytes(payload))
            self._window.sent(len(payload))
            self._count(down=len(payload))
        return "lxd"

    async def _drain_control(self) -> str:
        async for _message in self.control_ws:
            pass
        return "control"

    async def _taken_over(self) -> bool:
        """Whether the display ended because a newer viewer's operation took it (shared displays only)."""
        if self.console_type != "vga" or self.role not in ("owner", "member") or not self._operation_id:
            return False
        try:
            current = await get_spice_session_store().display_of(self.host, self.project, self.instance_name)
        except Exception:  # noqa: BLE001 - unknown reads as a lost connection, as before
            return False
        return bool(current) and current != self._operation_id

    async def _power_down(self, watch: PowerWatch) -> str:
        try:
            action = await watch(self.host, self.instance_name, self.project)
        except Exception as exc:  # noqa: BLE001 - the console works without its watch
            logger.info("Power watch for %s unavailable: %s", self._label, type(exc).__name__)
            # Never end the relay from here: wait until a real ending cancels this task.
            await asyncio.get_running_loop().create_future()
            raise
        return f"power:{action}"

    def _count(self, down: int = 0, up: int = 0) -> None:
        if self.role == "member":
            self._member_bytes[0] += down
            self._member_bytes[1] += up
            return
        if self._session is None:
            return
        if down:
            self._session.add_download_bytes(down)
        if up:
            self._session.add_upload_bytes(up)

    # ── teardown ─────────────────────────────────────────────────────────────────────────────

    async def _cleanup(self) -> None:
        self._window.release()
        for socket in (self.data_ws, self.control_ws):
            if socket is None:
                continue
            try:
                await socket.close()
            except Exception:  # noqa: BLE001 - already closed
                pass
        # note: a failure record is left to expire, so a channel arriving late still reads why.
        if self.role == "owner" and self.group and self._published:
            store = get_spice_session_store()
            # The operation ended with the control socket; give the other channels a moment to
            # report their last bytes, then fold them in and forget the group.
            await asyncio.sleep(_MEMBER_SETTLE_SECONDS)
            try:
                await self._fold_member_bytes(store, self.group)
                await store.retract(self.group)
            except Exception as exc:  # noqa: BLE001 - the record expires on its own
                logger.debug("SPICE session retract failed: %s", type(exc).__name__)
        if self.role == "owner" and self._operation_id:
            # Also when setup failed after the mark was written: a stale mark would read a later
            # drop of the real viewer as a takeover and end it on 4409.
            try:
                await get_spice_session_store().forget_display(self.host, self.project, self.instance_name, self._operation_id)
            except Exception as exc:  # noqa: BLE001 - the mark expires on its own
                logger.debug("SPICE display mark cleanup failed: %s", type(exc).__name__)
        if self._session is not None:
            get_session_tracker().unregister(self._session.session_id)


def _bounded(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))

