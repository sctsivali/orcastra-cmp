"""Several events on one outbox channel in one transaction get consecutive sequence numbers.

A project relocation enqueues one webhook per instance in its retention transaction. With
autoflush off, each would otherwise read the same max sequence and collide.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import JSON, BigInteger, Integer, MetaData, select
from sqlalchemy.dialects.postgresql import JSONB

from app.models.db_models import Organization
from app.models.migration_models import MigrationAuditOutbox, MigrationJob, MigrationPlan
from app.services.migrations import repository
from tests._sqlite_session import AsyncSessionFacade, sqlite_session


@pytest.fixture
def db() -> Iterator[AsyncSessionFacade]:
    facade = sqlite_session()
    metadata = MetaData()
    for model in (Organization, MigrationPlan, MigrationJob, MigrationAuditOutbox):
        table = model.__table__.to_metadata(metadata)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
            if column.primary_key and isinstance(column.type, BigInteger):
                column.type = Integer()  # SQLite autoincrements INTEGER PRIMARY KEY only.
            if column.server_default is not None and "::" in str(getattr(column.server_default, "arg", "")):
                column.server_default = None
        table.indexes.clear()
    metadata.create_all(facade.sync.get_bind())
    facade.sync.autoflush = False  # As the application sessions run (core/database.py).
    try:
        yield facade
    finally:
        facade.sync.close()
        facade.sync.get_bind().dispose()


async def test_events_on_one_channel_in_one_transaction_do_not_collide(db: AsyncSessionFacade) -> None:
    actor = {"email": "ops@example.test"}
    for name in ("web-1", "web-2", "web-3"):
        await repository.enqueue_event(
            db, job_id="job-1", event_type="orcastra.instance.created.v1", phase="verifying",
            message=name, actor=actor, channel="webhook", event_id=f"event-{name}",
        )
    await repository.enqueue_event(
        db, job_id="job-1", event_type="migration.phase_changed", phase="verifying", message="x", actor=actor
    )
    db.sync.commit()

    rows = db.sync.execute(
        select(MigrationAuditOutbox.channel, MigrationAuditOutbox.sequence).order_by(
            MigrationAuditOutbox.channel, MigrationAuditOutbox.sequence
        )
    ).all()
    assert [tuple(row) for row in rows] == [("audit", 0), ("webhook", 0), ("webhook", 1), ("webhook", 2)]
