"""The two ways this package runs something in a guest.

- `read`: LXD's `record-output` exec over the pinned REST session (`services/lxd/guest_output.py`).
  Used for everything without stdin, because the streaming path silently drops output above about
  12 KiB and a guest chooses how much it prints.
- `with_stdin`: the pinned exec in `pinned_exec.py`, only for the two calls that need stdin
  (chpasswd and the authorized_keys write). pylxd's own exec sockets are never used: they do not
  check the cluster's certificate, and what crosses them here is a password.

Both run as the uid and gid they are given; the key file is always touched as its owner.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.core.blocking import BlockingPoolFull, BlockingTimeout
from app.services.lxd.guest_output import run_recorded

from . import errors, runtime
from .pinned_exec import PinnedExecResult, run_with_stdin

logger = logging.getLogger(__name__)

# note: C locale so tools answer in the words the parsers expect; no HOME, since a uid exec has
# none and nothing here may depend on one.
ENVIRONMENT: Dict[str, str] = {"LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}


@dataclass(frozen=True)
class Output:
    stdout: str
    stderr: str
    exit_code: int
    truncated: bool


@dataclass
class Guest:
    """One instance this request reached, plus what admission learnt about it."""

    host: str
    project: str
    name: str
    instance: Any
    state: Dict[str, Any]
    instance_type: str

    async def read(self, argv: List[str], *, user: Optional[int] = None, group: Optional[int] = None,
                   limit: int = runtime.STDOUT_LIMIT, label: str = "read") -> Output:
        def call() -> Output:
            stdout, stderr, code, truncated = run_recorded(
                self.instance, argv, stdout_limit=limit, environment=ENVIRONMENT,
                user=user, group=group, timeout=runtime.EXEC_TIMEOUT,
            )
            return Output(stdout, stderr, code, truncated)

        try:
            return await runtime.blocking(call, label=f"guest_credentials.{label}", host=self.host)
        except BlockingPoolFull:
            raise errors.busy() from None
        except BlockingTimeout:
            raise errors.guest_timeout() from None
        except errors.CredentialError:
            raise
        except Exception as exc:  # noqa: BLE001 - a guest or cluster failure, never a stack to the caller
            raise _classify(exc) from None

    async def with_stdin(self, argv: List[str], stdin: bytes, *, user: Optional[int] = None,
                         group: Optional[int] = None, label: str = "write") -> PinnedExecResult:
        return await run_with_stdin(self, argv, stdin, user=user, group=group, label=label)


def _classify(exc: Exception) -> errors.CredentialError:
    if getattr(exc, "status_code", None) == 504:
        return errors.guest_timeout()
    text = str(exc).lower()
    if "agent" in text:
        return errors.agent_unavailable()
    if "timed out" in text or "timeout" in text:
        return errors.guest_timeout()
    if "not running" in text:
        return errors.not_running("")
    if "/bin/sh" in text and ("no such file" in text or "not found" in text):
        return errors.shell_missing()
    logger.warning("A guest credentials call failed: %s", type(exc).__name__)
    return errors.guest_failed()
