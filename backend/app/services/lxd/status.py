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

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.blocking import run_blocking
from app.core.cache import (
    NODES_AVAILABILITY_CACHE_KEY,
    NODES_AVAILABILITY_CACHE_TTL,
    NODES_STATUS_CACHE_KEY,
    get_cache_manager,
)
from app.core.config import get_settings
from app.core.node_metadata import get_node_metadata_store
from app.core.org_scope import apply_cluster_filter, get_scope_cache_key
from app.core.single_flight import single_flight
from app.core.type_utils import resolve_cluster_type
from app.models.cluster_status import AvailabilityReason, ClusterStatus, derive_status
from app.models.schemas import ClusterAvailability, ClusterAvailabilityResponse

from . import health
from .exceptions import LXDAuthorizationError, LXDConnectionError

logger = logging.getLogger(__name__)

# Bounded so a large fleet cannot outgrow the pool. Every probe carries its own socket
# timeouts, so the worst case is one read timeout of wall clock, not one per cluster.
_probe_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="lxd_probe")

# Availability probes get their own pool so a slow reachability sweep cannot starve the
# node-status fan-out, and vice versa.
_availability_executor = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="nodes_availability"
)
# One connect budget (3s) plus one read budget (8s) plus margin. The probes run in parallel,
# so this bounds the whole sweep, not one cluster.
AVAILABILITY_PROBE_TIMEOUT = 20.0

# The reason string reaches an operator, so it must stay short and must never carry a stack
# trace or a certificate path.
MAX_REASON_LENGTH = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Accept the datetime or the ISO string a cached body round-trips to."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _short(message: Optional[str]) -> Optional[str]:
    if not message:
        return None
    text = " ".join(str(message).split())
    return text if len(text) <= MAX_REASON_LENGTH else f"{text[: MAX_REASON_LENGTH - 3]}..."


def resolve_display_name(cluster_id: str, vault_display_name: Optional[str]) -> str:
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
    collected_at: Optional[datetime] = None,
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
        display_name=resolve_display_name(cluster_id, display_name),
        endpoint=endpoint or "unknown",
        status=status,
        reason_code=reason_code,
        reason=None if status == ClusterStatus.ONLINE else _short(message),
        node_type=node_type,
        # important: _now() only when the caller genuinely just checked. The projection
        # path below replays an already-cached /nodes body, and stamping it now reported
        # "checked just now" for data up to three minutes old, on the one field that
        # exists to say otherwise.
        checked_at=collected_at or _now(),
    )


def project_from_nodes_body(body: Dict[str, Any]) -> List[ClusterAvailability]:
    """Project a cached /nodes response into picker rows. Pure: no network, no Vault.

    This is the fast path. When the node-status page or the metrics prewarm has already paid
    for the fan-out, the picker rides it for the cost of one Redis read.
    """
    rows: List[ClusterAvailability] = []
    # The batch stamp covers offline nodes too, which carry no resources and so have no
    # sample time of their own. Absent on a pre-v5 body, which correctly falls back to now.
    body_collected_at = _parse_timestamp(body.get("collected_at"))
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
                collected_at=body_collected_at,
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
            display_name=resolve_display_name(cluster_id, None),
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
        pooled = None if force else connection_manager.get_client(cluster_id)
        if pooled is not None:
            return _availability(
                cluster_id,
                display_name=vault_display_name,
                endpoint=endpoint,
                connected=True,
                reachable=True,
                node_type=resolve_cluster_type(node_type, pooled),
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
            node_type=resolve_cluster_type(node_type, client),
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
                    display_name=resolve_display_name(cid, None),
                    endpoint="unknown",
                    status=ClusterStatus.ERROR,
                    reason_code=AvailabilityReason.UNKNOWN,
                    reason="Could not establish this cluster's status.",
                    node_type=None,
                    checked_at=_now(),
                )
            )
    return rows


