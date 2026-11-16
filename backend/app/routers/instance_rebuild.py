"""Instance rebuild endpoints (dashboard, JWT) on ``/instances``, apart from OrcaHub's ``/vms``.

Rebuild reinstalls the root disk from an image, an empty disk or an image plus
cloud-init, while LXD keeps the instance's identity, config and custom volumes
(`POST /1.0/instances/{name}/rebuild`, API extension `instances_rebuild`).
Stop -> rebuild -> start -> provisioning wait runs as a detached task that records
progress in ``rebuild_jobs`` for the UI to poll. LXD refuses to rebuild an instance
with snapshots, so that check runs synchronously and answers 409 up front.
"""
import asyncio
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import get_cache_manager
from app.core.database import get_db, async_session_maker
from app.core.dependencies import (
    check_read_only_mode,
    require_tenant,
    AuthenticatedUser,
)
from app.core.config import get_settings
from app.core.instance_access import verify_rebuild_access
from app.core.org_scope import (
    check_tenant_permission,
    get_tenant_acl,
    org_scope_dependency,
    verify_project_access,
    verify_tenant_permission,
    visible_projects_for,
)
from app.models.db_models import RebuildJob
from app.services import rebuild_admission
from app.services.lxd_service import (
    LXDService,
    LXDOperationError,
    LXDConnectionError,
    get_lxd_service,
)
from app.routers.instances import _target_engine, _validate_image_server
from app.services.activity_logger import get_activity_logger
from app.services.lxd.exceptions import LXDRebuildOutcomeUnknown
from app.services.migration_fence import (
    MigrationFenceConflict,
    guard_instance_mutation,
)
import logging

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

def _verify_instance_access(
    host: str,
    project: Optional[str],
    instance_name: str,
    permission: str = "view",
) -> str:
    """Authorize an action on one instance rather than on its whole cluster.

    `verify_cluster_access` only says the caller may reach the cluster, which for a tenant scoped
    to one instance holds for every neighbour's instance there. The view check always runs;
    other permissions follow the tenant ACL enforcement flag.

    important: `project` is used as given. Normalising it here (`or "default"`) would authorize
    `default` while the service sees a falsy value and searches every project. The caller
    normalises once and passes the same value to the action.
    """
    scoped_project = verify_project_access(host, project)
    verify_tenant_permission(host, scoped_project, instance_name, "view")
    if permission != "view" and get_settings().tenant_perm_enforcement_enabled:
        verify_tenant_permission(host, scoped_project, instance_name, permission)
    return scoped_project


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
_HEARTBEAT_SECONDS = rebuild_admission.HEARTBEAT.total_seconds()
_STRANDED_SWEEP_SECONDS = 60
# A detached job the event loop holds only weakly could be collected mid-run; keep a reference.
_RUNNING_JOBS: "set[asyncio.Task]" = set()
_INTERRUPTED_BEFORE_REBUILD = (
    "The backend stopped before the root disk was rebuilt (a restart or a deploy). The instance may "
    "be stopped with its old disk; start it or rebuild again."
)
_INTERRUPTED_AFTER_REBUILD = (
    "The backend stopped after the root disk was rebuilt (a restart or a deploy). Check that the "
    "instance started."
)
_STRANDED_ERROR = (
    "The rebuild did not finish and no worker is still driving it, most likely a "
    "backend restart. Check the instance on the cluster before retrying: it may be "
    "stopped with a partially written root disk."
)


# 64 KiB, as the larger public clouds cap instance user-data. The value lands in the instance config
# and in this table, so an unbounded one would bloat both.
CLOUD_INIT_MAX_CHARS = 64 * 1024


