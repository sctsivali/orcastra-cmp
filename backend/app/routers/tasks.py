"""The Tasks tab and the Operations page: what the journal recorded, in the caller's scope.

Why this router admits a tenant where `/operations` (`lxd_operations.py`) does not: that one
reads LXD's live operation bucket per cluster and cannot narrow a row below the cluster, so a
Partner tier was the only honest gate. The journal stores every row with its project and
resource, so `app.core.task_scope` narrows a tenant's reads to their own instances by the same
ACL the instance list applies, and the one mutation here (cancel) stays Partner and above.

No response cache on purpose: the panel is a live surface fed by the WebSocket, every read is
one indexed page, and a cached list would hand a narrowed caller a page built for a wider one.
"""

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser
from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.database import get_db
from app.core.dependencies import check_read_only_mode, require_partner, require_tenant
from app.core.error_sanitizer import sanitize_error
from app.core.lxd_path import safe_segment
from app.core.org_scope import (
    check_tenant_permission,
    get_current_allowed_clusters,
    org_scope_dependency,
    project_is_visible,
    verify_cluster_access,
)
from app.core.task_scope import (
    caller_tier,
    disconnected_for_caller,
    log_is_visible,
    task_conditions,
    task_is_visible,
)
from app.core.task_schedule_scope import schedule_conditions
from app.models.snapshot_schedule_models import SnapshotSchedule
from app.models.task_journal_models import TASK_KINDS, TASK_STATUSES, TaskJournal
from app.models.task_schemas import (
    CancelResponse,
    DueSchedule,
    QueueCluster,
    QueueResponse,
    TaskDetailResponse,
    TaskListResponse,
    TaskRecord,
)
from app.services.activity_logger import get_activity_logger
from app.services.lxd.availability import require_client
from app.services.lxd.lazy_client import client_for
from app.services.lxd.lxd_service import LXDService, get_lxd_service
from app.services.task_journal import present, reads
from app.services.task_journal.vocabulary import TERMINAL

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/tasks",
    tags=["Tasks"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# A cancel is one DELETE per click; the pool is small and bounded so a hung cluster can hold
# at most four threads and never the shared exec pool.
_CANCEL_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tasks-cancel")
_CANCEL_TIMEOUT = (3, 8)


def csv_filter(value: Optional[str], allowed: tuple, name: str) -> Optional[List[str]]:
    """A comma-separated filter, validated against the closed vocabulary (422 otherwise)."""
    if value is None or not value.strip():
        return None
    parts = [part.strip() for part in value.split(",") if part.strip()]
    unknown = sorted(set(parts) - set(allowed))
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown {name} value(s): {', '.join(unknown)}. Allowed: {', '.join(allowed)}",
        )
    return parts


def actor_email_filter(actor: Optional[str], user: AuthenticatedUser) -> Optional[str]:
    if actor != "me":
        return actor or None
    # important: a caller with no email cannot be scoped to "their own" rows; refuse rather
    # than let None fall through as "no filter" (the console sessions precedent).
    if not user.email:
        raise HTTPException(status_code=403, detail="Token carries no email claim")
    return user.email


def utc_bound(value: datetime) -> datetime:
    # note: a bare ISO stamp in the query is read as UTC; asyncpg refuses a naive datetime
    # against a timestamptz column.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def task_uuid(task_id: str) -> str:
    try:
        return str(uuid.UUID(task_id))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="task_id must be a UUID")


def _not_found() -> HTTPException:
    # The enumeration rule: a row outside the caller's scope reads exactly like a row that
    # does not exist, so an id cannot be used to probe another organisation's journal.
    return HTTPException(status_code=404, detail="Task not found")


async def _load_visible_task(db: AsyncSession, task_id: str) -> Dict[str, Any]:
    row = await reads.get_task(db, task_uuid(task_id))
    if row is None or not task_is_visible(row):
        raise _not_found()
    return dict(row)


