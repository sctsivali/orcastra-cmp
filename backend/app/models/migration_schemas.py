"""Strict public contracts for migration planning and durable execution.

The wire shapes carry only a sanitized resource projection. Migration secrets, certificates,
cloud-init, raw daemon configuration and arbitrary operation metadata are not valid fields here.
"""

from datetime import datetime
from enum import Enum
from typing import Annotated, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.core.lxd_names import LXD_NAME_DESCRIPTION, LXD_NAME_PATTERN


MappingName = Annotated[str, Field(min_length=1, max_length=255)]
Acknowledgement = Annotated[str, Field(min_length=1, max_length=200)]


class MigrationScope(str, Enum):
    INSTANCE = "instance"
    PROJECT = "project"


class MigrationIntent(str, Enum):
    COPY = "copy"
    MOVE = "move"


class MigrationPlanStatus(str, Enum):
    DRAFT = "draft"
    PREFLIGHTING = "preflighting"
    READY = "ready"
    BLOCKED = "blocked"
    EXPIRED = "expired"
    EXECUTED = "executed"


class MigrationJobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    MANUAL_RECOVERY = "manual_recovery"


class MigrationJobPhase(str, Enum):
    QUEUED = "queued"
    PREPARING_TARGET = "preparing_target"
    PREPARING_RECOVERY = "preparing_recovery"
    INITIAL_SYNC = "initial_sync"
    CUTOVER_PENDING = "cutover_pending"
    QUIESCING = "quiescing"
    CHECKPOINTING = "checkpointing"
    FINAL_SYNC = "final_sync"
    TARGET_VALIDATING = "target_validating"
    EXTERNAL_CUTOVER_PENDING = "external_cutover_pending"
    CUTTING_OVER = "cutting_over"
    VERIFYING = "verifying"
    SOURCE_RETAINED = "source_retained"
    CLEANUP_DUE = "cleanup_due"
    CLEANING_UP = "cleaning_up"
    UNKNOWN = "unknown"
    RECONCILING = "reconciling"
    ROLLBACK_AVAILABLE = "rollback_available"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    CLEANUP_BLOCKED = "cleanup_blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    MANUAL_RECOVERY = "manual_recovery"
    FAILED = "failed"


class MigrationResourceState(str, Enum):
    PENDING = "pending"
    BLOCKED = "blocked"
    PREPARING = "preparing"
    SYNCING = "syncing"
    READY = "ready"
    CUTTING_OVER = "cutting_over"
    RETAINED = "retained"
    CLEANING = "cleaning"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


class MigrationIssueSeverity(str, Enum):
    BLOCKER = "blocker"
    WARNING = "warning"
    INFO = "info"


class MigrationDowntimeClass(str, Enum):
    NONE = "none"
    BRIEF = "brief"
    COLD = "cold"
    UNKNOWN = "unknown"


class MigrationActionName(str, Enum):
    CANCEL = "cancel"
    CUTOVER = "cutover"
    CONFIRM_EXTERNAL_CUTOVER = "confirm_external_cutover"
    ROLLBACK = "rollback"
    RESTORE_CHECKPOINT = "restore_checkpoint"
    CLEANUP = "cleanup"
    RECHECK = "recheck"
    RELEASE = "release"


