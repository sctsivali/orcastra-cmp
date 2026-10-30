"""Guest paths as JSON strings, and which of them this service refuses to touch.

A Linux file name is bytes, and JSON cannot carry bytes that are not UTF-8. Pydantic refuses lone
surrogates, so Python's `surrogateescape` does not survive the trip. Instead an undecodable byte
0xNN travels as the private code point U+10FCNN, which every layer treats as an ordinary character,
and comes back as that byte. A real U+10FC80..U+10FCFF in a name is escaped the same way, one byte
at a time, so every name round-trips exactly. The interface shows these as `\\xNN`.
"""

import posixpath
import re
from typing import Iterable

from .errors import invalid_path, protected_path

MAX_PATH_BYTES = 4096
MAX_NAME_BYTES = 255

_ESCAPE_BASE = 0x10FC00
_ESCAPED = re.compile("[\U0010FC80-\U0010FCFF]")
_SURROGATE = re.compile("[\uDC80-\uDCFF]")
# Control characters are refused in a name this service creates. A guest may already hold such a
# file, which stays reachable, but nothing here adds one.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# Kernel views, not storage. A write here changes the running system, and a tree walk never ends.
PSEUDO_ROOTS = ("/proc", "/sys", "/dev")
# The top of a Linux system. Removing or renaming one of these is never what somebody meant.
SYSTEM_ROOTS = frozenset(
    {
        "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64", "/libx32",
        "/opt", "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr", "/var",
    }
)


def _escape_bytes(raw: bytes) -> str:
    return "".join(chr(_ESCAPE_BASE + byte) for byte in raw)


def from_guest(raw: bytes) -> str:
    """A guest path as a string that `to_guest` turns back into exactly these bytes."""
    text = raw.decode("utf-8", "surrogateescape")
    text = _ESCAPED.sub(lambda match: _escape_bytes(match.group().encode("utf-8")), text)
    return _SURROGATE.sub(lambda match: chr(_ESCAPE_BASE + (ord(match.group()) - 0xDC00)), text)


def to_guest(text: str) -> bytes:
    restored = _ESCAPED.sub(lambda match: chr(0xDC00 + (ord(match.group()) - _ESCAPE_BASE)), text)
    return restored.encode("utf-8", "surrogateescape")


def clean_path(value: str) -> str:
    """The absolute, normalized path a request names, or a refusal.

    note: `..` is folded here rather than refused. LXD resolves paths inside the guest, so it is
    no traversal against this host, and folding means the audit record says what was reached.
    """
    if not value.startswith("/"):
        raise invalid_path("A path must be absolute.")
    if "\x00" in value:
        raise invalid_path("A path cannot contain a null byte.")
    normalized = "/" + posixpath.normpath(value).lstrip("/")
    if len(to_guest(normalized)) > MAX_PATH_BYTES:
        raise invalid_path("This path is too long.")
    return normalized


def clean_name(value: str) -> str:
    """One new file or folder name, as somebody typed or dropped it."""
    if value in ("", ".", ".."):
        raise invalid_path("This is not a usable name.")
    if "/" in value or _CONTROL.search(value):
        raise invalid_path("A name cannot contain a slash or a control character.")
    if len(to_guest(value)) > MAX_NAME_BYTES:
        raise invalid_path("This name is too long.")
    return value


def is_safe_entry(raw: bytes) -> bool:
    """Whether a name a directory listing returned can be a child of that directory.

    important: a hostile agent can answer READDIR with `../../etc/shadow`. The interface joins a
    name to its folder to act on it, so such an entry must never reach it.
    """
    return bool(raw) and raw not in (b".", b"..") and b"/" not in raw and b"\x00" not in raw


def join(parent: str, name: str) -> str:
    return parent.rstrip("/") + "/" + name


def is_pseudo(path: str) -> bool:
    return any(path == root or path.startswith(root + "/") for root in PSEUDO_ROOTS)


def refuse_protected(paths: Iterable[str]) -> None:
    """Enforced here, not only in the interface: a system root is never removed or moved."""
    for path in paths:
        if path in SYSTEM_ROOTS or is_pseudo(path):
            raise protected_path()
