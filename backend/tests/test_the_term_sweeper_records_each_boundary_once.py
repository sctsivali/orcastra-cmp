"""Each paid-term boundary is audited once, however many workers sweep it.

The grant query enforces the dates on every request; the sweeper is what writes "this buyer's
access became read-only" and "this buyer's access ended" into the trail at the moment it happens,
and clears the lists cached under the old grant.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.services import tenant_term_sweeper
from tests._sqlite_session import AsyncSessionFacade

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")


class _Maker:
    def __init__(self, engine):
        self.engine = engine

    def __call__(self):
        return self

    async def __aenter__(self):
        self.db = AsyncSessionFacade(Session(self.engine, expire_on_commit=False))
        return self.db

    async def __aexit__(self, *_):
        self.db.sync.close()


@pytest.fixture
def world(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE tenant_access_policies (id INTEGER PRIMARY KEY, organization_id INTEGER, name TEXT, "
            "source TEXT, term_ended_at DATETIME, access_ends_at DATETIME, term_phase_recorded TEXT, "
            "updated_at DATETIME DEFAULT '2026-01-01 00:00:00')"
        ))
        conn.execute(text(
            "CREATE TABLE tenant_policy_rules (id INTEGER PRIMARY KEY, policy_id INTEGER, cluster_id TEXT, "
            "project_name TEXT, instance_name TEXT, permissions TEXT, created_at DATETIME)"
        ))
        conn.execute(text(
            "INSERT INTO tenant_access_policies (id, organization_id, name, source, term_ended_at, "
            "access_ends_at, term_phase_recorded) VALUES "
            "(1, 5, 'auto:shop', 'orcahub', :t1, :a1, NULL), "
            "(2, 5, 'auto:old', 'orcahub', :t2, :a2, 'read_only'), "
            "(3, 5, 'auto:paid', 'orcahub', NULL, NULL, NULL)"
        ), {
            "t1": _iso(NOW - timedelta(hours=1)), "a1": _iso(NOW + timedelta(days=29)),
            "t2": _iso(NOW - timedelta(days=31)), "a2": _iso(NOW - timedelta(minutes=1)),
        })
        conn.execute(text(
            "INSERT INTO tenant_policy_rules VALUES (11, 1, 'nuc2', 'shop', '*', '[]', '2026-01-01'), "
            "(12, 2, 'nuc2', 'old', '*', '[]', '2026-01-01')"
        ))
    audits: List[Dict[str, Any]] = []
    cleared: List[str] = []
    monkeypatch.setattr(tenant_term_sweeper.activity_logger, "log_activity", lambda **kw: audits.append(kw))

    class _Cache:
        def invalidate_after_access_change(self):
            cleared.append("access")

    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: _Cache())
    return {"maker": _Maker(engine), "audits": audits, "cleared": cleared, "engine": engine}


async def test_each_boundary_is_recorded_once(world) -> None:
    moved = await tenant_term_sweeper.sweep(world["maker"], now=NOW)
    again = await tenant_term_sweeper.sweep(world["maker"], now=NOW)

    assert sorted(moved) == [(1, "read_only"), (2, "ended")]
    assert again == [], "a second sweep, or a second worker, records nothing new"
    assert sorted((a["action"], a["target_project"]) for a in world["audits"]) == [
        ("tenant.access.expire", "old"),
        ("tenant.access.read_only", "shop"),
    ]
    assert world["cleared"] == ["access"], "lists built under the old grant are cleared once"


async def test_a_policy_without_dates_is_never_touched(world) -> None:
    await tenant_term_sweeper.sweep(world["maker"], now=NOW)
    with world["engine"].connect() as conn:
        recorded = conn.execute(text("SELECT term_phase_recorded FROM tenant_access_policies WHERE id = 3")).scalar()
    assert recorded is None


async def test_recording_a_boundary_is_not_an_edit_of_the_policy(world) -> None:
    await tenant_term_sweeper.sweep(world["maker"], now=NOW)
    with world["engine"].connect() as conn:
        stamps = conn.execute(text("SELECT DISTINCT updated_at FROM tenant_access_policies")).scalars().all()
    assert stamps == ["2026-01-01 00:00:00"]
