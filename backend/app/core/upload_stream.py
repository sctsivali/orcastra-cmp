"""
Bounded, off-loop staging of a resumable-upload (tus PATCH) body.

Two failure modes are reachable from a single request when a handler does
``chunk = await request.body()`` and then writes it inline:

  - the whole body is materialised in memory *before* any size check can run, so the 413
    the code intends to return never happens. A client that declares a small chunk and
    then sends a multi-GB one takes the worker out on memory instead;
  - the append is a synchronous write on the event loop, repeated once per chunk for the
    entire file, so a contended or slow disk stalls every other request on that worker.

Reading the body incrementally closes both: the ceiling is enforced per chunk before any
bytes are retained, and each flush goes to a bounded pool via ``run_blocking``.

important: on any failure the staging file is truncated back to the offset the caller
started from. The DB row is only advanced by the caller after this returns, so file and
row stay consistent and the session remains resumable at the recorded offset.
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

# Dedicated pool. Deliberately not the import pool: a multi-GB import must not be able to
# starve the staging writes of an unrelated upload, and neither may run on the loop's
# default executor, which FastAPI also uses for sync route handlers.
WRITE_POOL_WORKERS = _env_int("ORCASTRA_UPLOAD_WRITE_WORKERS", 4)
WRITE_MAX_IN_FLIGHT = _env_int("ORCASTRA_UPLOAD_WRITE_MAX_IN_FLIGHT", WRITE_POOL_WORKERS * 2)

write_executor = ThreadPoolExecutor(
    max_workers=WRITE_POOL_WORKERS, thread_name_prefix="upload_stage"
)


class UploadStagingUnavailable(HTTPException):
    """The backend's own staging storage refused the write.

    A distinct type rather than a 503 the caller has to recognise by its status code or its
    detail string. Callers need to tell this apart from every other 503 on this path, because
    this is the one that will recur on every retry until an operator intervenes: the session
    row can never make progress, so holding its slot for the full resumable window locks the
    key out for six hours. Matching on a message would be the substring-assertion trap the
    house rules warn about; a type cannot drift.
    """

    def __init__(self, detail: str):
        super().__init__(status_code=503, detail=detail)


def _write_at(path: str, pos: int, data: bytes) -> None:
    """Write ``data`` at ``pos``, discarding anything already past it.

    important: anchored to the caller's recorded offset rather than opened "ab". Append mode
    writes at EOF, so any time the file is longer than the offset in the session row the next
    chunk lands past a gap and the staged file is silently corrupt. That is reachable: a
    rollback that could not run logs a warning and leaves the file long, and a write whose
    caller timed out keeps running in its thread (run_blocking shields it) and may land after
    the 503. ``expected_sha256`` is optional, so nothing downstream would catch the corruption.
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

    Deliberately errno-based rather than isinstance-based. PermissionError covers EACCES and
    EPERM, but a read-only mount raises OSError(EROFS) and a full quota raises EDQUOT, and both
    are the same operator fault with the same remedy. ConnectionResetError is also an OSError
    and must NOT match, because a client that dropped mid-upload is exactly what the resumable
    protocol is for.
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
        # Losing the rollback is recoverable: the row still holds the old offset, so the next
        # PATCH is rejected with 409 and the client re-HEADs. Worth a loud line either way.
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
            # Not the filesystem refusing us. A socket error mid-stream is a client disconnect,
            # and retiring the session for that would destroy a resumable upload over a dropped
            # connection, which is the exact thing tus exists to survive.
            await _rollback(path, start_offset, label)
            raise
        # important: the staging directory is a host bind mount, and Docker creates a missing
        # bind-mount source root-owned. Every guard on the create path still passes, because
        # _ensure_dir's makedirs succeeds on an existing directory and the disk-usage check only
        # stats it. The first failure is therefore this open(O_CREAT), and it used to escape as
        # a bare PermissionError. There is no catch-all exception handler registered, so the
        # caller got a 500 with no reference id and no hint, while the session row stayed active
        # and held one of only two slots for the full six-hour TTL.
        #
        # 503 rather than 500 for the same reason LXDLocalStorageError is a 503 in main.py: the
        # backend's own storage failed, the request is retryable once an operator fixes it, and
        # it is emphatically not a cluster fault.
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
