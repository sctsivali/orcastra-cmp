"""Wire shapes of the task journal: what REST answers and what the WebSocket feed sends.

`frontend/src/types/tasks.ts` mirrors these one to one. Change both or neither.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

TaskStatus = Literal["pending", "running", "success", "failure", "cancelled", "lost"]
TaskKind = Literal["lxd_operation", "dashboard_action", "upload", "rebuild", "schedule"]
ActorKind = Literal["user", "api_key", "system", "external"]
TaskSource = Literal["dashboard", "events", "cli", "mcp", "integration", "schedule"]
LogKind = Literal["audit", "lifecycle", "warning", "task"]
LogSeverity = Literal["info", "notice", "warning", "error"]
FollowerState = Literal["connected", "disconnected", "unregistered"]


class Requestor(BaseModel):
    """What LXD reported about who asked. `address` is Admin-only on the wire."""

    username: Optional[str] = None
    protocol: Optional[str] = None
    address: Optional[str] = None


class TaskRecord(BaseModel):
    id: str
    seq: int
    kind: TaskKind
    cluster_id: str
    project: str
    resource_type: Optional[str] = None
    resource_name: Optional[str] = None
    operation_id: Optional[str] = None
    operation_class: Optional[str] = None
    action: Optional[str] = None
    description: str
    status: TaskStatus
    status_code: Optional[int] = None
    progress: Optional[str] = None
    error: Optional[str] = None
    may_cancel: bool
    actor_email: Optional[str] = None
    actor_role: Optional[str] = None
    actor_kind: ActorKind
    api_key_id: Optional[str] = None
    requestor: Optional[Requestor] = None
    source: TaskSource
    request_id: Optional[str] = None
    # Admin only; stripped below that tier.
    target_organization_id: Optional[int] = None
    target_organization_slug: Optional[str] = None
    # Cluster member the operation ran on; Admin and Partner only.
    location: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    lxd_created_at: Optional[datetime] = None
    lxd_updated_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    # finished - started when finished, now - started while open, None when never started.
    duration_ms: Optional[int] = None
    queue_position: Optional[int] = Field(
        None, ge=1, description="Pending rank within the caller's visible cluster queue; null elsewhere"
    )


class TaskListResponse(BaseModel):
    tasks: List[TaskRecord]
    total: int = Field(..., description="Rows matching the filters inside the caller's scope")
    running: int = Field(
        ..., description="Open rows in the caller's scope, filters ignored, LXD's own background tasks left out"
    )
    failed_recent: int = Field(
        ..., description="Failures finished in the last 15 minutes, in scope, LXD's own background tasks left out"
    )
    limit: int
    offset: int
    has_more: bool
    disconnected_clusters: List[str] = Field(
        default_factory=list,
        description="Followed clusters in scope whose event stream is not connected right now",
    )
    follower_active: bool = Field(..., description="Whether any worker holds the follower lock")


class ClusterLogEntry(BaseModel):
    id: int
    ts: datetime
    kind: LogKind
    severity: LogSeverity
    cluster_id: Optional[str] = None
    project: Optional[str] = None
    resource_type: Optional[str] = None
    resource_name: Optional[str] = None
    action: str
    message: str
    actor_email: Optional[str] = None
    actor_role: Optional[str] = None
    actor_kind: ActorKind
    api_key_id: Optional[str] = None
    result: Optional[str] = None
    request_id: Optional[str] = None
    event_id: Optional[str] = None
    task_id: Optional[str] = None
    target_organization_id: Optional[int] = None
    details: Optional[Dict[str, Any]] = None


class ClusterLogResponse(BaseModel):
    entries: List[ClusterLogEntry]
    total: int
    limit: int
    offset: int
    has_more: bool


class TaskDetailResponse(BaseModel):
    task: TaskRecord
    log: List[ClusterLogEntry]


class DueSchedule(BaseModel):
    schedule_id: int
    cluster_id: str
    project: str
    instance_name: str
    label: str
    next_run_at: datetime
    run_state: str


class QueueCluster(BaseModel):
    cluster_id: str
    state: FollowerState
    running: List[TaskRecord]
    pending: List[TaskRecord]
    due_schedules: List[DueSchedule]


class QueueResponse(BaseModel):
    clusters: List[QueueCluster]
    generated_at: datetime
    disconnected_clusters: List[str]
    follower_active: bool
    total_open: int = Field(..., ge=0, description="Open tasks inside the caller's scope")
    limit: int = Field(..., ge=1, le=500)
    offset: int = Field(..., ge=0)
    has_more: bool
    due_schedules_total: int = Field(..., ge=0)
    due_schedules_truncated: bool


class CancelResponse(BaseModel):
    success: bool
    task_id: str
    operation_id: str
    message: str


# WebSocket feed frames (server to client; the client sends nothing):
#   {"type": "hello", "server_time": iso, "follower_active": bool}
#   {"type": "task", "task": TaskRecord}          upsert by task.id, keep the higher seq
#   {"type": "log", "entry": ClusterLogEntry}     append by entry.id
#   {"type": "follower", "active": bool}          on change only
#   {"type": "resync", "reason": str}             the client refetches its lists
