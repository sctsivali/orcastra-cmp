"""One cluster, one client, obtained without punishing the caller for a dead peer.

`connection_manager.get_client` is memory-only. It answers from `_clients`, or rebuilds from
`_connection_info`, and returns None for a worker that has never connected to this cluster.
With WEB_CONCURRENCY=4 that is the ordinary state after a registration: the worker that served
the POST holds a client and the other three do not, so the same read answered 200 or 503
depending on which worker happened to take it, until the background sweep caught up.

`LXDService.ensure_connection` is the repair, and `connection.py` names it as such in the
comment above the branch that returns None. It reads the certificate out of Vault and connects,
and it consults the breaker before doing any of that.

Order matters and is not negotiable:

  1. the pooled client, which costs one revalidation;
  2. the breaker, inside ensure_connection, so a fleet of dead clusters costs nothing;
  3. Vault, only for a cluster that is registered, in scope, and not known to be down.

`documentation/reliability/cluster-connection-breaker.md` records what happens when a new
connect door skips step 2: gating one and not the other took /health from 10.3s to 20.1s. Any
future entry point belongs here rather than beside here.

important: this is deliberately NOT wired into `require_client`. That helper takes a client
someone else fetched, which is what keeps `tests/test_no_inline_get_client.py` able to see the
call, and a wrapper that fetched its own would hide a blocking revalidation back onto the loop.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking

logger = logging.getLogger(__name__)

# One bounded pool for every lazy connect in the process. A per-router pool would let N
# routers each pin their own threads against the same dead cluster.
_CONNECT_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lazy_connect")

# Wall clock the caller waits. The socket timeout inside pylxd is what actually frees the
# thread; this only bounds the wait, which is why core/blocking.py insists on both.
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

    note: the cold path calls get_client twice, once here and once inside ensure_connection.
    The second is a repeat of a None answer, so it costs another `_auto_reconnect` attempt at
    worst, and only until the breaker trips. Threading the client out of ensure_connection to
    save it would put a second meaning on a function eight other callers rely on.
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

    Returning None rather than raising keeps the caller's shape: `require_client` turns it into
    the one 503 with the breaker's recorded reason, so a slow connect and a refused one read
    the same way to an operator and there is no second error path to get wrong.
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
