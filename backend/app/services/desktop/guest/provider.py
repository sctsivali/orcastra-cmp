"""Driving a guest's desktop through the exec path that already exists.

Every call goes out as `exec_instance`, which means it inherits, rather than re-implements: the
argv-only contract, the bounded output streams, the privilege drop to a session user, the audit
record that stores the exact command, and the capability gate on top. A click is literally
`["xdotool","mousemove","--sync","678","568","click","1"]` in the audit index a year later.

important: the bounded stream is constructed HERE with its own cap rather than borrowing exec's.
`_BoundedStream` already takes a `limit`; `EXEC_OUTPUT_LIMIT` is only its default. A screenshot is
several hundred kilobytes of base64 against a 256 KiB default, so borrowing it would silently
return truncated base64, and truncated base64 is a corrupt image in front of a vision model. That
is the worst failure mode available here, so truncation is a hard error on the capture path rather
than the reported success it is for a command's output.
"""

import base64
import binascii
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from app.services.lxd.guest_exec import _BoundedStream
from app.services.lxd.guest_output import run_recorded

from ..image_header import read_geometry, within_model_limits
from ..protocol import Action, ActionResult, Display, ProbeReport, Target
from ..runtime import (
    DESKTOP_IMAGE_SOFT_LIMIT,
    DESKTOP_SCREENSHOT_TIMEOUT,
    DESKTOP_MAX_LONG_EDGE,
    DESKTOP_MAX_VISUAL_TOKENS,
    DESKTOP_STDERR_LIMIT,
    DESKTOP_STDOUT_LIMIT,
    DESKTOP_TARGET_HEIGHT,
    DESKTOP_TARGET_WIDTH,
)
from . import chains
from .probe import PROBE_SCRIPT, build_report

logger = logging.getLogger(__name__)

__all__ = ["GuestExecProvider"]

_GEOM = re.compile(r"^ORCA_GEOM\s+(\d+)\s+(\d+)\s*$", re.M)
_FMT = re.compile(r"^ORCA_FMT\s+(png|jpeg)\s*$", re.M)
_CURSOR_X = re.compile(r"^X=(\d+)\s*$", re.M)
_CURSOR_Y = re.compile(r"^Y=(\d+)\s*$", re.M)


