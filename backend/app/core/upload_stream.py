"""
Bounded, off-loop staging of a resumable-upload (tus PATCH) body.

``await request.body()`` would hold the whole body in memory before any size check, and an
inline write would block the event loop on every chunk. Here the body is read incrementally:
the ceiling is enforced per chunk before any bytes are retained, and each flush goes to a
bounded pool via ``run_blocking``.

important: on any failure the staging file is truncated back to the offset the caller
started from. The caller advances the DB row only after this returns, so file and row stay
consistent and the session remains resumable at the recorded offset.
"""

from __future__ import annotations

import errno
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException, Request

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except KeyError:
        return default
    except (TypeError, ValueError):
        logger.warning("invalid %s, falling back to %d", name, default)
        return default


# Bytes held in memory before a flush. Bounds peak memory per in-flight upload to roughly
# this plus one transport chunk, regardless of what the client sends in one PATCH.
FLUSH_BYTES = _env_int("ORCASTRA_UPLOAD_FLUSH_BYTES", 8 * 1024 * 1024)

# Wall-clock cap for a single flush. A local disk write of FLUSH_BYTES is milliseconds; this
# only fires when the filesystem itself is wedged, and it is what stops the request from
# waiting on it forever.
WRITE_TIMEOUT = float(_env_int("ORCASTRA_UPLOAD_WRITE_TIMEOUT", 60))

# Dedicated pool, separate from the import pool: a multi-GB import must not be able to
# starve the staging writes of an unrelated upload, and neither may run on the loop's
# default executor, which FastAPI also uses for sync route handlers.
WRITE_POOL_WORKERS = _env_int("ORCASTRA_UPLOAD_WRITE_WORKERS", 4)
WRITE_MAX_IN_FLIGHT = _env_int("ORCASTRA_UPLOAD_WRITE_MAX_IN_FLIGHT", WRITE_POOL_WORKERS * 2)

write_executor = ThreadPoolExecutor(
    max_workers=WRITE_POOL_WORKERS, thread_name_prefix="upload_stage"
)


class UploadStagingUnavailable(HTTPException):
    """The backend's own staging storage refused the write.

    A distinct type, so callers can tell it from every other 503 on this path. It recurs on
    every retry until an operator intervenes, so callers retire the session instead of letting
    it hold a slot for the full resumable window.
    """

    def __init__(self, detail: str):
        super().__init__(status_code=503, detail=detail)


def _write_at(path: str, pos: int, data: bytes) -> None:
    """Write ``data`` at ``pos``, discarding anything already past it.

    important: anchored to the caller's recorded offset instead of opened "ab": append mode
    writes at EOF, and the file can be longer than the row's offset (a rollback that could not
    run, or a timed-out write still running in its thread), which would corrupt it silently.
    Seek plus truncate makes the row authoritative and lets a stale file heal on the retry.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+b") as f:
        f.seek(pos)
        f.truncate()
        f.write(data)


# errnos that mean the filesystem itself refused the write, as opposed to a socket error that
# happens to surface as an OSError. Only these retire a session: the rest stay resumable.
_STORAGE_REFUSAL_ERRNOS = frozenset(
    e for e in (
        getattr(errno, name, None)
        for name in ("EACCES", "EPERM", "EROFS", "ENOSPC", "EDQUOT", "ENOTDIR", "EISDIR")
    )
    if e is not None
)


def _is_storage_refusal(exc: OSError) -> bool:
    """True when the staging volume refused the write and a retry cannot help.

    Matched by errno: PermissionError covers only EACCES and EPERM, while a read-only mount
    raises OSError(EROFS) and a full quota EDQUOT. ConnectionResetError is also an OSError and
    must NOT match: a client that dropped mid-upload is what the resumable protocol is for.
    """
    return exc.errno in _STORAGE_REFUSAL_ERRNOS


def _truncate(path: str, size: int) -> None:
    with open(path, "r+b") as f:
        f.truncate(size)


async def _rollback(path: str, size: int, label: str) -> None:
    """Best-effort: drop whatever was flushed so the file matches the caller's recorded offset."""
    try:
        await run_blocking(
            _truncate, path, size, timeout=WRITE_TIMEOUT, label=f"{label}[rollback]",
            executor=write_executor,
        )
    except Exception as exc:
        # Losing the rollback is recoverable: the row keeps its offset, so a PATCH at any other
        # offset gets 409, the client re-HEADs, and _write_at truncates the file.
        logger.warning("could not roll back staging file %s to %d: %s", path, size, exc)


async def append_request_body(
    request: Request,
    path: str,
    *,
    start_offset: int,
    limit: int,
    label: str,
) -> int:
    """Append the request body to ``path``, returning the new offset.

    ``limit`` is the absolute ceiling for the resulting file size (the caller's
    ``min(total_bytes, max_bytes)``). Exceeding it raises 413 before the offending bytes are
    ever accumulated. A saturated or wedged write pool raises 503.
    """
    offset = start_offset
    buf = bytearray()

    async def flush() -> None:
        nonlocal offset
        if not buf:
            return
        await run_blocking(
            _write_at, path, offset, bytes(buf), timeout=WRITE_TIMEOUT, label=label,
            executor=write_executor, max_in_flight=WRITE_MAX_IN_FLIGHT,
        )
        offset += len(buf)
        buf.clear()

    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            # Checked before the bytes are retained, so an oversized body is rejected on the
            # first chunk that crosses the line rather than after it is all in memory.
            if offset + len(buf) + len(chunk) > limit:
                raise HTTPException(status_code=413, detail="Upload exceeds declared size")
            buf.extend(chunk)
            if len(buf) >= FLUSH_BYTES:
                await flush()
        await flush()
    except HTTPException:
        await _rollback(path, start_offset, label)
        raise
    except BlockingPoolFull:
        await _rollback(path, start_offset, label)
        raise HTTPException(
            status_code=503, detail="Upload staging saturated; please retry shortly"
        ) from None
    except BlockingTimeout:
        await _rollback(path, start_offset, label)
        logger.error("staging write %s exceeded %.0fs; is the upload volume wedged?", label, WRITE_TIMEOUT)
        raise HTTPException(status_code=503, detail="Upload staging unavailable") from None
    except OSError as exc:
        if not _is_storage_refusal(exc):
            # Not a storage refusal. A socket error mid-stream is a client disconnect, and a
            # resumable upload must survive a dropped connection, so the session stays.
            await _rollback(path, start_offset, label)
            raise
        # The staging directory is a host bind mount, and Docker creates a missing source
        # root-owned; _ensure_dir and the disk-usage check both pass on it, so the first failure
        # is this open(O_CREAT). 503, as for LXDLocalStorageError in main.py, because the
        # backend's own storage failed.
        await _rollback(path, start_offset, label)
        logger.error(
            "staging write %s failed on the upload volume (%s: %s). If this is EACCES the "
            "bind-mount source is not writable by the app user; it must be owned by uid 1000.",
            label, type(exc).__name__, exc,
        )
        raise UploadStagingUnavailable(
            "Upload staging is not writable on the server. This is a backend storage "
            "fault, not a cluster problem. The session is retired so its slot is freed; "
            "start a new upload once the server's staging volume is writable."
        ) from None
    except Exception:
        await _rollback(path, start_offset, label)
        raise

    return offset
