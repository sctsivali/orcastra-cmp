"""Materialize LXD client certificates on the backend's filesystem.

pylxd and requests want ``cert=(certfile, keyfile)`` paths, so a PEM held in memory has to land
on disk before a client can use it. The filename is a pure function of the cluster id, so
concurrent connects to one cluster converge on a single pair of files instead of orphaning one
pair per attempt (``get_client`` runs in a thread pool, so concurrency is the normal case).
Writes land with ``os.replace``, which is atomic: a client already inside a TLS handshake keeps
reading the replaced inode and never sees a half-written file.
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

from .exceptions import LXDLocalStorageError

from app.core.units import format_size

logger = logging.getLogger(__name__)

# Anything outside this set is replaced, so the slug is a single path component.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# Usage at or above this fraction of the temp filesystem is worth an operator's attention.
DEGRADED_AT = 0.80

# How often a worker looks for directories left behind by exited processes.
SWEEP_INTERVAL_SECONDS = 60.0

# important: pinned instead of tempfile.gettempdir(), which falls through to /var/tmp when /tmp is
# full. /tmp is the memory-backed tmpfs the compose file mounts for this material and /var/tmp is
# the container's disk layer, so that fallback would write TLS private keys to disk. A full tmpfs
# must fail loudly. Overridable for tests and non-container runs.
DEFAULT_CERT_BASE = "/tmp"
CERT_BASE_ENV = "ORCASTRA_LXD_CERT_DIR"

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_last_sweep = float("-inf")


def cert_root() -> str:
    """Directory holding this process's materialized certificates.

    Keyed by pid: the container runs anyio pool workers alongside uvicorn, and a path shared
    between processes would let one worker's deregistration unlink a file another is serving from.
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
    """Fixed certificate and key paths for a cluster, a pure function of its id."""
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

    important: anyio replaces a pool worker idle for WORKER_MAX_IDLE_TIME (300s) with one under a
    fresh pid, so without this sweep pid-scoped directories grow without bound.

    Only directories whose pid is gone are touched, so a live sibling never loses its certificates
    during a handshake. Skipped without /proc, where a missing entry proves nothing and the check
    would delete a live peer's files.
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

    A reader that opens the path sees either the previous or the new complete file. Writing in
    place would expose a truncated certificate to a client that starts a handshake mid-write.
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
                pass  # shredding is best effort; the unlink below is what matters
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


PUBLIC_USAGE_FIELDS = ("available", "used_mib", "total_mib", "percent_used", "degraded")


def public_usage(usage: Dict[str, Any]) -> Dict[str, Any]:
    """Allow only health fields that reveal neither the scratch path nor its error text."""
    return {key: usage[key] for key in PUBLIC_USAGE_FIELDS if key in usage}


def describe_disk_usage(path: str) -> str:
    """One-line summary of free space, shared with the WebSocket path in lxd_ssl.

    It lives here because this module owns where certificate material goes, and both paths should
    report a full filesystem in the same words.
    """
    try:
        usage = shutil.disk_usage(path)
        return f"{path} has {format_size(usage.free)} free of {format_size(usage.total)}."
    except OSError:
        return f"Unable to read disk usage for {path}."


def staging_dir() -> str:
    """Pinned directory for short-lived certificate scratch files, created if absent.

    important: callers must pass this as ``dir=`` to tempfile. Otherwise tempfile resolves through
    gettempdir(), which falls through to /var/tmp once /tmp is full and would silently put TLS
    private keys on the container's disk layer.
    """
    base = cert_base()
    try:
        os.makedirs(base, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise _storage_error(exc) from exc
    return base


def _storage_error(exc: OSError) -> LXDLocalStorageError:
    if exc.errno == errno.ENOSPC:
        return LXDLocalStorageError(
            "Backend temporary storage is full while preparing LXD TLS credentials. "
            f"{describe_disk_usage(cert_base())} "
            "Increase BACKEND_TMPFS_SIZE or clear stale files in the backend container."
        )
    return LXDLocalStorageError(f"Could not prepare LXD TLS credentials: {exc}")
