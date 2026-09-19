"""The Cluster Log tab: dashboard audit records, LXD lifecycle events and LXD warnings.

Same gate as the task list (`require_tenant()` plus the org scope), narrowed by
`app.core.task_scope.log_conditions`: a Partner sees its clusters within its project bound,
a tenant sees lines about its own instances and projects, and a host-less line (an identity
or settings change, an access denial) reaches Admin and the actor who caused it.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser
from app.core.database import get_db
from app.core.dependencies import require_tenant
from app.core.org_scope import org_scope_dependency, verify_cluster_access
from app.core.task_scope import caller_tier, log_conditions, log_is_visible
from app.models.task_journal_models import LOG_KINDS, LOG_SEVERITIES, ClusterLog
from app.models.task_schemas import ClusterLogResponse
from app.routers.tasks import actor_email_filter, csv_filter, task_uuid, utc_bound
from app.services.task_journal import present, reads

router = APIRouter(
    prefix="/cluster-log",
    tags=["Cluster Log"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

DEFAULT_WINDOW = timedelta(hours=24)


@router.get("", response_model=ClusterLogResponse, summary="Log lines in the caller's scope")
async def list_cluster_log(
    kind: Optional[str] = Query(None, description="Comma-separated kinds"),
    severity: Optional[str] = Query(None, description="Comma-separated severities"),
    cluster_id: Optional[str] = Query(None, description="One cluster"),
    project: Optional[str] = Query(None, description="One LXD project"),
    actor: Optional[str] = Query(None, description="An actor email, or `me`"),
    q: Optional[str] = Query(None, max_length=200, description="Text in the message"),
    since: Optional[datetime] = Query(None, description="Lines at or after this time; default 24 hours ago"),
    task_id: Optional[str] = Query(None, description="Lines linked to one task"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _rbac: AuthenticatedUser = Depends(require_tenant()),
) -> ClusterLogResponse:
    if cluster_id:
        verify_cluster_access(cluster_id)
    kinds = csv_filter(kind, LOG_KINDS, "kind")
    severities = csv_filter(severity, LOG_SEVERITIES, "severity")
    actor_email = actor_email_filter(actor, _rbac)

    # note: the window is always bounded so a scan never walks the whole table.
    window_start = utc_bound(since) if since else datetime.now(timezone.utc) - DEFAULT_WINDOW
    filters: List[Any] = [ClusterLog.ts >= window_start]
    if kinds:
        filters.append(ClusterLog.kind.in_(kinds))
    if severities:
        filters.append(ClusterLog.severity.in_(severities))
    if cluster_id:
        filters.append(ClusterLog.cluster_id == cluster_id)
    if project:
        filters.append(ClusterLog.project == project)
    if actor_email:
        filters.append(ClusterLog.actor_email == actor_email)
    if q and q.strip():
        filters.append(ClusterLog.message.ilike(f"%{q.strip()}%"))
    if task_id:
        filters.append(ClusterLog.task_id == task_uuid(task_id))

    scope = log_conditions(ClusterLog, caller_email=_rbac.email)
    rows, total = await reads.list_log(db, scope, filters, limit, offset)
    tier = caller_tier()
    entries = [
        present.log_row(row, tier=tier)
        for row in rows
        if log_is_visible(row, caller_email=_rbac.email)
    ]
    return ClusterLogResponse(
        entries=entries,
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(rows) < total,
    )
