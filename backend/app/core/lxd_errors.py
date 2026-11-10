"""One mapper from an LXD daemon error onto an HTTP status.

The daemon answers a refused delete, a name clash and an unreachable socket with the same
kind of opaque string, and a router that catches `Exception` turns all three into 500. "The
volume is attached to an instance" is the operator's to fix; reported as a server fault, it
sends them to retry instead of detaching. The LXD routers share it so the mapping cannot drift.
"""

from typing import Optional

import requests.exceptions as requests_exc
import urllib3.exceptions as urllib3_exc
from fastapi import HTTPException
from pylxd.exceptions import LXDAPIException
from urllib3.exceptions import MaxRetryError

from app.core.error_sanitizer import sanitize_error

# Matched against the lowercased message. The daemon's wording is not a stable API, so these
# stay broad; a miss costs a 400 with the real text, never a wrong success.
_NOT_FOUND = ("not found", "no such object")
_CONFLICT = ("already exists",)
# "is busy running a ..." is LXD refusing a second operation on an instance that already has
# one in flight: a conflict like an in-use volume, which the caller can retry once the first
# operation finishes.
# important: match "is used by", never the bare "used by". LXD's in-use refusals read
# "Network default is used by instance web-01", but "used by" also matches "The image used by
# this instance is incompatible with secureboot", a configuration problem a 409 misreports.
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
        return HTTPException(status_code=503, detail="Could not reach the cluster")
    if any(t in low for t in _IN_USE):
        return HTTPException(status_code=409, detail=sanitize_error(msg))
    # Remaining LXD errors are user-actionable validation failures.
    return HTTPException(status_code=400, detail=sanitize_error(msg))


def read_timed_out(exc: BaseException) -> bool:
    """True when the peer accepted the connection and then stopped answering.

    important: `isinstance(exc, ReadTimeout)` never fires against this app's pooled clients.
    services/lxd/connection.py mounts `Retry(read=0)`, and urllib3 treats that 0 as a counter
    (util/retry.py increment): it decrements to -1, `is_exhausted()` sees a truthy negative,
    and the failure surfaces as MaxRetryError, which requests turns into a plain ConnectionError
    (adapters.py). `requests.exceptions.ReadTimeout` comes only from a session without a Retry,
    such as a caller's own, so both shapes are accepted.
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

    504 when the host accepted the connection and then ran past the read budget: it is up but
    slow, which points at daemon or storage load. routers/images.py, routers/networks.py,
    routers/vms.py and routers/instances.py answer an upstream that did not finish the same
    way.

    503 for anything else: no usable connection, which is what `require_client` and the
    reachability breaker already mean. Merging the two would report a healthy cluster offline.

    The detail reaches the client: main.py replaces the body of a 500 with an opaque reference
    but keeps 502, 503 and 504 intact.
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


def definitive_lxd_refusal(exc: LXDAPIException, phase: str) -> bool:
    """Whether LXD supplied a terminal verdict rather than a transport/wait ambiguity."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if phase == "submitting" and isinstance(status_code, int) and 400 <= status_code < 500:
        return True
    try:
        metadata = response.json().get("metadata")
    except Exception:
        return False
    return isinstance(metadata, dict) and metadata.get("status") in {"Failure", "Cancelled"}