class RebuildRequest(BaseModel):
    host: str = Field(..., min_length=1, max_length=255)
    project: str = Field("default", max_length=255)
    # "image": reinstall from a base image. "empty": blank root disk (install from
    # an ISO afterwards via the console). "cloud-init": image + apply cloud-init.
    mode: Literal["image", "empty", "cloud-init"]
    image: Optional[str] = Field(None, max_length=512)            # alias or 64-char fingerprint
    image_server: Optional[str] = Field(None, max_length=2048)    # remote simplestreams server
    image_protocol: Optional[Literal["simplestreams", "lxd"]] = None
    cloud_init: Optional[str] = Field(
        None, max_length=CLOUD_INIT_MAX_CHARS, description="cloud-init user-data (cloud-init mode)"
    )
    # Destructive-action gate: the client must echo the instance name.
    confirm_name: str = Field(..., max_length=255)
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


# A failed job's text is LXD's (storage paths, member names, whether an alias exists) or ours with
# LXD's appended. An operator needs it; a Tenant reading its own job gets this, and its provider
# reads the cause in the job row and the activity log.
TENANT_FAILURE_TEXT = (
    "The rebuild did not finish. Ask your provider to check the instance; the cause is in their activity log."
)


def _job_payload(row: RebuildJob) -> Dict[str, Any]:
    error = row.error
    if error and get_tenant_acl() is not None:
        error = TENANT_FAILURE_TEXT
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
        "error": error,
        "attached_volumes": list((row.attached_volumes or {}).keys()),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _set_status(job_id: str, status: Optional[str] = None, **fields: Any) -> bool:
    """Write to a job still in flight; False when the row is gone or already settled.

    important: the stranded sweep may have failed the row while this worker was cut off from the
    database. A settled row is never reopened, so it cannot read `ready` beside a stranded error.
    """
    values: Dict[str, Any] = dict(fields)
    if status is not None:
        values["status"] = status
    values["updated_at"] = datetime.now(timezone.utc)
    async with async_session_maker() as db:
        # One conditional UPDATE: a sweep that settles the row between a read and a write here
        # could otherwise be overwritten.
        result = await db.execute(
            update(RebuildJob)
            .where(RebuildJob.job_id == job_id, RebuildJob.status.in_(_IN_FLIGHT_STATUSES))
            .values(**values)
        )
        await db.commit()
    if not result.rowcount:
        return False

    # The instance list carries the rebuild status (stopping, rebuilding, starting, ready), so
    # every transition invalidates it; that also covers the failure path.
    if status is not None:
        get_cache_manager().invalidate_instance_cache()
    return True


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


async def _heartbeat(job_id: str) -> None:
    """Refresh the row while the job runs, so admission and the stranded sweep see a live driver."""
    missed = 0
    while True:
        await asyncio.sleep(_HEARTBEAT_SECONDS)
        try:
            async with async_session_maker() as db:
                await db.execute(
                    update(RebuildJob)
                    .where(RebuildJob.job_id == job_id, RebuildJob.status.in_(_IN_FLIGHT_STATUSES))
                    .values(updated_at=datetime.now(timezone.utc))
                )
                await db.commit()
            missed = 0
        except Exception as exc:  # noqa: BLE001 - a missed beat is caught up by the next one
            missed += 1
            # Two in a row is a minute of silence: the stranded sweep acts on five.
            log = logger.warning if missed >= 2 else logger.debug
            log("Rebuild heartbeat for %s missed %d time(s): %s", job_id, missed, type(exc).__name__)


class _SettledElsewhere(Exception):
    """The row was settled (the stranded sweep failed it) while this worker was cut off."""


async def _advance(job_id: str, status: str) -> None:
    # A job whose row another worker settled acts no further: the instance is no longer blocked,
    # and a second rebuild admitted since must not meet this one's start.
    if not await _set_status(job_id, status):
        raise _SettledElsewhere(status)