@router.get("", response_model=TaskListResponse, summary="List tasks in the caller's scope")
async def list_tasks(
    status: Optional[str] = Query(None, description="Comma-separated task statuses"),
    cluster_id: Optional[str] = Query(None, description="One cluster"),
    project: Optional[str] = Query(None, description="One LXD project"),
    actor: Optional[str] = Query(None, description="An actor email, or `me`"),
    kind: Optional[str] = Query(None, description="Comma-separated task kinds"),
    since: Optional[datetime] = Query(None, description="Rows changed at or after this time"),
    q: Optional[str] = Query(None, max_length=200, description="Text in description, resource or error"),
    limit: int = Query(50, ge=0, le=500, description="0 answers the counts only"),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
) -> TaskListResponse:
    if cluster_id:
        verify_cluster_access(cluster_id)
    statuses = csv_filter(status, TASK_STATUSES, "status")
    kinds = csv_filter(kind, TASK_KINDS, "kind")
    actor_email = actor_email_filter(actor, _rbac)

    filters: List[Any] = []
    if statuses:
        filters.append(TaskJournal.status.in_(statuses))
    if cluster_id:
        filters.append(TaskJournal.cluster_id == cluster_id)
    if project:
        filters.append(TaskJournal.project == project)
    if actor_email:
        filters.append(TaskJournal.actor_email == actor_email)
    if kinds:
        filters.append(TaskJournal.kind.in_(kinds))
    if since is not None:
        filters.append(TaskJournal.updated_at >= utc_bound(since))
    if q and q.strip():
        needle = f"%{q.strip()}%"
        filters.append(
            or_(
                TaskJournal.description.ilike(needle),
                TaskJournal.resource_name.ilike(needle),
                TaskJournal.error.ilike(needle),
            )
        )

    scope = task_conditions(TaskJournal)
    rows, total = await reads.list_tasks(db, scope, filters, limit, offset)
    running = await reads.count_running(db, scope)
    failed_recent = await reads.count_failed_recent(db, scope)
    states = await reads.follower_snapshot(db)
    active = await reads.follower_active(db)

    tier = caller_tier()
    tasks = [present.task_row(row, tier=tier) for row in rows if task_is_visible(row)]
    return TaskListResponse(
        tasks=tasks,
        total=total,
        running=running,
        failed_recent=failed_recent,
        limit=limit,
        offset=offset,
        has_more=offset + len(rows) < total,
        disconnected_clusters=disconnected_for_caller(states),
        follower_active=active,
    )


@router.get("/queue", response_model=QueueResponse, summary="Open tasks per cluster")
async def get_queue(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
) -> QueueResponse:
    scope = task_conditions(TaskJournal)
    rows, total = await reads.queue_rows(db, scope, limit, offset)
    states = await reads.follower_snapshot(db)
    active = await reads.follower_active(db)
    due, due_total = await reads.due_schedules(
        db,
        get_current_allowed_clusters(),
        conditions=schedule_conditions(SnapshotSchedule),
        limit=100 if offset == 0 else 0,
    )

    tier = caller_tier()
    state_by_cluster = {str(state["cluster_id"]): str(state["state"]) for state in states}
    buckets: Dict[str, Dict[str, list]] = {}

    def bucket(cluster_id: str) -> Dict[str, list]:
        return buckets.setdefault(cluster_id, {"running": [], "pending": [], "due": []})

    for row in rows:
        if not task_is_visible(row):
            continue
        record = TaskRecord(**present.task_row(row, tier=tier))
        bucket(record.cluster_id)[record.status].append(record)

    for schedule in due:
        cluster_id = str(schedule["cluster_id"])
        project = str(schedule["project"] or "default")
        name = str(schedule["instance_name"])
        # important: the snapshot permission, not "any rule here": a tenant holding `view` on
        # one instance must not learn another instance's schedule (the fleet snapshots idiom).
        if not project_is_visible(cluster_id, project):
            continue
        if not check_tenant_permission(cluster_id, project, name, "snapshot"):
            continue
        bucket(cluster_id)["due"].append(
            DueSchedule(
                schedule_id=int(schedule["schedule_id"]),
                cluster_id=cluster_id,
                project=project,
                instance_name=name,
                label=str(schedule["label"] or ""),
                next_run_at=schedule["next_run_at"],
                run_state=str(schedule["run_state"] or "idle"),
            )
        )

    clusters = [
        QueueCluster(
            cluster_id=cluster_id,
            state=state_by_cluster.get(cluster_id, "disconnected"),
            running=parts["running"],
            pending=parts["pending"],
            due_schedules=parts["due"],
        )
        for cluster_id, parts in sorted(buckets.items())
    ]
    return QueueResponse(
        clusters=clusters,
        generated_at=datetime.now(timezone.utc),
        disconnected_clusters=disconnected_for_caller(states),
        follower_active=active,
        total_open=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(rows) < total,
        due_schedules_total=due_total,
        due_schedules_truncated=len(due) < due_total,
    )


