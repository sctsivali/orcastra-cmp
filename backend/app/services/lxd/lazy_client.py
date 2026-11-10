"""A cluster's client, connected from Vault when this worker holds none.

`connection_manager.get_client` is memory-only and returns None on a worker that never
connected to the cluster, the normal state right after a registration, so one read would
answer 200 or 503 depending on the worker. The order is fixed: the pooled client (one
revalidation), then the breaker inside `LXDService.ensure_connection` (dead clusters cost
nothing), then the certificate from Vault, only for a registered, in-scope cluster. A new
connect entry point belongs in this module so none skips the breaker.

important: this is not wired into `require_client`. That helper takes a client the caller
fetched, which keeps the call visible to `tests/test_no_inline_get_client.py`; a wrapper that
fetched its own would hide a blocking revalidation on the event loop.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking

logger = logging.getLogger(__name__)

# One bounded pool for every lazy connect in the process. A per-router pool would let N
# routers each pin their own threads against the same dead cluster.
_CONNECT_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lazy_connect")

# Wall clock the caller waits. The socket timeout inside pylxd frees the thread; this only
# bounds the wait, so core/blocking.py requires both.
CONNECT_TIMEOUT: float = 30.0

# Match the worker count: queueing behind a saturated pool only moves the wait.
_MAX_IN_FLIGHT = 4


def connect_or_get_client(
    lxd_service: Any,
    cluster_id: str,
    expected_server_fingerprint: Optional[str] = None,
) -> Optional[Any]:
    """The pooled client, or one connected from Vault. None means "did not answer".

    Safe to call from a worker thread and from a fan-out helper that already runs in one.
    Async callers want `client_for`, which puts this on a bounded pool.

    The cold path calls get_client twice, here and inside ensure_connection. The second call
    costs at most another `_auto_reconnect` attempt, and only until the breaker trips; returning
    the client from ensure_connection instead would change a function many callers rely on.
    """
    client = (
        lxd_service.connection_manager.get_client(cluster_id)
        if expected_server_fingerprint is None
        else lxd_service.connection_manager.get_client(
            cluster_id,
            expected_server_fingerprint=expected_server_fingerprint,
        )
    )
    if client is not None:
        return client

    connected = (
        lxd_service.ensure_connection(cluster_id)
        if expected_server_fingerprint is None
        else lxd_service.ensure_connection(
            cluster_id,
            expected_server_fingerprint=expected_server_fingerprint,
        )
    )
    if not connected:
        return None

    return (
        lxd_service.connection_manager.get_client(cluster_id)
        if expected_server_fingerprint is None
        else lxd_service.connection_manager.get_client(
            cluster_id,
            expected_server_fingerprint=expected_server_fingerprint,
        )
    )


async def client_for(
    lxd_service: Any,
    cluster_id: str,
    *,
    label: str = "lazy_connect",
    expected_server_fingerprint: Optional[str] = None,
) -> Optional[Any]:
    """`connect_or_get_client` off the event loop. None on timeout or a saturated pool.

    Returning None keeps one error path: `require_client` turns it into a 503 with the breaker's
    recorded reason, so a slow connect and a refused one read the same to an operator.
    """
    try:
        connect_args = (
            (lxd_service, cluster_id)
            if expected_server_fingerprint is None
            else (lxd_service, cluster_id, expected_server_fingerprint)
        )
        return await run_blocking(
            connect_or_get_client,
            *connect_args,
            timeout=CONNECT_TIMEOUT,
            label=f"{label}[{cluster_id}]",
            executor=_CONNECT_EXECUTOR,
            max_in_flight=_MAX_IN_FLIGHT,
        )
    except BlockingTimeout:
        logger.warning("Connect to %s exceeded %ss", cluster_id, CONNECT_TIMEOUT)
        return None
    except BlockingPoolFull:
        logger.warning("Connect to %s shed: the connect pool is saturated", cluster_id)
        return None


# Wall clock for one pin refresh: two TLS handshakes at most, each with a 5 s socket timeout.
PIN_REFRESH_TIMEOUT: float = 15.0


async def refresh_pin_for(lxd_service: Any, cluster_id: str) -> Optional[str]:
    """Record a missing pin or a renewal on the bounded connect pool, one at a time per cluster.

    None when the refresh did not finish in time or the pool is saturated; the caller then reads
    whatever is pinned. A refused certificate (`LXDCertificatePinError`) propagates.
    """
    refresh = getattr(getattr(lxd_service, "connection_manager", None), "refresh_pin", None)
    if refresh is None:
        return None
    try:
        return await run_blocking(
            refresh,
            cluster_id,
            timeout=PIN_REFRESH_TIMEOUT,
            label="pin.refresh",
            executor=_CONNECT_EXECUTOR,
            max_in_flight=_MAX_IN_FLIGHT,
            concurrency_key=f"pin:{cluster_id}",
            max_in_flight_per_key=1,
        )
    except (BlockingTimeout, BlockingPoolFull) as exc:
        logger.warning("Pin refresh for %s did not finish: %s", cluster_id, exc)
        return None

