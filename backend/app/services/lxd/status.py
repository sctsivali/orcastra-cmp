"""
Cheap per-cluster reachability, for surfaces that need to know which clusters will answer
without paying for the ones that will not.

`/nodes` answers the same question, but it also reads resources, aggregates MicroCloud member
capacity and builds a GPU inventory per cluster. That is right for the node-status page and
wrong for a dropdown an operator opens six times an hour, both in latency and in payload: a
picker row needs a name and a status, not the hardware inventory of every cluster in scope.

Reachability is a one-round-trip question. `GET {endpoint}/1.0` with the client certificate
returns `metadata.auth`, which is the entire online / not_authorized / offline discriminator.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.node_metadata import get_node_metadata_store
from app.models.cluster_status import AvailabilityReason, ClusterStatus, derive_status
from app.models.schemas import ClusterAvailability

from . import health
from .exceptions import LXDAuthorizationError, LXDConnectionError

logger = logging.getLogger(__name__)

# Bounded so a large fleet cannot outgrow the pool. Every probe carries its own socket
# timeouts, so the worst case is one read timeout of wall clock, not one per cluster.
_probe_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="lxd_probe")

# The reason string reaches an operator, so it must stay short and must never carry a stack
# trace or a certificate path.
MAX_REASON_LENGTH = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _short(message: Optional[str]) -> Optional[str]:
    if not message:
        return None
    text = " ".join(str(message).split())
    return text if len(text) <= MAX_REASON_LENGTH else f"{text[: MAX_REASON_LENGTH - 3]}..."


def _resolve_display_name(cluster_id: str, vault_display_name: Optional[str]) -> str:
    """Vault metadata first, the local override store second, the id last.

    important: the two stores can disagree. A rename through PATCH /nodes/{name} writes the
    local JSON store only, so preferring one consistently is what stops a cluster having two
    names depending on which endpoint the page called.
    """
    if vault_display_name:
        return vault_display_name
    try:
        local = get_node_metadata_store().get_node_display_name(cluster_id)
    except Exception:
        local = None
    return local or cluster_id


def _availability(
    cluster_id: str,
    *,
    display_name: Optional[str],
    endpoint: str,
    connected: bool,
    reachable: Optional[bool],
    auth: Optional[str] = None,
    error: Optional[str] = None,
    node_type: Optional[str] = None,
) -> ClusterAvailability:
    # important: `error` is deliberately NOT forwarded to derive_status here. Raw exception
    # text from this layer carries the Vault base URL, the KV path to a cluster's certs and the
    # temp client-cert path, and this endpoint is reachable by every role down to tenant. The
    # curated sentence plus `reason_code` says everything an operator can act on; the detail
    # stays in the logs. The caller still passes `error` so it can be logged upstream.
    status, reason_code, message = derive_status(
        connected=connected, reachable=reachable, auth=auth
    )
    return ClusterAvailability(
        name=cluster_id,
        display_name=_resolve_display_name(cluster_id, display_name),
        endpoint=endpoint or "unknown",
        status=status,
        reason_code=reason_code,
        reason=None if status == ClusterStatus.ONLINE else _short(message),
        node_type=node_type,
        checked_at=_now(),
    )


def project_from_nodes_body(body: Dict[str, Any]) -> List[ClusterAvailability]:
    """Project a cached /nodes response into picker rows. Pure: no network, no Vault.

    This is the fast path. When the node-status page or the metrics prewarm has already paid
    for the fan-out, the picker rides it for the cost of one Redis read.
    """
    rows: List[ClusterAvailability] = []
    for node in body.get("nodes", []) or []:
        name = node.get("name")
        if not name:
            continue
        rows.append(
            _availability(
                name,
                display_name=node.get("display_name"),
                endpoint=node.get("endpoint") or "unknown",
                connected=bool(node.get("connected")),
                reachable=node.get("reachable"),
                auth=node.get("auth"),
                error=node.get("error"),
                node_type=node.get("node_type"),
            )
        )
    return rows


def probe_cluster(
    cluster_id: str,
    *,
    cert_manager,
    connection_manager,
    settings,
    force: bool = False,
) -> ClusterAvailability:
    """Probe one cluster, cheapest signal first. Never raises: a failure is the answer.

    Order is breaker, then the pooled client, then a fresh certificate connect. `force` skips
    the breaker and is reserved for an operator-initiated recheck, so an ordinary picker open
    can never re-probe a fleet of dead clusters.
    """
    endpoint = "unknown"
    vault_display_name = None
    node_type = None
    try:
        metadata = cert_manager.vault.vault_read(
            f"{settings.vault_clusters_path}/{cluster_id}/metadata"
        )
        endpoint = metadata.get("endpoint", "unknown")
        vault_display_name = metadata.get("display_name")
        node_type = metadata.get("type")
    except Exception as exc:
        # A metadata read failure is a fault in us, not an observation about the cluster.
        logger.warning("Could not read Vault metadata for %s: %s", cluster_id, exc)
        return ClusterAvailability(
            name=cluster_id,
            display_name=_resolve_display_name(cluster_id, None),
            endpoint="unknown",
            status=ClusterStatus.ERROR,
            reason_code=AvailabilityReason.UNKNOWN,
            reason="Could not read this cluster's registration.",
            node_type=None,
            checked_at=_now(),
        )

    if not force and health.is_tripped(cluster_id):
        answered = health.get_kind(cluster_id) == health.KIND_UNAUTHORIZED
        return _availability(
            cluster_id,
            display_name=vault_display_name,
            endpoint=endpoint,
            connected=False,
            reachable=answered,
            error=health.get_reason(cluster_id),
            node_type=node_type,
        )

    try:
        if not force and connection_manager.get_client(cluster_id) is not None:
            return _availability(
                cluster_id,
                display_name=vault_display_name,
                endpoint=endpoint,
                connected=True,
                reachable=True,
                node_type=node_type,
            )
    except Exception as exc:
        logger.debug("Pooled client lookup failed for %s: %s", cluster_id, exc)

    try:
        cert_data = cert_manager.get_cluster_certificate(cluster_id)
        client = connection_manager.connect_with_vault_cert(
            cluster_id=cluster_id,
            endpoint=endpoint,
            cert_content=cert_data["client.crt"],
            key_content=cert_data["client.key"],
            verify=False,
            force=force,
        )
        return _availability(
            cluster_id,
            display_name=vault_display_name,
            endpoint=endpoint,
            connected=client is not None,
            reachable=client is not None,
            error=None if client is not None else health.get_reason(cluster_id),
            node_type=node_type,
        )
    except LXDAuthorizationError as exc:
        # The host answered and refused the certificate. Pending Trust, not Offline.
        return _availability(
            cluster_id,
            display_name=vault_display_name,
            endpoint=endpoint,
            connected=False,
            reachable=True,
            error=str(exc),
            node_type=node_type,
        )
    except LXDConnectionError as exc:
        return _availability(
            cluster_id,
            display_name=vault_display_name,
            endpoint=endpoint,
            connected=False,
            reachable=False,
            error=str(exc),
            node_type=node_type,
        )
    except Exception as exc:
        logger.warning("Reachability probe failed for %s: %s", cluster_id, exc)
        return _availability(
            cluster_id,
            display_name=vault_display_name,
            endpoint=endpoint,
            connected=False,
            reachable=False,
            error=str(exc),
            node_type=node_type,
        )


def probe_clusters(
    cluster_ids: List[str],
    *,
    cert_manager,
    connection_manager,
    settings,
    force: bool = False,
) -> List[ClusterAvailability]:
    """Probe every cluster in parallel.

    important: a plain def, not async. Every caller must reach it through run_blocking, which
    is what tests/test_no_inline_get_client.py enforces: get_client revalidates with a live
    request, so calling it inline would park a whole event loop on a dead peer.

    important: `cluster_ids` MUST already be org-filtered by the caller. run_in_executor does
    not carry contextvars into the worker threads, so the org-scope check inside
    connection.get_client sees an empty context and never denies anything from here. The router
    filters through lxd_service.get_node_names() before offloading; a future caller that does
    not gets no second line of defence.
    """
    if not cluster_ids:
        return []
    futures = [
        _probe_executor.submit(
            probe_cluster,
            cid,
            cert_manager=cert_manager,
            connection_manager=connection_manager,
            settings=settings,
            force=force,
        )
        for cid in cluster_ids
    ]
    rows: List[ClusterAvailability] = []
    for cid, future in zip(cluster_ids, futures):
        try:
            rows.append(future.result())
        except Exception as exc:
            # A cluster is never dropped. An operator who cannot see a cluster in the list
            # concludes it was deregistered, which is a worse lie than "we could not tell".
            logger.warning("Probe raised for %s: %s", cid, exc)
            rows.append(
                ClusterAvailability(
                    name=cid,
                    display_name=_resolve_display_name(cid, None),
                    endpoint="unknown",
                    status=ClusterStatus.ERROR,
                    reason_code=AvailabilityReason.UNKNOWN,
                    reason="Could not establish this cluster's status.",
                    node_type=None,
                    checked_at=_now(),
                )
            )
    return rows
