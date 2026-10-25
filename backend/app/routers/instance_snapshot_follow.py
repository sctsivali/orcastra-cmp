"""Reads that let the console follow a snapshot action: a restore's plan and an operation's end.

Both sit beside `instance_snapshots.py` under the same tenant gate and the same per-instance
`_enforce_snapshot_access`, and neither writes anything.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.core.error_sanitizer import sanitize_error
from app.core.lxd_errors import lxd_http_error
from app.core.lxd_names import LXD_NAME_DESCRIPTION, LXD_NAME_PATTERN
from app.core.lxd_path import safe_segment
from app.core.org_scope import org_scope_dependency
from app.core.dependencies import AuthenticatedUser, require_tenant
from app.models.schemas import SnapshotOperationStatusResponse, SnapshotRestorePlanResponse
from app.routers.instance_snapshots import _blocking, _client, _enforce_snapshot_access, _validated_target
from app.services.lxd import snapshots as snapshots_service
from app.services.lxd_service import LXDService, get_lxd_service
from app.services.snapshots import operation_initiator, restore_plan

router = APIRouter(
    prefix="/instances",
    tags=["Instances - Snapshots"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

_OPERATION_ID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_NOT_FOUND = "Operation not found"


@router.get(
    "/{instance_name}/snapshots/{snapshot_name}/restore-plan",
    response_model=SnapshotRestorePlanResponse,
)
async def read_restore_plan(
    instance_name: Annotated[
        str,
        Path(pattern=LXD_NAME_PATTERN, description=f"Instance to act on. {LXD_NAME_DESCRIPTION}"),
    ],
    snapshot_name: str,
    host: str = Query(..., description="Cluster the instance lives on"),
    project: str = Query("default", description="LXD project the instance lives in"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    project = _enforce_snapshot_access(host, project, instance_name)
    snapshot_name = _validated_target(snapshot_name)
    client = await _client(lxd_service, host)
    try:
        existing = await _blocking(
            snapshots_service.list_instance_snapshots, client, instance_name, project,
            label="snapshots.list",
        )
        driver = await _blocking(
            restore_plan.read_root_pool_driver, client, instance_name, project,
            label="snapshots.pool",
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - mapped to a meaningful status below
        raise lxd_http_error(exc, "Instance", instance_name)
    if not any(row.get("name") == snapshot_name for row in existing):
        raise HTTPException(
            status_code=404, detail=f"Snapshot '{snapshot_name}' does not exist on {instance_name}."
        )
    plan = restore_plan.plan_restore(driver, existing, snapshot_name)
    return SnapshotRestorePlanResponse(
        storage_driver=plan.storage_driver,
        restorable=plan.restorable,
        safety_snapshot_supported=plan.safety_snapshot_supported,
        newer_snapshots=plan.newer_snapshots,
        reason=plan.reason,
    )


@router.get(
    "/{instance_name}/snapshot-operations/{operation_id}",
    response_model=SnapshotOperationStatusResponse,
)
async def read_snapshot_operation(
    instance_name: Annotated[
        str,
        Path(pattern=LXD_NAME_PATTERN, description=f"Instance to act on. {LXD_NAME_DESCRIPTION}"),
    ],
    operation_id: Annotated[str, Path(pattern=_OPERATION_ID, description="LXD operation id")],
    host: str = Query(..., description="Cluster the instance lives on"),
    project: str = Query("default", description="LXD project the instance lives in"),
    lxd_service: LXDService = Depends(get_lxd_service),
    user: AuthenticatedUser = Depends(require_tenant()),
):
    """The status of a snapshot operation, for the caller who started it and nobody else.

    important: the initiator record is the whole grant. It must name this caller, this cluster,
    this project and this instance; anything else, a missing record included, is the same 404 so
    an operation id is never an oracle for whose it is.
    """
    project = _enforce_snapshot_access(host, project, instance_name)
    initiator = operation_initiator.recall(host, operation_id)
    actor = operation_initiator.actor_of(user)
    if (
        initiator is None
        or initiator.actor != actor
        or initiator.host != host
        or initiator.project != project
        or initiator.instance != instance_name
    ):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)

    client = await _client(lxd_service, host)
    try:
        response = await _blocking(
            lambda: client.api.operations[safe_segment(operation_id)].get(
                params={"project": project}, timeout=(3, 15)
            ),
            label="snapshots.operation",
        )
    except Exception as exc:  # noqa: BLE001 - a reaped operation is a 404, anything else a 502
        if getattr(getattr(exc, "response", None), "status_code", None) == 404:
            raise HTTPException(status_code=404, detail=_NOT_FOUND) from exc
        raise HTTPException(status_code=502, detail="The cluster did not answer for this operation.") from exc
    metadata = response.json().get("metadata") or {}
    raw_code = metadata.get("status_code")
    return SnapshotOperationStatusResponse(
        status=metadata.get("status"),
        status_code=raw_code if isinstance(raw_code, int) else None,
        # important: a 200 never reaches the 5xx sanitizer, so the daemon's text is redacted here,
        # as /operations/{id} does (lxd_operations.py).
        err=sanitize_error(metadata.get("err"), limit=500) if metadata.get("err") else None,
    )
