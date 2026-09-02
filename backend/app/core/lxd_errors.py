"""One mapper from an LXD daemon error onto an HTTP status.

The daemon answers a refused delete, a name clash and an unreachable socket with the same
kind of opaque string, and a router that catches `Exception` turns all three into 500. That
is wrong in a way operators feel: "the volume is attached to an instance" is their mistake
to fix and reads as a server fault, so they retry instead of detaching.

This lived in `routers/networks.py` and was the only correct copy in the backend. It is here
so the image and storage-volume delete paths share it rather than growing a third variant
that drifts.
"""

from typing import Optional

import requests.exceptions as requests_exc
import urllib3.exceptions as urllib3_exc
from fastapi import HTTPException
from urllib3.exceptions import MaxRetryError

from app.core.error_sanitizer import sanitize_error

# note: matched against the lowercased message. The daemon's wording is not a stable API, so
# these stay broad on purpose. A miss costs a 400 with the real text, never a wrong success.
_NOT_FOUND = ("not found", "no such object")
_CONFLICT = ("already exists",)
# "is busy running a ..." is LXD refusing a second operation on an instance that already has
# one in flight. It is a conflict in exactly the way an in-use volume is: the caller can retry
# once the first operation finishes, and calling it a server fault sends them to the wrong
# place. Observed on a real create/start race, where DELETE answered 409 for this and START
# answered 500 for the identical condition.
# important: "is used by", not the looser "used by". LXD's in-use refusals read
# "Network default is used by instance web-01", but the bare substring also matches
# "The image used by this instance is incompatible with secureboot", which is a
# configuration problem and not a conflict at all. Observed: that message was being
# answered 409, telling the operator to wait for something to finish when the real
# remedy was to change a config key.
_IN_USE = ("in use", "is used by", "is busy", "busy running")
_UNREACHABLE = ("connect", "connection", "unreachable", "timed out", "timeout")


def lxd_http_error(exc: Exception, noun: str, name: str) -> HTTPException:
    """Map an LXD/daemon error onto a meaningful status instead of a blanket 500.

    `noun` and `name` only shape the 404 and 409 wording, ex; ("Image", fingerprint).
    """
    msg = str(exc)
    low = msg.lower()
    if any(t in low for t in _NOT_FOUND):
        return HTTPException(status_code=404, detail=f"{noun} '{name}' not found")
    if any(t in low for t in _CONFLICT):
        return HTTPException(status_code=409, detail=f"{noun} '{name}' already exists")
    if any(t in low for t in _UNREACHABLE):
        return HTTPException(status_code=503, detail="Could not reach the LXD host")
    if any(t in low for t in _IN_USE):
        return HTTPException(status_code=409, detail=sanitize_error(msg))
    # Remaining LXD errors are user-actionable validation failures.
    return HTTPException(status_code=400, detail=sanitize_error(msg))


def read_timed_out(exc: BaseException) -> bool:
    """True when the peer accepted the connection and then stopped answering.

    important: this cannot be decided by `isinstance(exc, ReadTimeout)`, which is the obvious
    thing to write and would never fire against this app's pooled clients. `connection.py`
    mounts `Retry(read=0)`, and urllib3 treats that 0 as a counter rather than as False
    (util/retry.py increment), so it decrements to -1, `is_exhausted()` sees a truthy negative,
    and the failure surfaces as MaxRetryError. requests then finds no ConnectTimeoutError, no
    ResponseError and no proxy or SSL cause, so it falls through to a plain ConnectionError
    (adapters.py). `requests.exceptions.ReadTimeout` is only raised on the path that bypasses
    Retry entirely, which the pooled adapter never takes.

    Both shapes are accepted, because a caller that builds its own session without a Retry
    would produce the first one.
    """
    if isinstance(exc, requests_exc.ReadTimeout):
        return True
    if isinstance(exc, requests_exc.ConnectTimeout):
        return False
    cause = exc.args[0] if getattr(exc, "args", None) else None
    if isinstance(cause, MaxRetryError):
        return isinstance(cause.reason, urllib3_exc.ReadTimeoutError)
    return False


def lxd_transport_error(exc: BaseException, host: str, action: str) -> HTTPException:
    """Map a transport failure onto the status that tells an operator what to do.

    504 when the host answered the connection and then ran past the read budget: it is up, it
    is reachable, and it is slower than we are willing to wait, which points at daemon or
    storage load. That is already this house's answer for an upstream that did not finish, at
    routers/images.py, routers/networks.py, routers/vms.py and routers/instances.py.

    503 for anything else, because "we never got a usable connection" is what `require_client`
    and the reachability breaker already mean, and collapsing the two would tell an operator
    their healthy cluster is offline.

    note: the detail survives to the client. main.py replaces the body of a 500 with an opaque
    reference and keeps 502/503/504 intact, which is precisely why the old blanket 500 reached
    the interface as an unactionable id.
    """
    if read_timed_out(exc):
        return HTTPException(
            status_code=504,
            detail=f"{host} did not finish {action} in time",
        )
    detail: Optional[str] = sanitize_error(str(exc)) if str(exc) else None
    return HTTPException(
        status_code=503,
        detail=f"Could not reach {host} while {action}" + (f": {detail}" if detail else ""),
    )
