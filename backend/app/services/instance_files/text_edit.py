"""Reading and saving a text file, and sniffing an image, all in memory and all bounded."""

import hashlib
import time
from typing import Any, Dict, Optional, Tuple

from . import sftp_codec as codec
from .errors import GuestFileError, not_regular_file, too_large
from .operations import resolve_regular
from .paths import from_guest, is_pseudo, to_guest
from .sftp_client import SftpClient
from .uploads_temp import TEMP_LIFETIME_SECONDS, temp_name

MAX_TEXT_BYTES = 2_000_000
MAX_IMAGE_BYTES = 10_000_000

_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


async def _read_up_to(guest: SftpClient, raw: bytes, cap: int) -> bytes:
    """The file's bytes, read to end of file rather than to its stated size.

    important: /proc/cpuinfo states size 0 and has content, and a guest can state anything, so the
    stated size decides nothing here. Only `cap` does, and one byte past it proves the file is over.
    """
    handle = await guest.open(raw, codec.PFLAG_READ)
    held = bytearray()
    try:
        async for piece in guest.read_stream(handle, limit=cap + 1):
            held += piece
    finally:
        await guest.close(handle)
    if len(held) > cap:
        raise too_large(cap)
    return bytes(held)


def etag_of(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


async def read_text(guest: SftpClient, path: str) -> Dict[str, Any]:
    raw, attrs = await resolve_regular(guest, path)
    if (attrs.size or 0) > MAX_TEXT_BYTES:
        raise too_large(MAX_TEXT_BYTES)
    payload = await _read_up_to(guest, raw, MAX_TEXT_BYTES)
    if b"\x00" in payload:
        raise GuestFileError("not_text", 409, "This file is not text. It can be downloaded instead.")
    try:
        text = payload.decode("utf-8")
        read_only = False
    except UnicodeDecodeError:
        # Readable, but a save would have to invent the bytes that failed to decode.
        text = payload.decode("utf-8", "replace")
        read_only = True
    return {
        "path": from_guest(raw),
        "text": text,
        "etag": etag_of(payload),
        "size": len(payload),
        "read_only": read_only,
    }


async def _atomic_replace(
    guest: SftpClient, target: bytes, payload: bytes, attrs: codec.Attrs, parent: bytes
) -> bool:
    """Temp then rename, so a crash leaves one whole version or the other. False if impossible."""
    temp = parent.rstrip(b"/") + b"/" + to_guest(temp_name(_expiry()))
    try:
        handle = await guest.open(temp, codec.PFLAG_WRITE | codec.PFLAG_CREAT | codec.PFLAG_EXCL, mode=0o600)
    except GuestFileError as failure:
        if failure.status >= 500 and failure.code not in ("read_only_fs", "guest_io_error"):
            raise
        return False
    try:
        await guest.write_all(handle, 0, payload)
        await guest.fsetstat(handle, codec.encode_attrs(uid=attrs.uid, gid=attrs.gid, mode=(attrs.mode or 0) & 0o7777))
        await guest.close(handle)
        await guest.replace(temp, target)
    except GuestFileError as failure:
        try:
            await guest.remove(temp)
        except GuestFileError:
            pass
        if failure.code in ("resource_busy", "operation_not_permitted", "guest_io_error", "cross_device"):
            return False
        raise
    return True


def _expiry() -> int:
    return int(time.time()) + TEMP_LIFETIME_SECONDS


async def write_text(
    guest: SftpClient,
    path: str,
    text: str,
    etag: str,
    *,
    allow_in_place: bool = False,
) -> Dict[str, Any]:
    payload = text.encode("utf-8")
    if len(payload) > MAX_TEXT_BYTES:
        raise too_large(MAX_TEXT_BYTES)
    # A link is edited through: replacing it with a file would break whatever relied on the link.
    raw, attrs = await resolve_regular(guest, path)
    if is_pseudo(from_guest(raw)):
        raise not_regular_file()
    current = await _read_up_to(guest, raw, MAX_TEXT_BYTES)
    if etag_of(current) != etag:
        raise GuestFileError(
            "etag_mismatch", 409, "This file changed after it was opened.", {"current_etag": etag_of(current)}
        )
    parent = raw.rsplit(b"/", 1)[0] or b"/"
    if allow_in_place:
        handle, _ = await guest.open_unchanged(raw, codec.PFLAG_WRITE, attrs)
        try:
            await guest.fsetstat(handle, codec.encode_attrs(size=0))
            await guest.write_all(handle, 0, payload)
        finally:
            await guest.close(handle)
    elif not await _atomic_replace(guest, raw, payload, attrs, parent):
        # A bind-mounted or immutable file cannot be renamed over. Writing through it is not atomic,
        # so a person chooses that with the risk in front of them.
        raise GuestFileError(
            "atomic_replace_unavailable", 409,
            "This file cannot be replaced safely, for example because it is a mount. "
            "Writing directly risks a partial file if the connection drops.",
        )
    return {"path": from_guest(raw), "etag": etag_of(payload), "size": len(payload)}


def sniff_image(payload: bytes) -> Optional[str]:
    """A fixed MIME type for bytes whose magic matches an image. Never SVG: it can carry script."""
    for magic, mime in _IMAGE_MAGIC:
        if payload.startswith(magic):
            return mime
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    return None


async def read_image(guest: SftpClient, path: str) -> Tuple[bytes, str]:
    raw, attrs = await resolve_regular(guest, path)
    if (attrs.size or 0) > MAX_IMAGE_BYTES:
        raise too_large(MAX_IMAGE_BYTES)
    payload = await _read_up_to(guest, raw, MAX_IMAGE_BYTES)
    mime = sniff_image(payload)
    if mime is None:
        raise GuestFileError("not_previewable", 409, "This file cannot be previewed.")
    return payload, mime