def _build_response(cache, cache_key: str, rows, source: str) -> ClusterAvailabilityResponse:
    """Build the availability body and cache it.

    note: an all-offline result IS the answer here and is cached like any other. That differs
    from the list fan-outs elsewhere, which refuse to cache an empty result because an empty
    list cannot be told apart from "nobody answered". Here the difference is the payload.
    """
    response = ClusterAvailabilityResponse(
        clusters=rows,
        online_count=sum(1 for row in rows if row.status == ClusterStatus.ONLINE),
        total_count=len(rows),
        source=source,
        generated_at=_now(),
    )
    if cache:
        cache.set(
            "api_responses",
            cache_key,
            response.model_dump(mode="json"),
            ttl=NODES_AVAILABILITY_CACHE_TTL,
        )
    return response


class RegistryUnavailable(Exception):
    """The cluster registry could not be read and nothing is known from before."""


@single_flight("nodes:availability")
async def resolve_availability(
    lxd_service, *, fresh: bool = False
) -> ClusterAvailabilityResponse:
    """Reachability for every cluster in the caller's org scope, cheapest path first.

    The one resolver behind both `/nodes/availability` and `/integrations/available-clusters`.
    Two endpoints deriving reachability separately is how the API key surface would come to
    disagree with the Clusters page about which cluster is answering, and on a surface whose
    only job is to say what a credential can reach that is the wrong kind of ambiguity.

    Three tiers, in cost order: the scoped availability cache, then projecting a warm `/nodes`
    body, then a reachability-only probe. Sharing the cache key is what keeps a second caller
    free: it is one Redis read, never a second fan-out.

    important: the cluster ids come from `lxd_service.get_node_names()`, which applies the
    org-scope filter. `probe_clusters` documents why that filter cannot be left to the layer
    below: contextvars do not cross into the worker threads.

    important: the single-flight collapse sits here rather than on `/nodes/availability`, where
    it used to. On the route it only collapsed arrivals at that one endpoint, so a picker open
    reaching this through `/integrations/available-clusters` started a fan-out of its own beside
    the one already running for the same scope. The rebuild is what needs collapsing, and this is
    the rebuild.

    Raises BlockingTimeout or BlockingPoolFull when the probe cannot run. Callers decide what
    that means for them; it is never "every cluster is offline".
    """
    cache = get_cache_manager()
    availability_key = get_scope_cache_key(NODES_AVAILABILITY_CACHE_KEY)

    if not fresh:
        cached = cache.get("api_responses", availability_key)
        if cached is not None:
            # note: overwrite `source`, and only `source`. The stored body records how it was
            # first produced, so returning it verbatim reports "probe" on every cache hit and
            # makes the field useless for the one question it exists to answer. `generated_at`
            # is deliberately left as stored: restamping it would claim data was built now.
            return ClusterAvailabilityResponse(**{**cached, "source": "availability_cache"})

        # The node-status page and the metrics prewarm both fill this key. Riding it costs one
        # Redis read and keeps every picker from generating its own fan-out.
        nodes_body = cache.get("api_responses", get_scope_cache_key(NODES_STATUS_CACHE_KEY))
        if nodes_body is not None:
            return _build_response(
                cache, availability_key, project_from_nodes_body(nodes_body), "nodes_cache"
            )

    # note: the registry read is a Vault LIST with its own budget (core/vault_client), so it
    # runs off the loop; inline it parked every request for the length of a lost LIST. A lost
    # registry with nothing known is the one case this cannot answer: probing an empty fleet
    # would report "no clusters". While the last known registry is served the probe runs over
    # it like any other read.
    registry = await asyncio.to_thread(lxd_service.cert_manager.read_registry)
    if not registry.readable and registry.source is None:
        raise RegistryUnavailable()
    cluster_ids = apply_cluster_filter(registry.cluster_ids)
    rows = await run_blocking(
        probe_clusters,
        cluster_ids,
        cert_manager=lxd_service.cert_manager,
        connection_manager=lxd_service.connection_manager,
        settings=get_settings(),
        # Only an operator-initiated recheck may re-probe past the breaker. An ordinary
        # picker open must not walk a fleet of dead clusters paying connect timeouts.
        force=fresh,
        timeout=AVAILABILITY_PROBE_TIMEOUT,
        label="nodes.availability",
        executor=_availability_executor,
        # important: without a cap, _acquire never raises and the pool-full path is dead code.
        # single_flight collapses same-scope arrivals; this bounds what it cannot, sweeps for
        # *different* scopes queueing up on a degraded fleet.
        max_in_flight=4,
    )
    return _build_response(cache, availability_key, rows, "probe")
