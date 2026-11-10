"""Running a guest command through LXD's recorded output instead of the streaming exec socket.

important: pylxd's `Instance.execute` can lose output above roughly 12 KiB without saying so. On
LXD 5.21.7, 16000 bytes of output came back as 0 and 16384 as 8192, with `exit_code` 0 and
`truncated` false. The cause is a race in pylxd (`models/instance.py`): `execute` calls
`finish_soon()` once the operation reports a return code, the next binary frame marks the socket
finished in `_CommandWebsocketClient.received_message`, and frames still in flight are dropped.

LXD's `record-output` writes stdout and stderr to files on the LXD host and returns their paths,
which are read with a bounded GET and then deleted. Nothing is written inside the guest. It costs
three round trips instead of one; `guest_exec` uses it for every call without stdin or cwd.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from .proxy import FILE_CHUNK_BYTES
from app.core.lxd_path import safe_segment

logger = logging.getLogger(__name__)

__all__ = ["RecordedResult", "run_recorded"]

# stderr carries diagnostics, so it gets a fixed cap of its own. A guest writing megabytes there
# is misbehaving, and reading it all would hold that much in the worker's memory.
_STDERR_LIMIT = 64 * 1024


class RecordedResult(tuple):
    """(stdout, stderr, exit_code, truncated), as a tuple so it destructures like the exec path."""

    __slots__ = ()

    def __new__(cls, stdout: str, stderr: str, exit_code: int, truncated: bool) -> "RecordedResult":
        return super().__new__(cls, (stdout, stderr, exit_code, truncated))


def _read_bounded(response: Any, limit: int) -> Tuple[bytes, bool]:
    """Collect a log body up to `limit`, stopping as chunks arrive rather than after."""
    chunks: List[bytes] = []
    size = 0
    truncated = False
    try:
        for chunk in response.iter_content(chunk_size=FILE_CHUNK_BYTES):
            if not chunk:
                continue
            remaining = limit - size
            if remaining <= 0:
                truncated = True
                break
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                truncated = True
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        response.close()
    return b"".join(chunks), truncated


def _fetch_log(inst: Any, name: str, limit: int) -> Tuple[bytes, bool]:
    response = inst.api.logs["exec-output"][safe_segment(name)].get(
        is_api=False, stream=True, timeout=(3, 30)
    )
    return _read_bounded(response, limit)


def _discard_log(inst: Any, name: str) -> None:
    """Best effort. A log left behind is untidy on the host, never a leak inside the guest."""
    try:
        inst.api.logs["exec-output"][safe_segment(name)].delete(timeout=(3, 15))
    except Exception:  # noqa: BLE001
        logger.warning("Could not remove the recorded exec log %s on %s", name, inst.name)


def run_recorded(
    inst: Any,
    argv: List[str],
    *,
    stdout_limit: int,
    environment: Optional[Dict[str, str]] = None,
    user: Optional[int] = None,
    group: Optional[int] = None,
    timeout: float = 30.0,
) -> RecordedResult:
    """Run one command and read its output from LXD's recorded logs rather than a live socket."""
    body: Dict[str, Any] = {
        "command": argv,
        "environment": environment or {},
        "wait-for-websocket": False,
        "interactive": False,
        "record-output": True,
    }
    if user is not None:
        body["user"] = user
    if group is not None:
        body["group"] = group

    started = inst.api.exec.post(json=body, timeout=(3, int(timeout) + 5)).json()
    operation_id = started["operation"].rsplit("/", 1)[-1]

    # The socket window has to outlast the command's own budget, or the wait expires while the
    # command is still running. The proxy's `_client_for` applies the same rule.
    inst.client.api.operations[safe_segment(operation_id)].wait.get(timeout=(3, int(timeout) + 10))
    operation = inst.client.api.operations[safe_segment(operation_id)].get(timeout=(3, 15)).json()["metadata"]

    metadata = operation.get("metadata") or {}
    exit_code = metadata.get("return")
    if exit_code is None:
        raise HTTPException(
            status_code=504,
            detail=f"the command did not finish within {timeout:g}s on {inst.name}",
        )

    output = metadata.get("output") or {}
    stdout_name = (output.get("1") or "").rsplit("/", 1)[-1]
    stderr_name = (output.get("2") or "").rsplit("/", 1)[-1]

    stdout_bytes, truncated = (b"", False)
    stderr_bytes = b""
    try:
        if stdout_name:
            stdout_bytes, truncated = _fetch_log(inst, stdout_name, stdout_limit)
        if stderr_name:
            stderr_bytes, _ = _fetch_log(inst, stderr_name, _STDERR_LIMIT)
    finally:
        # Always, including on a failed read: LXD does not expire these logs on its own.
        for name in (stdout_name, stderr_name):
            if name:
                _discard_log(inst, name)

    return RecordedResult(
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
        int(exit_code),
        truncated,
    )
