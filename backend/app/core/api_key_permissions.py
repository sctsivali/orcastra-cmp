"""The one permission check for an external API key's per-cluster grant.

The proxy routes (routers/integrations.py) and both upload routers call this instead of keeping
their own copy.

A grant stored as ``"permissions": []`` is valid input (ClusterPermission declares
``permissions: List[str] = Field(default=["read"])`` with no minimum length), and
``grant.get("permissions", ["read"])`` returns the default only when the key is ABSENT. An empty
grant is a decision the caller expressed, so it answers 403 rather than reaching ``max()`` with
nothing in it and failing as a 500.
"""

from typing import Any, Dict, List, Optional

from fastapi import HTTPException

# Ordered weakest to strongest. A grant satisfies a requirement when it reaches its level.
PERMISSION_LEVELS: Dict[str, int] = {"read": 0, "write": 1, "admin": 2}


def granted_level(permissions: Optional[List[str]]) -> int:
    """Strongest level in a grant, or -1 when it grants nothing.

    important: an empty grant is -1 because 0 is the level of "read" and would hand out read access
    the caller never chose. Every required level is above -1, so an empty grant satisfies nothing.
    """
    if not permissions:
        return -1
    return max(PERMISSION_LEVELS.get(name, 0) for name in permissions)


def granted_level_name(permissions: Optional[List[str]]) -> Optional[str]:
    """The name of the level `granted_level` enforces, or None when the grant holds nothing."""
    level = granted_level(permissions)
    return next((name for name, rank in PERMISSION_LEVELS.items() if rank == level), None)


def refuse_a_paused_grant(api_key: Dict[str, Any], cluster_id: str) -> None:
    """404 `cluster_not_registered` when the key holds `cluster_id` only through a paused grant.

    A paused grant was made for a registration of the id that has ended (services/
    cluster_generations.py), so the cluster it was granted is gone: the same answer an id no cluster
    is registered under gets. The key already holds the id, so this discloses nothing. Call it
    wherever a missing grant would otherwise answer 403.
    """
    live = {g.get("cluster_id") for g in api_key.get("cluster_permissions", []) if isinstance(g, dict)}
    if cluster_id in live:
        return
    # A grant left out because the purchase its creator reaches the cluster by has ended
    # (services/key_paid_term.py): the cluster is registered, the key's access to it is over.
    if (api_key.get("_purchase_clusters") or {}).get(cluster_id) == "ended":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "purchase_ended",
                "message": f"The purchase that gave this key access to cluster '{cluster_id}' has ended",
            },
        )
    if any(
        isinstance(held, dict) and held.get("cluster_id") == cluster_id
        for held in api_key.get("_held_cluster_permissions", [])
    ):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "cluster_not_registered",
                "message": f"Cluster '{cluster_id}' is not registered with this control plane",
            },
        )


def assert_cluster_permission(
    api_key: Dict[str, Any], cluster_id: str, required: str
) -> Dict[str, Any]:
    """Raise 403 unless the key's grant for ``cluster_id`` reaches ``required``.

    Returns the matched grant so callers that also need its projects do not look it up twice.
    """
    grants = {
        grant["cluster_id"]: grant
        for grant in api_key.get("cluster_permissions", [])
        if isinstance(grant, dict) and "cluster_id" in grant
    }
    grant = grants.get(cluster_id)
    if grant is None:
        refuse_a_paused_grant(api_key, cluster_id)
        raise HTTPException(status_code=403, detail=f"No permission for cluster {cluster_id}")

    held = grant.get("permissions", ["read"])
    if granted_level(held) < PERMISSION_LEVELS.get(required, 0):
        # Naming what the key holds points an operator at the check instead of the credential.
        # The caller can read the same list from GET /integrations/whoami, so this discloses
        # nothing it does not already hold.
        raise HTTPException(
            status_code=403,
            detail=(
                f"Operation requires {required} on cluster {cluster_id}; "
                f"this key holds {sorted(held) or ['nothing']}. "
                "GET /api/v1/integrations/whoami lists what the key may do."
            ),
        )
    return grant
