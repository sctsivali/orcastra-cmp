"""Runtime settings and thread pools for the external LXD proxy endpoints.

The proxy calls ``client.api.session`` directly, which bypasses pylxd's ``_APINode`` and the
timeout it injects into every call, so this module applies that timeout at the call site. It
also owns dedicated thread pools, so externally triggered traffic cannot pin the pools the
dashboard's own requests use. Every tunable can be overridden from the environment.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional, Tuple

import requests

from .connection import DEFAULT_LXD_TIMEOUT

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("invalid %s, falling back to %s", name, default)
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("invalid %s, falling back to %s", name, default)
        return default


# important: external proxy traffic gets its own pool. The loop's default executor runs FastAPI's
# sync route handlers and _blocking_executor runs the dashboard's own LXD fan-outs; sharing either
# lets an external caller starve the operator console.
PROXY_POOL_WORKERS = _env_int("ORCASTRA_PROXY_POOL_WORKERS", 8)
PROXY_MAX_IN_FLIGHT = _env_int("ORCASTRA_PROXY_MAX_IN_FLIGHT", PROXY_POOL_WORKERS * 4)

proxy_executor = ThreadPoolExecutor(
    max_workers=PROXY_POOL_WORKERS, thread_name_prefix="integrations_proxy"
)

# API key validation gets its own pool, so a saturated proxy pool cannot block authentication
# for every other external endpoint, image uploads included.
AUTH_POOL_WORKERS = _env_int("ORCASTRA_AUTH_POOL_WORKERS", 4)
auth_executor = ThreadPoolExecutor(
    max_workers=AUTH_POOL_WORKERS, thread_name_prefix="integrations_auth"
)

# Below the ~16s worst case of a single Vault call (timeout (2,3) with Retry(total=2) is 3
# attempts plus backoff). A healthy Vault answers in tens of milliseconds; waiting out its
# retries only turns a Vault blip into a loop stall.
VALIDATE_KEY_TIMEOUT = _env_float("ORCASTRA_VALIDATE_KEY_TIMEOUT", 8.0)

# How long validated key metadata may be served from process memory. A revoked key keeps
# working for at most this long unless it is evicted explicitly, which revoke_api_key does.
API_KEY_CACHE_TTL = _env_float("ORCASTRA_API_KEY_CACHE_TTL", 30.0)

# last_used_at only needs minute-level resolution. A per-request write costs 2 Vault reads plus
# a write, and the write invalidates the read cache for that path, so every request would miss.
LAST_USED_WRITE_INTERVAL = _env_float("ORCASTRA_LAST_USED_WRITE_INTERVAL", 300.0)

# Wall-clock caps on how long the caller waits. The socket timeout (socket_timeout_for) is what
# frees the worker thread; with read retries disabled it is the primary bound (about connect +
# read). These caps back up everything else in the call path.
RAW_PROXY_TIMEOUT = _env_float("ORCASTRA_RAW_PROXY_TIMEOUT", 20.0)
PROXY_CONNECT_TIMEOUT = _env_float("ORCASTRA_PROXY_CONNECT_TIMEOUT", 15.0)
PROXY_OP_TIMEOUT = _env_float("ORCASTRA_PROXY_OP_TIMEOUT", 60.0)

# An exec holds a worker for its whole run, so it has its own budget. Kept short: a command that
# needs longer is a job, which the caller should start in a way that survives the connection.
EXEC_TIMEOUT = _env_float("ORCASTRA_EXEC_TIMEOUT", 30.0)

# Per stream. Exec output is caller-chosen: `cat /dev/urandom` would otherwise stream until the
# timeout with the whole buffer held in the worker's memory. The response says when it truncated.
EXEC_OUTPUT_LIMIT = _env_int("ORCASTRA_EXEC_OUTPUT_LIMIT", 256 * 1024)

# important: exec gets its own small pool. A worker keeps running after the caller gives up
# until its socket timeout fires (core/blocking.py), and pylxd's exec polls over a websocket with
# no such bound, so a runaway command holds a thread until it ends. On the shared proxy pool
# eight of them would block every read, raw proxy call and fan-out; here only other execs wait.
EXEC_POOL_WORKERS = _env_int("ORCASTRA_EXEC_POOL_WORKERS", 4)
EXEC_MAX_IN_FLIGHT = _env_int("ORCASTRA_EXEC_MAX_IN_FLIGHT", EXEC_POOL_WORKERS)

exec_executor = ThreadPoolExecutor(
    max_workers=EXEC_POOL_WORKERS, thread_name_prefix="integrations_exec"
)

# stdin spares a caller stuffing data into argv, which is worse for quoting and for the audit
# record. File uploads go through instance.files.
EXEC_STDIN_LIMIT = _env_int("ORCASTRA_EXEC_STDIN_LIMIT", 64 * 1024)

# File transfer inside a guest. Shares the exec pool and the exec timeout: it is the same kind of
# act against the same guest.
#
# important: the read cap is enforced as chunks arrive. pylxd's FilesManager.get is
# `return response.content`, which buffers the whole body with no bound
# (pylxd/models/instance.py), so `get_file` on /dev/zero would grow a worker's memory until it
# died. guest_files.py reads through a bounded reader instead.
#
# Larger than EXEC_OUTPUT_LIMIT: a file has a legitimate reason to be big and a command's stdout
# usually does not. The response says when it truncated.
FILE_READ_LIMIT = _env_int("ORCASTRA_FILE_READ_LIMIT", 4 * 1024 * 1024)

# What a caller may push in. Bounded separately from the read: a write is caller-chosen in a way a
# read is not, and this whole payload sits in the request body and then in the worker's memory.
FILE_WRITE_LIMIT = _env_int("ORCASTRA_FILE_WRITE_LIMIT", 4 * 1024 * 1024)

# Chunk size for the bounded read. Small enough that the cap is honoured promptly on a huge file,
# large enough not to make a normal file a thousand round trips through the iterator.
FILE_CHUNK_BYTES = _env_int("ORCASTRA_FILE_CHUNK_BYTES", 64 * 1024)

# Per-cluster budget for the OrcaHub sync fan-out. Applied per cluster, and the clusters run
# concurrently, so total wall clock is the slowest cluster rather than the sum.
SYNC_CLUSTER_TIMEOUT = _env_float("ORCASTRA_SYNC_CLUSTER_TIMEOUT", 20.0)

ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def socket_timeout_for(client: Any) -> Tuple[int, int]:
    """Per-attempt (connect, read) the pooled pylxd client was built with.

    Read from the client, not hardcoded, so proxy traffic follows the same timeout as every other
    LXD call.
    """
    configured = getattr(getattr(client, "api", None), "_timeout", None)
    if isinstance(configured, (tuple, list)) and len(configured) == 2:
        return (configured[0], configured[1])
    return DEFAULT_LXD_TIMEOUT


def raw_request(
    session: requests.Session,
    method: str,
    url: str,
    body: Optional[Dict[str, Any]],
    timeout: Tuple[int, int],
) -> requests.Response:
    """Blocking half of raw_proxy. Only ever runs on proxy_executor, never on the event loop.

    important: redirects are never followed. `requests` does not remove dot segments from a
    path, so a caller-supplied `..` reaches LXD, whose Go mux cleans the path and answers 301;
    following it would send the cluster's mTLS client certificate to a location outside `/1.0`.
    Every path here is a direct LXD API endpoint, so no call needs a redirect.

    `timeout=` is spelled out on every call rather than passed through a **kwargs dict:
    `tests/test_no_untimed_http_calls.py` reads these calls with the AST and cannot see through
    a dict.
    """
    if method == "GET":
        return session.get(url, verify=False, timeout=timeout, allow_redirects=False)
    if method == "POST":
        return session.post(url, json=body, verify=False, timeout=timeout, allow_redirects=False)
    if method == "PUT":
        return session.put(url, json=body, verify=False, timeout=timeout, allow_redirects=False)
    if method == "PATCH":
        return session.patch(url, json=body, verify=False, timeout=timeout, allow_redirects=False)
    if method == "DELETE":
        return session.delete(url, verify=False, timeout=timeout, allow_redirects=False)
    raise ValueError(f"Unsupported method: {method}")