def _says_already_stopped(error: Exception) -> bool:
    # LXD and Incus answer a stop of an instance that is not running (Stopped, or Error after a
    # failed start) with "The instance is already stopped"; the rebuild accepts such an instance.
    return "already stopped" in str(error).lower()


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

    op_id: Optional[str] = None
    phase = "pending"
    beat = asyncio.create_task(_heartbeat(job_id))
    try:
        # Hold the same cluster advisory lock used by migration execution through the final
        # daemon mutation/readback. A migration that wins first fences this job before stop;
        # a rebuild that wins first makes migration wait and preflight the rebuilt instance.
        async with guard_instance_mutation(
            cluster_id=host,
            project=project,
            instance_name=name,
        ):
            # 1. Stop (rebuild requires the instance stopped). Force-stop covers a
            #    running or frozen guest.
            phase = "stopping"
            await _advance(job_id, phase)
            # stop_vm answers a Stopped instance without a request and reads the state after a
            # refused stop, so what it raises is a real failure (rebuilding a guest that is still
            # running would fail later with a less clear LXD error), except "already stopped".
            try:
                await lxd_service.stop_vm(name, host, force=True, project=project)
            except LXDOperationError as e:
                if not _says_already_stopped(e):
                    raise LXDOperationError(f"Could not stop the instance before the rebuild: {e}") from e
                logger.info("Rebuild job %s: %s reads already stopped; rebuilding it", job_id, name)

            # 2. Rebuild the root disk and wait for the accepted operation to settle.
            phase = "rebuilding"
            await _advance(job_id, phase)
            op_id = await lxd_service.rebuild_instance(
                name, host, source, project=project, cloud_init=cloud_init
            )
            await _set_status(job_id, operation_id=op_id or None)

            if mode != "empty":
                # 3. Start. Provisioning below is read-only, so it does not extend the
                #    cluster-wide mutation lock after start readback has completed.
                phase = "starting"
                await _advance(job_id, phase)
                await lxd_service.start_vm(name, host, project=project)

        if mode != "empty" and run_provisioning:
            phase = "provisioning"
            await _advance(job_id, phase)
            await _wait_for_provisioning(lxd_service, name, host, project)
        recorded = await _set_status(job_id, "ready")

        details = {"mode": mode, "job_id": job_id, "operation_id": op_id, "project": project}
        if not recorded:
            # The sweep failed the row while this worker could not reach the database; the
            # rebuild itself finished. The row keeps its failure, the audit says what happened.
            details["after_marked_stranded"] = True
            logger.warning("Rebuild job %s finished after it was marked stranded: instance=%s", job_id, name)
        activity_logger.log_activity(
            action="instance.rebuild", target_type="instance", target_id=name,
            target_host=host, target_project=project, result="success", details=details,
        )
        logger.info("Rebuild job %s completed: instance=%s mode=%s", job_id, name, mode)
    except LXDRebuildOutcomeUnknown as unknown:
        # important: LXD accepted the rebuild and may still be rewriting the disk. The row stays
        # `rebuilding` (with the reason) and keeps blocking a second rebuild and Migration Center
        # until LXD's wait is over; the stranded sweep settles it then.
        await _set_status(job_id, error=str(unknown)[:1000], operation_id=unknown.operation_id or None)
        logger.warning("Rebuild job %s: outcome unknown, the row blocks until the sweep: %s", job_id, unknown)
    except _SettledElsewhere as settled:
        logger.warning(
            "Rebuild job %s was settled elsewhere before %s; acting no further on %s", job_id, settled, name,
        )
    except asyncio.CancelledError:
        # important: the backend is stopping (a deploy, an upgrade, a restart). In `rebuilding` LXD
        # may still run the rebuild it accepted, so the row stays in flight and keeps blocking a
        # second rebuild and Migration Center until LXD's wait is over (the sweep fails it then).
        # In any other phase no daemon rebuild is running: fail the row now, so the instance is
        # free at once.
        if phase == "rebuilding":
            logger.warning(
                "Rebuild job %s interrupted by shutdown while LXD rebuilds %s; the row blocks until the sweep",
                job_id, name,
            )
            raise
        error = _INTERRUPTED_AFTER_REBUILD if phase in ("starting", "provisioning") else _INTERRUPTED_BEFORE_REBUILD
        try:
            await asyncio.wait_for(_set_status(job_id, "failed", error=error), timeout=10)
        except BaseException:  # noqa: BLE001 - shutting down; the stranded sweep is the fallback
            pass
        activity_logger.log_activity(
            action="instance.rebuild", target_type="instance", target_id=name,
            target_host=host, target_project=project, result="failure", error_message=error,
            details={"mode": mode, "job_id": job_id, "operation_id": op_id, "project": project,
                     "interrupted": True, "phase": phase},
        )
        logger.warning("Rebuild job %s interrupted by shutdown in %s: instance=%s", job_id, phase, name)
        raise
    except Exception as e:  # noqa: BLE001 - record the failure for the operator
        await _set_status(job_id, "failed", error=str(e)[:1000])
        activity_logger.log_activity(
            action="instance.rebuild", target_type="instance", target_id=name,
            target_host=host, target_project=project, result="failure", error_message=str(e),
            details={"mode": mode, "job_id": job_id, "operation_id": op_id, "project": project},
        )
        logger.error("Rebuild job %s failed: instance=%s error=%s", job_id, name, e)
    finally:
        beat.cancel()


