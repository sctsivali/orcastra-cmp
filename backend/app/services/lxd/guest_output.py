"""Running a guest command whose output is too big for the streaming exec path to deliver intact.

important: this exists because pylxd's `Instance.execute` silently loses output above roughly
12 KiB, and "silently" is the part that matters. Measured against LXD 5.21.7 on a live cluster:

    12000 bytes asked for -> 12000 returned
    16000 bytes asked for -> 0 returned
    16384 bytes asked for -> 8192 returned

with `exit_code` 0 and the bounded stream's `truncated` flag FALSE in every case, so a caller is
told the command succeeded and handed a short or empty result with nothing to indicate loss.

The cause is a race in pylxd (`models/instance.py`). `execute` waits for the operation to report a
return code, then calls `finish_soon()` on the stdout and stderr sockets. In
`_CommandWebsocketClient.received_message`, the FIRST binary frame that arrives once `finish_off`
is set marks the socket `finished`, and `execute` then stops the manager. Any frame still in
flight behind it is dropped. Below about 12 KiB the output has already arrived by the time the
command exits, so the race never opens.

The way around it is not to stream at all. LXD's own `record-output` writes stdout and stderr to
files on the LXD HOST and hands back their paths, which can then be read with an ordinary bounded
HTTP GET. Nothing is written inside the guest, which keeps the property the capture path depends
on, and the log is deleted afterwards on the host where the delete is ours to make.

The cost is three round trips instead of one, so this is for the calls that need it rather than
for exec in general.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from .proxy import FILE_CHUNK_BYTES

logger = logging.getLogger(__name__)

__all__ = ["RecordedResult", "run_recorded"]

# stderr is diagnostics: a marker line and whatever a tool complained about. A guest that writes
# megabytes there is misbehaving, and reading it all would be the memory hole this module exists
# to avoid on the other stream.
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
    response = inst.api.logs["exec-output"][name].get(
        is_api=False, stream=True, timeout=(3, 30)
    )
    return _read_bounded(response, limit)


def _discard_log(inst: Any, name: str) -> None:
    """Best effort. A log left behind is untidy on the host, never a leak inside the guest."""
    try:
        inst.api.logs["exec-output"][name].delete(timeout=(3, 15))
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

    # The socket window has to outlast the command's own budget, or the wait expires on a path
    # that does not know the work survived. Same rule the proxy's `_client_for` already applies.
    inst.client.api.operations[operation_id].wait.get(timeout=(3, int(timeout) + 10))
    operation = inst.client.api.operations[operation_id].get(timeout=(3, 15)).json()["metadata"]

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
        # Always, including on a failed read: the log is on the host and nothing else will clean
        # it up. LXD does not expire these on its own.
        for name in (stdout_name, stderr_name):
            if name:
                _discard_log(inst, name)

    return RecordedResult(
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
        int(exit_code),
        truncated,
    )
