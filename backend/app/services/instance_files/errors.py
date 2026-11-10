"""Stable failures a file operation can end in, and how an SFTP status becomes one of them.

SFTP v3 has nine status codes, so ENOSPC, EROFS, EXDEV, EBUSY and EEXIST all arrive as FAILURE
plus free text. That text comes from the peer, which a compromised guest writes, so it is matched
as a hint and never returned, logged at info or put in an audit record.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from fastapi import HTTPException


@dataclass
class GuestFileError(Exception):
    """A refusal with a code the interface can branch on and a message written here."""

    code: str
    status: int
    message: str
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.code)

    def to_http(self) -> HTTPException:
        headers = {"Retry-After": str(self.extra["retry_after"])} if "retry_after" in self.extra else None
        return HTTPException(
            status_code=self.status,
            detail={"error": self.code, "message": self.message, **self.extra},
            headers=headers,
        )


def protocol_error(reason: str) -> GuestFileError:
    # `reason` is written by this package and names the rule that broke, for the log line.
    return GuestFileError(
        "guest_protocol_error", 502, "The guest answered in a way this service does not accept.",
        {"reason": reason},
    )


def guest_timeout() -> GuestFileError:
    return GuestFileError(
        "guest_timeout", 504, "The guest did not answer in time. It may be busy or still starting."
    )


def guest_disconnected() -> GuestFileError:
    return GuestFileError(
        "guest_disconnected", 503, "The connection to the guest closed. The instance may have stopped."
    )


def agent_unavailable() -> GuestFileError:
    return GuestFileError(
        "agent_unavailable", 503, "The guest agent is not reachable inside this virtual machine."
    )


def cluster_unreachable() -> GuestFileError:
    return GuestFileError("cluster_unreachable", 503, "The cluster did not answer.")


def cluster_not_pinned() -> GuestFileError:
    return GuestFileError(
        "cluster_not_pinned", 503, "This cluster's certificate could not be confirmed, so nothing was sent."
    )


def not_running(status_text: str) -> GuestFileError:
    if status_text == "Frozen":
        return GuestFileError("instance_frozen", 409, "This instance is frozen. Resume it to reach its files.")
    return GuestFileError("instance_not_running", 409, "This instance is not running. Start it to reach its files.")


def not_found() -> GuestFileError:
    return GuestFileError("not_found", 404, "This path does not exist in the guest.")


def permission_denied() -> GuestFileError:
    return GuestFileError("permission_denied", 403, "The guest refused access to this path.")


def invalid_path(message: str) -> GuestFileError:
    return GuestFileError("invalid_path", 422, message)


def busy(retry_after: int = 3) -> GuestFileError:
    return GuestFileError(
        "busy", 429, "Too many file operations are running. Trying again shortly usually works.",
        {"retry_after": retry_after},
    )


def too_large(limit: int) -> GuestFileError:
    return GuestFileError("too_large", 413, "This is larger than the limit for this action.", {"limit_bytes": limit})


def not_regular_file() -> GuestFileError:
    return GuestFileError("not_regular_file", 409, "Only regular files can be read this way.")


def already_exists() -> GuestFileError:
    return GuestFileError("already_exists", 409, "Something with this name already exists.")


def no_space() -> GuestFileError:
    return GuestFileError("no_space", 507, "The guest does not have enough free space.")


def path_changed() -> GuestFileError:
    return GuestFileError("path_changed", 409, "This path changed while it was being used. Nothing was altered.")


def protected_path() -> GuestFileError:
    return GuestFileError("protected_path", 409, "This system path cannot be changed from here.")


# Matched lower-cased against Go's error text, which ends every SFTP failure on LXD. The first hit
# wins. Anything else stays guest_io_error, so a new message degrades to a vague answer, not a
# wrong one.
_HINTS = (
    ("file exists", lambda: already_exists()),
    ("directory not empty", lambda: GuestFileError("directory_not_empty", 409, "This folder is not empty.")),
    ("not a directory", lambda: GuestFileError("not_a_directory", 409, "This path is not a folder.")),
    ("is a directory", lambda: GuestFileError("is_a_directory", 409, "This path is a folder.")),
    ("cross-device", lambda: GuestFileError(
        "cross_device", 409, "This move crosses a disk boundary, which a rename cannot do."
    )),
    ("no space left", no_space),
    ("disk quota", no_space),
    ("read-only file system", lambda: GuestFileError("read_only_fs", 502, "This filesystem is read only.")),
    ("device or resource busy", lambda: GuestFileError("resource_busy", 409, "This path is in use, for example a mount.")),
    ("text file busy", lambda: GuestFileError("resource_busy", 409, "This file is in use by a running program.")),
    ("operation not permitted", lambda: GuestFileError(
        "operation_not_permitted", 403, "The guest does not permit this change. The file may be immutable."
    )),
    ("file name too long", lambda: invalid_path("This name is too long for the guest's filesystem.")),
    ("too many levels of symbolic", lambda: GuestFileError("symlink_loop", 409, "This path has a symlink loop.")),
)


def from_sftp_status(code: int, text: bytes = b"", path_kind: Optional[str] = None) -> GuestFileError:
    """The stable failure for an SFTP status. `text` is a hint and goes no further than here."""
    if code == 2:
        return not_found()
    if code == 3:
        return permission_denied()
    if code == 8:
        return GuestFileError("unsupported_server", 503, "This cluster does not support that operation.")
    lowered = text[:512].decode("utf-8", "replace").lower()
    for needle, build in _HINTS:
        if needle in lowered:
            return build()
    return GuestFileError("guest_io_error", 502, "The guest could not complete this operation.")
