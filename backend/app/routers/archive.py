"""
API Router for Log Archive & Compliance operations.

Provides endpoints for managing OpenSearch log archive:
- Archive health overview
- Snapshot listing, creation, deletion, restore
- Index ISM status monitoring
- Snapshot repository verification

All endpoints require OPENSEARCH_ARCHIVE_ENABLED=true.
"""

from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
import logging

from app.core.dependencies import check_read_only_mode, require_admin
from app.services.archive_service import (
    ArchiveService,
    ArchiveServiceError,
    get_archive_service,
)
from app.services.activity_logger import get_activity_logger

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/archive",
    tags=["Archive & Compliance"],
    dependencies=[Depends(require_admin())],
)


# -- Response Models --

class ArchiveOverviewResponse(BaseModel):
    """Archive system overview."""
    timestamp: str
    cluster: Dict[str, Any] = {}
    snapshot_repository: Dict[str, Any] = {}
    snapshots: Dict[str, Any] = {}
    indices: Dict[str, Any] = {}
    ism_policies: Dict[str, Any] = {}


class SnapshotInfo(BaseModel):
    """Single snapshot details."""
    snapshot: str
    state: str = ""
    start_time: str = ""
    end_time: str = ""
    duration_in_millis: int = 0
    indices: List[str] = []
    shards: Dict[str, Any] = {}


class SnapshotListResponse(BaseModel):
    """List of snapshots."""
    total: int
    snapshots: List[Dict[str, Any]]