@router.get("/{task_id}", response_model=TaskDetailResponse, summary="One task and its log lines")
async def get_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
) -> TaskDetailResponse:
    row = await _load_visible_task(db, task_id)
    tier = caller_tier()
    lines = await reads.task_log(db, row["id"])
    return TaskDetailResponse(
        task=present.task_row(row, tier=tier),
        log=[
            present.log_row(line, tier=tier)
            for line in lines
            if log_is_visible(line, caller_email=_rbac.email)
        ],
    )


def _refuse(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": code, "message": message})


def _lxd_says_gone(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 404 or type(exc).__name__ == "NotFound"


@router.delete(
    "/{task_id}",
    response_model=CancelResponse,
    summary="Cancel a running hypervisor operation",
    dependencies=[Depends(check_read_only_mode)],
)
async def cancel_task(
    task_id: str,
    db: AsyncSession = Depends(get_db),
    lxd_service: LXDService = Depends(get_lxd_service),
    _rbac: AuthenticatedUser = Depends(require_partner()),
) -> CancelResponse:
    row = await _load_visible_task(db, task_id)
    operation_id = row.get("operation_id")
    if not operation_id:
        raise _refuse("NO_OPERATION", "This task is not a cancellable hypervisor operation")
    if row.get("operation_class") == "websocket":
        # D16: console and exec sessions are ended from the Sessions page, never by an
        # operation DELETE that would drop the socket under the operator.
        raise _refuse("WEBSOCKET_CLASS", "Console and exec sessions are ended from the Sessions page")
    if row.get("status") in TERMINAL:
        raise _refuse("ALREADY_FINISHED", "This task has already finished")
    if not row.get("may_cancel"):
        raise _refuse("NOT_CANCELLABLE", "The cluster reports this operation cannot be cancelled")

    cluster_id = str(row["cluster_id"])
    audit = get_activity_logger()
    details = {"task_id": row["id"], "operation_id": operation_id}
    try:
        client = require_client(
            await client_for(lxd_service, cluster_id, label="tasks.cancel"), cluster_id
        )
        await run_blocking(
            partial(client.api.operations[safe_segment(operation_id)].delete, timeout=_CANCEL_TIMEOUT),
            timeout=15,
            label="tasks.cancel",
            executor=_CANCEL_EXECUTOR,
            max_in_flight=4,
        )
    except HTTPException:
        # important: let require_client's 503 through; the broad handler below would turn it
        # into a 500 and an offline cluster would read as a server fault.
        raise
    except (BlockingTimeout, BlockingPoolFull) as exc:
        audit.log_activity(
            action="task.cancel", target_type="task", target_id=row["id"], target_host=cluster_id,
            target_project=row.get("project"), result="failure",
            error_message=sanitize_error(str(exc)), details=details,
        )
        raise HTTPException(status_code=503, detail=f"Cluster '{cluster_id}' did not answer in time")
    except Exception as exc:
        if _lxd_says_gone(exc):
            raise _refuse("ALREADY_FINISHED", "The cluster no longer holds this operation")
        message = sanitize_error(str(exc))
        logger.warning("cancel of %s on %s failed: %s", operation_id, cluster_id, message)
        audit.log_activity(
            action="task.cancel", target_type="task", target_id=row["id"], target_host=cluster_id,
            target_project=row.get("project"), result="failure", error_message=message,
            details=details,
        )
        raise HTTPException(status_code=502, detail=f"The cluster refused the cancel: {message}")

    # The router does not set the status: the follower's Cancelled frame does.
    audit.log_activity(
        action="task.cancel",
        target_type="task",
        target_id=row["id"],
        target_host=cluster_id,
        target_project=row.get("project"),
        details=details,
    )
    return CancelResponse(
        success=True,
        task_id=row["id"],
        operation_id=str(operation_id),
        message="Cancel requested; the task closes when the cluster confirms",
    )
