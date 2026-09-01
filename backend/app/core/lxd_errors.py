"""One mapper from an LXD daemon error onto an HTTP status.

The daemon answers a refused delete, a name clash and an unreachable socket with the same
kind of opaque string, and a router that catches `Exception` turns all three into 500. That
is wrong in a way operators feel: "the volume is attached to an instance" is their mistake
to fix and reads as a server fault, so they retry instead of detaching.

This lived in `routers/networks.py` and was the only correct copy in the backend. It is here
so the image and storage-volume delete paths share it rather than growing a third variant
that drifts.
"""

from fastapi import HTTPException

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
