"""A real SQL session on in-memory SQLite, behind the AsyncSession calls handlers make.

Scripted fakes answer whatever the test author expected a query to return, so a missing WHERE
term reads the same as a present one. This runs the handler's own statements against real rows.
Only tables without Postgres-only column types can be created here (no JSONB, no ARRAY).

note: a Postgres partial unique index is created without its WHERE clause by SQLite, which makes
it a full unique index. Such indexes are rebuilt below with the same predicate.
"""

from typing import Any, Iterable, List, Type

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base

_PARTIAL_INDEXES = {
    "organization_clusters": [
        (
            "uq_org_cluster_null",
            "CREATE UNIQUE INDEX uq_org_cluster_null ON organization_clusters (cluster_id) "
            "WHERE organization_id IS NULL",
        )
    ],
}


class AsyncSessionFacade:
    """The awaited AsyncSession surface over a synchronous Session."""

    def __init__(self, session: Session) -> None:
        self.sync = session
        self.statements: List[Any] = []

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        self.statements.append(statement)
        return self.sync.execute(statement, *args, **kwargs)

    async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        self.statements.append(statement)
        return self.sync.scalar(statement, *args, **kwargs)

    async def commit(self) -> None:
        self.sync.commit()

    async def rollback(self) -> None:
        self.sync.rollback()

    async def flush(self) -> None:
        self.sync.flush()

    async def delete(self, instance: Any) -> None:
        self.sync.delete(instance)

    async def get(self, model: Any, ident: Any) -> Any:
        return self.sync.get(model, ident)

    async def refresh(self, instance: Any) -> None:
        self.sync.refresh(instance)

    async def close(self) -> None:
        self.sync.close()

    def add(self, instance: Any) -> None:
        self.sync.add(instance)

    def add_all(self, instances: Iterable[Any]) -> None:
        self.sync.add_all(instances)


def sqlite_session(*models: Type[Any]) -> AsyncSessionFacade:
    """A fresh in-memory database holding only the given models' tables."""
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    tables = [model.__table__ for model in models]
    Base.metadata.create_all(engine, tables=tables)
    with engine.begin() as conn:
        for table in tables:
            for name, ddl in _PARTIAL_INDEXES.get(table.name, []):
                conn.execute(text(f"DROP INDEX IF EXISTS {name}"))
                conn.execute(text(ddl))
    return AsyncSessionFacade(Session(engine, expire_on_commit=False))


def org_tables() -> tuple:
    """Organization, the two tables its relationships load eagerly, and the assignment table a
    member delete cascades to through the ORM."""
    from app.models.db_models import (
        Organization,
        OrganizationCluster,
        OrganizationMember,
        TenantPolicyAssignment,
    )

    return (Organization, OrganizationMember, OrganizationCluster, TenantPolicyAssignment)