async def _project_config(lxd_service: LXDService, host: str, project: str) -> Dict[str, Any]:
    from app.services.lxd.project_scope import list_project_configs

    def project_config() -> Dict[str, Any]:
        client = lxd_service.get_client_with_reconnect(host)
        if client is None:
            raise LXDConnectionError(f"Could not connect to {host}")
        return list_project_configs(client).get(project) or {}

    try:
        return await asyncio.wait_for(asyncio.to_thread(project_config), timeout=30)
    except (asyncio.TimeoutError, LXDConnectionError) as exc:
        raise HTTPException(
            status_code=503, detail="Target project configuration could not be verified. No change was sent."
        ) from exc


async def _refuse_a_rebuild_in_an_unisolated_project(
    lxd_service: LXDService,
    user: AuthenticatedUser,
    host: str,
    project: str,
    db: AsyncSession,
    *,
    tenant: bool = False,
) -> None:
    """409 for a rebuild on a shared cluster in a project that does not isolate itself.

    A rebuild takes its image from the project's store, which for a project without
    features.images is `default`'s, where another party's private images live. Instance create
    and config writes refuse it the same way.

    important: a Tenant is judged here too. On a cluster bound to more than one organization (the
    request's project bound is set, org_scope._clusters_with_several_orgs) its project must prove
    isolation, and `default`, which every holder shares, is refused outright. The write policy
    alone answers "no allowlist needed" for every Tenant, because Tenants may not write config at
    all, and that would wave its rebuild through. The party rule Partners get
    (services.cluster_parties) does not fit a Tenant: its own organization's rows carry role
    `tenant`, which that rule does not count as the caller's side, so every cluster would read
    as shared.
    """
    from app.services.instance_security_policy import (
        InstanceSecurityPolicyViolation,
        resolve_cluster_write_policy,
        shared_project_findings,
        validate_target_project_isolation,
    )

    if tenant:
        if visible_projects_for(host) is None:
            return
        if project == "default":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "shared_default_project",
                    "message": (
                        "Instances in the default project of a cluster shared with other organizations "
                        "cannot be rebuilt from here. Ask your provider."
                    ),
                    "violations": [],
                },
            )
        findings = shared_project_findings(await _project_config(lxd_service, host, project))
        if findings:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "shared_cluster_project_not_isolated",
                    "message": (
                        "This project is not isolated from the other organizations on this cluster, so it "
                        "cannot be rebuilt from here. Ask your provider."
                    ),
                    "violations": [{"code": f.code, "field": f.field} for f in findings],
                },
            )
        return

    try:
        policy = await resolve_cluster_write_policy(user, host, db)
    except InstanceSecurityPolicyViolation as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc
    except Exception as exc:  # noqa: BLE001 - ownership that cannot be proven refuses closed
        logger.error("Could not verify write policy for cluster %s: %s", host, type(exc).__name__)
        raise HTTPException(status_code=503, detail="Cluster ownership could not be verified. No change was sent.") from exc
    if not policy.requires_safe_allowlist:
        return
    try:
        validate_target_project_isolation(policy, await _project_config(lxd_service, host, project))
    except InstanceSecurityPolicyViolation as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc


