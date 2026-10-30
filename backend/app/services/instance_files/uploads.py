"""Uploading a file of any allowed size, in chunks that each survive on their own.

A chunk is a complete, separately authorized request, which is what makes a long upload outlive a
token refresh, a dropped connection, a worker restart or a proxy that limits one request's body.
The session is a signed token rather than a record: Redis here is `allkeys-lru` and shared with the
cache, so eviction could end an upload, and a database row would need a migration to hold what the
token already proves. The client keeps the offset, because after a failed chunk only it knows what
was acknowledged: pipelined writes leave holes, so the temp's size proves nothing.
"""

import base64
import hashlib
import hmac
import json
import time
from contextlib import aclosing
from dataclasses import asdict, dataclass
from typing import Any, AsyncIterator, Dict, Optional

from . import sftp_codec as codec
from .errors import GuestFileError, already_exists, no_space, too_large
from .operations import _move_without_replacing, make_directory, stat_path
from .paths import clean_name, clean_path, is_pseudo, is_safe_entry, from_guest, to_guest
from .sftp_client import SftpClient, is_directory, is_symlink
from .uploads_temp import TEMP_LIFETIME_SECONDS, is_expired_temp, is_upload_temp, temp_name

MAX_CHUNK_BYTES = 32 * 1024 * 1024
CHUNK_HINT_BYTES = 8 * 1024 * 1024
MAX_SESSIONS_PER_FOLDER = 8
_FLUSH_BYTES = 2 * 1024 * 1024
_MAC_CONTEXT = b"orcastra:instance-file-upload:v1"


def session_lost() -> GuestFileError:
    return GuestFileError("upload_session_lost", 409, "This upload can no longer continue. Start it again.")


@dataclass(frozen=True)
class UploadSession:
    user: str
    host: str
    project: str
    instance: str
    temp: str
    dest: str
    size: int
    on_conflict: str
    expires_at: int


def _mac_key() -> bytes:
    from app.core.config import get_settings

    secret = get_settings().secret_key or ""
    if not secret:
        raise GuestFileError("feature_disabled", 503, "File access is turned off on this deployment.")
    return hmac.new(secret.encode("utf-8"), _MAC_CONTEXT, hashlib.sha256).digest()


def seal(session: UploadSession) -> str:
    body = base64.urlsafe_b64encode(json.dumps(asdict(session), separators=(",", ":")).encode("utf-8"))
    mac = hmac.new(_mac_key(), body, hashlib.sha256).hexdigest()
    return f"{body.decode('ascii')}.{mac}"


def unseal(token: str, *, user: str, host: str, project: str, instance: str) -> UploadSession:
    """The session a token proves, or a refusal. Bound to the caller and the target in the URL."""
    body, _, mac = token.partition(".")
    expected = hmac.new(_mac_key(), body.encode("ascii", "ignore"), hashlib.sha256).hexdigest()
    if not mac or not hmac.compare_digest(mac, expected):
        raise session_lost()
    try:
        session = UploadSession(**json.loads(base64.urlsafe_b64decode(body)))
    except (ValueError, TypeError) as exc:
        raise session_lost() from exc
    if (session.user, session.host, session.project, session.instance) != (user, host, project, instance):
        raise session_lost()
    if session.expires_at < time.time():
        raise session_lost()
    return session


async def sweep_temps(guest: SftpClient, folder: bytes) -> int:
    """Remove temps past their expiry here and report how many are still in use."""
    now = time.time()
    active = 0
    expired = []
    seen = 0
    async with aclosing(guest.iter_directory(folder)) as batches:
        async for batch in batches:
            for item in batch:
                seen += 1
                name = from_guest(item.filename)
                if is_upload_temp(name) and is_safe_entry(item.filename):
                    if is_expired_temp(name, now):
                        expired.append(item.filename)
                    else:
                        active += 1
            if seen >= 20_000:
                break
    for filename in expired:
        try:
            await guest.remove(folder.rstrip(b"/") + b"/" + filename)
        except GuestFileError as failure:
            if failure.status >= 500:
                raise
    return active


async def purge_temps(guest: SftpClient, path: str) -> Dict[str, Any]:
    raw = to_guest(path)
    before = time.time()
    removed = 0
    expired = []
    async with aclosing(guest.iter_directory(raw)) as batches:
        async for batch in batches:
            expired += [
                item.filename
                for item in batch
                if is_safe_entry(item.filename) and is_expired_temp(from_guest(item.filename), before)
            ]
    for filename in expired:
        await guest.remove(raw.rstrip(b"/") + b"/" + filename)
        removed += 1
    return {"removed": removed}


async def _existing(guest: SftpClient, raw: bytes) -> Optional[codec.Attrs]:
    try:
        return await guest.lstat(raw)
    except GuestFileError as failure:
        if failure.code == "not_found":
            return None
        raise


def _check_destination(dest: str) -> None:
    if is_pseudo(dest):
        raise GuestFileError("protected_path", 409, "Nothing can be uploaded under this system path.")
    clean_name(dest.rsplit("/", 1)[-1])


async def commit_temp(guest: SftpClient, temp: bytes, dest: bytes, *, replace: bool) -> None:
    """Put a finished temp in place, with a sensible owner, atomically.

    important: this service writes as root and the person is usually not root. A file uploaded to
    /home/lab left owned by root is one they cannot edit, so a new file takes its folder's owner
    and a replaced file keeps the owner and mode it had.
    """
    existing = await _existing(guest, dest)
    if existing is not None and is_directory(existing):
        raise GuestFileError("is_a_directory", 409, "A folder already has this name.")
    if existing is not None and not replace:
        raise already_exists()
    keep = existing if existing is not None and not is_symlink(existing) else None
    if keep is not None:
        attrs = codec.encode_attrs(uid=keep.uid, gid=keep.gid, mode=(keep.mode or 0) & 0o7777)
    else:
        folder = await guest.lstat(dest.rsplit(b"/", 1)[0] or b"/")
        attrs = codec.encode_attrs(uid=folder.uid, gid=folder.gid, mode=0o644)
    await guest.setstat(temp, attrs)
    if existing is not None:
        await guest.replace(temp, dest)
    else:
        await _move_without_replacing(guest, temp, dest)