class CreateSnapshotRequest(BaseModel):
    """Request body for manual snapshot creation."""
    name: str = Field(
        ...,
        description="Snapshot name (must be lowercase, no spaces)",
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    indices: str = Field(
        default="orcastra-*,vault-audit-*",
        description="Comma-separated index pattern(s) to snapshot",
    )
    wait_for_completion: bool = Field(
        default=False,
        description="Wait for snapshot to complete before responding",
    )


class RestoreSnapshotRequest(BaseModel):
    """Request body for restoring from snapshot."""
    snapshot_name: str = Field(..., description="Name of snapshot to restore")
    indices: Optional[str] = Field(
        default=None,
        description="Specific indices to restore (default: all in snapshot)",
    )
    rename_pattern: Optional[str] = Field(
        default=None,
        description="Regex pattern for renaming restored indices (ex; '(.+)')",
    )
    rename_replacement: Optional[str] = Field(
        default=None,
        description="Replacement for rename (ex; 'restored-$1')",
    )


class MessageResponse(BaseModel):
    """Generic message response."""
    message: str
    details: Dict[str, Any] = {}


# -- Dependency --

def _get_service() -> ArchiveService:
    """Get the archive service, raising 503 if not enabled."""
    try:
        return get_archive_service()
    except ArchiveServiceError as e:
        raise HTTPException(
            status_code=503,
            detail=str(e),
        )


# -- Endpoints ---

@router.get(
    "/status",
    response_model=ArchiveOverviewResponse,
    summary="Archive system overview",
    description="Get comprehensive archive status: cluster health, snapshots, indices, ISM policies.",
)
async def get_archive_status():
    """Get archive health overview."""
    # log audit view access
    activity_logger.log_activity(
        action="audit.view",
        target_type="archive",
        target_id="status",
        result="success",
        details={"view": "archive_overview"},
    )
    svc = _get_service()
    try:
        overview = await svc.get_archive_overview()
        return ArchiveOverviewResponse(**overview)
    except ArchiveServiceError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get(
    "/repository",
    response_model=Dict[str, Any],
    summary="Snapshot repository info",
    description="Get snapshot repository configuration and health.",
)
async def get_repository():
    """Get snapshot repository details."""
    svc = _get_service()
    try:
        return await svc.get_snapshot_repository()
    except ArchiveServiceError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post(
    "/repository/verify",
    response_model=Dict[str, Any],
    summary="Verify snapshot repository",
    description="Verify that the snapshot repository is functional and accessible.",
)
async def verify_repository():
    """Verify snapshot repository works correctly."""
    svc = _get_service()
    try:
        return await svc.verify_snapshot_repository()
    except ArchiveServiceError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get(
    "/snapshots",
    response_model=SnapshotListResponse,
    summary="List all snapshots",
    description="List all snapshots in the archive repository, sorted by creation time.",
)
async def list_snapshots():
    """List all snapshots."""
    activity_logger.log_activity(
        action="audit.view",
        target_type="archive",
        target_id="snapshots",
        result="success",
        details={"view": "snapshot_list"},
    )
    svc = _get_service()
    try:
        snapshots = await svc.list_snapshots()
        return SnapshotListResponse(total=len(snapshots), snapshots=snapshots)
    except ArchiveServiceError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get(
    "/snapshots/{snapshot_name}",
    response_model=Dict[str, Any],
    summary="Get snapshot details",
    description="Get detailed information about a specific snapshot.",
)
async def get_snapshot(snapshot_name: str):
    """Get a specific snapshot's details."""
    svc = _get_service()
    try:
        return await svc.get_snapshot(snapshot_name)
    except ArchiveServiceError as e:
        raise HTTPException(status_code=404 if "not found" in str(e).lower() else 502, detail=str(e))


@router.post(
    "/snapshots",
    response_model=MessageResponse,
    summary="Create manual snapshot",
    description="Trigger a manual snapshot of indices to the archive repository.",
)
async def create_snapshot(req: CreateSnapshotRequest, _: None = Depends(check_read_only_mode)):
    """Create a new snapshot manually."""
    svc = _get_service()
    try:
        result = await svc.create_snapshot(
            snapshot_name=req.name,
            indices=req.indices,
            wait_for_completion=req.wait_for_completion,
        )
        accepted = result.get("accepted", result.get("snapshot", {}).get("state") == "SUCCESS")
        return MessageResponse(
            message=f"Snapshot '{req.name}' {'completed' if req.wait_for_completion and accepted else 'initiated'}",
            details=result,
        )
    except ArchiveServiceError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.delete(
    "/snapshots/{snapshot_name}",
    response_model=MessageResponse,
    summary="Delete snapshot",
    description="Delete a snapshot from the archive repository. This frees up storage but the data will no longer be restorable from this snapshot.",
)
async def delete_snapshot(snapshot_name: str, _: None = Depends(check_read_only_mode)):
    """Delete a snapshot."""
    svc = _get_service()
    try:
        result = await svc.delete_snapshot(snapshot_name)
        return MessageResponse(
            message=f"Snapshot '{snapshot_name}' deleted",
            details=result,
        )
    except ArchiveServiceError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post(
    "/restore",
    response_model=MessageResponse,
    summary="Restore from snapshot",
    description="Restore indices from a snapshot. Optionally rename restored indices to avoid conflicts with existing ones.",
)
async def restore_snapshot(req: RestoreSnapshotRequest, _: None = Depends(check_read_only_mode)):
    """Restore indices from a snapshot."""
    svc = _get_service()
    try:
        result = await svc.restore_snapshot(
            snapshot_name=req.snapshot_name,
            indices=req.indices,
            rename_pattern=req.rename_pattern,
            rename_replacement=req.rename_replacement,
        )
        return MessageResponse(
            message=f"Restore from '{req.snapshot_name}' initiated",
            details=result,
        )
    except ArchiveServiceError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get(
    "/snapshots-in-progress",
    response_model=Dict[str, Any],
    summary="Snapshot status (in-progress)",
    description="Get status of any currently running snapshot operations.",
)
async def get_snapshot_in_progress():
    """Get status of currently running snapshots."""
    svc = _get_service()
    try:
        return await svc.get_snapshot_status()
    except ArchiveServiceError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get(
    "/indices",
    response_model=List[Dict[str, Any]],
    summary="List log indices",
    description="List all orcastra and vault-audit indices with health, doc count, and size.",
)
async def list_indices():
    """List all log indices."""
    activity_logger.log_activity(
        action="audit.view",
        target_type="archive",
        target_id="indices",
        result="success",
        details={"view": "index_list"},
    )
    svc = _get_service()
    try:
        return await svc.list_archive_indices()
    except ArchiveServiceError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get(
    "/indices/{index_name}/ism",
    response_model=Dict[str, Any],
    summary="Index ISM status",
    description="Get ISM policy execution status for a specific index (current state, policy, etc).",
)
async def get_index_ism(index_name: str):
    """Get ISM status for a specific index."""
    svc = _get_service()
    try:
        return await svc.get_index_ism_status(index_name)
    except ArchiveServiceError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get(
    "/ism/status",
    response_model=Dict[str, Any],
    summary="All indices ISM status",
    description="Get ISM lifecycle status for all orcastra and vault-audit indices.",
)
async def get_all_ism_status(
    pattern: str = Query(
        default="orcastra-*,vault-audit-*",
        description="Index pattern to check ISM status for",
    )
):
    """Get ISM status for all matching indices."""
    svc = _get_service()
    try:
        return await svc.list_indices_ism_status(pattern)
    except ArchiveServiceError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get(
    "/ism/policies",
    response_model=List[Dict[str, Any]],
    summary="List ISM policies",
    description="List all Index State Management policies configured in OpenSearch.",
)
async def list_ism_policies():
    """List all ISM policies."""
    svc = _get_service()
    try:
        return await svc.list_ism_policies()
    except ArchiveServiceError as e:
        raise HTTPException(status_code=502, detail=str(e))
