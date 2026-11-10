"""Whether a host is a multi-member cluster, and who its members are.

Shared by the create paths that have to be two-phase on a cluster (define on every member
with ?target=, then create cluster-wide): networks and storage pools.
"""

import logging
from typing import List

logger = logging.getLogger(__name__)


def is_clustered(client) -> bool:
    """True when the LXD host is a multi-member cluster.

    Unknown or unsupported reads as standalone.
    """
    try:
        meta = client.api.cluster.get().json().get("metadata", {}) or {}
        if not meta.get("enabled"):
            return False
        return len(cluster_member_names(client)) > 1
    except Exception as e:
        logger.debug(f"Could not determine cluster state, assuming standalone: {e}")
        return False


def cluster_member_names(client) -> List[str]:
    """Member names from /1.0/cluster/members (returns URLs at recursion=0)."""
    members = client.api.cluster.members.get().json().get("metadata", []) or []
    names = []
    for m in members:
        if isinstance(m, str):
            names.append(m.rstrip("/").rsplit("/", 1)[-1])
        elif isinstance(m, dict) and m.get("server_name"):
            names.append(m["server_name"])
    return names