class GuestExecProvider:
    """Capture and input driven by tooling inside the guest."""

    name = "guest-exec"

    def __init__(self, instance: Any) -> None:
        self._instance = instance

    # ── running one command inside the guest ───────────────────────────────────────────────────

    def _run_recorded(
        self,
        argv: List[str],
        *,
        stdout_limit: int,
        environment: Optional[Dict[str, str]] = None,
        user: Optional[int] = None,
        timeout: float = 30.0,
    ) -> Tuple[str, str, int, bool]:
        """For output the streaming path cannot deliver intact.

        important: a screenshot is always tens to hundreds of kilobytes, which is well past the
        point where pylxd's `execute` starts dropping frames without saying so. `guest_output`
        explains the race and the measurements. Nothing is written inside the guest either way:
        LXD records to a file on its own host, which is then read and deleted there.
        """
        return run_recorded(
            self._instance,
            argv,
            stdout_limit=stdout_limit,
            environment=environment or None,
            user=user,
            timeout=timeout,
        )

    def _run(
        self,
        argv: List[str],
        *,
        stdout_limit: int,
        environment: Optional[Dict[str, str]] = None,
        user: Optional[int] = None,
        stdin: Optional[str] = None,
    ) -> Tuple[str, str, int, bool]:
        stdout = _BoundedStream(limit=stdout_limit)
        stderr = _BoundedStream(limit=DESKTOP_STDERR_LIMIT)
        result = self._instance.execute(
            argv,
            stdout_handler=stdout,
            stderr_handler=stderr,
            environment=environment or None,
            user=user,
            stdin_payload=stdin,
            # important: same reason exec_instance passes it. Frames stay bytes, so the cap counts
            # bytes and a capture cannot raise inside pylxd's unguarded utf-8 decode on its way
            # through. A screenshot is the most binary thing this surface will ever carry.
            decode=False,
        )
        exit_code = getattr(result, "exit_code", 0)
        return stdout.text(), stderr.text(), exit_code, stdout.truncated

    # ── probe ──────────────────────────────────────────────────────────────────────────────────

    def probe(self, target: Target, *, agent: bool = True) -> ProbeReport:
        if not agent:
            return build_report("", agent=False)
        stdout, _stderr, _code, _truncated = self._run(
            ["/bin/sh", "-c", PROBE_SCRIPT], stdout_limit=DESKTOP_STDERR_LIMIT
        )
        return build_report(stdout, agent=True)

    # ── acting ─────────────────────────────────────────────────────────────────────────────────

    def perform(self, target: Target, action: Action, report: ProbeReport) -> ActionResult:
        if action.name in ("screenshot", "zoom"):
            return self._capture(action, report)
        if action.name == "cursor_position":
            return self._cursor_position(report)
        if action.name == "clipboard_read":
            return self._clipboard_read(report)
        return self._input(action, report)

    def _capture(self, action: Action, report: ProbeReport) -> ActionResult:
        if not report.capture_tool:
            return ActionResult(
                ok=False, action=action.name, error_code="desktop_not_ready",
                detail=report.reason or "this guest has no usable capture tool",
            )
        argv = chains.capture_chain(
            report.session_type,
            report.capture_tool,
            width=DESKTOP_TARGET_WIDTH,
            height=DESKTOP_TARGET_HEIGHT,
            jpeg_threshold=DESKTOP_IMAGE_SOFT_LIMIT,
        )
        stdout, stderr, exit_code, truncated = self._run_recorded(
            argv,
            stdout_limit=DESKTOP_STDOUT_LIMIT,
            environment=report.environment or None,
            timeout=DESKTOP_SCREENSHOT_TIMEOUT,
        )
        if truncated:
            # Inverting exec's contract deliberately. A truncated command output is a reported
            # success; a truncated image is bytes that will decode to garbage or not at all.
            return ActionResult(
                ok=False, action=action.name, error_code="desktop_capture_truncated",
                detail=(
                    f"the capture exceeded the {DESKTOP_STDOUT_LIMIT} byte transport cap. Reduce "
                    "the guest's display resolution."
                ),
            )
        if exit_code != 0 or not stdout.strip():
            return ActionResult(
                ok=False, action=action.name, error_code="desktop_capture_failed",
                detail=(stderr.strip() or f"capture exited {exit_code} with no output")[:400],
            )

        try:
            payload = base64.b64decode(stdout.strip(), validate=True)
        except (binascii.Error, ValueError):
            return ActionResult(
                ok=False, action=action.name, error_code="desktop_capture_failed",
                detail="the guest returned something that is not base64",
            )

        # Header only, never a decode. The bytes come from a guest that may be compromised, and an
        # image parser inside the control plane would be the first place that could be used.
        geometry = read_geometry(payload)
        if geometry is None:
            return ActionResult(
                ok=False, action=action.name, error_code="desktop_capture_failed",
                detail="the guest returned bytes that are not a PNG or JPEG",
            )
        allowed, why = within_model_limits(
            geometry,
            max_long_edge=DESKTOP_MAX_LONG_EDGE,
            max_visual_tokens=DESKTOP_MAX_VISUAL_TOKENS,
        )
        if not allowed:
            # Refusing beats returning it: a computer-use tool result over the model's limits is
            # rejected with a validation error the operator cannot diagnose from the far side.
            return ActionResult(
                ok=False, action=action.name, error_code="desktop_display_too_large", detail=why
            )

        fmt = _FMT.search(stderr)
        media_type = f"image/{fmt.group(1)}" if fmt else "image/png"
        return ActionResult(
            ok=True,
            action=action.name,
            display=self._display_from(stderr, geometry),
            image_base64=stdout.strip(),
            image_media_type=media_type,
            image_bytes=len(payload),
        )

    def _cursor_position(self, report: ProbeReport) -> ActionResult:
        if report.session_type != "x11" or report.input_tool != "xdotool":
            # Wayland has no unprivileged way to ask. Saying so beats inventing a coordinate.
            return ActionResult(
                ok=False, action="cursor_position", error_code="desktop_unsupported",
                detail="the pointer position can only be read on an X11 session",
            )
        stdout, stderr, exit_code, _ = self._run(
            chains.cursor_position_chain(report.session_type, report.input_tool),
            stdout_limit=DESKTOP_STDERR_LIMIT,
            environment=report.environment or None,
        )
        x, y = _CURSOR_X.search(stdout), _CURSOR_Y.search(stdout)
        if exit_code != 0 or not x or not y:
            return ActionResult(
                ok=False, action="cursor_position", error_code="desktop_action_failed",
                detail=(stderr.strip() or "could not read the pointer position")[:400],
            )
        # Back into screenshot pixels, the units every coordinate on this surface is in.
        display = report.display or Display(width=DESKTOP_TARGET_WIDTH, height=DESKTOP_TARGET_HEIGHT)
        reported = display.to_screenshot(int(x.group(1)), int(y.group(1)))
        return ActionResult(
            ok=True, action="cursor_position", cursor=reported, display=display
        )

    def _clipboard_read(self, report: ProbeReport) -> ActionResult:
        try:
            argv = chains.clipboard_read_chain(report.session_type)
        except ValueError as exc:
            return ActionResult(
                ok=False, action="clipboard_read", error_code="desktop_unsupported", detail=str(exc)
            )
        # Recorded too: a clipboard holding a pasted document is exactly the size that goes
        # missing on the streaming path, and losing half of it silently would be worse than
        # failing.
        stdout, stderr, exit_code, truncated = self._run_recorded(
            argv, stdout_limit=DESKTOP_STDOUT_LIMIT, environment=report.environment or None
        )
        if exit_code != 0:
            return ActionResult(
                ok=False, action="clipboard_read", error_code="desktop_action_failed",
                detail=(stderr.strip() or f"clipboard read exited {exit_code}")[:400],
            )
        # note: the contents are returned to the caller and never logged, at any level. This is a
        # channel for whatever the user last copied, and an operator debugging a failure must not
        # end up with it in the log index.
        return ActionResult(
            ok=True, action="clipboard_read",
            extra={"text": stdout, "truncated": truncated, "bytes": len(stdout.encode("utf-8"))},
        )

    def _input(self, action: Action, report: ProbeReport) -> ActionResult:
        if not report.input_tool:
            return ActionResult(
                ok=False, action=action.name, error_code="desktop_not_ready",
                detail=report.reason or "this guest has no usable input tool",
            )
        try:
            argv, stdin = self._input_chain(action, report)
        except (ValueError, KeyError) as exc:
            return ActionResult(
                ok=False, action=action.name, error_code="desktop_unsupported",
                detail=f"{action.name} is not supported on a {report.session_type} session ({exc})",
            )
        _stdout, stderr, exit_code, _ = self._run(
            argv,
            stdout_limit=DESKTOP_STDERR_LIMIT,
            environment=report.environment or None,
            stdin=stdin,
        )
        if exit_code != 0:
            return ActionResult(
                ok=False, action=action.name, error_code="desktop_action_failed",
                detail=(stderr.strip() or f"the action exited {exit_code}")[:400],
            )
        return ActionResult(ok=True, action=action.name, display=report.display)

    def _input_chain(self, action: Action, report: ProbeReport) -> Tuple[List[str], Optional[str]]:
        session, tool = report.session_type, report.input_tool
        display = report.display or Display(width=DESKTOP_TARGET_WIDTH, height=DESKTOP_TARGET_HEIGHT)
        point = display.to_guest(*action.coordinate) if action.coordinate else None

        if action.name == "mouse_move":
            return chains.mouse_move_chain(session, tool, *point), None
        if action.name in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
            button = {"left_click": "left", "right_click": "right", "middle_click": "middle",
                      "double_click": "left", "triple_click": "left"}[action.name]
            repeat = {"double_click": 2, "triple_click": 3}.get(action.name, 1)
            return chains.click_chain(
                session, tool, button, coordinate=point, repeat=repeat, modifiers=action.modifiers
            ), None
        if action.name == "left_click_drag":
            end = display.to_guest(*action.end_coordinate)
            return chains.drag_chain(session, tool, point, end), None
        if action.name in ("left_mouse_down", "left_mouse_up"):
            verb = "mousedown" if action.name == "left_mouse_down" else "mouseup"
            return ["xdotool", verb, "1"], None
        if action.name == "scroll":
            return chains.scroll_chain(
                session, tool, action.direction, action.amount, coordinate=point
            ), None
        if action.name == "type":
            # important: only when the probe actually proved `--file` works. Falling back to argv
            # is correct but not free: argv is visible in the guest's process table to every user
            # on that machine, and the first thing an agent types at a login prompt is a password.
            use_stdin = tool == "xdotool" and report.supports_type_file
            argv = chains.type_chain(session, tool, use_stdin=use_stdin, text=action.text)
            return argv, (action.text if use_stdin else None)
        if action.name == "key":
            return chains.key_chain(session, tool, action.text, action.modifiers, action.amount), None
        if action.name == "hold_key":
            return chains.hold_key_chain(
                session, tool, action.text, action.modifiers, action.duration
            ), None
        raise ValueError(f"no chain for {action.name}")

    @staticmethod
    def _display_from(stderr: str, geometry: Any) -> Display:
        """Geometry the guest reported, falling back to what the image itself says.

        The image is authoritative for what the model is looking at; the guest's own report is
        authoritative for where a click lands. When a capture was resized they differ, and `scale`
        is what reconciles them.
        """
        reported = _GEOM.search(stderr)
        if not reported:
            return Display(width=geometry.width, height=geometry.height, scale=1.0)
        guest_width, guest_height = int(reported.group(1)), int(reported.group(2))
        # Derived from whichever axis is non-zero, and cross-checked against the other. A capture
        # tool that letterboxes rather than scaling uniformly would give two different ratios, and
        # silently trusting the width would put every click a fixed fraction off.
        scale_x = (geometry.width / guest_width) if guest_width else 1.0
        scale_y = (geometry.height / guest_height) if guest_height else scale_x
        if abs(scale_x - scale_y) > 0.02:
            logger.warning(
                "Capture scaled unevenly (%.3f by width, %.3f by height) on a %sx%s display; "
                "using the smaller so a click lands inside the target rather than past it",
                scale_x, scale_y, guest_width, guest_height,
            )
        return Display(
            width=geometry.width, height=geometry.height, scale=min(scale_x, scale_y)
        )
