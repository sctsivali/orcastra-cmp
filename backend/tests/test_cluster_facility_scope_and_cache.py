from __future__ import annotations

import pytest

from app.core import cache as cache_module
from app.routers import cluster_registration


class _Cache:
    def __init__(self) -> None:
        self.generation = "before"
        self.writes: list[object] = []

    def get(self, _namespace: str, _key: str):
        return None

    def resource_list_generation(self, kind: str) -> str:
        assert kind == "clusters"
        return self.generation

    def set(self, _namespace: str, _key: str, value: object, ttl: int) -> None:
        assert ttl == 300
        self.writes.append(value)


class _Certificates:
    def __init__(self, cache: _Cache, *, race: bool = False) -> None:
        self.cache = cache
        self.race = race
        self.info_reads: list[str] = []

    def list_clusters(self, *, use_cache: bool):
        assert use_cache is False
        return ["tenant-a", "tenant-b"]

    def get_cluster_info(self, cluster_id: str):
        self.info_reads.append(cluster_id)
        if self.race:
            self.cache.generation = "after"
        # A complete location, as registration and the location update store one: a record with
        # only facility fields reads as no location (lxd_certs/storage.py stored_location).
        return {
            "metadata": {
                "location": {
                    "region_id": "id-3174",
                    "region_name": "Jakarta Selatan",
                    "country_code": "ID",
                    "country_name": "Indonesia",
                    "city": "Jakarta Selatan",
                    "latitude": -6.26,
                    "longitude": 106.81,
                    "timezone": "Asia/Jakarta",
                    "continent": "AS",
                    "facility_name": f"facility-{cluster_id}",
                    "facility_provider": f"provider-{cluster_id}",
                }
            }
        }


async def _inline_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


@pytest.mark.asyncio
async def test_facility_suggestions_only_read_clusters_in_the_actor_scope(monkeypatch) -> None:
    cache = _Cache()
    certificates = _Certificates(cache)
    monkeypatch.setattr(cache_module, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(cluster_registration, "apply_cluster_filter", lambda values: [values[0]])
    monkeypatch.setattr(cluster_registration.asyncio, "to_thread", _inline_to_thread)

    result = await cluster_registration.get_facility_suggestions(
        cert_manager=certificates,
        fresh=True,
    )

    assert certificates.info_reads == ["tenant-a"]
    assert result == [
        {
            "facility_name": "facility-tenant-a",
            "facility_provider": "provider-tenant-a",
        }
    ]
    assert cache.writes == [result]


@pytest.mark.asyncio
async def test_facility_read_racing_cluster_mutation_is_not_cached(monkeypatch) -> None:
    cache = _Cache()
    certificates = _Certificates(cache, race=True)
    monkeypatch.setattr(cache_module, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(cluster_registration, "apply_cluster_filter", lambda values: values)
    monkeypatch.setattr(cluster_registration.asyncio, "to_thread", _inline_to_thread)

    result = await cluster_registration.get_facility_suggestions(
        cert_manager=certificates,
        fresh=True,
    )

    assert len(result) == 2
    assert cache.writes == []


@pytest.mark.asyncio
async def test_facility_registry_failure_is_not_cached_as_an_empty_scope(monkeypatch) -> None:
    cache = _Cache()

    class _Unavailable:
        def list_clusters(self, *, use_cache: bool):
            raise RuntimeError("registry unavailable")

    monkeypatch.setattr(cache_module, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(cluster_registration.asyncio, "to_thread", _inline_to_thread)

    result = await cluster_registration.get_facility_suggestions(
        cert_manager=_Unavailable(),
        fresh=True,
    )

    assert result == []
    assert cache.writes == []
