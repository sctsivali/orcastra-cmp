"""Dashboard API for safe edits to an existing instance's resource specification."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Literal, Optional

import requests.exceptions as requests_exc
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import (
    AuthenticatedUser,
    check_read_only_mode,
    require_partner,
    require_tenant,
)
from app.core.error_sanitizer import sanitize_error
from app.core.lxd_names import LXD_NAME_DESCRIPTION, LXD_NAME_PATTERN
from app.core.org_scope import (
    org_scope_dependency,
    verify_instance_view_access,
)
from app.services.instance_job_identity import resolve_instance_job_organization
from app.services.instance_operation_audit import drain_instance_operation_audits
from app.services.migration_fence import (
    MigrationFenceConflict,
    guard_instance_mutation,
)
from app.services.instance_specifications import (
    SpecificationError,
    create_operation_job,
    get_operation_job,
    get_operation_job_by_request,
    job_payload,
    preflight_specification,
    read_specification,
    request_hash,
    run_specification_job,
    safe_requested,
)

router = APIRouter(
    prefix="/instances",
    tags=["Instances - Specifications"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)
logger = logging.getLogger(__name__)
LXD_NAME_RE = re.compile(LXD_NAME_PATTERN)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SpecificationChangeRequest(_StrictModel):
    host: str = Field(..., min_length=1, max_length=255)
    project: str = Field("default", min_length=1, max_length=255)
    cpu_count: Optional[int] = Field(None, ge=1, le=65_536)
    memory_bytes: Optional[int] = Field(None, ge=1, le=2**63 - 1)
    root_disk_bytes: Optional[int] = Field(None, ge=1, le=2**63 - 1)

    @field_validator("host", "project")
    @classmethod
    def _strip_scope(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Scope values must not be blank")
        return stripped

    @model_validator(mode="after")
    def _has_a_change(self) -> "SpecificationChangeRequest":
        if all(
            value is None
            for value in (self.cpu_count, self.memory_bytes, self.root_disk_bytes)
        ):
            raise ValueError("At least one specification value is required")
        return self


class SpecificationApplyRequest(SpecificationChangeRequest):
    plan_revision: str = Field(..., min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    apply_mode: Literal["live_only", "graceful_restart"] = "live_only"
    acknowledgements: List[str] = Field(default_factory=list, max_length=8)

    @field_validator("acknowledgements")
    @classmethod
    def _known_acknowledgements(cls, values: List[str]) -> List[str]:
        allowed = {"memory_decrease", "root_disk_growth", "restart_instance"}
        if any(value not in allowed for value in values):
            raise ValueError("One or more acknowledgement codes are not recognized")
        return sorted(set(values))


class ResourceValue(_StrictModel):
    value: Optional[int]
    source: Literal["instance", "profile", "default"]


class MemoryValue(_StrictModel):
    value_bytes: Optional[int]
    source: Literal["instance", "profile", "default"]


class RootDiskValue(_StrictModel):
    value_bytes: Optional[int]
    pool: Optional[str]
    source: Literal["instance", "profile", "default"]
    editable: bool
    pending_quota_change: bool = False
    reason: Optional[str]


class SpecificationCapabilities(_StrictModel):
    live_update: bool
    restart_supported: bool


class InstanceSpecificationResponse(_StrictModel):
    instance_name: str
    host: str
    project: str
    type: str
    status: str
    cpu: ResourceValue
    memory: MemoryValue
    root_disk: RootDiskValue
    capabilities: SpecificationCapabilities
    etag: Optional[str]


class PlanChange(_StrictModel):
    field: Literal["cpu_count", "memory_bytes", "root_disk_bytes"]
    before: Optional[int]
    after: int
    source: Literal["instance", "profile", "default"]


class PlanWarning(_StrictModel):
    code: str
    message: str
    requires_acknowledgement: bool


class PlanBlocker(_StrictModel):
    code: str
    message: str
    field: Optional[str] = None


class SpecificationPreflightResponse(_StrictModel):
    instance_name: str
    host: str
    project: str
    status: str
    type: str
    etag: Optional[str]
    plan_revision: str
    execution: Literal["live", "restart_required", "stopped"]
    changes: List[PlanChange]
    warnings: List[PlanWarning]
    blockers: List[PlanBlocker]
    required_acknowledgements: List[str]


class InstanceOperationResponse(_StrictModel):
    job_id: str
    kind: Literal["create", "specification"]
    instance_name: str
    host: str
    project: str
    phase: str
    status: Literal["pending", "running", "success", "failure", "partial", "unknown"]
    operation_id: Optional[str]
    error: Optional[str]
    outcome: Optional[Literal["success", "failure", "partial"]]
    result: Optional[Dict[str, Any]]
    verified_readback: Optional[Dict[str, Any]]
    created_at: Optional[str]
    updated_at: Optional[str]


def _instance_project(host: str, project: str, instance_name: str) -> str:
    """Authorize the exact instance as well as its project before any daemon I/O."""
    return verify_instance_view_access(host, project, instance_name)


def _valid_instance_name(name: str) -> None:
    if not LXD_NAME_RE.fullmatch(name) or len(name) > 63:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid instance name. {LXD_NAME_DESCRIPTION}.",
        )


def _raise_service_error(exc: BaseException, host: str) -> None:
    if isinstance(exc, SpecificationError):
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if isinstance(exc, (requests_exc.Timeout, requests_exc.ConnectionError)):
        raise HTTPException(
            status_code=503,
            detail=f"Could not read the instance specification from {host}.",
        ) from exc
    raise HTTPException(status_code=400, detail=sanitize_error(str(exc))) from exc


def _idempotency_key(value: str) -> str:
    key = value.strip()
    if (
        not key
        or len(key) > 128
        or any(ord(char) < 33 or ord(char) > 126 for char in key)
    ):
        raise HTTPException(
            status_code=422,
            detail="Idempotency-Key must contain between 1 and 128 characters.",
        )
    return key


# Literal route first so it can never be interpreted as an instance called "operations" by a
# future broad instance route added to this router.
@router.get(
    "/operations/{job_id}",
    response_model=InstanceOperationResponse,
    summary="Get an instance operation",
)
async def get_instance_operation(job_id: str) -> Dict[str, Any]:
    row = await get_operation_job(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Instance operation not found")
    _instance_project(row.cluster_id, row.project, row.instance_name)
    return job_payload(row)


@router.get(
    "/{instance_name}/specification",
    response_model=InstanceSpecificationResponse,
    summary="Read the editable instance specification",
)
async def get_instance_specification(
    instance_name: str,
    response: Response,
    host: str = Query(..., min_length=1, max_length=255),
    project: str = Query("default", min_length=1, max_length=255),
) -> Dict[str, Any]:
    _valid_instance_name(instance_name)
    scoped_project = verify_instance_view_access(host, project, instance_name)
    try:
        current = await read_specification(instance_name, host, scoped_project)
    except Exception as exc:  # noqa: BLE001 - centralized transport/status mapping
        _raise_service_error(exc, host)
    if current.etag:
        response.headers["ETag"] = current.etag
    return current.public


@router.post(
    "/{instance_name}/specification/preflight",
    response_model=SpecificationPreflightResponse,
    summary="Plan an instance specification change",
)
async def preflight_instance_specification(
    instance_name: str,
    body: SpecificationChangeRequest,
    _rbac: AuthenticatedUser = Depends(require_partner()),
) -> Dict[str, Any]:
    _valid_instance_name(instance_name)
    scoped_project = _instance_project(body.host, body.project, instance_name)
    try:
        _, plan = await preflight_specification(
            instance_name,
            body.host,
            scoped_project,
            safe_requested(body.model_dump()),
        )
        return plan
    except Exception as exc:  # noqa: BLE001
        _raise_service_error(exc, body.host)


@router.patch(
    "/{instance_name}/specification",
    response_model=InstanceOperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Apply an instance specification change",
)
async def update_instance_specification(
    instance_name: str,
    body: SpecificationApplyRequest,
    if_match: str = Header(..., alias="If-Match"),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    _valid_instance_name(instance_name)
    scoped_project = _instance_project(body.host, body.project, instance_name)
    key = _idempotency_key(idempotency_key)
    requester = user.user_id or user.email
    requested = safe_requested(body.model_dump())
    digest = request_hash(
        host=body.host,
        project=scoped_project,
        instance_name=instance_name,
        requested=requested,
        plan_revision=body.plan_revision,
        apply_mode=body.apply_mode,
        acknowledgements=body.acknowledgements,
    )

    # An idempotent retry remains useful while the cluster is offline. Authorize its recorded
    # target, compare the complete request hash, and return the durable result without new I/O.
    replay = await get_operation_job_by_request(requester, key)
    if replay is not None:
        if (
            replay.kind != "specification"
            or replay.cluster_id != body.host
            or replay.project != scoped_project
            or replay.instance_name != instance_name
            or replay.payload_hash != digest
        ):
            raise HTTPException(
                status_code=409,
                detail="This Idempotency-Key was already used for a different request.",
            )
        return job_payload(replay)

    try:
        # Final read/preflight and durable admission are one critical section with migration
        # execution. The worker reacquires the same guard before any daemon mutation.
        async with guard_instance_mutation(
            db,
            cluster_id=body.host,
            project=scoped_project,
            instance_name=instance_name,
        ):
            current, plan = await preflight_specification(
                instance_name,
                body.host,
                scoped_project,
                requested,
            )
            if not current.etag or not (
                if_match.strip().removeprefix("W/").strip('"')
                == current.etag.strip().removeprefix("W/").strip('"')
            ):
                raise HTTPException(
                    status_code=412,
                    detail=(
                        "This instance changed since it was read. Reload it and review the "
                        "change again."
                    ),
                )
            if plan["plan_revision"] != body.plan_revision:
                raise HTTPException(
                    status_code=409,
                    detail={"message": "The specification plan is stale.", "preflight": plan},
                )
            if plan["blockers"]:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "The specification change is blocked.",
                        "preflight": plan,
                    },
                )
            if (
                plan["execution"] == "restart_required"
                and body.apply_mode != "graceful_restart"
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "This change requires explicit graceful-restart approval.",
                        "preflight": plan,
                    },
                )
            missing = set(plan["required_acknowledgements"]) - set(
                body.acknowledgements
            )
            if missing:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Required warnings were not acknowledged.",
                        "missing": sorted(missing),
                        "preflight": plan,
                    },
                )

            job_organization = await resolve_instance_job_organization(
                db,
                cluster_id=body.host,
                project=scoped_project,
            )

            row, created = await create_operation_job(
                kind="specification",
                host=body.host,
                project=scoped_project,
                instance_name=instance_name,
                requester=requester,
                target_organization_id=job_organization.organization_id,
                target_organization_slug=job_organization.organization_slug,
                target_organization_resolution=job_organization.resolution,
                idempotency_key=key,
                payload_hash=digest,
                requested=requested,
                plan_revision=body.plan_revision,
                apply_mode=body.apply_mode,
                instance_type=current.public["type"],
                before_spec=current.public,
                target_spec={
                    "acknowledgements": sorted(set(body.acknowledgements)),
                    "execution": plan["execution"],
                },
            )
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_service_error(exc, body.host)

    if created:
        try:
            await drain_instance_operation_audits(job_id=row.job_id)
        except Exception as exc:  # noqa: BLE001 - startup recovery replays the outbox
            logger.warning(
                "Could not drain accepted audit for specification job %s: %s",
                row.job_id,
                sanitize_error(str(exc)),
            )
        asyncio.create_task(run_specification_job(row.job_id))
    return job_payload(row)
