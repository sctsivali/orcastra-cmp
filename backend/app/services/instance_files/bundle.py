"""Many small files in one request, because a folder should not cost one request per file.

All browsers reach the backend through one Next process, so they share one rate-limit bucket of 500
requests a minute per worker. A folder of 1,000 files sent one by one would refuse everyone else on
the deployment. The body here is `<u32 manifest length><manifest JSON><file bytes in order>`.
"""

import json
import struct
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Set

from . import sftp_codec as codec
from .errors import GuestFileError, invalid_path, too_large
from .operations import make_directory
from .paths import clean_name, clean_path, is_pseudo, to_guest
from .sftp_client import SftpClient
from .uploads import BODY_STALL_SECONDS, commit_open_temp, stalled_after
from .uploads_temp import TEMP_LIFETIME_SECONDS, temp_name

MAX_BUNDLE_FILES = 256
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 512 * 1024


class _Body:
    """Exactly-sized reads from a request stream, never holding more than one piece past them."""

    def __init__(self, stream: AsyncIterator[bytes]) -> None:
        self._stream = stream
        self._held = bytearray()

    async def read(self, length: int) -> bytes:
        while len(self._held) < length:
            try:
                self._held += await self._stream.__anext__()
            except StopAsyncIteration:
                raise invalid_path("This upload ended before its files did.") from None
        taken = bytes(self._held[:length])
        del self._held[:length]
        return taken


def _relative(base: str, relative: str) -> str:
    parts = relative.split("/")
    for part in parts:
        clean_name(part)
    return clean_path(base.rstrip("/") + "/" + relative)


def _parse_manifest(raw: bytes) -> Dict[str, Any]:
    try:
        manifest = json.loads(raw)
    except ValueError as exc:
        raise invalid_path("This upload's manifest is not readable.") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise invalid_path("This upload's manifest is not readable.")
    return manifest


async def receive_bundle(
    guest: SftpClient,
    stream: AsyncIterator[bytes],
    *,
    now: int,
    still_allowed: Optional[Callable[[], Awaitable[None]]] = None,
) -> Dict[str, Any]:
    """Write a bundle. `still_allowed` runs between files, since a slow body can outlast a fence."""
    body = _Body(stalled_after(stream, BODY_STALL_SECONDS))
    length = struct.unpack(">I", await body.read(4))[0]
    if length > MAX_MANIFEST_BYTES:
        raise too_large(MAX_MANIFEST_BYTES)
    manifest = _parse_manifest(await body.read(length))
    base = clean_path(str(manifest.get("base") or ""))
    if is_pseudo(base):
        raise GuestFileError("protected_path", 409, "Nothing can be uploaded under this system path.")
    replace = manifest.get("on_conflict") == "replace"
    files = manifest["files"]
    folders = manifest.get("folders") or []
    if len(files) > MAX_BUNDLE_FILES or len(folders) > MAX_BUNDLE_FILES:
        raise too_large(MAX_BUNDLE_FILES)
    sizes = [entry.get("size") for entry in files if isinstance(entry, dict)]
    if len(sizes) != len(files) or not all(isinstance(size, int) and size >= 0 for size in sizes):
        raise invalid_path("This upload's manifest is not readable.")
    if sum(sizes) > MAX_BUNDLE_BYTES:
        raise too_large(MAX_BUNDLE_BYTES)

    made: Set[str] = set()

    async def ensure_folder(path: str) -> None:
        if path not in made:
            await make_directory_if_missing(guest, path)
            made.add(path)

    results: List[Dict[str, Any]] = []
    for folder in folders:
        await ensure_folder(_relative(base, str(folder)))
    for entry in files:
        # The bytes are read first: a refused file must not shift every later file's offset.
        payload = await body.read(entry["size"])
        name = str(entry.get("path") or "")
        if still_allowed is not None:
            await still_allowed()
        try:
            dest = _relative(base, name)
            await ensure_folder(dest.rsplit("/", 1)[0] or "/")
            await _place(guest, dest, payload, replace=replace, now=now)
            results.append({"path": name, "error": None})
        except GuestFileError as failure:
            if failure.status >= 500 and failure.code != "no_space":
                raise
            results.append({"path": name, "error": failure.code})
    return {"results": results}


async def make_directory_if_missing(guest: SftpClient, path: str) -> None:
    try:
        await make_directory(guest, path, parents=True)
    except GuestFileError as failure:
        if failure.code != "already_exists":
            raise


async def _place(guest: SftpClient, dest: str, payload: bytes, *, replace: bool, now: int) -> None:
    folder = dest.rsplit("/", 1)[0] or "/"
    temp = to_guest(folder.rstrip("/") + "/" + temp_name(now + TEMP_LIFETIME_SECONDS))
    handle = await guest.open(temp, codec.PFLAG_WRITE | codec.PFLAG_CREAT | codec.PFLAG_EXCL, mode=0o600)
    try:
        await guest.write_all(handle, 0, payload)
        await commit_open_temp(guest, handle, temp, to_guest(dest), replace=replace)
    except GuestFileError as failure:
        if failure.status < 500 or failure.code == "no_space":
            try:
                await guest.close(handle)
            except GuestFileError:
                pass
            try:
                await guest.remove(temp)
            except GuestFileError:
                pass
        raise
