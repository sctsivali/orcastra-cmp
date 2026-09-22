from __future__ import annotations

import pytest

from app.core.cache import CacheManager
from app.services import tag_service


class _Result:
    def scalar(self):
        return 0

    def scalar_one_or_none(self):
        return None

    def scalars(self):
        return self

    def all(self):
        return []

    def __iter__(self):
        return iter(())


class _RaceCache:
    def __init__(self) -> None:
        self.generation = "before"
        self.writes: list[tuple[str, str]] = []

    def get(self, _namespace: str, _key: str):
        return None

    def set(self, namespace: str, key: str, _value, *, ttl: int) -> None:
        assert ttl > 0
        self.writes.append((namespace, key))

    def resource_list_generation(self, kind: str) -> str:
        assert kind == "tags"
        return self.generation


class _RacingDb:
    def __init__(self, cache: _RaceCache) -> None:
        self.cache = cache

    async def execute(self, _statement):
        self.cache.generation = "after"
        return _Result()


def _service(monkeypatch) -> tuple[tag_service.TagService, _RaceCache]:
    cache = _RaceCache()
    monkeypatch.setattr(tag_service, "get_cache_manager", lambda: cache)
    return tag_service.TagService(_RacingDb(cache)), cache


@pytest.mark.asyncio
async def test_category_read_racing_a_tag_mutation_is_not_cached(monkeypatch) -> None:
    service, cache = _service(monkeypatch)

    assert await service.list_categories() == []
    assert cache.writes == []


@pytest.mark.asyncio
async def test_tag_list_racing_an_assignment_is_not_cached(monkeypatch) -> None:
    service, cache = _service(monkeypatch)

    response = await service.list_tags(include_usage=False)

    assert response.total == 0
    assert not any(key.startswith("tags:list:") for _namespace, key in cache.writes)


@pytest.mark.asyncio
async def test_tag_statistics_racing_an_assignment_are_not_cached(monkeypatch) -> None:
    service, cache = _service(monkeypatch)

    response = await service.get_tag_statistics()

    assert response.total_tags == 0
    assert response.total_assignments == 0
    assert not any(key.startswith("stats:") for _namespace, key in cache.writes)


def test_tag_invalidation_advances_generation_and_clears_every_projection() -> None:
    cache = CacheManager(redis_url=None)
    cache.set("tags", "tags:list:none:none:none", {"tags": []})
    cache.set("tags", "categories:all:False", [])
    cache.set("tags", "stats:overview", {"total_tags": 1})

    cache.invalidate_tag_cache()

    assert cache.resource_list_generation("tags") is not None
    assert cache.get("tags", "tags:list:none:none:none") is None
    assert cache.get("tags", "categories:all:False") is None
    assert cache.get("tags", "stats:overview") is None
