"""Running one command inside a guest, for the key-authenticated surface.

Kept apart from ``proxy_operations``: everything else there manages an instance from outside,
and this reaches in. It is also the only operation with bounds of its own, since the caller
chooses the output size and the runtime.

important: exec runs as root unless the caller names a user. It costs its own capability
(``instance.exec``), its output is capped, and the audit record carries the command.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from .proxy import EXEC_OUTPUT_LIMIT, EXEC_STDIN_LIMIT, EXEC_TIMEOUT

logger = logging.getLogger(__name__)

__all__ = ["exec_instance"]


class _BoundedStream:
    """Collects one output stream, and stops collecting once it has enough.

    important: the cap applies as frames arrive. pylxd buffers every frame and joins them on
    access, so truncating afterwards would let `cat /dev/zero` exhaust the worker's memory.
    Passing a handler makes pylxd hand each frame here instead (``pylxd/models/instance.py``).

    The limit counts bytes, as advertised; counting characters would let a multibyte stream
    return several times that.
    """

    def __init__(self, limit: int = EXEC_OUTPUT_LIMIT) -> None:
        self._chunks: List[bytes] = []
        self._size = 0
        self._limit = limit
        self.truncated = False

    def __call__(self, frame: Any) -> None:
        if not frame:
            return
        if isinstance(frame, str):
            frame = frame.encode("utf-8", errors="replace")
        remaining = self._limit - self._size
        if remaining <= 0:
            self.truncated = True
            return
        if len(frame) > remaining:
            self._chunks.append(frame[:remaining])
            self._size = self._limit
            self.truncated = True
            return
        self._chunks.append(frame)
        self._size += len(frame)

    @property
    def size(self) -> int:
        return self._size

    def text(self) -> str:
        # errors="replace" so `cat` on a binary returns replacement characters instead of
        # failing. pylxd's own decode has no error handler, so this decodes the bytes itself.
        return b"".join(self._chunks).decode("utf-8", errors="replace")


def _argv(params: Dict[str, Any]) -> List[str]:
    """The command, as an argv list.

    important: a list, never a shell string. LXD takes argv; splitting a string here would need a
    quoting rule invented at this layer, and then the audit record and what ran could disagree. A
    caller that wants a shell passes `["/bin/sh", "-c", "..."]`, which the audit trail shows.
    """
    command = params.get("command")
    if isinstance(command, str):
        raise HTTPException(
            status_code=400,
            detail=(
                "params.command must be a list of arguments, not a string. For a shell, pass "
                '["/bin/sh", "-c", "<script>"] so the audit record says that is what ran.'
            ),
        )
    if not isinstance(command, list) or not command:
        raise HTTPException(status_code=400, detail="params.command must be a non-empty list")
    if not all(isinstance(argument, str) for argument in command):
        raise HTTPException(status_code=400, detail="every entry in params.command must be a string")
    return command


def _identity(params: Dict[str, Any], field: str) -> Optional[int]:
    """A uid or gid, validated here rather than handed to LXD to reject.

    LXD rejects a wrong type with an unmarshal error that does not name the field.
    """
    value = params.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HTTPException(
            status_code=400, detail=f"params.{field} must be a non-negative integer uid"
        )
    return value


def _validated_stdin(params: Dict[str, Any]) -> Optional[str]:
    payload = params.get("stdin")
    if payload is None:
        return None
    if not isinstance(payload, str):
        raise HTTPException(status_code=400, detail="params.stdin must be a string")
    if len(payload.encode("utf-8")) > EXEC_STDIN_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"params.stdin exceeds {EXEC_STDIN_LIMIT} bytes; use instance.files instead",
        )
    return payload


def _validated_environment(params: Dict[str, Any]) -> Optional[Dict[str, str]]:
    environment = params.get("environment") or {}
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
    ):
        raise HTTPException(status_code=400, detail="params.environment must be a string map")
    return environment or None


def _exec_recorded(
    inst: Any,
    command: List[str],
    environment: Optional[Dict[str, str]],
    user: Optional[int],
    group: Optional[int],
    params: Dict[str, Any],
) -> Dict[str, Any]:
    """The same contract, over the transport that does not drop frames."""
    from .guest_output import run_recorded

    stdout, stderr, exit_code, truncated = run_recorded(
        inst,
        command,
        stdout_limit=EXEC_OUTPUT_LIMIT,
        environment=environment,
        user=user,
        group=group,
        timeout=EXEC_TIMEOUT,
    )
    if truncated:
        logger.warning(
            "exec output truncated at %s bytes on %s", EXEC_OUTPUT_LIMIT, getattr(inst, "name", "?")
        )
    return {
        "name": inst.name,
        "command": command,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_bytes": len(stdout.encode("utf-8")),
        "stderr_bytes": len(stderr.encode("utf-8")),
        "truncated": truncated,
        "output_limit_bytes": EXEC_OUTPUT_LIMIT,
        # Which path ran, so a caller can tell whether the streaming path's frame loss applies.
        # The recorded form does not support cwd, so a request naming one runs as "streaming".
        "transport": "recorded",
    }


def exec_instance(inst: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Run one command inside a guest and return what it did.

    A non-zero exit code is a successful call reporting a failed command. Mapping it to an
    error would make "grep found nothing" look the same as "the cluster is unreachable".
    """
    command = _argv(params)
    stdin_payload = _validated_stdin(params)
    environment = _validated_environment(params)
    user = _identity(params, "user")
    group = _identity(params, "group")

    if stdin_payload is None and not params.get("cwd"):
        # important: the default path, because the streaming one loses output. pylxd's `execute`
        # calls `finish_soon()` once the operation reports a return code, and the next frame to
        # arrive stops the manager, dropping frames still in flight. On LXD 5.21.7, 16000 bytes
        # of output came back as 0 and 16384 as 8192, with exit_code 0 and `truncated` false.
        #
        # `record-output` has LXD write both streams to files on its own host, read back with a
        # bounded GET and deleted. No race, and nothing is written inside the guest.
        return _exec_recorded(inst, command, environment, user, group, params)

    # stdin keeps the streaming path: LXD refuses `wait-for-websocket` together with
    # `record-output`, and stdin needs the websocket. The loss above still applies to a call that
    # sends stdin and reads back more than about 12 KiB; `transport` tells the caller which ran.
    stdout, stderr = _BoundedStream(), _BoundedStream()
    result = inst.execute(
        command,
        environment=environment,
        stdin_payload=stdin_payload,
        # decode=False keeps the frames as bytes so the cap counts bytes and a binary stream
        # cannot raise inside pylxd's unguarded utf-8 decode.
        decode=False,
        stdout_handler=stdout,
        stderr_handler=stderr,
        # important: LXD execs as root by default; naming a user is the only way an integration
        # can drop privilege inside the guest.
        user=user,
        group=group,
        cwd=params.get("cwd"),
    )

    truncated = stdout.truncated or stderr.truncated
    if truncated:
        logger.warning(
            "exec output truncated at %s bytes on %s", EXEC_OUTPUT_LIMIT, getattr(inst, "name", "?")
        )
    return {
        "name": inst.name,
        "command": command,
        "exit_code": result.exit_code,
        "stdout": stdout.text(),
        "stderr": stderr.text(),
        "stdout_bytes": stdout.size,
        "stderr_bytes": stderr.size,
        "truncated": truncated,
        "output_limit_bytes": EXEC_OUTPUT_LIMIT,
        "transport": "streaming",
    }
