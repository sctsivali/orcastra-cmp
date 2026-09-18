"""The set of registered cluster ids, for a caller that has no org scope to lean on.

The OrcaHub-facing writers store a `cluster_id` the caller typed. Before this read they only
checked it against the API key's own grant, which is validated at key creation and can carry an
id no cluster has (the dev installation held 25 rules under three spellings of clusters that
had been registered again under new ids). A rule under an unregistered id counts for nothing
anywhere, so it is refused at the boundary instead.
"""

from __future__ import annotations

import asyncio
from typing import FrozenSet, Optional


async def registered_cluster_ids() -> Optional[FrozenSet[str]]:
    """Every registered cluster id, or None when the registry cannot be read and nothing is known.

    Reads the Vault registry through the certificate manager (one cached LIST, served from the
    last-known memo during an outage). Last-known is authoritative here: a registration writes
    Vault before anything else, so a cluster the memo has never seen does not exist yet.

    note: not `connection_manager.get_node_names()`, which lists only the clusters this worker
    has connected, and no org-scope filter: the external router carries none and this answers
    for the whole registry. Module-level so a test can replace it.
    """
    from app.services.lxd_service import get_lxd_service

    manager = get_lxd_service().cert_manager
    if manager is None:
        return None
    read = await asyncio.to_thread(manager.read_registry, True)
    if read.source is None:
        return None
    return frozenset(read.cluster_ids)
