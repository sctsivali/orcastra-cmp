"""A real SQL session on in-memory SQLite, behind the AsyncSession calls handlers make.

Scripted fakes answer whatever the test author expected a query to return, so a missing WHERE
term reads the same as a present one. This runs the handler's own statements against real rows.
Only tables without Postgres-only column types can be created here (no JSONB, no ARRAY).

SQLite creates a Postgres partial unique index without its WHERE clause, which makes it a full
unique index, so such indexes are rebuilt below with the same predicate.
"""

from typing import Any, Iterable, List, Type

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import Base

_PARTIAL_INDEXES = {
    "cluster_registrations": [
        (
            "uq_cluster_registration_active",
            "CREATE UNIQUE INDEX uq_cluster_registration_active ON cluster_registrations (cluster_id) "
            "WHERE ended_at IS NULL",
        )
    ],
    "project_delete_followups": [
        (
            "uq_project_delete_followup_open",
            "CREATE UNIQUE INDEX uq_project_delete_followup_open ON project_delete_followups "
            "(cluster_id, project_name) WHERE state IN ('sending', 'pending')",
        )
    ],
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

    def begin_nested(self) -> Any:
        """A savepoint, as `async with db.begin_nested():` uses it."""
        nested = self.sync.begin_nested()

        class _Savepoint:
            async def __aenter__(self) -> Any:
                return nested

            async def __aexit__(self, exc_type: Any, _exc: Any, _tb: Any) -> bool:
                if exc_type is None:
                    nested.commit()
                else:
                    nested.rollback()
                return False

        return _Savepoint()

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


def with_json_tables(db: "AsyncSessionFacade", *models: Type[Any]) -> "AsyncSessionFacade":
    """Add tables whose Postgres JSONB columns SQLite cannot render, with those columns as JSON."""
    from sqlalchemy import JSON, BigInteger, Integer, MetaData
    from sqlalchemy.dialects.postgresql import JSONB

    copy = MetaData()
    pending = [model.__table__ for model in models]
    while pending:
        source = pending.pop()
        if source.name in copy.tables:
            continue
        table = source.to_metadata(copy)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
                # A JSONB default is a Postgres cast ('{}'::jsonb) SQLite cannot parse.
                column.server_default = None
            elif column.primary_key and isinstance(column.type, BigInteger):
                # SQLite assigns a row id only to an INTEGER PRIMARY KEY, never a BIGINT one.
                column.type = Integer()
        # A foreign key needs its target in the same metadata; existing tables are skipped below.
        pending.extend(fk.column.table for fk in source.foreign_keys)
    copy.create_all(db.sync.get_bind(), checkfirst=True)
    return db


def installation_rows(db: "AsyncSessionFacade", *claims: tuple) -> "AsyncSessionFacade":
    """The two key columns of extension_installations, one (api_key_id, previous_api_key_id) row each.

    The full model has Postgres-only columns (ARRAY, JSONB), so SQLite holds only the columns the
    claim and the current-credential lookups name (services/add_on_credential_edits.py).
    """
    db.sync.execute(text(
        "CREATE TABLE IF NOT EXISTS extension_installations "
        "(id INTEGER PRIMARY KEY, api_key_id TEXT NOT NULL UNIQUE, previous_api_key_id TEXT)"
    ))
    for current, previous in claims:
        db.sync.execute(
            text("INSERT INTO extension_installations (api_key_id, previous_api_key_id) VALUES (:c, :p)"),
            {"c": current, "p": previous},
        )
    db.sync.commit()
    return db


def org_tables() -> tuple:
    """Organization, the two tables its relationships load eagerly, the assignment table a member
    delete cascades to through the ORM, and the owner set every claimed organization has."""
    from app.models.db_models import (
        Organization,
        OrganizationCluster,
        OrganizationMember,
        OrganizationOwnerKey,
        TenantPolicyAssignment,
    )

    return (Organization, OrganizationMember, OrganizationCluster, TenantPolicyAssignment, OrganizationOwnerKey)
