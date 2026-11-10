"""The fleet snapshot list answers from its cache, stale included, while one read refreshes it.

Nothing polls the list, so with a plain TTL nearly every visit would miss and pay the whole
fan-out. It goes through swr.serve like the other fleet lists: a hit inside the soft TTL, a stale
answer for up to SWR_MAX_STALE_SECONDS past it with one background read, and `fresh=true` still
reads the clusters before it answers.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.core import swr
from app.core.cache_envelope import unwrap, wrap
from tests.test_a_list_read_that_raced_an_invalidation_is_not_cached import (
    _SnapshotService,
    _manager,
    _snapshot_client,
)


def _age_entries(manager, seconds: float) -> int:
    aged = 0
    for key, (value, expires) in list(manager.memory_cache.cache.items()):
        if "snapshots_fleet:list" not in key:
            continue
        entry = unwrap(value)
        manager.memory_cache.cache[key] = (
            wrap(entry.value, stored_at=time.time() - seconds, build_started_at=time.time() - seconds, fresh_for=entry.fresh_for),
            expires,
        )
        aged += 1
    return aged


def test_a_second_visit_inside_the_ttl_reads_nothing(monkeypatch):
    manager = _manager()
    service = _SnapshotService(manager, invalidate_during_read=False)
    with _snapshot_client(manager, service, monkeypatch) as client:
        assert client.get("/snapshots").status_code == 200
        assert client.get("/snapshots").status_code == 200
    assert service.reads == 1


def test_a_stale_entry_answers_at_once_and_one_read_replaces_it(monkeypatch):
    manager = _manager()
    service = _SnapshotService(manager, invalidate_during_read=False)
    with _snapshot_client(manager, service, monkeypatch) as client:
        assert client.get("/snapshots").status_code == 200
        assert _age_entries(manager, 60) == 1
        assert client.get("/snapshots").status_code == 200
        deadline = time.monotonic() + 5
        while service.reads < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
    assert service.reads == 2, "no background read replaced the stale entry"


def test_an_entry_past_the_stale_window_is_a_miss(monkeypatch):
    manager = _manager()
    service = _SnapshotService(manager, invalidate_during_read=False)
    with _snapshot_client(manager, service, monkeypatch) as client:
        client.get("/snapshots")
        _age_entries(manager, 30 + swr.SWR_MAX_STALE_SECONDS + 5)
        client.get("/snapshots")
    assert service.reads == 2


def test_fresh_always_reads_the_clusters(monkeypatch):
    manager = _manager()
    service = _SnapshotService(manager, invalidate_during_read=False)
    with _snapshot_client(manager, service, monkeypatch) as client:
        client.get("/snapshots")
        client.get("/snapshots?fresh=true")
    assert service.reads == 2


def test_a_reachability_change_clears_the_list(monkeypatch):
    manager = _manager()
    service = _SnapshotService(manager, invalidate_during_read=False)
    with _snapshot_client(manager, service, monkeypatch) as client:
        client.get("/snapshots")
        manager.invalidate_cluster_reachability("mc-lab")
        client.get("/snapshots")
    assert service.reads == 2
