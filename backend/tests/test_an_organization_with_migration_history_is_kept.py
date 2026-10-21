"""Deleting an organization that owns migration history answers 409 with the reason, never 500.

Migration plans and jobs keep their organization through RESTRICT foreign keys: a migration record
is audit history. The delete route went straight to the delete, so the database refused it and the
operator got a bare 500 (found tearing down the migration lab organization). It now counts the
history first and refuses with what is holding it, and any other foreign key refusal is a 409 too.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.routers import organizations
from app.services.organization_service import OrganizationService


class _Db:
    def __init__(self) -> None:
        self.rolled_back = False
        self.statements: list[Any] = []

    async def rollback(self) -> None:
        self.rolled_back = True

    async def scalar(self, statement: Any) -> int:
        self.statements.append(statement)
        return 0


def _service(*, history: tuple[int, int], delete: Any = True) -> type:
    class _Service:
        def __init__(self, _db: Any) -> None:
            pass

        async def get_organization(self, org_id: int) -> object:
            return object()

        async def migration_history(self, org_id: int) -> tuple[int, int]:
            return history

        async def delete_organization(self, org_id: int) -> bool:
            if isinstance(delete, Exception):
                raise delete
            return delete

    return _Service


@pytest.fixture(autouse=True)
def unguarded(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def guard(_db: Any, _org_id: int) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(organizations, "_guard_organization_write", guard)


async def test_migration_history_is_a_409_with_the_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(organizations, "OrganizationService", _service(history=(74, 120)))

    with pytest.raises(HTTPException) as caught:
        await organizations.delete_organization(464, user=object(), db=_Db())

    assert caught.value.status_code == 409
    assert "74 jobs, 120 plans" in caught.value.detail


async def test_any_other_foreign_key_refusal_is_a_409(monkeypatch: pytest.MonkeyPatch) -> None:
    refusal = IntegrityError("DELETE", {}, Exception("violates foreign key constraint"))
    monkeypatch.setattr(organizations, "OrganizationService", _service(history=(0, 0), delete=refusal))
    db = _Db()

    with pytest.raises(HTTPException) as caught:
        await organizations.delete_organization(7, user=object(), db=db)

    assert caught.value.status_code == 409
    assert db.rolled_back


async def test_an_organization_without_history_is_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(organizations, "OrganizationService", _service(history=(0, 0)))

    assert await organizations.delete_organization(7, user=object(), db=_Db()) is None


async def test_history_counts_both_sides_of_every_record() -> None:
    db = _Db()

    assert await OrganizationService(db).migration_history(5) == (0, 0)

    compiled = [str(statement.compile(compile_kwargs={"literal_binds": True})) for statement in db.statements]
    assert len(compiled) == 2
    for sql, table in zip(compiled, ("migration_jobs", "migration_plans")):
        assert table in sql
        assert "source_organization_id = 5" in sql and "target_organization_id = 5" in sql
