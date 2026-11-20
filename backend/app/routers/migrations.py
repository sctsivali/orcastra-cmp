"""Migration Center API for durable plans, execution, recovery actions, and history."""

from __future__ import annotations

import re
import logging
from collections.abc import Awaitable
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.dependencies import (
    AuthenticatedUser,
    check_read_only_mode,
    require_admin,
    require_partner,
)
from app.core.org_scope import (
    get_current_allowed_clusters,
    get_current_visible_projects,
    get_user_org_ids,
    org_scope_dependency,
    verify_project_access,
)
from app.core.rbac import Role, get_highest_role, resolve_roles
from app.models.migration_schemas import (
    MigrationActionRequest,
    MigrationDestructiveActionRequest,
    MigrationExecuteRequest,
    MigrationJobResponse,
    MigrationListResponse,
    MigrationOperationalMetricsResponse,
    MigrationPlanCreateRequest,
    MigrationPlanListResponse,
    MigrationPlanPatchRequest,
    MigrationPlanResponse,
)
from app.services.activity_logger import get_activity_logger
from app.services.migrations import MigrationService
from app.services.migrations.authority import (
    AuthorityDeniedError,
    AuthorityIdentityError,
    CurrentAuthorityError,
    authority_verifier,
)
from app.services.migrations.orchestrator import (
    MigrationAuthorityError,
    MigrationConflict,
    MigrationNotFound,
    _authority_pin,
)
from app.services.migrations.contracts import MigrationClusterUnavailable, MigrationOwnerChoiceRequired
from app.services.migrations.metrics import collect_migration_metrics
from app.services.migrations.presenters import job_response, plan_response
from app.services.migrations.repository import MigrationListVisibility
from app.services.migrations.security import MigrationSecurityError, sanitize_error
from app.services.snapshots.schedule_authority import standing_authority_refusal


_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

_IDENTITY_BINDING_REQUIRED_DETAIL = {
    "error": "identity_binding_required",
    "message": (
        "Migration Center could not verify this sign-in's identity binding. "
        "Sign out, sign in again, and retry."
    ),
}
_CURRENT_ROLE_DENIED_DETAIL = {
    "error": "current_role_denied",
    "message": "Migration Center requires a current Admin or Partner role.",
}
_AUTHORITY_UNAVAILABLE_DETAIL = {
    "error": "authority_unavailable",
    "message": "Migration Center could not verify current access. Retry shortly.",
}


def require_migration_admission() -> None:
    """Block new durable work while preserving recovery for existing jobs."""
    if not get_settings().migration_center_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


# Compatibility name retained for dependency overrides while recovery reads stay available.
require_migration_center = require_migration_admission


router = APIRouter(
    prefix="/migrations",
    tags=["Migrations"],
    dependencies=[Depends(org_scope_dependency)],
)


def _role(user: AuthenticatedUser) -> str:
    return get_highest_role(resolve_roles(user)).value


def _if_match(value: Optional[str]) -> int:
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match is required",
        )
    candidate = value.strip()
    if candidate.startswith("W/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Weak If-Match values are not accepted",
        )
    if len(candidate) >= 2 and candidate[0] == candidate[-1] == '"':
        candidate = candidate[1:-1]
    if not candidate.isdigit() or int(candidate) < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="If-Match must contain a positive plan revision",
        )
    return int(candidate)


def _idempotency_key(value: Optional[str]) -> str:
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key is required",
        )
    if not _IDEMPOTENCY_KEY.fullmatch(value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key contains unsupported characters",
        )
    return value


