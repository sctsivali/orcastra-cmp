"""Cluster-id checks and caller-facing error text for the integration (API key) routes.

A key grant names clusters by the id it was given, and a cluster registered again under a new
id leaves the earlier id behind in grants. The key routes report such an id as unregistered
instead of looking it up, the proxies answer 404 `cluster_not_registered` instead of an outage,
and a grant write refuses to add one.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, FrozenSet, List

from fastapi import HTTPException
from pylxd.exceptions import LXDAPIException
from requests import exceptions as requests_exceptions
from urllib3 import exceptions as urllib3_exceptions

from app.services.lxd import registry as cluster_registry
from app.services.lxd.exceptions import LXDOperationError

logger = logging.getLogger(__name__)


async def unregistered_cluster_ids(cluster_ids: Any) -> FrozenSet[str]:
    """The ids in a key's grant that no cluster is registered under.

    Empty when the registry cannot be read, so an outage changes nothing a caller sees. Grants
    are stored as typed, so a cluster registered again under a new id leaves the earlier id
    behind. The entry stays in what the key reports: the published v1 contract says /validate
    and /whoami describe what the key holds.
    """
    registered = await cluster_registry.registered_cluster_ids()
    if registered is None:
        return frozenset()
    return frozenset(c for c in cluster_ids if c and c not in registered)


async def refuse_new_grants_to_unregistered_clusters(
    requested: Any, already_held: FrozenSet[str] = frozenset()
) -> None:
    """A grant write may add only ids a cluster is registered under; ids it already holds stay.

    Hygiene only: the caller's allowed set, checked first, is the authorization boundary. A
    grant naming an id no cluster holds authorises nothing, but it is reported as held and
    polled forever. Ids already stored stay editable so a key is never frozen until repaired
    (backend/scripts/repair_key_grant_cluster_ids.py). An unreadable registry refuses nothing.
    """
    new_ids = {c for c in requested if c} - set(already_held)
    if not new_ids:
        return
    registered = await cluster_registry.registered_cluster_ids(fresh=True)
    if registered is None:
        logger.warning("Cluster registry unreadable; a grant to %s was not checked against it", sorted(new_ids))
        return
    missing = sorted(new_ids - registered)
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "cluster_not_registered",
                "message": f"No cluster is registered under: {', '.join(missing)}",
            },
        )


async def refuse_a_cluster_not_registered(cluster_id: str) -> None:
    """404 `cluster_not_registered` for an id the key's grant names but no cluster holds.

    Called after the key's own grant check, so it tells a caller nothing about an id its grant
    does not already name. Non-retryable, so a polling integration does not treat the id as an
    outage and retry it. When the registry cannot be read nothing is refused here.
    """
    if cluster_id in await unregistered_cluster_ids([cluster_id]):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "cluster_not_registered",
                "message": f"Cluster '{cluster_id}' is not registered with this control plane",
            },
        )


def cluster_display_names_sync(cert_manager: Any, cluster_ids: List[str]) -> Dict[str, str]:
    """Display names from the Vault registry; runs on a worker thread, never on the loop."""
    names: Dict[str, str] = {}
    for cluster_id in cluster_ids:
        try:
            meta = cert_manager.get_cluster_info(cluster_id).get("metadata", {})
            names[cluster_id] = meta.get("display_name") or meta.get("name") or cluster_id
        except Exception:  # noqa: BLE001 - a name is decoration; the id stands in
            names[cluster_id] = cluster_id
    return names


def cluster_infos_sync(cert_manager: Any, cluster_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    infos: Dict[str, Dict[str, Any]] = {}
    for cluster_id in cluster_ids:
        try:
            infos[cluster_id] = cert_manager.get_cluster_info(cluster_id)
        except Exception as e:  # noqa: BLE001 - one cluster's record must not hide the others
            logger.warning(f"Could not get region info for {cluster_id}: {e}")
    return infos


def reported_grant(grant: Dict[str, Any], unregistered: FrozenSet[str]) -> Dict[str, Any]:
    if grant.get("cluster_id") in unregistered:
        return {**grant, "registered": False}
    return grant


def proxy_error_text(error: Exception, error_code: str, operation: str, cluster_id: str) -> str:
    """The caller-facing sentence for a failed proxy operation.

    important: never the text of a transport exception. A requests or urllib3 error names the LXD
    host's address and port (`HTTPSConnectionPool(host='10.x.x.x', port=8443)`), infrastructure an
    integration must not learn. The daemon's own refusal (an LXDAPIException, or the service
    layer's errors built from one) is safe and useful.
    """
    if isinstance(error, (requests_exceptions.RequestException, urllib3_exceptions.HTTPError)):
        if error_code == "timeout":
            return f"{operation} did not get an answer from {cluster_id} in time"
        return f"Could not reach {cluster_id} for {operation}"
    if isinstance(error, (LXDAPIException, LXDOperationError, HTTPException)):
        return str(error)
    return f"{operation} failed on {cluster_id}"
