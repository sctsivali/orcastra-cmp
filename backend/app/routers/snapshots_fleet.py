"""Fleet view of snapshots, at /snapshots.

Separate from the per-instance surface because it answers a different question: not "what can I
roll this instance back to" but "what snapshots exist in this project, and what are they costing
me". The per-instance router owns the actions; this one only reads.

The read is one LXD call. `recursion=2` folds each instance's snapshots into the instance
listing, so a project of forty instances costs one request rather than forty-one. LXD omits the
`snapshots` key entirely for an instance with none, so it is read defensively.
"""
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.lxd_errors import lxd_http_error
from app.core.org_scope import org_scope_dependency, verify_cluster_access
from app.core.dependencies import require_tenant
from app.services.lxd import snapshots as snapshots_service
from app.services.lxd.lazy_client import client_for
from app.routers.instance_snapshots import _blocking, _enforce_snapshot_access
from app.services.lxd_service import LXDService, get_lxd_service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/snapshots",
    tags=["Snapshots - Fleet"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)


class FleetSnapshotListResponse(BaseModel):
    total: int = Field(..., description="Number of snapshots returned")
    items: List[Dict[str, Any]] = Field(default_factory=list, description="Snapshots across the requested scope")
    unreachable_nodes: List[str] = Field(
        default_factory=list,
        description="Clusters that could not be read. Distinguishes an empty result from a partial one",
    )


@router.get("", response_model=FleetSnapshotListResponse)
async def list_fleet_snapshots(
    host: str = Query(..., description="Cluster to read"),
    project: str = Query("default", description="LXD project to read"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Every snapshot in one project, in a single LXD call.

    Scoped to one cluster and project deliberately. A fleet-wide sweep would fan out across every
    cluster on every page load, and this console already learned that lesson: an all-node read
    that fails partially must never be cached as if it were empty.
    """
    verify_cluster_access(host)
    _enforce_snapshot_access(host, project, None)

    unreachable: List[str] = []
    client = await client_for(lxd_service, host, label="snapshots.fleet")
    if client is None:
        # Not an error and not an empty project. The caller is told which node went unread so the
        # UI can say so rather than showing a confident, wrong, empty table.
        return FleetSnapshotListResponse(total=0, items=[], unreachable_nodes=[host])

    try:
        rows = await _blocking(
            snapshots_service.list_project_snapshots,
            client,
            project,
            label="snapshots.fleet",
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise lxd_http_error(exc, "Project", project)

    for row in rows:
        row["host"] = host
    return FleetSnapshotListResponse(
        total=len(rows), items=rows, unreachable_nodes=unreachable
    )