async def _service_call(awaitable: Awaitable[Any]) -> Any:
    try:
        return await awaitable
    except MigrationNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except MigrationAuthorityError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except AuthorityIdentityError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_IDENTITY_BINDING_REQUIRED_DETAIL,
        ) from exc
    except AuthorityDeniedError as exc:
        # Only a conclusive current-role refusal may be described as a role denial. Missing,
        # disabled, replaced, or rebound accounts are identity-binding failures and must not
        # disclose which authoritative account check failed.
        detail = (
            _CURRENT_ROLE_DENIED_DETAIL
            if exc.code == "actor_role_not_authorized"
            else _IDENTITY_BINDING_REQUIRED_DETAIL
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail) from exc
    except CurrentAuthorityError as exc:
        logger.warning("Migration authority verification failed: %s", exc.code)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_AUTHORITY_UNAVAILABLE_DETAIL,
        ) from exc
    except MigrationOwnerChoiceRequired as exc:
        # Only an Admin reaches this (planning._organizations), so the candidates disclose nothing.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "source_owner_choice_required",
                "message": str(exc),
                "candidates": [
                    {"id": org_id, "slug": slug, "name": name} for org_id, slug, name in exc.candidates
                ],
            },
        ) from exc
    except (MigrationConflict, MigrationSecurityError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except MigrationClusterUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Migration API request failed: %s", sanitize_error(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Migration service is temporarily unavailable.",
        ) from exc


def _authorize_refs(source: Any, target: Any, *, conceal: bool = False) -> None:
    try:
        verify_project_access(source.cluster_id, source.project)
        verify_project_access(target.cluster_id, target.project)
    except HTTPException as exc:
        if conceal:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found") from exc
        raise


def _authorize_row(row: Any) -> None:
    class Ref:
        def __init__(self, cluster_id: str, project: str) -> None:
            self.cluster_id = cluster_id
            self.project = project

    _authorize_refs(
        Ref(row.source_cluster_id, row.source_project),
        Ref(row.target_cluster_id, row.target_project),
        conceal=True,
    )


async def _current_authority(user: AuthenticatedUser) -> Any:
    return await _service_call(authority_verifier().pin_user(user))


def _list_visibility(authority: Any) -> MigrationListVisibility:
    allowed_clusters = get_current_allowed_clusters()
    visible_projects = get_current_visible_projects()
    return MigrationListVisibility(
        role=authority.role.value,
        actor_issuer=authority.pin.identity.issuer,
        actor_subject=authority.pin.identity.subject,
        actor_email=authority.pin.identity.email,
        actor_provider_uuid=authority.pin.provider_uuid,
        allowed_clusters=(
            None if allowed_clusters is None else frozenset(allowed_clusters)
        ),
        visible_projects=(
            None
            if visible_projects is None
            else tuple(
                sorted(
                    (cluster_id, frozenset(projects))
                    for cluster_id, projects in visible_projects.items()
                )
            )
        ),
    )


def _assert_list_row_visibility(
    row: Any,
    *,
    authority: Any,
    exact_actor: bool,
) -> None:
    """Fail closed if a repository implementation violates its SQL visibility contract."""
    try:
        _authorize_row(row)
        if exact_actor and _authority_pin(row) != authority.pin:
            raise MigrationAuthorityError("Migration plan actor pin mismatch.")
    except (HTTPException, MigrationAuthorityError) as exc:
        logger.warning("Migration list visibility invariant failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Migration access changed while the list was loading. Retry.",
        ) from exc


async def _authorize_row_current(
    row: Any,
    *,
    authority: Any,
    user: AuthenticatedUser,
    db: AsyncSession,
) -> None:
    _authorize_row(row)
    if authority.role != Role.PARTNER:
        return
    organization_ids = set(await get_user_org_ids(user, db))
    if row.source_organization_id not in organization_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    for cluster_id, project in (
        (row.source_cluster_id, row.source_project),
        (row.target_cluster_id, row.target_project),
    ):
        refusal = await standing_authority_refusal(
            db,
            actor_email=authority.pin.identity.email,
            organization_id=row.source_organization_id,
            cluster_id=cluster_id,
            project=project,
        )
        if refusal is not None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


async def _authorize_plan_current(
    row: Any,
    *,
    authority: Any,
    user: AuthenticatedUser,
    db: AsyncSession,
) -> None:
    """Keep durable drafts private to the exact current Authentik actor."""
    await _authorize_row_current(row, authority=authority, user=user, db=db)
    try:
        actor_pin = _authority_pin(row)
    except MigrationAuthorityError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found",
        ) from exc
    if actor_pin != authority.pin:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


