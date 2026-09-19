"""Doubles for the snapshot scheduler.

Dataclasses rather than mocks, for the reason _snapshot_fakes.py states about itself: an assertion
about what reached the daemon has to be an assertion about a recorded call, and a mock that answers
anything makes that impossible. The FakeDB here discriminates on the statement, unlike the one the
webhook dispatcher's tests use, because fire-time authorization asks Postgres more than one thing.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional

from tests._snapshot_fakes import HOST, INSTANCE, PROJECT

UTC = timezone.utc


@dataclass
class FakeSchedule:
    """Every column the runner and the state machine read, with workable defaults."""

    id: int = 12
    cluster_id: str = HOST
    project: str = PROJECT
    instance_name: str = INSTANCE
    instance_uuid: Optional[str] = None
    cluster_fingerprint: Optional[str] = None
    label: str = "nightly"
    frequency: str = "daily"
    hour_utc: int = 3
    minute_utc: int = 0
    weekday: Optional[int] = None
    keep_last: int = 3
    expiry_days: int = 7
    owner_email: str = "ops@sivali.co.id"
    organization_id: Optional[int] = 6
    state: str = "active"
    paused_reason: Optional[str] = None
    next_run_at: datetime = field(default_factory=lambda: datetime(2026, 9, 9, 3, 0, tzinfo=UTC))
    due_slot: Optional[datetime] = None
    run_state: str = "idle"
    locked_by: Optional[str] = None
    locked_at: Optional[datetime] = None
    last_run_at: Optional[datetime] = None
    last_run_slot: Optional[datetime] = None
    last_snapshot_name: Optional[str] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    missed_runs: int = 0
    updated_at: Optional[datetime] = None


class _Rows:
    def __init__(self, rows: List[Any]):
        self._rows = list(rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def scalars(self):
        # Match SQLAlchemy's ScalarResult: selecting one column returns the value,
        # not the one-element Row tuple. The standing-authority check compares the
        # resulting organization ids with ``schedule.organization_id``.
        return _Rows([row[0] if isinstance(row, tuple) and len(row) == 1 else row for row in self._rows])

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeDB:
    """Answers the org-binding query and records that it was asked.

    `bound` False is how a revoked organization is modelled: the row still names the cluster and
    the join no longer finds it, which is exactly the state a schedule outlives its grant into.
    """

    def __init__(self, *, bound: bool = True):
        self.bound = bound
        self.queries = 0

    async def execute(self, _stmt, *args, **kwargs):
        self.queries += 1
        if not self.bound:
            return _Rows([])
        sql = str(_stmt)
        if "organization_clusters.organization_id, organization_clusters.id" in sql:
            return _Rows([(6, 1)])
        return _Rows([(6,)])

    async def commit(self):
        return None

    async def rollback(self):
        return None


def snapshot_row(name: str, created_at: str) -> dict:
    return {"name": name, "created_at": created_at, "expires_at": None}


def ops_rows(schedule_id: int, count: int, label: str = "nightly") -> List[dict]:
    """`count` snapshots this schedule made, oldest first."""
    return [
        snapshot_row(f"ops-sch{schedule_id}-{label}-2026090{i}T030000Z", f"2026-09-0{i}T03:00:00Z")
        for i in range(1, count + 1)
    ]
