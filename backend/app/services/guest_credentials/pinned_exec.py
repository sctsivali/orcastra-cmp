"""A non-interactive exec whose stdin crosses only sockets held to the cluster's certificate pin.

important: this exists because pylxd's `Instance.execute` opens its exec sockets with ws4py and
only a client certificate (`pylxd/client.py`, `ssl_options`), so the peer is never checked against
the pin the REST session enforces. On those sockets the fd 0 secret travels in the upgrade request,
and on this path what follows is root's chpasswd line. Anyone between this service and the cluster
could read the password, or open fd 0 first and set their own. Here every socket goes through
`lxd_ws.open_lxd_socket`, which drops a stranger certificate before the upgrade line is sent, and
admission has already refused a cluster for which no pin is held.

The sequence: POST the exec (pinned REST), open fds 0, 1, 2 and control together (LXD starts the
command only when all four are connected), send stdin as one binary frame then an empty text frame
(LXD's end of input, the convention pylxd and `lxc exec` use), read stdout and stderr until LXD
closes them, then read the exit code from the operation. Waiting for the close is what avoids the
frame race that makes pylxd lose output.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from app.core.blocking import BlockingPoolFull, BlockingTimeout
from app.core.lxd_path import safe_segment
from app.services.lxd_ws import SessionSetupError, open_lxd_socket, socket_material

from . import errors, runtime

if TYPE_CHECKING:
    from .transport import Guest

logger = logging.getLogger(__name__)

_SOCKET_TIMEOUT = 5.0


@dataclass(frozen=True)
class PinnedExecResult:
    stdout: str
    stderr: str
    exit_code: int


async def _drain(socket: Any, limit: int) -> bytes:
    kept = bytearray()
    try:
        async for message in socket:
            if isinstance(message, str):
                message = message.encode("utf-8", "replace")
            if len(kept) < limit:
                kept.extend(message[: limit - len(kept)])
    except Exception:  # noqa: BLE001 - a closed or reset stream ends the read; the exit code decides
        pass
    return bytes(kept)


async def run_with_stdin(
    guest: "Guest",
    argv: List[str],
    stdin: bytes,
    *,
    user: Optional[int],
    group: Optional[int],
    label: str,
) -> PinnedExecResult:
    from .transport import ENVIRONMENT

    body: Dict[str, Any] = {
        "command": argv,
        "environment": ENVIRONMENT,
        "wait-for-websocket": True,
        "interactive": False,
        "record-output": False,
    }
    if user is not None:
        body["user"] = user
    if group is not None:
        body["group"] = group

    def start() -> Dict[str, Any]:
        base, context, pin = socket_material(guest.host)
        if not pin:
            raise errors.cluster_not_pinned()
        operation = guest.instance.api.exec.post(json=body, timeout=(3, 15)).json()
        return {"base": base, "context": context, "pin": pin, "operation": operation}

    try:
        started = await runtime.blocking(start, label=f"guest_credentials.{label}.start", host=guest.host)
    except BlockingPoolFull:
        raise errors.busy() from None
    except BlockingTimeout:
        raise errors.guest_timeout() from None
    except errors.CredentialError:
        raise
    except Exception as exc:  # noqa: BLE001 - before stdin left, so this is an ordinary failure
        logger.warning("Starting a guest credentials exec failed: %s", type(exc).__name__)
        raise errors.guest_failed() from None

    metadata = started["operation"].get("metadata") or {}
    operation_id = str(metadata.get("id") or "")
    fds = (metadata.get("metadata") or {}).get("fds") or {}
    if not operation_id or not all(fds.get(key) for key in ("0", "1", "2", "control")):
        raise errors.guest_failed()
    socket_base = f"{started['base']}/1.0/operations/{safe_segment(operation_id)}/websocket"

    def url(key: str) -> str:
        return f"{socket_base}?secret={fds[key]}"

    sockets: List[Any] = []
    stdin_sent = False
    try:
        async with asyncio.timeout(runtime.EXEC_TIMEOUT):
            opened = await asyncio.gather(
                *(open_lxd_socket(url(key), started["context"], pin=started["pin"], timeout=_SOCKET_TIMEOUT,
                                  max_size=64 * 1024)
                  for key in ("0", "1", "2", "control")),
                return_exceptions=True,
            )
            sockets = [item for item in opened if not isinstance(item, BaseException)]
            failure = next((item for item in opened if isinstance(item, BaseException)), None)
            if failure is not None:
                if isinstance(failure, SessionSetupError) and not failure.retryable:
                    raise errors.cluster_not_pinned()
                raise errors.guest_failed()
            stdin_socket, stdout_socket, stderr_socket, _control = opened
            readers = (
                asyncio.ensure_future(_drain(stdout_socket, runtime.STDIN_CALL_OUTPUT_LIMIT)),
                asyncio.ensure_future(_drain(stderr_socket, runtime.STDIN_CALL_OUTPUT_LIMIT)),
            )
            stdin_sent = True
            await stdin_socket.send(stdin)
            await stdin_socket.send("")
            stdout, stderr = await asyncio.gather(*readers)
    except errors.CredentialError:
        if stdin_sent:
            raise errors.outcome_unknown() from None
        raise
    except Exception as exc:  # noqa: BLE001 - TimeoutError or a dropped socket
        logger.warning("A guest credentials exec did not finish: %s", type(exc).__name__)
        raise (errors.outcome_unknown() if stdin_sent else errors.guest_failed()) from None
    finally:
        for socket in sockets:
            try:
                await socket.close()
            except Exception:  # noqa: BLE001 - closing a dead socket is not news
                pass

    def finish() -> Optional[int]:
        operations = guest.instance.client.api.operations[safe_segment(operation_id)]
        waited = operations.wait.get(params={"timeout": 10}, timeout=(3, 15)).json()
        return ((waited.get("metadata") or {}).get("metadata") or {}).get("return")

    try:
        exit_code = await runtime.blocking(finish, label=f"guest_credentials.{label}.finish", host=guest.host)
    except Exception as exc:  # noqa: BLE001 - stdin went out; only the answer is missing
        logger.warning("Reading a guest credentials exit code failed: %s", type(exc).__name__)
        raise errors.outcome_unknown() from None
    if exit_code is None:
        raise errors.outcome_unknown()
    return PinnedExecResult(
        stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace"), int(exit_code)
    )
