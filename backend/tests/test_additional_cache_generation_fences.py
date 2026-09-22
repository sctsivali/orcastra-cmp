from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.cache import CacheManager
from app.routers import my_keys, vms


class _RaceCache:
    def __init__(self) -> None:
        self.instance_generation = "before"
        self.resource_generations = {"my_keys": "before"}
        self.writes: list[tuple[str, str, object, int]] = []

    def get(self, _namespace: str, _key: str):
        return None

    def set(self, namespace: str, key: str, value: object, ttl: int) -> None:
        self.writes.append((namespace, key, value, ttl))

    def instance_list_generation(self) -> str:
        return self.instance_generation

    def resource_list_generation(self, kind: str) -> str:
        return self.resource_generations[kind]


@pytest.mark.asyncio
async def test_legacy_vm_read_cannot_restore_a_pre_mutation_list(monkeypatch) -> None:
    cache = _RaceCache()

    class _Lxd:
        async def list_all_vms(self):
            cache.instance_generation = "after"
            return [], ["cluster-a"]

        def get_node_names(self):
            return ["cluster-a"]

    monkeypatch.setattr(vms, "get_cache_manager", lambda: cache)

    response = await vms.list_vms(
        host=None,
        status=None,
        offset=0,
        limit=0,
        lxd_service=_Lxd(),
        fresh=True,
    )

    assert response.nodes == ["cluster-a"]
    assert cache.writes == []


@pytest.mark.asyncio
async def test_key_read_cannot_restore_a_pre_mutation_vault_roster(monkeypatch) -> None:
    cache = _RaceCache()

    def _read(_client):
        cache.resource_generations["my_keys"] = "after"
        return []

    monkeypatch.setattr(my_keys, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(my_keys, "get_vault_keys_client", lambda: object())
    monkeypatch.setattr(my_keys, "list_keys_from_vault", _read)
    monkeypatch.setattr(my_keys, "get_current_allowed_clusters", lambda: None)

    response = await my_keys.list_keys(
        status_filter=None,
        search=None,
        fresh=True,
        _rbac=SimpleNamespace(email="operator@example.invalid"),
    )

    assert response.total == 0
    assert cache.writes == []


def test_key_mutation_invalidation_clears_every_actor_scope() -> None:
    cache = CacheManager(redis_url=None)
    cache.set("api_responses", "my_keys:list:scope=actor-a", [{"id": "a"}])
    cache.set("api_responses", "my_keys:list:scope=actor-b", [{"id": "b"}])

    cache.invalidate_resource_cache("my_keys")

    assert cache.get("api_responses", "my_keys:list:scope=actor-a") is None
    assert cache.get("api_responses", "my_keys:list:scope=actor-b") is None
    assert cache.resource_list_generation("my_keys") is not None
