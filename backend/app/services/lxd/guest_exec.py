"""Running one command inside a guest, for the key-authenticated surface.

Split out of ``proxy_operations`` because it is a different kind of act from the rest of that
vocabulary: everything else there manages an instance from outside, and this reaches in. It is
also the only operation with bounds of its own, since the output size and the runtime are both
chosen by the caller.

important: this exists because the power already did, without any of the controls. The raw proxy
refuses exactly one path (``/certificates``), so ``POST /1.0/instances/<n>/exec`` ran arbitrary
commands as root for any key holding ``lxd.proxy`` and ``write``, with no capability specific to
it, no size cap, and an audit trail that recorded the path but never the command. Demonstrated
against this deployment before the fix: ``uid=0(root)`` from an ordinary preset key.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from .proxy import EXEC_OUTPUT_LIMIT, EXEC_STDIN_LIMIT

logger = logging.getLogger(__name__)

__all__ = ["exec_instance"]


class _BoundedStream:
    """Collects one output stream, and stops collecting once it has enough.

    important: the cap has to apply as frames ARRIVE, not to the finished result. pylxd appends
    every frame to a list and joins it on access, so truncating afterwards bounds the response
    while the worker has already held the whole thing, plus a copy, in memory. `cat /dev/zero`
    would grow that until the container died. Passing a handler makes pylxd hand each frame here
    instead of buffering it (``pylxd/models/instance.py``), so this is where the bound belongs.

    note: bytes, not characters. The limit is advertised in bytes and a multibyte-heavy stream
    would otherwise return several times what the caller was told.
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
        # errors="replace" rather than a raise: a caller that runs `cat` on a binary should get
        # replacement characters, not a failed call. pylxd's own decode has no error handler,
        # which is why this reads the bytes itself.
        return b"".join(self._chunks).decode("utf-8", errors="replace")


def _argv(params: Dict[str, Any]) -> List[str]:
    """The command, as an argv list.

    important: a list, never a shell string. LXD takes argv, so accepting `"rm -rf /tmp/x"` would
    mean this layer inventing a quoting rule, and every quoting rule invented at a boundary is a
    place where the audit record and what actually ran disagree. A caller that genuinely wants a
    shell asks for one: `["/bin/sh", "-c", "..."]`, which is explicit in the audit trail.
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

    Passing a string through produced an LXD unmarshal error surfaced with LXD's own wording,
    which tells a caller nothing about which field it got wrong.
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


def exec_instance(inst: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Run one command inside a guest and return what it did.

    note: a non-zero exit code is a SUCCESSFUL call reporting a failed command. Mapping it to an
    error would make "grep found nothing" indistinguishable from "the cluster is unreachable",
    which is the distinction an unattended caller most needs.
    """
    command = _argv(params)
    stdin_payload = _validated_stdin(params)
    environment = _validated_environment(params)
    user = _identity(params, "user")
    group = _identity(params, "group")

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
        # note: passing these through is a security feature, not a convenience. LXD execs as root
        # by default, and naming a user is the only way an integration can drop privilege inside
        # the guest.
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
    }
