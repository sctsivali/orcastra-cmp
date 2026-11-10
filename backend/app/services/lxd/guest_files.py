"""Reading and writing one file inside a guest, for the key-authenticated surface.

Implements the `instance.files` capability. Kept apart from `guest_exec` and `proxy_operations`
because it reaches inside a guest and carries bounds of its own.

important: the read is bounded as chunks arrive, which is why this does not call pylxd's
`FilesManager.get`. That returns `response.content` (pylxd/models/instance.py) and buffers the
whole body, so reading `/dev/zero`, a sparse image or a multi-gigabyte log would exhaust the
worker's memory. `_BoundedStream` solves the same problem on the exec path.
"""

import base64
import binascii
import json
import logging
from typing import Any, Dict, Optional

from fastapi import HTTPException

from .guest_exec import _identity
from .proxy import FILE_CHUNK_BYTES, FILE_READ_LIMIT, FILE_WRITE_LIMIT

logger = logging.getLogger(__name__)

__all__ = ["get_file", "put_file"]

# Paths are guest-side and absolute. LXD resolves them inside the guest, so `..` is no traversal
# against this host. A relative path is refused: it would resolve against a working directory this
# layer does not choose, and could mean different things on different LXD versions.
_MAX_PATH_BYTES = 4096


def _validated_path(params: Dict[str, Any]) -> str:
    path = params.get("path")
    if not isinstance(path, str) or not path:
        raise HTTPException(status_code=400, detail="params.path must be a non-empty string")
    if not path.startswith("/"):
        raise HTTPException(
            status_code=400,
            detail="params.path must be absolute, so the request means one thing on every host",
        )
    if "\x00" in path:
        raise HTTPException(status_code=400, detail="params.path must not contain a null byte")
    if len(path.encode("utf-8")) > _MAX_PATH_BYTES:
        raise HTTPException(
            status_code=400, detail=f"params.path exceeds {_MAX_PATH_BYTES} bytes"
        )
    return path


def _validated_mode(params: Dict[str, Any]) -> Optional[str]:
    """An octal file mode, as LXD wants it: a string like ``0644``.

    Accepts an int or a string and normalises here, so a bad value gets a clear 400 instead of
    LXD's unmarshalling error.
    """
    mode = params.get("mode")
    if mode is None:
        return None
    if isinstance(mode, bool):
        raise HTTPException(status_code=400, detail="params.mode must be an octal string or int")
    if isinstance(mode, int):
        if not 0 <= mode <= 0o7777:
            raise HTTPException(status_code=400, detail="params.mode is out of range")
        return format(mode, "04o")
    if isinstance(mode, str):
        candidate = mode.strip()
        try:
            value = int(candidate, 8)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="params.mode must be octal, ex; \"0644\""
            ) from None
        if not 0 <= value <= 0o7777:
            raise HTTPException(status_code=400, detail="params.mode is out of range")
        return format(value, "04o")
    raise HTTPException(status_code=400, detail="params.mode must be an octal string or int")


def _payload_bytes(params: Dict[str, Any]) -> bytes:
    """The bytes to write, from exactly one of ``content_base64`` or ``content``.

    Text can be sent as is, and a binary goes as base64 so no UTF-8 round trip mangles it. Naming
    both is refused, since silently preferring one would hide which was written.
    """
    raw = params.get("content_base64")
    text = params.get("content")
    if raw is not None and text is not None:
        raise HTTPException(
            status_code=400,
            detail="pass either params.content or params.content_base64, not both",
        )
    if raw is not None:
        if not isinstance(raw, str):
            raise HTTPException(status_code=400, detail="params.content_base64 must be a string")
        try:
            # validate=True so a truncated or mistyped payload is an error instead of shorter
            # bytes landing in the guest.
            payload = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(
                status_code=400, detail="params.content_base64 is not valid base64"
            ) from None
    elif text is not None:
        if not isinstance(text, str):
            raise HTTPException(status_code=400, detail="params.content must be a string")
        payload = text.encode("utf-8")
    else:
        raise HTTPException(
            status_code=400, detail="params.content or params.content_base64 is required"
        )

    if len(payload) > FILE_WRITE_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"content exceeds {FILE_WRITE_LIMIT} bytes after decoding",
        )
    return payload


def _read_bounded(response: Any) -> tuple[bytes, bool]:
    """Collect the body up to the cap, stopping as chunks arrive.

    Returns the bytes and whether more were available. The connection is closed either way, so
    stopping early does not return a half-read socket to the pool.
    """
    chunks: list[bytes] = []
    size = 0
    truncated = False
    try:
        for chunk in response.iter_content(chunk_size=FILE_CHUNK_BYTES):
            if not chunk:
                continue
            remaining = FILE_READ_LIMIT - size
            if remaining <= 0:
                truncated = True
                break
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                size = FILE_READ_LIMIT
                truncated = True
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        response.close()
    return b"".join(chunks), truncated


def _maybe_text(payload: bytes) -> Optional[str]:
    """The bytes as text, when they are text.

    Strict decode: a file that is not UTF-8 gets `text: null` and the caller reads
    content_base64, instead of a string of replacement characters that looks like the file.
    """
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return None


def get_file(inst: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Read one path inside a guest.

    A directory returns its entries instead of an error. Listing is how a caller finds a file,
    and refusing it would push callers to `exec_instance` with `ls`, a larger capability.
    """
    path = _validated_path(params)

    # is_api=False because the body is the file, not an LXD response envelope. stream=True keeps
    # requests from buffering the whole body before the cap can apply.
    response = inst.api.files.get(
        params={"path": path}, is_api=False, stream=True
    )

    headers = response.headers or {}
    entry_type = headers.get("X-LXD-type", "file")

    if entry_type == "directory":
        payload, truncated = _read_bounded(response)
        try:
            listing = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(
                status_code=502, detail=f"{path} is a directory but its listing was unreadable"
            ) from None
        entries = listing.get("metadata") if isinstance(listing, dict) else listing
        return {
            "path": path,
            "type": "directory",
            "entries": entries if isinstance(entries, list) else [],
            "truncated": truncated,
        }

    payload, truncated = _read_bounded(response)
    text = _maybe_text(payload)

    # important: never log the content, at any level, or a file like /etc/shadow ends up in the
    # log index. The exec path applies the same rule to command output.
    return {
        "path": path,
        "type": "symlink" if entry_type == "symlink" else "file",
        "content_base64": base64.b64encode(payload).decode("ascii"),
        "text": text,
        "size_bytes": len(payload),
        "truncated": truncated,
        "read_limit_bytes": FILE_READ_LIMIT,
        "uid": headers.get("X-LXD-uid"),
        "gid": headers.get("X-LXD-gid"),
        "mode": headers.get("X-LXD-mode"),
    }


def put_file(inst: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """Write one path inside a guest, creating or replacing it."""
    path = _validated_path(params)
    payload = _payload_bytes(params)
    mode = _validated_mode(params)
    uid = _identity(params, "uid")
    gid = _identity(params, "gid")

    headers: Dict[str, str] = {}
    if mode is not None:
        headers["X-LXD-mode"] = mode
    if uid is not None:
        headers["X-LXD-uid"] = str(uid)
    if gid is not None:
        headers["X-LXD-gid"] = str(gid)

    inst.api.files.post(params={"path": path}, data=payload, headers=headers)

    # size_bytes is the payload length. LXD writes a file all or nothing, so it is also the
    # written size.
    return {
        "path": path,
        "written": True,
        "size_bytes": len(payload),
        "mode": mode,
        "uid": uid,
        "gid": gid,
    }
