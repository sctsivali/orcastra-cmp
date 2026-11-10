"""A hand-rolled session for the task journal tests: records compiled SQL and parameters.

The repository uses raw `text()` SQL, so tests pin the compiled text and its parameters. A
responder decides what each statement returns, ex; the row a `SELECT ... FOR UPDATE` finds or the
rowcount an `UPDATE` reports.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.services.task_journal.repository import ActorFields, CorrelationKey, OperationFrame


class FakeResult:
    def __init__(self, rows: Optional[List[Any]] = None, rowcount: int = 0, scalar: Any = None):
        self._rows = rows or []
        self.rowcount = rowcount
        self._scalar = scalar

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> List[Any]:
        return list(self._rows)

    def scalar(self) -> Any:
        return self._scalar

    def mappings(self) -> "FakeResult":
        return self


Responder = Callable[[str, Any], Optional[FakeResult]]


class FakeDB:
    def __init__(self, respond: Optional[Responder] = None):
        self.calls: List[Tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self._respond = respond

    async def execute(self, statement: Any, params: Any = None) -> FakeResult:
        sql = str(statement)
        self.calls.append((sql, params))
        if self._respond is not None:
            answer = self._respond(sql, params)
            if answer is not None:
                return answer
        return FakeResult()

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def __aenter__(self) -> "FakeDB":
        return self

    async def __aexit__(self, *_a: Any) -> bool:
        return False

    @property
    def statements(self) -> List[str]:
        return [sql for sql, _ in self.calls]


class FakeSessionMaker:
    """`maker()` is an async context manager yielding one FakeDB per batch."""

    def __init__(self, respond: Optional[Responder] = None, fail_times: int = 0):
        self.sessions: List[FakeDB] = []
        self.fail_times = fail_times
        self.opened = 0
        self._respond = respond

    def __call__(self) -> FakeDB:
        self.opened += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("database unavailable")
        db = FakeDB(self._respond)
        self.sessions.append(db)
        return db

    @property
    def statements(self) -> List[str]:
        return [sql for db in self.sessions for sql in db.statements]


def frame(
    status: str = "running",
    *,
    operation_id: str = "op-7",
    cluster_id: str = "nuc1",
    project: str = "default",
    resource_name: Optional[str] = "web-01",
    action: Optional[str] = "instance.start",
    lxd_created_at: Optional[datetime] = None,
    lxd_updated_at: Optional[datetime] = None,
    progress: Optional[str] = None,
    error: Optional[str] = None,
) -> OperationFrame:
    return OperationFrame(
        cluster_id=cluster_id,
        operation_id=operation_id,
        operation_class="task",
        description="Starting instance",
        status=status,
        status_code=103,
        progress=progress,
        error=error,
        may_cancel=True,
        project=project,
        resource_type="instance" if resource_name else None,
        resource_name=resource_name,
        action=action,
        lxd_created_at=lxd_created_at,
        lxd_updated_at=lxd_updated_at,
        metadata={},
        requestor=None,
    )


def actor(**overrides: Any) -> ActorFields:
    fields: Dict[str, Any] = {
        "actor_email": "ops@example.test",
        "actor_role": "partner",
        "actor_kind": "user",
        "api_key_id": None,
        "source": "dashboard",
        "request_id": "req-1",
        "target_organization_id": None,
        "target_organization_slug": None,
    }
    fields.update(overrides)
    return ActorFields(**fields)


def key(**overrides: Any) -> CorrelationKey:
    fields: Dict[str, Any] = {
        "cluster_id": "nuc1",
        "project": "default",
        "resource_type": "instance",
        "resource_name": "web-01",
        "action": "instance.start",
    }
    fields.update(overrides)
    return CorrelationKey(**fields)


def audit_record(
    action: str = "instance.start",
    *,
    result: str = "success",
    user_id: Optional[str] = "ops@example.test",
    role: Optional[str] = "partner",
    host: Optional[str] = "nuc1",
    project: Optional[str] = "default",
    target_type: str = "instance",
    target_id: str = "web-01",
    details: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
    duration_ms: Optional[float] = None,
    actor_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The dict `log_activity` builds, plus the `actor_extra` the sink call adds."""
    return {
        "@timestamp": datetime.now(timezone.utc).isoformat(),
        "log_type": "audit",
        "event_id": "evt_1",
        "request_id": "req-1",
        "service": "orcastra-backend",
        "version": "test",
        "action": action,
        "category": "lxd_infra",
        "severity": "medium",
        "actor": {
            "user_id": user_id,
            "user_type": role,
            "role": role,
            "groups": [],
            "organizations": [],
            "session_id": None,
        },
        "target": {
            "type": target_type,
            "id": target_id,
            "host": host,
            "project": project,
            "organization_id": None,
            "organization_slug": None,
        },
        "result": result,
        "error_code": None,
        "error_message": error_message,
        "details": details,
        "metadata": {"duration_ms": duration_ms, "before": None, "after": None}
        if duration_ms
        else None,
        "actor_extra": actor_extra or {},
    }