_FINGERPRINT = re.compile(r"[0-9a-f]{64}")


async def _refuse_an_image_a_tenant_may_not_use(lxd_service: LXDService, body: "RebuildRequest") -> None:
    """403 unless a Tenant rebuilds from a public catalogue or an image its own instances use.

    A bare alias or fingerprint resolves in the project's store, which can hold the Partner's or,
    through `default`, another party's private images. The tenant image list shows only images a
    visible instance was built from (routers/images.py); the server holds the rebuild to the same set.
    """
    if body.mode == "empty" or body.image_server:
        # The catalogue pull is held to the image server allowlist (_validate_image_server).
        return
    from app.routers.images import _allowed_image_fingerprints_by_host

    fingerprint = (body.image or "").strip().lower()
    allowed = await _allowed_image_fingerprints_by_host(lxd_service) or {}
    if not _FINGERPRINT.fullmatch(fingerprint) or fingerprint not in {
        str(f).lower() for f in allowed.get(body.host, set())
    }:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "image_not_allowed",
                "message": (
                    "Choose an image from the list: a public catalogue image, or one an instance you "
                    "can see was built from."
                ),
            },
        )


def _tenant_projects(host: str, project: str) -> Optional[set]:
    """The projects whose rebuilds count against this Tenant's cap on `host`; None means every one.

    The projects its rules name, within its organization's project bound. A `*` rule covers the
    bound, or the whole cluster when there is none.
    """
    acl = get_tenant_acl() or {}
    named = {rule_project for (rule_project, _instance) in (acl.get(host) or {})}
    bound = visible_projects_for(host)
    if bound is not None:
        # Every holder shares `default` on a bound cluster, and a Tenant cannot rebuild there; its
        # rebuilds would only make another organization's count against this one.
        bound = set(bound) - {"default"}
    if "*" in named:
        return bound
    if bound is not None:
        named &= bound
    return named or {project}


