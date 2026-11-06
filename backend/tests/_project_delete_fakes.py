"""Durable project-delete follow-ups on SQLite, with the announcement recorded instead of sent."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, FrozenSet, List, Tuple

from tests._sqlite_session import sqlite_session


class SessionMaker:
    """`async with session_maker() as db` over one shared SQLite facade, like the app's maker."""

    def __init__(self, db: Any) -> None:
        self.db = db

    def __call__(self) -> "SessionMaker":
        return self

    async def __aenter__(self) -> Any:
        return self.db

    async def __aexit__(self, *_: Any) -> None:
        self.db.sync.rollback()


@dataclass
class DurableDeletes:
    db: Any
    maker: SessionMaker
    announced: List[Tuple[Any, bool]] = field(default_factory=list)
    # The organizations each announcement named as having held the project, in the same order.
    reached: List[FrozenSet[int]] = field(default_factory=list)
    cleared: List[str] = field(default_factory=list)

    def rows(self) -> List[Any]:
        from app.models.project_delete_models import ProjectDeleteFollowup

        self.db.sync.expire_all()
        return list(self.db.sync.query(ProjectDeleteFollowup).order_by(ProjectDeleteFollowup.id))


def durable_deletes(monkeypatch, *, record_announcements: bool = True) -> DurableDeletes:
    from app.models.project_delete_models import ProjectDeleteFollowup
    from app.services import project_delete_announce, project_delete_store

    db = sqlite_session(ProjectDeleteFollowup)
    fakes = DurableDeletes(db=db, maker=SessionMaker(db))
    monkeypatch.setattr(project_delete_store, "session_maker", fakes.maker)
    monkeypatch.setattr(project_delete_announce, "clear_caches", fakes.cleared.append)
    if record_announcements:

        def record(followup: Any, *, settled_later: bool, reached_organizations=()):
            fakes.announced.append((followup, settled_later))
            fakes.reached.append(frozenset(reached_organizations))
            return asyncio.sleep(0)

        monkeypatch.setattr(project_delete_announce, "announce_and_finish", record)
    return fakes
