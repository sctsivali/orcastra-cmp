"""Instance rebuild endpoints (dashboard, JWT).

Rebuild reinstalls an instance's root disk from a fresh source (image, empty, or
image + cloud-init) while LXD preserves the instance's identity, config and
attached custom volumes -- it is NOT delete-then-create. Backed by LXD
`POST /1.0/instances/{name}/rebuild` (the `instances_rebuild` API extension).

Because a rebuild is a multi-step, potentially slow orchestration (stop ->
rebuild -> start -> wait for provisioning), it runs as a detached background task
and its progress is recorded in the durable ``rebuild_jobs`` table, which the UI
polls. The one thing enforced synchronously is the snapshot guard: LXD refuses to
rebuild an instance that has snapshots, so we return a clear 409 up front.

Kept on the dashboard's ``/instances`` surface (Partner + org-scope). OrcaHub's
``/vms`` is untouched.
"""
import asyncio
import secrets
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import get_cache_manager
from app.core.database import get_db, async_session_maker
from app.core.dependencies import (
    check_read_only_mode,
    require_tenant,
    require_partner,
    AuthenticatedUser,
)
from app.core.org_scope import org_scope_dependency, verify_cluster_access
from app.models.db_models import RebuildJob
from app.services.lxd_service import (
    LXDService,
    LXDOperationError,
    LXDConnectionError,
    get_lxd_service,
)
from app.routers.instances import _validate_image_server
from app.services.activity_logger import get_activity_logger
import logging

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/instances",
    tags=["Instances - Rebuild"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# Best-effort wait for the guest to come up (get an IP / agent) after an
# image/cloud-init rebuild before the job is marked ready. Informational only:
# timing out does not fail the rebuild, which already succeeded.
_PROVISION_TIMEOUT_SECONDS = 180
_PROVISION_POLL_SECONDS = 5


class RebuildRequest(BaseModel):
    host: str
    project: str = "default"
    # "image": reinstall from a base image. "empty": blank root disk (install from
    # an ISO afterwards via the console). "cloud-init": image + apply cloud-init.
    mode: Literal["image", "empty", "cloud-init"]
    image: Optional[str] = None            # alias or 64-char fingerprint (image/cloud-init)
    image_server: Optional[str] = None     # remote simplestreams server (optional)
    image_protocol: Optional[str] = None
    cloud_init: Optional[str] = Field(None, description="cloud-init user-data (cloud-init mode)")
    # Destructive-action gate: the client must echo the instance name.
    confirm_name: str
    # Options
    run_provisioning: bool = True          # wait for cloud-init/agent after start


def _build_source(body: RebuildRequest) -> Dict[str, Any]:
    """Build the LXD rebuild `source`, mirroring instance-create's source shape."""
    if body.mode == "empty":
        return {"type": "none"}
    source: Dict[str, Any] = {"type": "image"}
    image = (body.image or "").strip()
    if body.image_server:
        source["mode"] = "pull"
        source["server"] = body.image_server
        source["protocol"] = body.image_protocol or "simplestreams"
        source["alias"] = image
    elif len(image) == 64:
        source["fingerprint"] = image
    else:
        source["alias"] = image
    return source


def _job_payload(row: RebuildJob) -> Dict[str, Any]:
    return {
        "job_id": row.job_id,
        "instance_name": row.instance_name,
        "host": row.cluster_id,
        "project": row.project,
        "mode": row.mode,
        "requested_image": row.requested_image,
        "old_image": row.old_image,
        "status": row.status,
        "operation_id": row.operation_id,
        "error": row.error,
        "attached_volumes": list((row.attached_volumes or {}).keys()),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _set_status(job_id: str, status: Optional[str] = None, **fields: Any) -> None:
    async with async_session_maker() as db:
        row = (
            await db.execute(select(RebuildJob).where(RebuildJob.job_id == job_id))
        ).scalar_one_or_none()
        if row is None:
            return
        if status is not None:
            row.status = status
        for k, v in fields.items():
            setattr(row, k, v)
        await db.commit()

    # A rebuild walks the instance through stopping, rebuilding, starting and ready, and the
    # instance list carries that status. Invalidating on every transition rather than only at
    # the end keeps the list honest while the job runs, and covers the failure path for free.
    if status is not None:
        get_cache_manager().invalidate_instance_cache()


async def _wait_for_provisioning(lxd_service: LXDService, name: str, host: str, project: str) -> None:
    """Best-effort: poll until the instance is Running with an IP (a sign the OS
    booted / cloud-init is progressing), or the timeout elapses. Never raises."""
    waited = 0
    while waited < _PROVISION_TIMEOUT_SECONDS:
        try:
            vm = await lxd_service.get_vm(name, host)
            if vm and vm.status == "Running" and (vm.ip_addresses or []):
                return
        except Exception:  # noqa: BLE001 - provisioning wait is best-effort
            pass
        await asyncio.sleep(_PROVISION_POLL_SECONDS)
        waited += _PROVISION_POLL_SECONDS


async def _run_rebuild(job_id: str, run_provisioning: bool) -> None:
    """Detached orchestration: stop -> rebuild -> (start -> provision) -> ready.

    For 'empty' mode the instance is left STOPPED after the rebuild so the user can
    attach an install ISO and boot it from the console; image/cloud-init modes are
    started and (optionally) waited on until provisioned.
    """
    lxd_service = get_lxd_service()
    async with async_session_maker() as db:
        row = (
            await db.execute(select(RebuildJob).where(RebuildJob.job_id == job_id))
        ).scalar_one_or_none()
        if row is None:
            return
        name, host, project = row.instance_name, row.cluster_id, row.project
        mode, source, cloud_init = row.mode, row.source, row.cloud_init

    try:
        # 1. Stop (rebuild requires the instance stopped). Force-stop covers a
        #    running or frozen guest.
        await _set_status(job_id, "stopping")
        try:
            await lxd_service.stop_vm(name, host, force=True, project=project)
        except LXDOperationError as e:
            # A not-running instance is fine; anything else is a real failure.
            if "not found" in str(e).lower():
                raise

        # 2. Rebuild the root disk.
        await _set_status(job_id, "rebuilding")
        op_id = await lxd_service.rebuild_instance(
            name, host, source, project=project, cloud_init=cloud_init
        )
        await _set_status(job_id, operation_id=op_id or None)

        if mode == "empty":
            # Leave it stopped: the user attaches an ISO + starts it from the console.
            await _set_status(job_id, "ready")
        else:
            # 3. Start.
            await _set_status(job_id, "starting")
            await lxd_service.start_vm(name, host, project=project)
            # 4. Optionally wait for provisioning (best-effort, informational).
            if run_provisioning:
                await _set_status(job_id, "provisioning")
                await _wait_for_provisioning(lxd_service, name, host, project)
            await _set_status(job_id, "ready")

        activity_logger.log_activity(
            action="instance.rebuild", target_type="instance", target_id=name,
            target_host=host, result="success",
            details={"mode": mode, "job_id": job_id},
        )
        logger.info("Rebuild job %s completed: instance=%s mode=%s", job_id, name, mode)
    except Exception as e:  # noqa: BLE001 - record the failure for the operator
        await _set_status(job_id, "failed", error=str(e)[:1000])
        activity_logger.log_activity(
            action="instance.rebuild", target_type="instance", target_id=name,
            target_host=host, result="failure", error_message=str(e),
            details={"mode": mode, "job_id": job_id},
        )
        logger.error("Rebuild job %s failed: instance=%s error=%s", job_id, name, e)


@router.post("/{instance_name}/rebuild")
async def rebuild_instance(
    instance_name: str,
    body: RebuildRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
):
    verify_cluster_access(body.host)

    # Destructive-action confirmation: the client must echo the instance name.
    if body.confirm_name != instance_name:
        raise HTTPException(status_code=422, detail="Confirmation name does not match the instance name")
    if body.mode in ("image", "cloud-init") and not (body.image or "").strip():
        raise HTTPException(status_code=422, detail="An image is required for this rebuild mode")
    if body.mode == "cloud-init" and not (body.cloud_init or "").strip():
        raise HTTPException(status_code=422, detail="cloud-init user-data is required for the cloud-init mode")
    _validate_image_server(body.image_server)

    # Preflight: status + snapshots + config/volume snapshot. Snapshots block the
    # rebuild (LXD rejects it), so surface a clear 409 before doing anything.
    try:
        pf = await lxd_service.get_rebuild_preflight(instance_name, body.host, body.project)
    except LXDConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))

    if pf.get("snapshots"):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Rebuild is blocked while the instance has snapshots. Delete them first.",
                "snapshots": pf["snapshots"],
            },
        )

    source = _build_source(body)
    job_id = secrets.token_hex(16)
    row = RebuildJob(
        job_id=job_id,
        cluster_id=body.host,
        project=pf.get("project") or body.project or "default",
        instance_name=instance_name,
        mode=body.mode,
        requested_image=(body.image if body.mode != "empty" else None),
        old_image=pf.get("base_image"),
        source=source,
        previous_config=pf.get("config"),
        attached_volumes=pf.get("attached_volumes") or {},
        cloud_init=(body.cloud_init if body.mode == "cloud-init" else None),
        requester=(user.email or user.user_id),
        status="pending",
    )
    db = async_session_maker()
    async with db as session:
        session.add(row)
        await session.commit()

    asyncio.create_task(_run_rebuild(job_id, body.run_provisioning))
    # Readers hold a cached list; without this the change is invisible until the TTL.
    get_cache_manager().invalidate_instance_cache()
    return _job_payload(row)


