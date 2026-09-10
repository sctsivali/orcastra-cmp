"""
Snapshot operations for custom storage volumes.

important: nothing calls any of this. There is no router, no interface and no test, and the
feature doc records it as the starting point for a volume-snapshot surface rather than as
half-wired code. It lives in its own module so the instance snapshot path, which is on every
operator page load, is not read through a hundred lines that never execute.

The naming policy is shared deliberately: whenever this surface is built, an operator volume
snapshot must be identifiable and must not collide with the prefixes OrcaHub owns, for the same
reasons recorded in snapshots.py.
"""

import logging
from typing import Any, Dict, List, Optional

from app.services.lxd.snapshots import (
    MAX_OPS_SNAPSHOTS_PER_INSTANCE,
    SnapshotLimitError,
    _metadata,
    _normalize,
    _operation_id,
    _policy_from_config,
    _project_params,
    is_operator_snapshot,
)
from app.core.lxd_path import safe_segment

logger = logging.getLogger(__name__)


def _volume_api(client, pool: str, volume_name: str, volume_type: str = "custom"):
    return client.api.storage_pools[safe_segment(pool)].volumes[safe_segment(volume_type)][safe_segment(volume_name)]


def list_volume_snapshots(
    client, pool: str, volume_name: str, project: str, volume_type: str = "custom"
) -> List[Dict[str, Any]]:
    response = _volume_api(client, pool, volume_name, volume_type).snapshots.get(
        params=_project_params(project, recursion=1)
    )
    return [_normalize(entry, volume_name) for entry in (_metadata(response) or [])]


def create_volume_snapshot(
    client,
    pool: str,
    volume_name: str,
    project: str,
    snapshot_name: str,
    *,
    expires_at: Optional[str] = None,
    volume_type: str = "custom",
):
    body: Dict[str, Any] = {"name": snapshot_name}
    if expires_at:
        body["expires_at"] = expires_at
    response = _volume_api(client, pool, volume_name, volume_type).snapshots.post(
        json=body, params=_project_params(project)
    )
    return _operation_id(response)


def restore_volume_snapshot(
    client,
    pool: str,
    volume_name: str,
    project: str,
    snapshot_name: str,
    volume_type: str = "custom",
):
    response = _volume_api(client, pool, volume_name, volume_type).put(
        json={"restore": snapshot_name}, params=_project_params(project)
    )
    return _operation_id(response)


def delete_volume_snapshot(
    client,
    pool: str,
    volume_name: str,
    project: str,
    snapshot_name: str,
    volume_type: str = "custom",
):
    response = (
        _volume_api(client, pool, volume_name, volume_type)
        .snapshots[snapshot_name]
        .delete(params=_project_params(project))
    )
    return _operation_id(response)


def read_volume_policy(
    client, pool: str, volume_name: str, project: str, volume_type: str = "custom"
) -> Dict[str, Any]:
    response = _volume_api(client, pool, volume_name, volume_type).get(
        params=_project_params(project)
    )
    config = (_metadata(response) or {}).get("config") or {}
    return _policy_from_config(config)


def count_operator_volume_snapshots(
    client, pool: str, volume_name: str, project: str, volume_type: str = "custom"
) -> int:
    return sum(
        1
        for snapshot in list_volume_snapshots(client, pool, volume_name, project, volume_type)
        if is_operator_snapshot(snapshot["name"])
    )


def assert_volume_capacity(
    client, pool: str, volume_name: str, project: str, volume_type: str = "custom"
) -> None:
    held = count_operator_volume_snapshots(client, pool, volume_name, project, volume_type)
    if held >= MAX_OPS_SNAPSHOTS_PER_INSTANCE:
        raise SnapshotLimitError(
            f"{volume_name} already holds {held} operator snapshots, the maximum is "
            f"{MAX_OPS_SNAPSHOTS_PER_INSTANCE}. Delete one before taking another."
        )