@router.post(
    "/plans",
    response_model=MigrationPlanResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_migration_admission)],
)
async def create_plan(
    body: MigrationPlanCreateRequest,
    response: Response,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> MigrationPlanResponse:
    _authorize_refs(body.source, body.target)
    row = await _service_call(
        MigrationService(db).create_plan(body, user=user, requester_role=_role(user))
    )
    response.headers["ETag"] = f'"{row.revision}"'
    return plan_response(row)


@router.get("/plans", response_model=MigrationPlanListResponse)
async def list_plans(
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> MigrationPlanListResponse:
    authority = await _current_authority(user)
    rows, total = await _service_call(
        MigrationService(db).list_plans(
            visibility=_list_visibility(authority),
            limit=limit,
            offset=offset,
        )
    )
    for row in rows:
        _assert_list_row_visibility(row, authority=authority, exact_actor=True)
    return MigrationPlanListResponse(
        items=[plan_response(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(rows) < total,
    )


@router.get("/plans/{plan_id}", response_model=MigrationPlanResponse)
async def get_plan(
    plan_id: str,
    response: Response,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> MigrationPlanResponse:
    authority = await _current_authority(user)
    row = await _service_call(MigrationService(db).get_plan(plan_id))
    await _authorize_plan_current(row, authority=authority, user=user, db=db)
    response.headers["ETag"] = f'"{row.revision}"'
    return plan_response(row)


@router.delete("/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def discard_plan(
    plan_id: str,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Discard a draft plan that was never started. Only its author may discard it."""
    authority = await _current_authority(user)
    service = MigrationService(db)
    row = await _service_call(service.get_plan(plan_id))
    await _authorize_plan_current(row, authority=authority, user=user, db=db)
    await _service_call(service.discard_plan(plan_id))
    activity_logger.log_activity(
        action="migration.plan.discard",
        target_type="migration_plan",
        target_id=plan_id,
        target_host=row.source_cluster_id,
        target_project=row.source_project,
        result="success",
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/plans/{plan_id}",
    response_model=MigrationPlanResponse,
    dependencies=[Depends(require_migration_admission)],
)
async def patch_plan(
    plan_id: str,
    body: MigrationPlanPatchRequest,
    response: Response,
    if_match: Optional[str] = Header(None, alias="If-Match"),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> MigrationPlanResponse:
    authority = await _current_authority(user)
    service = MigrationService(db)
    existing = await _service_call(service.get_plan(plan_id))
    await _authorize_plan_current(
        existing, authority=authority, user=user, db=db
    )
    if body.target is not None:
        _authorize_refs(
            plan_response(existing).source,
            body.target,
        )
    row = await _service_call(
        service.patch_plan(
            plan_id,
            body,
            expected_revision=_if_match(if_match),
            user=user,
            requester_role=_role(user),
        )
    )
    response.headers["ETag"] = f'"{row.revision}"'
    return plan_response(row)


@router.post(
    "/plans/{plan_id}/execute",
    response_model=MigrationJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_migration_admission)],
)
async def execute_plan(
    plan_id: str,
    body: MigrationExecuteRequest,
    if_match: Optional[str] = Header(None, alias="If-Match"),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
    _readonly: None = Depends(check_read_only_mode),
) -> MigrationJobResponse:
    authority = await _current_authority(user)
    service = MigrationService(db)
    plan = await _service_call(service.get_plan(plan_id))
    await _authorize_plan_current(plan, authority=authority, user=user, db=db)
    row, _created = await _service_call(
        service.execute_plan(
            plan_id,
            expected_revision=_if_match(if_match),
            idempotency_key=_idempotency_key(idempotency_key),
            reason=body.reason,
            acknowledgements=body.acknowledgements,
            user=user,
            requester_role=_role(user),
        )
    )
    return await job_response(db, row)


@router.get("", response_model=MigrationListResponse)
async def list_migrations(
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    view: Optional[Literal["active", "attention", "history"]] = Query(None),
) -> MigrationListResponse:
    authority = await _current_authority(user)
    service = MigrationService(db)
    visibility = _list_visibility(authority)
    rows, total = await _service_call(
        service.list_jobs(
            visibility=visibility,
            limit=limit,
            offset=offset,
            view=view,
        )
    )
    for row in rows:
        _assert_list_row_visibility(row, authority=authority, exact_actor=False)
    counts = await _service_call(service.count_jobs_by_view(visibility=visibility))
    return MigrationListResponse(
        items=[await job_response(db, row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(rows) < total,
        counts=counts,
    )


@router.get("/metrics", response_model=MigrationOperationalMetricsResponse)
async def get_migration_metrics(
    user: AuthenticatedUser = Depends(require_admin()),
    db: AsyncSession = Depends(get_db),
) -> MigrationOperationalMetricsResponse:
    authority = await _current_authority(user)
    if authority.role != Role.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access is required.",
        )
    return await _service_call(collect_migration_metrics(db))


@router.get("/{job_id}", response_model=MigrationJobResponse)
async def get_migration(
    job_id: str,
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
) -> MigrationJobResponse:
    authority = await _current_authority(user)
    row = await _service_call(MigrationService(db).get_job(job_id))
    await _authorize_row_current(row, authority=authority, user=user, db=db)
    return await job_response(db, row)


async def _action(
    *,
    job_id: str,
    action: str,
    body: MigrationActionRequest,
    user: AuthenticatedUser,
    db: AsyncSession,
    confirmation: Optional[str] = None,
) -> MigrationJobResponse:
    service = MigrationService(db)
    existing = await _service_call(service.get_job(job_id))
    _authorize_row(existing)
    row = await _service_call(
        service.act(
            job_id,
            action=action,
            expected_revision=body.expected_revision,
            reason=body.reason,
            confirmation=confirmation,
            user=user,
            requester_role=_role(user),
        )
    )
    return await job_response(db, row)


def _action_route(action: str, path: str) -> None:
    async def endpoint(
        job_id: str,
        body: MigrationActionRequest,
        user: AuthenticatedUser = Depends(require_partner()),
        db: AsyncSession = Depends(get_db),
        _readonly: None = Depends(check_read_only_mode),
    ) -> MigrationJobResponse:
        return await _action(
            job_id=job_id,
            action=action,
            body=body,
            user=user,
            db=db,
        )

    endpoint.__name__ = f"{action}_migration"
    router.post(
        f"/{{job_id}}/{path}",
        response_model=MigrationJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )(endpoint)


for _action_name, _action_path in (
    ("cancel", "cancel"),
    ("cutover", "cutover"),
    ("confirm_external_cutover", "confirm-external-cutover"),
    ("recheck", "recheck"),
):
    _action_route(_action_name, _action_path)


def _destructive_action_route(action: str, path: str) -> None:
    async def endpoint(
        job_id: str,
        body: MigrationDestructiveActionRequest,
        user: AuthenticatedUser = Depends(require_partner()),
        db: AsyncSession = Depends(get_db),
        _readonly: None = Depends(check_read_only_mode),
    ) -> MigrationJobResponse:
        return await _action(
            job_id=job_id,
            action=action,
            body=body,
            user=user,
            db=db,
            confirmation=body.confirmation,
        )

    endpoint.__name__ = f"{action}_migration"
    router.post(
        f"/{{job_id}}/{path}",
        response_model=MigrationJobResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )(endpoint)


for _action_name, _action_path in (
    ("rollback", "rollback"),
    ("restore_checkpoint", "restore-checkpoint"),
    ("cleanup", "cleanup"),
    ("release", "release"),
):
    _destructive_action_route(_action_name, _action_path)


__all__ = ["require_migration_admission", "require_migration_center", "router"]
