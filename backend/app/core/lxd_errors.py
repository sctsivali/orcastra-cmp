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
_IN_USE = ("in use", "used by")
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
