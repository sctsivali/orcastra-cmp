"""Focused regressions for mutation/list cache coherence.

These tests stay below live LXD and Redis. They pin the two ordering guarantees that matter:
mutation generations advance before a stale read can repopulate a cleared family, and an upload
cannot publish ``ready`` until the matching inventory caches have been invalidated.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.core.cache import (
    CAPACITY_GENERATION_KIND,
    MEMBER_RESOURCES_CACHE_PREFIX,
    MICROCLOUD_RESOURCES_CACHE_PREFIX,
    NODES_STATUS_CACHE_KEY,
    CacheInvalidationError,
    CacheManager,
)
from app.routers import image_uploads, iso_uploads, nodes, profiles
from app.services.snapshots import schedule_runner


def _cached(manager: CacheManager, fragment: str) -> bool:
    return any(fragment in key for key in manager.memory_cache.cache)


def test_instance_lifecycle_clears_used_by_and_capacity_families() -> None:
    manager = CacheManager(redis_url=None)
    dependent_kinds = (
        "profiles",
        "networks",
        "storage_pools",
        "storage_pool_resources",
        "storage_volumes",
        "snapshots_fleet",
    )
    for kind in dependent_kinds:
        manager.set("api_responses", f"{kind}:list:scope=all", {"stale": True})
    manager.set("api_responses", f"{NODES_STATUS_CACHE_KEY}:scope=all", {"stale": True})
    manager.set(
        "lxd_resources",
        f"{MEMBER_RESOURCES_CACHE_PREFIX}:cluster-a:member-a:scope=all",
        {"stale": True},
    )
    manager.set(
        "lxd_resources",
        f"{MICROCLOUD_RESOURCES_CACHE_PREFIX}:cluster-a",
        {"stale": True},
    )

    manager.invalidate_instance_cache()

    for kind in dependent_kinds:
        assert not _cached(manager, f":{kind}:list:")
        assert manager.resource_list_generation(kind) is not None
    assert not _cached(manager, NODES_STATUS_CACHE_KEY)
    assert not _cached(manager, MEMBER_RESOURCES_CACHE_PREFIX)
    assert not _cached(manager, MICROCLOUD_RESOURCES_CACHE_PREFIX)
    assert manager.resource_list_generation(CAPACITY_GENERATION_KIND) is not None


def test_generation_advances_before_a_resource_family_is_deleted() -> None:
    class OrderedCache(CacheManager):
        def __init__(self) -> None:
            super().__init__(redis_url=None)
            self.events: list[str] = []

        def _advance_resource_generation(
            self,
            kind: str,
            *,
            require_shared: bool = False,
        ) -> None:
            self.events.append(f"generation:{kind}")
            super()._advance_resource_generation(kind, require_shared=require_shared)

        def delete_pattern(
            self,
            namespace: str,
            pattern: str = "*",
            *,
            require_shared: bool = False,
        ) -> None:
            self.events.append(f"delete:{namespace}:{pattern}")
            super().delete_pattern(
                namespace,
                pattern,
                require_shared=require_shared,
            )

    manager = OrderedCache()
    manager.invalidate_resource_cache("profiles")

    assert manager.events[:2] == [
        "generation:profiles",
        "delete:api_responses:profiles:*",
    ]


def test_strict_invalidation_allows_intentional_memory_only_mode() -> None:
    manager = CacheManager(redis_url=None)
    manager.set("api_responses", "images:list:scope=all", {"stale": True})

    manager.invalidate_resource_cache("images", require_shared=True)

    assert manager.get("api_responses", "images:list:scope=all") is None


def test_strict_invalidation_rejects_configured_but_unavailable_redis() -> None:
    manager = CacheManager(redis_url=None)
    manager._redis_configured = True
    manager.set("api_responses", "images:list:scope=all", {"stale": True})

    with pytest.raises(CacheInvalidationError, match="shared resource cache operation"):
        manager.invalidate_resource_cache("images", require_shared=True)

    assert manager.get("api_responses", "images:list:scope=all") is None


def test_profile_read_racing_an_instance_change_is_not_cached(monkeypatch) -> None:
    manager = CacheManager(redis_url=None)

    class ConnectionManager:
        @staticmethod
        def get_node_names() -> list[str]:
            return ["cluster-a"]

    service = SimpleNamespace(
        connection_manager=ConnectionManager(),
        get_node_names=lambda: ["cluster-a"],
    )

    def read_profiles(_service, _node, _project):
        manager.invalidate_instance_cache()
        return [], True

    monkeypatch.setattr(profiles, "get_cache_manager", lambda: manager)
    monkeypatch.setattr(profiles, "_fetch_profiles_for_node", read_profiles)

    response = asyncio.run(
        profiles.list_profiles.__wrapped__(
            host=None,
            project=None,
            lxd_service=service,
            fresh=True,
        )
    )

    assert response.total == 0
    assert not _cached(manager, "profiles:list")


def test_capacity_read_racing_an_instance_change_is_not_cached(monkeypatch) -> None:
    manager = CacheManager(redis_url=None)

    class Service:
        async def check_nodes_status_async(self):
            manager.invalidate_instance_cache()
            return []

    monkeypatch.setattr(nodes, "get_cache_manager", lambda: manager)

    response = asyncio.run(
        nodes.get_nodes_status.__wrapped__(
            lxd_service=Service(),
            fresh=True,
            _rbac=None,
        )
    )

    assert response.nodes == []
    assert not _cached(manager, NODES_STATUS_CACHE_KEY)


def _assert_upload_ready_order(monkeypatch, module, result: dict) -> None:
    events: list[str] = []

    class Row:
        fingerprint = None
        size = None
        error = "old"

        def __init__(self) -> None:
            self._status = "importing"

        @property
        def status(self) -> str:
            return self._status

        @status.setter
        def status(self, value: str) -> None:
            events.append(f"status:{value}")
            self._status = value

    class DB:
        async def commit(self) -> None:
            events.append("commit")

    async def invalidate() -> None:
        events.append("invalidate")

    monkeypatch.setattr(module, "_invalidate_ready_caches", invalidate)
    row = Row()
    asyncio.run(module._publish_ready(DB(), row, result))

    assert events == ["invalidate", "status:ready", "commit"]
    assert row.error is None


def _assert_upload_invalidation_failure_is_not_published(monkeypatch, module, result: dict) -> None:
    events: list[str] = []

    class Row:
        fingerprint = None
        size = None
        error = None

        def __init__(self) -> None:
            self._status = "importing"

        @property
        def status(self) -> str:
            return self._status

        @status.setter
        def status(self, value: str) -> None:
            events.append(f"status:{value}")
            self._status = value

    class DB:
        async def commit(self) -> None:
            events.append("commit")

    async def fail_invalidation() -> None:
        raise module._ReadyCacheInvalidationError("cache unavailable")

    monkeypatch.setattr(module, "_invalidate_ready_caches", fail_invalidation)
    row = Row()

    with pytest.raises(module._ReadyCacheInvalidationError, match="cache unavailable"):
        asyncio.run(module._publish_ready(DB(), row, result))

    assert row.status == "importing"
    assert events == []


def test_image_upload_invalidates_before_publishing_ready(monkeypatch) -> None:
    _assert_upload_ready_order(
        monkeypatch,
        image_uploads,
        {"fingerprint": "f" * 64, "size": 42},
    )


def test_iso_upload_invalidates_before_publishing_ready(monkeypatch) -> None:
    _assert_upload_ready_order(monkeypatch, iso_uploads, {"size": 42})


def test_image_upload_does_not_publish_ready_when_invalidation_fails(monkeypatch) -> None:
    _assert_upload_invalidation_failure_is_not_published(
        monkeypatch,
        image_uploads,
        {"fingerprint": "f" * 64, "size": 42},
    )


def test_iso_upload_does_not_publish_ready_when_invalidation_fails(monkeypatch) -> None:
    _assert_upload_invalidation_failure_is_not_published(
        monkeypatch,
        iso_uploads,
        {"size": 42},
    )


@pytest.mark.parametrize(
    ("module", "cache_kind", "result"),
    [
        (image_uploads, "images", {"fingerprint": "f" * 64, "size": 42}),
        (iso_uploads, "storage_volumes", {"size": 42}),
    ],
)
def test_upload_ready_uses_strict_shared_cache_invalidation(
    monkeypatch,
    module,
    cache_kind: str,
    result: dict,
) -> None:
    class FailingDeleteRedis:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        def setex(self, key: str, _ttl: int, value: str) -> None:
            self.values[key] = value

        def keys(self, pattern: str) -> list[str]:
            prefix = pattern.rstrip("*")
            return [key for key in self.values if key.startswith(prefix)]

        def delete(self, *_keys: str) -> None:
            raise RuntimeError("Redis delete failed")

    class Row:
        fingerprint = None
        size = None
        error = None
        status = "importing"

    class DB:
        def __init__(self) -> None:
            self.commits = 0

        async def commit(self) -> None:
            self.commits += 1

    manager = CacheManager(redis_url=None)
    redis_client = FailingDeleteRedis()
    manager.redis_client = redis_client
    stale_key = manager._make_key("api_responses", f"{cache_kind}:list:scope=all")
    redis_client.values[stale_key] = '{"stale": true}'
    manager.memory_cache.set(stale_key, {"stale": True}, ttl=300)
    monkeypatch.setattr(module, "get_cache_manager", lambda: manager)

    async def inline_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    # The contract under test is CacheManager's Redis failure propagation, not Python's
    # executor wakeup. Keeping it inline also makes this regression deterministic in sandboxed CI.
    monkeypatch.setattr(module.asyncio, "to_thread", inline_to_thread)
    row = Row()
    db = DB()

    with pytest.raises(module._ReadyCacheInvalidationError):
        asyncio.run(module._publish_ready(db, row, result))

    assert row.status == "importing"
    assert db.commits == 0
    assert stale_key in redis_client.values
    assert stale_key not in manager.memory_cache.cache


@pytest.mark.parametrize(
    ("module", "landed"),
    [
        (image_uploads, {"fingerprint": "f" * 64, "size": 42}),
        (iso_uploads, {"size": 42}),
    ],
)
def test_expired_upload_adopts_landed_artifact_before_failure(
    monkeypatch,
    module,
    landed: dict,
) -> None:
    events: list[str] = []

    class Row:
        status = "importing"
        error = None
        fingerprint = None
        size = None
        cluster_id = "cluster-a"
        project = "default"
        alias = "custom-image"
        pool = "default"
        volume_name = "custom-iso"

    class DB:
        async def commit(self) -> None:
            events.append("commit")

        async def rollback(self) -> None:
            events.append("rollback")

    class SessionMaker:
        async def __aenter__(self):
            return DB()

        async def __aexit__(self, *_args):
            return False

    class InlineLoop:
        async def run_in_executor(self, _executor, fn, *args):
            return fn(*args)

    row = Row()
    monkeypatch.setattr(module, "async_session_maker", lambda: SessionMaker())
    monkeypatch.setattr(module, "_get_session", lambda *_args: asyncio.sleep(0, result=row))
    monkeypatch.setattr(module.asyncio, "get_event_loop", lambda: InlineLoop())
    resolver_name = "_resolve_alias_sync" if module is image_uploads else "_resolve_volume_sync"
    monkeypatch.setattr(module, resolver_name, lambda *_args: landed)

    async def invalidate() -> None:
        events.append("invalidate")

    monkeypatch.setattr(module, "_invalidate_ready_caches", invalidate)
    monkeypatch.setattr(module, "_cleanup_file", lambda path: events.append(f"cleanup:{path}"))

    asyncio.run(
        module._finalize_from_existing(
            "session-1",
            absent_error="import did not complete within the recovery window",
            cleanup_path="/staging/upload.bin",
        )
    )

    assert row.status == "ready"
    assert events == ["invalidate", "commit", "cleanup:/staging/upload.bin"]


@pytest.mark.parametrize("module", [image_uploads, iso_uploads])
def test_expired_upload_stays_non_terminal_when_exact_read_is_unknown(
    monkeypatch,
    module,
) -> None:
    events: list[str] = []

    class Row:
        status = "importing"
        error = None
        cluster_id = "cluster-a"
        project = "default"
        alias = "custom-image"
        pool = "default"
        volume_name = "custom-iso"

    class DB:
        async def commit(self) -> None:
            events.append("commit")

        async def rollback(self) -> None:
            events.append("rollback")

    class SessionMaker:
        async def __aenter__(self):
            return DB()

        async def __aexit__(self, *_args):
            return False

    class InlineLoop:
        async def run_in_executor(self, _executor, fn, *args):
            return fn(*args)

    def unknown(*_args):
        raise TimeoutError("cluster read timed out")

    row = Row()
    monkeypatch.setattr(module, "async_session_maker", lambda: SessionMaker())
    monkeypatch.setattr(module, "_get_session", lambda *_args: asyncio.sleep(0, result=row))
    monkeypatch.setattr(module.asyncio, "get_event_loop", lambda: InlineLoop())
    resolver_name = "_resolve_alias_sync" if module is image_uploads else "_resolve_volume_sync"
    monkeypatch.setattr(module, resolver_name, unknown)
    monkeypatch.setattr(module, "_cleanup_file", lambda path: events.append(f"cleanup:{path}"))

    asyncio.run(
        module._finalize_from_existing(
            "session-1",
            absent_error="import did not complete within the recovery window",
            cleanup_path="/staging/upload.bin",
        )
    )

    assert row.status == "importing"
    assert row.error is None
    assert events == ["rollback", "cleanup:/staging/upload.bin"]


def test_scheduled_snapshot_refreshes_rows_and_capacity(monkeypatch) -> None:
    seen: list[tuple[str, ...]] = []
    manager = SimpleNamespace(
        invalidate_resource_cache=lambda *kinds: seen.append(kinds),
    )
    monkeypatch.setattr(schedule_runner, "get_cache_manager", lambda: manager)

    schedule_runner._invalidate_snapshot_views()

    assert seen == [("snapshots_fleet", "storage_pool_resources")]


def test_scheduled_snapshot_cache_failure_never_escapes(monkeypatch) -> None:
    manager = SimpleNamespace(
        invalidate_resource_cache=lambda *_kinds: (_ for _ in ()).throw(
            RuntimeError("cache unavailable")
        ),
    )
    monkeypatch.setattr(schedule_runner, "get_cache_manager", lambda: manager)

    schedule_runner._invalidate_snapshot_views()
