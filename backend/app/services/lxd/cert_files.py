"""The one place an LXD client certificate reaches the backend's filesystem.

pylxd and requests both want ``cert=(certfile, keyfile)`` paths, so a PEM held in memory has to
land on disk before any client can use it. Every site that did this reached for
``tempfile.mkstemp`` with a random suffix and recorded the result in a single-slot dict keyed by
cluster id. That is unbounded by construction: two threads connecting to the same cluster each
minted a pair and each overwrote the slot, so every pair but the last was orphaned with nothing
left holding its path to unlink it. ``get_client`` runs inside a thread pool via ``run_blocking``,
so this was the ordinary case, not a rare interleaving. Measured on the dev stack: 8940 files in
46 hours against a 256 MiB tmpfs, heavily weighted to unreachable clusters because those never
populate the client cache and so re-connect on every single request.

The filename here is a pure function of the cluster id. No amount of concurrency can produce more
than one pair per cluster, which removes the leak by construction rather than by wrapping a lock
around a scheme that grows anyway. Writes land with ``os.replace``, which is atomic: a client
already inside a TLS handshake keeps reading the old inode and never observes a half-written file.
"""

import errno
import hashlib
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from typing import Any, Dict, Tuple

from app.services.lxd_ssl import describe_disk_usage

from .exceptions import LXDLocalStorageError

logger = logging.getLogger(__name__)

# Anything outside this set is replaced, which is what makes the slug a single path component.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# Usage at or above this fraction of the temp filesystem is worth an operator's attention.
DEGRADED_AT = 0.80

# A worker only needs to look for exited siblings occasionally; this is not the hot path.
SWEEP_INTERVAL_SECONDS = 60.0

# important: pinned, not tempfile.gettempdir(). gettempdir() probes for a WRITABLE directory and
# silently falls through to /var/tmp when /tmp is full. /tmp is a memory-backed tmpfs the compose
# file mounts for exactly this material, /var/tmp is the container's disk layer, so that fallback
# would quietly start writing TLS private keys to disk at the precise moment things are going
# wrong. A full tmpfs has to be a loud failure, not a silent downgrade. Overridable so tests and
# non-container runs can point somewhere else.
DEFAULT_CERT_BASE = "/tmp"
CERT_BASE_ENV = "ORCASTRA_LXD_CERT_DIR"

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_last_sweep = float("-inf")


def cert_root() -> str:
    """Directory holding this process's materialized certificates.

    note: keyed by pid. The container already runs several anyio pool workers alongside uvicorn,
    and sharing a deterministic path between processes would let one worker's deregistration
    unlink a file another is serving from.
    """
    return os.path.join(cert_base(), "orcastra-lxd-certs", str(os.getpid()))


def cert_base() -> str:
    """Filesystem that holds certificate material. See DEFAULT_CERT_BASE for why it is pinned."""
    return os.environ.get(CERT_BASE_ENV) or DEFAULT_CERT_BASE


def _slug(cluster_id: str) -> str:
    """A filesystem-safe single path component that maps 1:1 to the cluster id.

    important: cluster ids come from the registration API, so this is also the traversal guard.
    Replacing the separators is what keeps ``../../etc/passwd`` a filename instead of a path; the
    digest keeps two ids that sanitize to the same text from sharing a certificate.
    """
    cleaned = _UNSAFE.sub("_", cluster_id)[:64].strip(".") or "cluster"
    digest = hashlib.sha256(cluster_id.encode("utf-8")).hexdigest()[:12]
    return f"{cleaned}-{digest}"


def paths_for(cluster_id: str) -> Tuple[str, str]:
    """Where this cluster's certificate and key live. Same input, same answer, always."""
    base = os.path.join(cert_root(), _slug(cluster_id))
    return f"{base}.crt", f"{base}.key"