@router.get("/{instance_name}/rebuild-preflight")
async def rebuild_preflight(
    instance_name: str,
    host: str,
    project: str = "default",
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Read-only pre-check for the rebuild dialog: current status, snapshots (the
    block condition), the custom volumes that will survive, and the base image."""
    verify_cluster_access(host)
    try:
        pf = await lxd_service.get_rebuild_preflight(instance_name, host, project)
    except LXDConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "instance_name": instance_name,
        "host": host,
        "status": pf.get("status"),
        "type": pf.get("type"),
        "snapshots": pf.get("snapshots") or [],
        "attached_volumes": list((pf.get("attached_volumes") or {}).keys()),
        "base_image": pf.get("base_image"),
    }


@router.get("/{instance_name}/rebuild/{job_id}")
async def get_rebuild_job(
    instance_name: str,
    job_id: str,
    db: AsyncSession = Depends(get_db),
):
    row = (
        await db.execute(select(RebuildJob).where(RebuildJob.job_id == job_id))
    ).scalar_one_or_none()
    if row is None or row.instance_name != instance_name:
        raise HTTPException(status_code=404, detail="Rebuild job not found")
    verify_cluster_access(row.cluster_id)
    return _job_payload(row)


@router.get("/{instance_name}/rebuild")
async def get_latest_rebuild_job(
    instance_name: str,
    host: str,
    db: AsyncSession = Depends(get_db),
):
    verify_cluster_access(host)
    row = (
        await db.execute(
            select(RebuildJob)
            .where(RebuildJob.instance_name == instance_name, RebuildJob.cluster_id == host)
            .order_by(RebuildJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return {"job": None}
    return {"job": _job_payload(row)}
