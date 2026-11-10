"""The wire shape of an operator snapshot schedule.

Kept out of models/schemas.py, which is large and imported early enough that additions there risk
import cycles.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.core.lxd_names import LXD_NAME_DESCRIPTION, LXD_NAME_PATTERN
from app.services.snapshots import recurrence
from app.services.snapshots.limits import (
    DEFAULT_SCHEDULE_EXPIRY_DAYS,
    MAX_SCHEDULE_EXPIRY_DAYS,
    MAX_SCHEDULE_KEEP_LAST,
    MAX_SCHEDULE_LABEL,
)

Frequency = Literal["every_6_hours", "every_12_hours", "daily", "weekly"]


class _ScheduleShape(BaseModel):
    """The recurrence and retention rules both create and edit have to satisfy."""

    @model_validator(mode="after")
    def _check_recurrence(self):
        frequency = getattr(self, "frequency", None)
        if frequency is None:
            return self
        try:
            recurrence.validate(
                frequency,
                getattr(self, "hour_utc", 0),
                getattr(self, "minute_utc", 0),
                getattr(self, "weekday", None),
            )
        except recurrence.RecurrenceError as exc:
            raise ValueError(str(exc)) from exc

        keep_last = getattr(self, "keep_last", None)
        expiry_days = getattr(self, "expiry_days", None)
        if keep_last is None or expiry_days is None:
            return self
        # The two bounds can contradict each other. Weekly, keeping 3 and expiring after 7 days,
        # each snapshot expires as the next is taken, so the schedule only ever holds one.
        span_hours = recurrence.period_hours(frequency) * keep_last
        if span_hours > expiry_days * 24:
            raise ValueError(
                f"Keeping {keep_last} snapshots {recurrence.describe(frequency, getattr(self, 'hour_utc', 0), getattr(self, 'minute_utc', 0), getattr(self, 'weekday', None))} "
                f"spans {span_hours // 24} days, but each one expires after {expiry_days}. "
                "Keep fewer, run more often, or expire later."
            )
        return self


class SnapshotScheduleCreateRequest(_ScheduleShape):
    host: str = Field(..., pattern=LXD_NAME_PATTERN, description=f"Cluster. {LXD_NAME_DESCRIPTION}")
    project: str = Field("default", pattern=LXD_NAME_PATTERN, description="LXD project")
    instance_name: str = Field(..., pattern=LXD_NAME_PATTERN, description="Instance to snapshot")
    label: str = Field(
        ...,
        min_length=1,
        max_length=MAX_SCHEDULE_LABEL,
        description="Short human label, folded into the generated ops- name",
    )
    frequency: Frequency = Field(..., description="How often this runs")
    hour_utc: int = Field(..., ge=0, le=23, description="Anchor hour, UTC")
    minute_utc: Literal[0, 15, 30, 45] = Field(0, description="Anchor minute, UTC")
    weekday: Optional[int] = Field(
        None, ge=0, le=6, description="0 Monday to 6 Sunday. Weekly only"
    )
    keep_last: int = Field(
        3,
        ge=1,
        le=MAX_SCHEDULE_KEEP_LAST,
        description=(
            "How many of this schedule's snapshots to keep. Capped below the per-instance limit "
            "of 5 so an operator can always still take one by hand"
        ),
    )
    expiry_days: int = Field(DEFAULT_SCHEDULE_EXPIRY_DAYS, ge=1, le=MAX_SCHEDULE_EXPIRY_DAYS)


class SnapshotScheduleUpdateRequest(_ScheduleShape):
    """Fields to change on a schedule. Omitted fields keep their value.

    A recurrence is edited as a whole: changing when a schedule runs means sending `frequency`,
    `hour_utc` and `minute_utc` together, plus `weekday` for a weekly schedule, so the validator
    can check the combination.
    """

    label: Optional[str] = Field(None, min_length=1, max_length=MAX_SCHEDULE_LABEL)
    frequency: Optional[Frequency] = None
    hour_utc: Optional[int] = Field(None, ge=0, le=23)
    minute_utc: Optional[Literal[0, 15, 30, 45]] = None
    weekday: Optional[int] = Field(None, ge=0, le=6)
    keep_last: Optional[int] = Field(None, ge=1, le=MAX_SCHEDULE_KEEP_LAST)
    expiry_days: Optional[int] = Field(None, ge=1, le=MAX_SCHEDULE_EXPIRY_DAYS)

    @model_validator(mode="after")
    def _recurrence_moves_together(self):
        parts = (self.frequency, self.hour_utc, self.minute_utc)
        if any(p is not None for p in parts) and self.frequency is None:
            raise ValueError(
                "Changing when a schedule runs means sending frequency, hour_utc and minute_utc "
                "together, plus weekday for a weekly schedule."
            )
        return self


class SnapshotScheduleInfo(BaseModel):
    """One schedule, as an operator sees it.

    `state` is the stored active or paused state, and a failing schedule stays active. Read
    `consecutive_failures` to tell a failing schedule apart.
    """

    id: int
    host: str
    project: str
    instance_name: str
    label: str
    frequency: Frequency
    hour_utc: int
    minute_utc: int
    weekday: Optional[int]
    summary: str = Field(..., description="One sentence naming when this fires")
    keep_last: int
    expiry_days: int
    state: Literal["active", "paused"]
    paused_reason: Optional[str]
    next_run_at: Optional[str]
    last_run_at: Optional[str]
    last_snapshot_name: Optional[str]
    last_error: Optional[str]
    consecutive_failures: int
    missed_runs: int
    created_by: str
    created_at: Optional[str]
    updated_at: Optional[str]


class SnapshotScheduleListResponse(BaseModel):
    total: int
    items: List[SnapshotScheduleInfo] = Field(default_factory=list)


class SnapshotScheduleActionResponse(BaseModel):
    success: bool
    message: str
    schedule: Optional[SnapshotScheduleInfo] = None