def _lock_for(cluster_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(cluster_id)
        if lock is None:
            lock = threading.Lock()
            _locks[cluster_id] = lock
        return lock


def materialize(cluster_id: str, cert_pem: str, key_pem: str) -> Tuple[str, str]:
    """Write the cluster's client certificate to its fixed path and return (cert, key).

    Safe to call concurrently for the same cluster: callers converge on one pair of files rather
    than accumulating one pair each. Raises LXDLocalStorageError if the write cannot happen.
    """
    cert_path, key_path = paths_for(cluster_id)
    _maybe_sweep_dead_siblings()
    with _lock_for(cluster_id):
        try:
            os.makedirs(cert_root(), mode=0o700, exist_ok=True)
            _write_atomic(cert_path, cert_pem)
            _write_atomic(key_path, key_pem)
        except OSError as exc:
            raise _storage_error(exc) from exc
    return cert_path, key_path


def _maybe_sweep_dead_siblings() -> None:
    """Reclaim cert directories belonging to processes that have exited.

    important: the worker processes here are recycled, so a pid-scoped directory is not
    self-limiting on its own. anyio kills a pool worker once it has been idle for
    WORKER_MAX_IDLE_TIME (300s) and spawns a replacement with a fresh pid, which would leave the
    old directory behind and reintroduce the same unbounded growth this module exists to remove,
    only slower. Four worker directories were already present one minute after boot.

    Only directories whose pid is gone are touched, so a live sibling never has its certificates
    pulled out from under an open handshake. Skipped entirely without /proc, where a missing
    entry would prove nothing and the check would delete a live peer's files.
    """
    global _last_sweep
    if not os.path.isdir("/proc/self"):
        return
    now = time.monotonic()
    with _locks_guard:
        if now - _last_sweep < SWEEP_INTERVAL_SECONDS:
            return
        _last_sweep = now

    parent = os.path.dirname(cert_root())
    mine = str(os.getpid())
    try:
        entries = os.listdir(parent)
    except OSError:
        return

    for entry in entries:
        if entry == mine or not entry.isdigit() or os.path.exists(f"/proc/{entry}"):
            continue
        stale = os.path.join(parent, entry)
        try:
            discard(*(os.path.join(stale, name) for name in os.listdir(stale)))
            os.rmdir(stale)
            logger.info("Reclaimed LXD certificate directory for exited process %s", entry)
        except OSError as exc:
            logger.debug("Could not reclaim %s: %s", stale, exc)


def _write_atomic(path: str, content: str) -> None:
    """Stage alongside the target, then rename onto it.

    The rename is the point: a reader that opens the path either sees the previous complete file
    or the new complete file. Writing in place would expose a truncated certificate to any client
    that started a handshake mid-write.
    """
    staged_fd, staged = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".staging-")
    try:
        with os.fdopen(staged_fd, "wb") as handle:
            handle.write(content.encode("utf-8"))
        os.chmod(staged, 0o600)
        os.replace(staged, path)
    except BaseException:
        try:
            os.unlink(staged)
        except OSError:
            pass
        raise


def discard(*paths: str) -> None:
    """Overwrite then remove materialized certificate files. Best effort, never raises."""
    for path in paths:
        try:
            if not os.path.exists(path):
                continue
            try:
                size = os.path.getsize(path)
                with open(path, "wb") as handle:
                    handle.write(b"\x00" * size)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                pass  # shredding is a courtesy; removal below is the part that matters
            os.unlink(path)
        except OSError as exc:
            logger.debug("Could not remove %s: %s", path, exc)


def describe_usage() -> Dict[str, Any]:
    """Certificate filesystem usage, for the health endpoint."""
    temp_dir = cert_base()
    try:
        usage = shutil.disk_usage(temp_dir)
    except OSError as exc:
        return {"path": temp_dir, "available": False, "error": str(exc)}

    used_ratio = (usage.used / usage.total) if usage.total else 0.0
    return {
        "path": temp_dir,
        "available": True,
        "used_mib": round(usage.used / 1024 / 1024, 1),
        "total_mib": round(usage.total / 1024 / 1024, 1),
        "percent_used": round(used_ratio * 100, 1),
        "degraded": used_ratio >= DEGRADED_AT,
    }


def _storage_error(exc: OSError) -> LXDLocalStorageError:
    if exc.errno == errno.ENOSPC:
        return LXDLocalStorageError(
            "Backend temporary storage is full while preparing LXD TLS credentials. "
            f"{describe_disk_usage(cert_base())} "
            "Increase BACKEND_TMPFS_SIZE or clear stale files in the backend container."
        )
    return LXDLocalStorageError(f"Could not prepare LXD TLS credentials: {exc}")
