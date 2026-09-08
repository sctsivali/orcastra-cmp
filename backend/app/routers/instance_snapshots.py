"""Instance snapshot endpoints (dashboard, JWT).

Operator rollback points, not the tenant snapshot product. OrcaHub sells snapshots as a metered
per-GB service and drives them through `/integrations/raw-proxy`; this surface exists for the
different job of an operator taking a restore point before risky maintenance, which previously
meant leaving the console and running `lxc snapshot` over SSH.

Three rules follow from that split and are load-bearing:

* Everything created here is named `ops-...` and carries an LXD `expires_at`, so LXD prunes it
  whether or not this service is running, and so the snapshot is identifiable as operator work
  by anyone reading the cluster.
* `snapshots.schedule` and `snapshots.expiry` are read and never written. They are OrcaHub's
  retention mechanism for a schedule a tenant pays for, and a write here would leave its ledger
  disagreeing with the cluster.
* A source carrying an external schedule is badged and the caller warned, but not blocked.
  Refusing does not prevent the snapshot, it moves it back to an SSH session where neither
  product can see it.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.dependencies import (
    AuthenticatedUser,
    check_read_only_mode,
    require_tenant,
)
from app.core.lxd_errors import lxd_http_error
from app.core.org_scope import (
    org_scope_dependency,
    verify_cluster_access,
    verify_tenant_permission,
)
from app.core.tenant_visibility import tenant_acl_active
from app.core.blocking import run_blocking
from app.services.activity_logger import get_activity_logger
from app.services.lxd import snapshots as snapshots_service
from app.services.lxd.availability import require_client
from app.services.lxd.lazy_client import client_for
from app.services.lxd.proxy import PROXY_MAX_IN_FLIGHT, proxy_executor
from app.services.lxd_service import LXDService, get_lxd_service
from app.services.orcahub_webhook import schedule_orcahub_event

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/instances",
    tags=["Instances - Snapshots"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

_LXD_CALL_TIMEOUT = 30


def _blocking(fn, *args, label: str, **kwargs):
    """Every LXD call here goes off the event loop on the shared proxy pool.

    important: the pooled client's own socket timeout is what actually frees the worker
    thread. run_blocking only frees the caller, so a call without a bounded socket read
    would leak the thread even though the request returned promptly.
    """
    return run_blocking(
        fn,
        *args,
        timeout=_LXD_CALL_TIMEOUT,
        label=label,
        executor=proxy_executor,
        max_in_flight=PROXY_MAX_IN_FLIGHT,
        **kwargs,
    )


class SnapshotInfo(BaseModel):
    name: str = Field(..., description="Snapshot name as LXD reports it, without the source prefix")
    source_name: str = Field(..., description="Instance the snapshot belongs to")
    created_at: Optional[str] = Field(None, description="RFC3339 creation time")
    expires_at: Optional[str] = Field(None, description="RFC3339 expiry, null when the snapshot never expires")
    stateful: Optional[bool] = Field(None, description="Whether guest memory was captured, which needs CRIU on the host")
    size: Optional[int] = Field(None, description="Size in bytes where the storage driver reports it")
    architecture: Optional[str] = Field(None, description="Instance architecture at capture time")
    operator_managed: bool = Field(False, description="True when Orcastra created it, identified by the ops- prefix")


class SnapshotListResponse(BaseModel):
    total: int = Field(..., description="Number of snapshots returned")
    items: List[SnapshotInfo] = Field(default_factory=list, description="The snapshots")
    policy: Dict[str, Any] = Field(default_factory=dict, description="The source's LXD-native schedule, read only")


class SnapshotCreateRequest(BaseModel):
    host: str = Field(..., description="Cluster the instance lives on")
    project: str = Field("default", description="LXD project the instance lives in")
    label: str = Field(..., min_length=1, max_length=48, description="Short human label, folded into the generated ops- name")
    expiry_days: int = Field(
        snapshots_service.DEFAULT_EXPIRY_DAYS,
        ge=1,
        le=snapshots_service.MAX_EXPIRY_DAYS,
        description="Days until LXD prunes the snapshot",
    )
    stateful: bool = Field(False, description="Capture guest memory. Requires CRIU on the host and usually fails without it")
    reason: Optional[str] = Field(
        None,
        max_length=280,
        description="Why the snapshot is being taken. Required when the source carries an external snapshot schedule",
    )


class SnapshotActionResponse(BaseModel):
    success: bool = Field(..., description="Whether the action was accepted")
    instance_name: str = Field(..., description="The source instance")
    snapshot: str = Field(..., description="The snapshot acted on")
    operation_id: Optional[str] = Field(None, description="LXD operation id when the action is asynchronous")
    message: str = Field(..., description="Operator-facing summary")


class SnapshotRestoreRequest(BaseModel):
    host: str = Field(..., description="Cluster the instance lives on")
    project: str = Field("default", description="LXD project the instance lives in")
    confirm_name: str = Field(..., description="Must equal the instance name, echoed back by the caller")
    safety_snapshot: bool = Field(True, description="Take an automatic ops-pre- snapshot before restoring")


def _enforce_snapshot_access(
    host: str,
    project: str,
    instance_name: Optional[str],
    *,
    need_operate: bool = False,
) -> None:
    """Authorize a snapshot action for the current caller.

    A no-op for Admin and Partner, who carry no tenant ACL. For a tenant the `snapshot`
    permission is honoured only when its own flag is on: the permission has been granted by
    OrcaHub's sync for a long time while nothing enforced it, so a large number of live grants
    would otherwise have activated the moment this shipped.

    important: called before the try in every handler. Raised inside one, the 403 is swallowed
    by the generic except and re-raised as a 500, which reads as a broken server rather than a
    refusal.
    """
    if not tenant_acl_active():
        return
    if not get_settings().snapshot_tenant_enforcement_enabled:
        raise HTTPException(
            status_code=403,
            detail="Snapshot actions are not enabled for tenant accounts on this deployment.",
        )
    verify_tenant_permission(host, project, instance_name, "snapshot")
    if need_operate:
        # Restore stops the instance and discards everything written since the snapshot. That is
        # a lifecycle action, so it needs the lifecycle permission on top of the snapshot one.
        verify_tenant_permission(host, project, instance_name, "operate")


async def _settle(client, operation_id: Optional[str], project: str) -> Dict[str, Any]:
    """Wait briefly for an LXD operation so the response describes reality.

    A snapshot on a copy-on-write pool settles in well under a second. On a `dir` pool it is a
    full copy and may not settle at all inside this window, which is reported as unsettled
    rather than as either success or failure.
    """
    if not operation_id:
        return {"status": None, "err": None, "settled": True}
    try:
        return await _blocking(
            snapshots_service.wait_for_operation,
            client,
            operation_id,
            project,
            label="snapshots.wait",
        )
    except Exception as exc:  # noqa: BLE001 - a failed wait is not a failed operation
        logger.debug("Could not wait on operation %s: %s", operation_id, exc)
        return {"status": None, "err": None, "settled": False}


async def _client(lxd_service: LXDService, host: str):
    return require_client(await client_for(lxd_service, host, label="snapshots"), host)


def _audit(action: str, instance_name: str, host: str, project: str, **kwargs: Any) -> None:
    activity_logger.log_activity(
        action=action,
        target_type="instance_snapshot",
        target_id=instance_name,
        target_host=host,
        target_project=project,
        **kwargs,
    )


@router.get("/{instance_name}/snapshots", response_model=SnapshotListResponse)
async def list_snapshots(
    instance_name: str,
    host: str = Query(..., description="Cluster the instance lives on"),
    project: str = Query("default", description="LXD project the instance lives in"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    verify_cluster_access(host)
    _enforce_snapshot_access(host, project, instance_name)

    client = await _client(lxd_service, host)
    try:
        items = await _blocking(
            snapshots_service.list_instance_snapshots,
            client,
            instance_name,
            project,
            label="snapshots.list",
        )
        policy = await _blocking(
            snapshots_service.read_instance_policy,
            client,
            instance_name,
            project,
            label="snapshots.policy",
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - mapped to a meaningful status below
        raise lxd_http_error(exc, "Instance", instance_name)

    return SnapshotListResponse(total=len(items), items=items, policy=policy)


@router.post("/{instance_name}/snapshots", response_model=SnapshotActionResponse, status_code=201)
async def create_snapshot(
    instance_name: str,
    body: SnapshotCreateRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_tenant()),
):
    verify_cluster_access(body.host)
    _enforce_snapshot_access(body.host, body.project, instance_name)

    client = await _client(lxd_service, body.host)
    try:
        policy = await _blocking(
            snapshots_service.read_instance_policy,
            client,
            instance_name,
            body.project,
            label="snapshots.policy",
        )
        # A source under an external schedule is another product's territory. The snapshot is
        # allowed, because blocking it only pushes the operator back to SSH, but it must carry a
        # reason so the audit trail explains why someone reached into a managed instance.
        if policy.get("externally_managed") and not (body.reason or "").strip():
            raise HTTPException(
                status_code=422,
                detail=(
                    "This instance has an externally managed snapshot schedule. "
                    "A reason is required to take an operator snapshot here."
                ),
            )

        await _blocking(
            snapshots_service.assert_capacity,
            client,
            instance_name,
            body.project,
            label="snapshots.capacity",
        )
        snapshot_name = snapshots_service.build_operator_snapshot_name(body.label)
        expires_at = snapshots_service.default_expiry(body.expiry_days)

        operation_id = await _blocking(
            snapshots_service.create_instance_snapshot,
            client,
            instance_name,
            body.project,
            snapshot_name,
            stateful=body.stateful,
            expires_at=expires_at,
            label="snapshots.create",
        )
        outcome = await _settle(client, operation_id, body.project)
    except HTTPException:
        raise
    except snapshots_service.SnapshotLimitError as exc:
        _audit(
            "instance.snapshot.create", instance_name, body.host, body.project,
            result="denied", error_message=str(exc),
        )
        raise HTTPException(status_code=409, detail=str(exc))
    except snapshots_service.SnapshotNameError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        _audit(
            "instance.snapshot.create", instance_name, body.host, body.project,
            result="failure", error_message=str(exc),
        )
        raise lxd_http_error(exc, "Instance", instance_name)

    if outcome.get("status") == "Failure":
        # The daemon refused after accepting the request. Reporting 201 here would be the exact
        # defect this feature exists to avoid: a control that says it worked and did not.
        _audit(
            "instance.snapshot.create", instance_name, body.host, body.project,
            result="failure", error_message=outcome.get("err"),
            details={"snapshot": snapshot_name},
        )
        raise HTTPException(
            status_code=502,
            detail=f"The cluster refused the snapshot: {outcome.get('err') or 'unknown error'}",
        )

    _audit(
        "instance.snapshot.create", instance_name, body.host, body.project,
        result="success" if outcome.get("settled") else "accepted",
        details={
            "snapshot": snapshot_name,
            "expires_at": expires_at,
            "stateful": body.stateful,
            "externally_managed_source": bool(policy.get("externally_managed")),
            "reason": (body.reason or "").strip() or None,
            "requester": user.email or user.user_id,
            "settled": outcome.get("settled"),
        },
    )
    # OrcaHub's receiver already understands SNAPSHOT events; emitting means a snapshot taken
    # here is known to it immediately rather than being discovered an hour later by its sweep.
    schedule_orcahub_event(
        event_type="snapshot.created",
        cluster_id=body.host,
        project=body.project,
        resource_type="SNAPSHOT",
        name=snapshot_name,
    )
    return SnapshotActionResponse(
        success=True,
        instance_name=instance_name,
        snapshot=snapshot_name,
        operation_id=operation_id,
        message=(
            f"Snapshot {snapshot_name} created, expiring {expires_at}."
            if outcome.get("settled")
            else f"Snapshot {snapshot_name} is still being written. It will expire {expires_at}."
        ),
    )


@router.delete("/{instance_name}/snapshots/{snapshot_name}", response_model=SnapshotActionResponse)
async def delete_snapshot(
    instance_name: str,
    snapshot_name: str,
    host: str = Query(..., description="Cluster the instance lives on"),
    project: str = Query("default", description="LXD project the instance lives in"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_tenant()),
):
    verify_cluster_access(host)
    _enforce_snapshot_access(host, project, instance_name)

    # This surface only manages what it made. A snapshot belonging to the paid product is
    # visible here and deliberately not deletable, so an operator cannot silently remove a
    # restore point a tenant is paying to keep.
    if not snapshots_service.is_operator_snapshot(snapshot_name):
        raise HTTPException(
            status_code=403,
            detail=(
                f"'{snapshot_name}' is not an operator snapshot. Only "
                f"'{snapshots_service.OPS_PREFIX}' snapshots can be deleted from this console."
            ),
        )

    client = await _client(lxd_service, host)
    try:
        operation_id = await _blocking(
            snapshots_service.delete_instance_snapshot,
            client,
            instance_name,
            project,
            snapshot_name,
            label="snapshots.delete",
        )
        await _settle(client, operation_id, project)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _audit(
            "instance.snapshot.delete", instance_name, host, project,
            result="failure", error_message=str(exc), details={"snapshot": snapshot_name},
        )
        raise lxd_http_error(exc, "Snapshot", snapshot_name)

    _audit(
        "instance.snapshot.delete", instance_name, host, project,
        result="accepted",
        details={"snapshot": snapshot_name, "requester": user.email or user.user_id},
    )
    schedule_orcahub_event(
        event_type="snapshot.deleted",
        cluster_id=host,
        project=project,
        resource_type="SNAPSHOT",
        name=snapshot_name,
    )
    return SnapshotActionResponse(
        success=True,
        instance_name=instance_name,
        snapshot=snapshot_name,
        operation_id=operation_id,
        message=f"Snapshot {snapshot_name} deleted.",
    )


@router.post(
    "/{instance_name}/snapshots/{snapshot_name}/restore",
    response_model=SnapshotActionResponse,
)
async def restore_snapshot(
    instance_name: str,
    snapshot_name: str,
    body: SnapshotRestoreRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_tenant()),
):
    verify_cluster_access(body.host)
    _enforce_snapshot_access(body.host, body.project, instance_name, need_operate=True)

    if body.confirm_name != instance_name:
        raise HTTPException(
            status_code=422, detail="Confirmation name does not match the instance name"
        )

    client = await _client(lxd_service, body.host)
    safety_name: Optional[str] = None
    try:
        # The safety copy is taken first and its failure aborts the restore. Taken afterwards it
        # would capture the already-rolled-back state and protect nothing.
        if body.safety_snapshot:
            safety_name = snapshots_service.build_safety_snapshot_name(instance_name)
            await _blocking(
                snapshots_service.create_instance_snapshot,
                client,
                instance_name,
                body.project,
                safety_name,
                stateful=False,
                expires_at=snapshots_service.default_expiry(),
                label="snapshots.safety",
            )

        operation_id = await _blocking(
            snapshots_service.restore_instance_snapshot,
            client,
            instance_name,
            body.project,
            snapshot_name,
            label="snapshots.restore",
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _audit(
            "instance.snapshot.restore", instance_name, body.host, body.project,
            result="failure",
            error_message=str(exc),
            details={"snapshot": snapshot_name, "safety_snapshot": safety_name},
        )
        raise lxd_http_error(exc, "Snapshot", snapshot_name)

    _audit(
        "instance.snapshot.restore", instance_name, body.host, body.project,
        result="accepted",
        details={
            "snapshot": snapshot_name,
            "safety_snapshot": safety_name,
            "requester": user.email or user.user_id,
        },
    )
    return SnapshotActionResponse(
        success=True,
        instance_name=instance_name,
        snapshot=snapshot_name,
        operation_id=operation_id,
        message=(
            f"Restoring {instance_name} to {snapshot_name}."
            + (f" Safety snapshot {safety_name} taken first." if safety_name else "")
        ),
    )