class MigrationEndpointRef(BaseModel):
    """Canonical source or destination selected by an operator."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    cluster_id: str = Field(..., min_length=1, max_length=255)
    project: str = Field(
        "default", pattern=LXD_NAME_PATTERN, description=f"LXD or Incus project. {LXD_NAME_DESCRIPTION}"
    )
    resource_name: Optional[str] = Field(None, pattern=LXD_NAME_PATTERN)
    member: Optional[str] = Field(None, min_length=1, max_length=255)
    cluster_fingerprint: Optional[str] = Field(None, min_length=16, max_length=128)


class MigrationMappings(BaseModel):
    """Explicit infrastructure mappings; values are names, never raw daemon configuration."""

    model_config = ConfigDict(extra="forbid")

    storage_pools: Dict[MappingName, MappingName] = Field(default_factory=dict, max_length=256)
    networks: Dict[MappingName, MappingName] = Field(default_factory=dict, max_length=256)
    profiles: Dict[MappingName, MappingName] = Field(default_factory=dict, max_length=256)
    volumes: Dict[MappingName, MappingName] = Field(default_factory=dict, max_length=1024)
    members: Dict[MappingName, MappingName] = Field(default_factory=dict, max_length=256)
    external_dependencies: Dict[MappingName, MappingName] = Field(
        default_factory=dict, max_length=256
    )


class MigrationPlanCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: MigrationScope
    intent: MigrationIntent = MigrationIntent.MOVE
    source: MigrationEndpointRef
    target: MigrationEndpointRef
    retention_hours: int = Field(24, ge=1, le=168)
    checkpoint_requested: bool = False
    external_cutover_required: bool = False
    mappings: MigrationMappings = Field(default_factory=MigrationMappings)
    # Only an Admin may send it, and only for a source whose owner the bindings cannot
    # name: `default` on a cluster several organizations hold (services/migrations/planning.py).
    source_organization_id: Optional[int] = Field(
        None,
        ge=1,
        description="The organization that owns an ambiguous default source. Admins only.",
    )

    @model_validator(mode="after")
    def _validate_resource_shape(self):
        if self.scope == MigrationScope.INSTANCE and not self.source.resource_name:
            raise ValueError("An instance migration requires source.resource_name")
        if self.scope == MigrationScope.PROJECT and self.source.resource_name is not None:
            raise ValueError("A project migration cannot set source.resource_name")
        if self.scope == MigrationScope.PROJECT and self.target.resource_name is not None:
            raise ValueError("A project migration cannot set target.resource_name")
        return self


class MigrationPlanPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: Optional[MigrationEndpointRef] = None
    retention_hours: Optional[int] = Field(None, ge=1, le=168)
    checkpoint_requested: Optional[bool] = None
    external_cutover_required: Optional[bool] = None
    mappings: Optional[MigrationMappings] = None

    @model_validator(mode="after")
    def _require_a_change(self):
        if not self.model_fields_set:
            raise ValueError("At least one plan field must be provided")
        return self


class MigrationExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(..., min_length=8, max_length=1000)
    acknowledgements: List[Acknowledgement] = Field(default_factory=list, max_length=32)


class MigrationActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_revision: int = Field(..., ge=1)
    reason: str = Field(..., min_length=8, max_length=1000)


class MigrationDestructiveActionRequest(MigrationActionRequest):
    confirmation: str = Field(..., min_length=1, max_length=255)


class MigrationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1, max_length=64)
    severity: MigrationIssueSeverity
    message: str = Field(..., min_length=1, max_length=2000)
    remediation: Optional[str] = Field(None, max_length=2000)
    resource_key: Optional[str] = Field(None, max_length=512)


class MigrationCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1, max_length=64)
    supported: bool
    reason: Optional[str] = Field(None, max_length=2000)


class MigrationPlannedResource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_key: str = Field(..., min_length=1, max_length=512)
    resource_type: str = Field(..., min_length=1, max_length=64)
    # Live status at planning time. A running source is stopped for the transfer.
    source_status: Optional[str] = Field(None, max_length=32)
    source: Dict[str, JsonValue] = Field(default_factory=dict)
    target: Dict[str, JsonValue] = Field(default_factory=dict)
    dependency_keys: List[str] = Field(default_factory=list, max_length=1024)


class MigrationPlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    plan_id: str
    revision: int
    scope: MigrationScope
    intent: MigrationIntent
    status: MigrationPlanStatus
    source: MigrationEndpointRef
    target: MigrationEndpointRef
    strategy: Optional[str] = Field(None, max_length=64)
    downtime_class: MigrationDowntimeClass = MigrationDowntimeClass.UNKNOWN
    retention_hours: int = Field(..., ge=1, le=168)
    checkpoint_requested: bool
    external_cutover_required: bool
    capabilities: List[MigrationCapability] = Field(default_factory=list)
    resources: List[MigrationPlannedResource] = Field(default_factory=list)
    mappings: MigrationMappings = Field(default_factory=MigrationMappings)
    blockers: List[MigrationIssue] = Field(default_factory=list)
    warnings: List[MigrationIssue] = Field(default_factory=list)
    rollback_contract: Dict[str, JsonValue] = Field(default_factory=dict)
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class MigrationResourceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    resource_id: str
    resource_key: str
    resource_type: str
    ordinal: int = Field(..., ge=0)
    state: MigrationResourceState
    source: Dict[str, JsonValue] = Field(default_factory=dict)
    target: Dict[str, JsonValue] = Field(default_factory=dict)
    dependency_keys: List[str] = Field(default_factory=list)
    operation_refs: List[Dict[str, JsonValue]] = Field(default_factory=list)
    progress: Dict[str, JsonValue] = Field(default_factory=dict)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    updated_at: datetime
    finished_at: Optional[datetime] = None


class MigrationTimelineEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(..., ge=0)
    phase: MigrationJobPhase
    message: str = Field(..., min_length=1, max_length=2000)
    created_at: datetime


class MigrationJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    job_id: str
    plan_id: str
    plan_revision: int = Field(..., ge=1)
    revision: int = Field(..., ge=1)
    scope: MigrationScope
    intent: MigrationIntent
    source: MigrationEndpointRef
    target: MigrationEndpointRef
    strategy: str
    downtime_class: MigrationDowntimeClass
    phase: MigrationJobPhase
    status: MigrationJobStatus
    retention_hours: int = Field(..., ge=1, le=168)
    retention_deadline: Optional[datetime] = None
    external_cutover_required: bool
    external_cutover_confirmed_at: Optional[datetime] = None
    resources: List[MigrationResourceResponse] = Field(default_factory=list)
    timeline: List[MigrationTimelineEvent] = Field(default_factory=list)
    available_actions: List[MigrationActionName] = Field(default_factory=list)
    safe_result: Optional[Dict[str, JsonValue]] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class MigrationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[MigrationJobResponse] = Field(default_factory=list)
    total: int = Field(..., ge=0)
    limit: int = Field(..., ge=1)
    offset: int = Field(..., ge=0)
    has_more: bool
    # Exact job counts of the console's three views, whatever the page or view asked for.
    counts: Optional[Dict[str, int]] = None


class MigrationPlanListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[MigrationPlanResponse] = Field(default_factory=list)
    total: int = Field(..., ge=0)
    limit: int = Field(..., ge=1)
    offset: int = Field(..., ge=0)
    has_more: bool


class MigrationPhaseDurationAggregate(BaseModel):
    """Bounded historical timing derived from durable phase-transition audit rows."""

    model_config = ConfigDict(extra="forbid")

    sample_count: int = Field(..., ge=0)
    average_seconds: Optional[float] = Field(None, ge=0)
    maximum_seconds: Optional[float] = Field(None, ge=0)


class MigrationUnavailableMetric(BaseModel):
    """Explicit marker for telemetry that the durable engine does not yet record."""

    model_config = ConfigDict(extra="forbid")

    available: Literal[False] = False
    reason: str = Field(..., min_length=1, max_length=500)


class MigrationTransferMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resources_total: int = Field(..., ge=0)
    resources_by_state: Dict[str, int] = Field(default_factory=dict)
    byte_progress: MigrationUnavailableMetric


class MigrationReconciliationMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events_total: int = Field(..., ge=0)
    jobs_total: int = Field(..., ge=0)


class MigrationRollbackMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completed_jobs: int = Field(..., ge=0)
    move_jobs: int = Field(..., ge=0)
    completion_rate: float = Field(..., ge=0, le=1)


class MigrationCleanupOverdueMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jobs_total: int = Field(..., ge=0)
    blocked_jobs: int = Field(..., ge=0)


class MigrationAuthorizationDenialMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: Literal[True] = True
    denied_requests_total: int = Field(..., ge=0)
    coverage: str = Field(..., min_length=1, max_length=500)


class MigrationOrphanDetectionMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    control_plane_available: Literal[True] = True
    orphaned_lock_rows: int = Field(..., ge=0)
    daemon_resources: MigrationUnavailableMetric


class MigrationOperationalMetricsResponse(BaseModel):
    """Low-cardinality operational aggregates with no tenant or resource labels."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    jobs_total: int = Field(..., ge=0)
    jobs_by_phase: Dict[str, int] = Field(default_factory=dict)
    jobs_by_outcome: Dict[str, int] = Field(default_factory=dict)
    phase_duration_seconds: Dict[str, MigrationPhaseDurationAggregate] = Field(
        default_factory=dict
    )
    transfer_progress: MigrationTransferMetrics
    reconciliation: MigrationReconciliationMetrics
    rollback: MigrationRollbackMetrics
    cleanup_overdue: MigrationCleanupOverdueMetrics
    authorization_denials: MigrationAuthorizationDenialMetrics
    orphan_detection: MigrationOrphanDetectionMetrics
