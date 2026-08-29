"""
Runtime for the external (OrcaHub) LXD proxy endpoints.

The proxy reaches into ``client.api.session`` directly, which bypasses pylxd's ``_APINode``
and therefore bypasses the timeout ``_APINode`` injects into every call. This module
re-applies that timeout explicitly at the call site, and owns a dedicated thread pool so
externally triggered traffic can never pin the pools the dashboard's own requests use.

All tunables are env-overridable so an incident can be mitigated without a rebuild.
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


# important: OrcaHub traffic gets its own pool. Sharing the loop's default executor would put
# it where FastAPI runs sync route handlers, and sharing _blocking_executor would put it where
# the dashboard's own LXD fan-outs run. Either lets an external caller starve the operator
# console. The thread_name_prefix means the next thread dump names the pool.
PROXY_POOL_WORKERS = _env_int("ORCASTRA_PROXY_POOL_WORKERS", 8)
PROXY_MAX_IN_FLIGHT = _env_int("ORCASTRA_PROXY_MAX_IN_FLIGHT", PROXY_POOL_WORKERS * 4)

proxy_executor = ThreadPoolExecutor(
    max_workers=PROXY_POOL_WORKERS, thread_name_prefix="integrations_proxy"
)

# note: API key validation gets its own pool. Sharing proxy_executor would mean a saturated
# proxy pool also blocks authentication for every other external endpoint, including the
# image upload surface, which has nothing to do with the cluster that went silent.
AUTH_POOL_WORKERS = _env_int("ORCASTRA_AUTH_POOL_WORKERS", 4)
auth_executor = ThreadPoolExecutor(
    max_workers=AUTH_POOL_WORKERS, thread_name_prefix="integrations_auth"
)

# Deliberately below the ~16s worst case of a single Vault call (timeout (2,3) with
# Retry(total=2) is 3 attempts plus backoff). A healthy Vault answers in tens of
# milliseconds, so waiting out its retries only converts a Vault blip into a loop stall.
VALIDATE_KEY_TIMEOUT = _env_float("ORCASTRA_VALIDATE_KEY_TIMEOUT", 8.0)

# How long validated key metadata may be served from process memory. A revoked key keeps
# working for at most this long unless it is evicted explicitly, which revoke_api_key does.
API_KEY_CACHE_TTL = _env_float("ORCASTRA_API_KEY_CACHE_TTL", 30.0)

# last_used_at only needs minute-level resolution. Writing it per request costs 2 Vault reads
# plus a write, and the write invalidates the read cache for that exact path, so the next
# request is guaranteed a cache miss. That is a self-inflicted permanent-miss loop.
LAST_USED_WRITE_INTERVAL = _env_float("ORCASTRA_LAST_USED_WRITE_INTERVAL", 300.0)

# Wall-clock caps. These bound how long the CALLER waits. The socket timeout below is what
# actually frees the worker thread, and with read retries disabled it is the primary bound
# (~connect + read). These caps are the backstop for everything else in the call path.
RAW_PROXY_TIMEOUT = _env_float("ORCASTRA_RAW_PROXY_TIMEOUT", 20.0)
PROXY_CONNECT_TIMEOUT = _env_float("ORCASTRA_PROXY_CONNECT_TIMEOUT", 15.0)
PROXY_OP_TIMEOUT = _env_float("ORCASTRA_PROXY_OP_TIMEOUT", 60.0)

# Per-cluster budget for the OrcaHub sync fan-out. Applied per cluster, and the clusters run
# concurrently, so total wall clock is the slowest cluster rather than the sum.
SYNC_CLUSTER_TIMEOUT = _env_float("ORCASTRA_SYNC_CLUSTER_TIMEOUT", 20.0)

ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def socket_timeout_for(client: Any) -> Tuple[int, int]:
    """Per-attempt (connect, read) the pooled pylxd client was built with.

    Reused rather than hardcoded so proxy traffic behaves identically to every other LXD call
    and self-corrects if the pool timeout is ever retuned.
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

    important: redirects are not followed. `requests` follows them by default and does not remove
    dot segments from a path, so a caller-supplied `..` reached LXD, whose Go mux cleans the path
    and answers 301, and the client then followed that redirect to a location outside `/1.0` with
    the cluster's mTLS client certificate attached. Refusing to follow means the caller sees the
    301 and the credential never travels anywhere it was not authorised to go. Nothing in this
    proxy has a legitimate need for a redirect: every path is a direct LXD API endpoint.

    note: `timeout=` is spelled out on every call rather than collapsed into a **kwargs dict.
    `tests/test_no_untimed_http_calls.py` reads these statements with the AST and cannot see
    through a dict, so folding them would silently retire a ratchet that exists to stop an
    untimed call from parking a worker forever.
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