@router.post("/{instance_name}/rebuild")
async def rebuild_instance(
    instance_name: str,
    body: RebuildRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_tenant()),
    db: AsyncSession = Depends(get_db),
):
    # A Tenant needs view, operate and rebuild on this instance, whatever the rollout flag says;
    # Partner and Admin hold no tenant ACL and meet only the project bound here.
    project = verify_rebuild_access(body.host, body.project, instance_name)
    tenant = get_tenant_acl() is not None

    # Destructive-action confirmation: the client must echo the instance name. Checked before
    # anything reaches the cluster.
    if body.confirm_name != instance_name:
        raise HTTPException(status_code=422, detail="Confirmation name does not match the instance name")
    if tenant and body.mode == "empty":
        # An empty root disk boots only from an install ISO, and attaching one is a Partner action
        # (routers/console.py). A Tenant would be left with an instance it cannot start.
        raise HTTPException(
            status_code=422,
            detail="An empty disk needs an install ISO, which only your provider can attach. Choose an image.",
        )
    if body.mode in ("image", "cloud-init") and not (body.image or "").strip():
        raise HTTPException(status_code=422, detail="An image is required for this rebuild mode")
    if body.mode == "cloud-init" and not (body.cloud_init or "").strip():
        raise HTTPException(status_code=422, detail="cloud-init user-data is required for the cloud-init mode")
    if tenant and body.mode == "cloud-init" and not check_tenant_permission(body.host, project, instance_name, "console"):
        # User-data runs as root on first boot, which reaches everything a console would.
        raise HTTPException(
            status_code=403,
            detail={
                "error": "console_required",
                "message": "Cloud-init runs as root inside the guest, so a cloud-init rebuild also needs the Console grant.",
            },
        )
    _validate_image_server(body.image_server, _target_engine(lxd_service, body.host))
    if tenant:
        await _refuse_an_image_a_tenant_may_not_use(lxd_service, body)
    await _refuse_a_rebuild_in_an_unisolated_project(lxd_service, user, body.host, project, db, tenant=tenant)

    settings = get_settings()
    try:
        # Admission is committed under the same advisory lock as migration execution. The
        # worker reacquires this guard before mutation, so a migration cannot fit between this
        # fence read and a later stop/rebuild submission. The limits are counted under it too,
        # so two requests cannot both pass a count and both start.
        async with guard_instance_mutation(
            cluster_id=body.host,
            project=project,
            instance_name=instance_name,
        ):
            async with async_session_maker() as session:
                await rebuild_admission.admit_rebuild(
                    session,
                    host=body.host,
                    project=project,
                    instance_name=instance_name,
                    tenant=tenant,
                    organization_projects=_tenant_projects(body.host, project) if tenant else None,
                    per_hour=settings.tenant_rebuild_per_instance_per_hour,
                    concurrent=settings.tenant_rebuild_concurrent_per_org,
                )
            # Preflight: status + snapshots + config/volume snapshot. Snapshots block the
            # rebuild (LXD rejects it), so surface a clear 409 before doing anything.
            pf = await lxd_service.get_rebuild_preflight(
                instance_name, body.host, project
            )
            if pf.get("snapshots"):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": (
                            "Rebuild is blocked while the instance has snapshots. "
                            "Delete them first."
                        ),
                        "snapshots": pf["snapshots"],
                    },
                )

            source = _build_source(body)
            job_id = secrets.token_hex(16)
            row = RebuildJob(
                job_id=job_id,
                cluster_id=body.host,
                project=pf.get("project") or project,
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
            async with async_session_maker() as session:
                session.add(row)
                await session.commit()
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except LXDConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail="Instance not found" if tenant else str(e))
        logger.warning("Rebuild of %s on %s refused at admission: %s", instance_name, body.host, e)
        raise HTTPException(status_code=400, detail="The cluster refused the rebuild." if tenant else str(e))

    # The request is on record before any change reaches the cluster; the finished job writes
    # its own row with the result.
    activity_logger.log_activity(
        action="instance.rebuild.requested", target_type="instance", target_id=instance_name,
        target_host=body.host, target_project=row.project, result="success",
        details={"mode": body.mode, "job_id": job_id, "image": row.requested_image, "project": row.project},
    )
    task = asyncio.create_task(_run_rebuild(job_id, body.run_provisioning))
    _RUNNING_JOBS.add(task)
    task.add_done_callback(_RUNNING_JOBS.discard)
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
    # The preflight returns the instance's snapshot names, custom volumes, base image and status,
    # so a cluster check alone is not enough.
    #
    # important: normalised once, here, and the same value goes to the resolver. An empty
    # `?project=` is falsy, and the service reads a falsy project as "search every project".
    scoped_project = _verify_instance_access(host, project, instance_name, "view")
    tenant = get_tenant_acl() is not None
    try:
        pf = await lxd_service.get_rebuild_preflight(instance_name, host, scoped_project)
    except LXDConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail="Instance not found" if tenant else str(e))
        logger.warning("Rebuild preflight of %s on %s failed: %s", instance_name, host, e)
        raise HTTPException(status_code=400, detail="The cluster could not be read." if tenant else str(e))
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
    # The row records which project the instance was in, so the check is against the instance
    # rather than against the cluster it happens to live on.
    _verify_instance_access(row.cluster_id, row.project, row.instance_name, "view")
    return _job_payload(row)


