"""GET /monitoring/history: the same JSON as before the store changed, and the refill rules.

The endpoint now returns the store's encoded body instead of letting FastAPI serialize a list of
models, so the first tests pin the bytes against what `response_model=List[MetricsHistoryPoint]`
produced. The rest pin when the endpoint refills a scope: the previous release's key for every
scope, Postgres for the admin scope only and only when its history starts well inside the day,
nothing while Redis is failing, and one scoped collection when a scope has no point at all.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import List

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.blocking import BlockingPoolFull
from app.core.cache import CacheManager
from app.models.schemas import MetricsHistoryPoint
from app.routers import monitoring
from app.services import monitoring_history_store as store
from app.services.monitoring_history import history_key
from tests._fake_zset_redis import FakeRedis

ADMIN = "dashboard:scope=all"
TENANT = "dashboard:scope=org:7:acl:3"


def _sweep_point(seconds_ago: float, index: int) -> dict:
    """The shape build_point writes, with values a sweep can produce."""
    stamp = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return {
        "timestamp": stamp.isoformat(),
        "cpu_usage_percent": None if index == 0 else round(3.17 * index, 2),
        "memory_usage_percent": 41.07,
        "storage_usage_percent": 7.5,
        "network_rx_bytes": 0,
        "network_tx_bytes": 0,
        "network_rx_bytes_per_sec": None if index == 0 else 1234567.891 + index,
        "network_tx_bytes_per_sec": 0.0333 * index,
        "disk_io_bytes": 0,
        "running_instances": 12 + index,
        "nodes": {
            "cluster-a": {
                "cpu_usage_percent": 9.25,
                "memory_usage_percent": 0,
                "storage_usage_percent": 7.5,
                "network_rx_bytes_per_sec": None,
                "network_tx_bytes_per_sec": 512.5,
                "running_instances": 4,
                "gpu_total": 1,
                "gpu_used": 0,
            },
            "pusat-jakarta-ü": {"cpu_usage_percent": None, "running_instances": 0},
        },
    }


@pytest.fixture
def stored(monkeypatch):
    cache = CacheManager(redis_url=None)
    cache.redis_client = FakeRedis()
    monkeypatch.setattr(monitoring, "get_cache_manager", lambda: cache)

    async def no_postgres(**_kwargs):
        return []

    from app.services import metrics_history_service

    monkeypatch.setattr(metrics_history_service, "load_metrics_history", no_postgres)
    return cache


def _as_scope(monkeypatch, scope_key: str) -> None:
    monkeypatch.setattr(monitoring, "get_scope_cache_key", lambda _name: scope_key)


def _write(cache, scope_key: str, points: List[dict]) -> None:
    for point in points:
        assert asyncio.run(store.record_point(cache, history_key(scope_key), point))


def _history(**params):
    return asyncio.run(monitoring.get_metrics_history(lxd_service=None, **{"window": None, "points": None, **params}))


def _previous_body(points: List[dict]) -> bytes:
    """What the endpoint answered before: FastAPI serializing models through response_model."""
    app = FastAPI()

    @app.get("/history", response_model=List[MetricsHistoryPoint])
    async def history():
        return [MetricsHistoryPoint(**point) for point in points]

    return TestClient(app).get("/history").content


def test_the_body_is_byte_for_byte_what_response_model_produced(monkeypatch, stored):
    _as_scope(monkeypatch, ADMIN)
    points = [_sweep_point(30 * (9 - i), i) for i in range(10)]
    _write(stored, ADMIN, points)

    response = _history(window=3600)

    assert response.media_type == "application/json"
    assert response.body == _previous_body(points)


def test_an_exponent_float_is_the_same_number_even_where_its_text_differs(monkeypatch, stored):
    _as_scope(monkeypatch, ADMIN)
    points = [dict(_sweep_point(10, 1), network_tx_bytes_per_sec=1e-05)]
    _write(stored, ADMIN, points)

    assert json.loads(_history(window=300).body) == json.loads(_previous_body(points))


def test_the_openapi_contract_still_names_the_point_model():
    app = FastAPI()
    app.include_router(monitoring.router)

    schema = app.openapi()["paths"]["/monitoring/history"]["get"]["responses"]["200"]["content"]

    assert schema["application/json"]["schema"]["items"]["$ref"].endswith("/MetricsHistoryPoint")


def _spy(monkeypatch, name: str, result: int = 0) -> list:
    calls = []

    async def spy(cache, key):
        calls.append(key)
        return result

    monkeypatch.setattr(monitoring, name, spy)
    return calls


def test_every_scope_imports_its_previous_key_and_only_admin_reads_postgres(monkeypatch, stored):
    imports = _spy(monkeypatch, "import_previous_history")
    hydrates = _spy(monkeypatch, "hydrate_from_postgres")
    for scope in (ADMIN, TENANT):
        _write(stored, scope, [_sweep_point(30, 1)])
        _as_scope(monkeypatch, scope)
        _history(window=300)

    assert imports == [history_key(ADMIN), history_key(TENANT)]
    assert hydrates == [history_key(ADMIN)], "Postgres only holds the admin stream"


def test_a_full_day_of_admin_history_is_not_refilled(monkeypatch, stored):
    hydrates = _spy(monkeypatch, "hydrate_from_postgres")
    _as_scope(monkeypatch, ADMIN)
    _write(stored, ADMIN, [_sweep_point(24 * 3600 - 120, 1), _sweep_point(30, 2)])

    _history(window=300)

    assert hydrates == []


def test_a_refill_that_added_points_is_read_back_in_the_same_request(monkeypatch, stored):
    _as_scope(monkeypatch, ADMIN)
    _write(stored, ADMIN, [_sweep_point(30, 1)])

    async def refill(cache, key):
        return int(await store.record_point(cache, key, _sweep_point(90, 2)))

    monkeypatch.setattr(monitoring, "hydrate_from_postgres", refill)

    assert len(json.loads(_history(window=300).body)) == 2


def test_no_refill_is_attempted_while_redis_is_failing(monkeypatch, stored):
    imports = _spy(monkeypatch, "import_previous_history")
    hydrates = _spy(monkeypatch, "hydrate_from_postgres")
    _as_scope(monkeypatch, ADMIN)
    stored.redis_client.fail_with = ConnectionError("connection refused")
    _write(stored, ADMIN, [_sweep_point(30, 1)])

    response = _history(window=300)

    assert len(json.loads(response.body)) == 1, "this worker's buffer answers"
    assert imports == [] and hydrates == []


def test_an_empty_scope_collects_once_and_answers_what_that_collection_stored(monkeypatch, stored):
    _as_scope(monkeypatch, TENANT)
    collections = []

    async def collect(lxd_service, fresh):
        collections.append(fresh)
        await store.record_point(stored, history_key(TENANT), _sweep_point(1, 1))

    monkeypatch.setattr(monitoring, "get_monitoring_dashboard", collect)

    body = json.loads(_history(window=300).body)

    assert collections == [False]
    assert len(body) == 1


def test_an_empty_scope_whose_collection_fails_answers_an_empty_list(monkeypatch, stored):
    _as_scope(monkeypatch, TENANT)

    async def collect(lxd_service, fresh):
        raise RuntimeError("no cluster answered")

    monkeypatch.setattr(monitoring, "get_monitoring_dashboard", collect)

    assert _history(window=300).body == b"[]"


def test_a_refused_read_is_a_503_with_a_reason(monkeypatch, stored):
    _as_scope(monkeypatch, ADMIN)

    async def full(*_args, **_kwargs):
        raise BlockingPoolFull("monitoring.history.read", 64)

    monkeypatch.setattr(monitoring, "history_body", full)

    with pytest.raises(HTTPException) as refused:
        _history(window=300)

    assert refused.value.status_code == 503
    assert "retries" in refused.value.detail