async def _ensure_folder(guest: SftpClient, folder: str, raw_folder: bytes) -> None:
    """The destination folder, made when missing.

    note: a folder upload sends its small files and new folders in bundles while large files start
    alongside them, so a large file can arrive before the bundle that makes its folder.
    """
    try:
        attrs = await guest.stat(raw_folder)
    except GuestFileError as failure:
        if failure.code != "not_found":
            raise
        try:
            await make_directory(guest, folder, parents=True)
        except GuestFileError as raced:
            if raced.code != "already_exists":
                raise
        return
    if not is_directory(attrs):
        raise GuestFileError("not_a_directory", 409, "This path is not a folder.")


async def create(
    guest: SftpClient,
    *,
    user: str,
    host: str,
    project: str,
    instance: str,
    dest: str,
    size: int,
    on_conflict: str,
    max_bytes: int,
) -> Dict[str, Any]:
    dest = clean_path(dest)
    _check_destination(dest)
    if size > max_bytes:
        raise too_large(max_bytes)
    raw_dest = to_guest(dest)
    folder = dest.rsplit("/", 1)[0] or "/"
    raw_folder = to_guest(folder)
    await _ensure_folder(guest, folder, raw_folder)
    existing = await _existing(guest, raw_dest)
    if existing is not None:
        if is_directory(existing):
            raise GuestFileError("is_a_directory", 409, "A folder already has this name.")
        if on_conflict != "replace":
            raise already_exists()
    if await sweep_temps(guest, raw_folder) >= MAX_SESSIONS_PER_FOLDER:
        raise GuestFileError(
            "busy", 429, "Too many uploads are running into this folder.", {"retry_after": 10}
        )
    vfs = await guest.statvfs(raw_folder)
    if vfs.blocks_available * (vfs.fragment_size or vfs.block_size) < size:
        raise no_space()
    expires_at = int(time.time()) + TEMP_LIFETIME_SECONDS
    temp = folder.rstrip("/") + "/" + temp_name(expires_at)
    handle = await guest.open(
        to_guest(temp), codec.PFLAG_WRITE | codec.PFLAG_CREAT | codec.PFLAG_EXCL, mode=0o600
    )
    await guest.close(handle)
    session = UploadSession(user, host, project, instance, temp, dest, size, on_conflict, expires_at)
    return {"token": seal(session), "chunk_hint": CHUNK_HINT_BYTES, "expires_at": expires_at}


async def write_chunk(
    guest: SftpClient,
    session: UploadSession,
    *,
    offset: int,
    length: int,
    body: AsyncIterator[bytes],
) -> int:
    """Write one chunk at `offset`, acknowledging only after every byte of it was acknowledged."""
    if offset < 0 or offset + length > session.size:
        raise GuestFileError("offset_conflict", 409, "This chunk does not fit the upload.")
    temp = to_guest(session.temp)
    try:
        handle = await guest.open(temp, codec.PFLAG_WRITE)
    except GuestFileError as failure:
        if failure.code == "not_found":
            raise session_lost() from None
        raise
    held = (await guest.fstat(handle)).size or 0
    if offset > held:
        raise GuestFileError("offset_conflict", 409, "This chunk does not fit the upload.", {"temp_size": held})
    if offset < held:
        # What a failed attempt left past the acknowledged offset, holes included.
        await guest.fsetstat(handle, codec.encode_attrs(size=offset))
    at = offset
    received = 0
    buffered = bytearray()
    async for piece in body:
        received += len(piece)
        if received > length:
            raise too_large(length)
        buffered += piece
        if len(buffered) >= _FLUSH_BYTES:
            await guest.write_all(handle, at, bytes(buffered))
            at += len(buffered)
            buffered.clear()
    if received != length:
        raise GuestFileError("offset_conflict", 409, "This chunk arrived incomplete.")
    if buffered:
        await guest.write_all(handle, at, bytes(buffered))
    await guest.close(handle)
    return offset + length


async def status(guest: SftpClient, session: UploadSession) -> Dict[str, Any]:
    attrs = await _existing(guest, to_guest(session.temp))
    if attrs is None:
        raise session_lost()
    return {"temp_size": attrs.size or 0, "expires_at": session.expires_at}


async def complete(
    guest: SftpClient, session: UploadSession, *, on_conflict: Optional[str] = None
) -> Dict[str, Any]:
    """Idempotent: a retry after the answer was lost finds the file already in place."""
    temp, dest = to_guest(session.temp), to_guest(session.dest)
    attrs = await _existing(guest, temp)
    if attrs is None:
        placed = await _existing(guest, dest)
        if placed is not None and (placed.size or 0) == session.size:
            return {**(await stat_path(guest, session.dest)), "already_committed": True}
        raise session_lost()
    if (attrs.size or 0) != session.size:
        raise GuestFileError("size_mismatch", 409, "The uploaded size does not match the file.")
    await commit_temp(guest, temp, dest, replace=(on_conflict or session.on_conflict) == "replace")
    return {**(await stat_path(guest, session.dest)), "already_committed": False}


async def abort(guest: SftpClient, session: UploadSession) -> None:
    try:
        await guest.remove(to_guest(session.temp))
    except GuestFileError as failure:
        if failure.code != "not_found":
            raise