@router.get("/{instance_name}/rebuild")
async def get_latest_rebuild_job(
    instance_name: str,
    host: str,
    project: str = "default",
    db: AsyncSession = Depends(get_db),
):
    project = _verify_instance_access(host, project, instance_name, "view")
    row = (
        await db.execute(
            select(RebuildJob)
            .where(
                RebuildJob.instance_name == instance_name,
                RebuildJob.cluster_id == host,
                RebuildJob.project == project,
            )
            .order_by(RebuildJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return {"job": None}
    return {"job": _job_payload(row)}


# ── recovery ────────────────────────────────────────────────────────────────────────────────

# A rebuild is driven by a detached task, so a kill (out of memory, SIGKILL) loses the driver while
# the row stays mid-flight and the instance stays stopped. Without this sweep the operator would
# see "rebuilding" forever on an instance that is not being rebuilt.
#
# A live driver refreshes its row every heartbeat, so silence means no worker is behind it. Only
# `rebuilding` waits longer, for the daemon rebuild LXD may still run (rebuild_admission.silent_window).

_IN_FLIGHT_STATUSES = rebuild_admission.IN_FLIGHT_STATUSES


async def recover_stranded_rebuilds() -> int:
    """Fail rebuild jobs that no worker can still be driving.

    important: mark them `failed` with a reason naming what to check, never retry silently. The
    instance may be stopped with a half-written root disk, and what to do with it is the
    operator's decision.
    """
    from app.core.database import async_session_maker

    now = datetime.now(timezone.utc)
    others = [s for s in _IN_FLIGHT_STATUSES if s != "rebuilding"]
    async with async_session_maker() as db:
        # One UPDATE claims each row: every worker runs this sweep, and only the one whose
        # statement changed a row reads it back, so each failure is audited once.
        rows = (
            await db.execute(
                update(RebuildJob)
                .where(or_(
                    and_(
                        RebuildJob.status == "rebuilding",
                        RebuildJob.updated_at < now - rebuild_admission.silent_window("rebuilding"),
                    ),
                    and_(
                        RebuildJob.status.in_(others),
                        RebuildJob.updated_at < now - rebuild_admission.silent_window(None),
                    ),
                ))
                .values(status="failed", error=_STRANDED_ERROR, updated_at=now)
                .returning(
                    RebuildJob.job_id, RebuildJob.instance_name, RebuildJob.cluster_id,
                    RebuildJob.project, RebuildJob.mode,
                )
                .execution_options(synchronize_session=False)
            )
        ).all()
        await db.commit()
    if rows:
        get_cache_manager().invalidate_instance_cache()
        logger.warning(
            "Marked %d stranded rebuild job(s) failed", len(rows),
            extra={"job_ids": [r.job_id for r in rows]},
        )
        for row in rows:
            activity_logger.log_activity(
                action="instance.rebuild", target_type="instance", target_id=row.instance_name,
                target_host=row.cluster_id, target_project=row.project, result="failure",
                error_message=_STRANDED_ERROR,
                details={"mode": row.mode, "job_id": row.job_id, "stranded": True, "project": row.project},
            )
    return len(rows)


async def stranded_rebuild_loop() -> None:
    """Runs in every worker; the first pass runs at once, so a restart frees what its kill left."""
    while True:
        try:
            await recover_stranded_rebuilds()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Stranded rebuild sweep error: %s", exc)
        await asyncio.sleep(_STRANDED_SWEEP_SECONDS)


_sweep_task: "Optional[asyncio.Task]" = None


def start_stranded_rebuild_sweep() -> None:
    """Idempotent: safe to call more than once per worker."""
    global _sweep_task
    if _sweep_task is None or _sweep_task.done():
        _sweep_task = asyncio.create_task(stranded_rebuild_loop())
