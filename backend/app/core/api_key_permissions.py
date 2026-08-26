"""The one permission check for an external API key's per-cluster grant.

This existed three times: inline in the raw proxy (`routers/integrations.py`) and copied verbatim
as `_assert_cluster_permission` into both `routers/image_uploads.py` and `routers/iso_uploads.py`.
All three carried the same defect, which is what duplication costs.

``max()`` over an empty sequence raises ValueError, and ``perm.get("permissions", ["read"])``
returns the default only when the key is ABSENT. A grant stored as ``"permissions": []`` therefore
reached ``max()`` with nothing in it. Pydantic accepts that shape on the way in: ClusterPermission
declares ``permissions: List[str] = Field(default=["read"])`` with no minimum length, so an empty
list is a valid request body, not corruption.

The crash surfaced as HTTP 500 from a generic handler. Fail-closed by accident is still the wrong
answer: an empty grant is a decision the caller expressed, and the honest reply is 403.
"""

from typing import Any, Dict, List, Optional

from fastapi import HTTPException

# Ordered weakest to strongest. A grant satisfies a requirement when it reaches its level.
PERMISSION_LEVELS: Dict[str, int] = {"read": 0, "write": 1, "admin": 2}


def granted_level(permissions: Optional[List[str]]) -> int:
    """Strongest level in a grant, or -1 when it grants nothing.

    important: -1, not 0. Zero is the level of "read", so collapsing an empty grant to zero would
    hand out read access nobody asked for. Any required level is above -1, so an empty grant
    satisfies nothing.
    """
    if not permissions:
        return -1
    return max(PERMISSION_LEVELS.get(name, 0) for name in permissions)


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
        raise HTTPException(status_code=403, detail=f"No permission for cluster {cluster_id}")

    if granted_level(grant.get("permissions", ["read"])) < PERMISSION_LEVELS.get(required, 0):
        raise HTTPException(
            status_code=403, detail=f"Operation requires {required} permission"
        )
    return grant
