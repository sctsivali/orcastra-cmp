"""load_metrics_history(since=...) reads only the rows collected inside the window.

The history store refills the admin chart from Postgres with the last 24 hours. Without a lower
bound the query returned the newest N rows whatever their age, so a collector that had been down
for a day would have refilled the chart with points the store's own retention drops again.
"""

import asyncio
from datetime import datetime, timezone

from sqlalchemy.dialects import postgresql

from app.services import metrics_history_service

SINCE = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


class _Session:
    def __init__(self, seen: list) -> None:
        self._seen = seen

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *_exc) -> None:
        return None

    async def execute(self, statement):
        self._seen.append(statement)

        class _Rows:
            def scalars(self):
                return self

            def all(self):
                return []

        return _Rows()


def _query(monkeypatch, **kwargs):
    seen: list = []
    monkeypatch.setattr(metrics_history_service, "async_session_maker", lambda: _Session(seen))
    assert asyncio.run(metrics_history_service.load_metrics_history(**kwargs)) == []
    (statement,) = seen
    return statement.compile(dialect=postgresql.dialect())


def test_since_bounds_the_query_by_collection_time(monkeypatch):
    compiled = _query(monkeypatch, points=8640, scope="all", since=SINCE)

    assert "metrics_snapshots.collected_at >= " in str(compiled)
    assert SINCE in compiled.params.values()
    assert 8640 in compiled.params.values()


def test_without_since_the_query_is_unchanged(monkeypatch):
    compiled = _query(monkeypatch, points=60, scope="all")

    assert "collected_at >=" not in str(compiled)
    assert "ORDER BY metrics_snapshots.collected_at DESC" in str(compiled)
    assert not any(isinstance(value, datetime) for value in compiled.params.values())


def test_since_is_an_optional_keyword_so_old_callers_keep_working():
    import inspect

    parameter = inspect.signature(metrics_history_service.load_metrics_history).parameters["since"]

    assert parameter.default is None
