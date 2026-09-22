"""Project quota vocabulary and cache-race regressions."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.cache import CacheManager
from app.core.org_scope import get_scope_cache_key
from app.routers import projects


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1B", 1),
        ("1 KB", 1_000),
        ("1.5MB", 1_500_000),
        ("1GB", 1_000_000_000),
        ("2TB", 2_000_000_000_000),
        ("1KiB", 1_024),
        ("1.5MiB", 1_572_864),
        ("1GiB", 1_073_741_824),
        ("2TiB", 2_199_023_255_552),
        ("4096", 4_096),
    ],
)
def test_project_quota_parser_preserves_decimal_and_binary_units(
    value: str,
    expected: int,
) -> None:
    assert projects._parse_size_to_bytes(value) == expected


def test_project_quota_parser_rejects_unknown_units() -> None:
    with pytest.raises(ValueError, match="Invalid size format"):
        projects._parse_size_to_bytes("4XB")


def test_project_quota_refusals_use_canonical_resource_vocabulary() -> None:
    resources = {
        "cpu_total": 80,
        "memory_total": 256 * 1024**3,
        "storage_total": 5_000_000_000,
    }

    with pytest.raises(HTTPException) as cpu_error:
        projects._refuse_cap_above_hardware(
            resources,
            max_cpu=81,
            ram_bytes=None,
            storage_bytes=None,
        )
    assert cpu_error.value.detail == (
        "Project allocation (81 vCPU) exceeds this cluster's capacity of 80 hardware threads"
    )

    with pytest.raises(HTTPException) as memory_error:
        projects._refuse_cap_above_hardware(
            resources,
            max_cpu=None,
            ram_bytes=512 * 1024**3,
            storage_bytes=None,
        )
    assert memory_error.value.detail == (
        "Memory limit (512 GiB) exceeds this cluster's capacity of 256 GiB"
    )

    with pytest.raises(HTTPException) as storage_error:
        projects._refuse_cap_above_hardware(
            resources,
            max_cpu=None,
            ram_bytes=None,
            storage_bytes=6_000_000_000,
        )
    assert storage_error.value.detail == (
        "Storage limit (6 GB) exceeds this cluster's capacity of 5 GB"
    )


@pytest.mark.asyncio
async def test_project_list_read_cannot_restore_cache_after_invalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = CacheManager(redis_url=None)

    class Service:
        @staticmethod
        def get_node_names() -> list[str]:
            return ["cluster-a"]

        @staticmethod
        async def list_projects(host: str) -> list[dict[str, object]]:
            cache.invalidate_resource_cache(projects.PROJECT_LIST_CACHE_KIND)
            return [
                {
                    "name": "project-a",
                    "description": "",
                    "config": {},
                    "used_by": [],
                    "host": host,
                }
            ]

    monkeypatch.setattr(projects, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(projects, "filter_projects_by_acl", lambda _host, names: names)
    monkeypatch.setattr(projects, "project_is_visible", lambda _host, _project: True)

    response = await projects.list_all_projects.__wrapped__(
        lxd_service=Service(),
        fresh=True,
    )

    assert response[0].total == 1
    assert cache.get(
        "api_responses",
        get_scope_cache_key("projects:list:all"),
    ) is None


@pytest.mark.asyncio
async def test_project_usage_read_cannot_restore_cache_after_invalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = CacheManager(redis_url=None)

    class Service:
        @staticmethod
        async def get_project_usage(_host: str, _project: str) -> SimpleNamespace:
            cache.invalidate_resource_cache(projects.PROJECT_USAGE_CACHE_KIND)
            return SimpleNamespace(
                limits={"cpu": 4, "memory_bytes": 8 * 1024**3},
                used={"cpu": 1, "memory_bytes": 1024**3},
                complete=True,
            )

    monkeypatch.setattr(projects, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(projects, "verify_cluster_access", lambda _host: None)
    monkeypatch.setattr(projects, "project_is_visible", lambda _host, _project: True)
    monkeypatch.setattr(projects, "filter_projects_by_acl", lambda _host, names: names)

    response = await projects.get_project_usage(
        host="cluster-a",
        project_name="project-a",
        lxd_service=Service(),
        fresh=True,
    )

    assert response.used.cpu == 1
    assert cache.get(
        "api_responses",
        get_scope_cache_key("project_usage:host=cluster-a:project=project-a"),
    ) is None


def test_project_mutations_fence_and_clear_both_cache_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = CacheManager(redis_url=None)
    cache.set("api_responses", "projects:list:all:scope=all", {"stale": True})
    cache.set(
        "api_responses",
        "project_usage:host=cluster-a:project=project-a:scope=all",
        {"stale": True},
    )
    monkeypatch.setattr(projects, "get_cache_manager", lambda: cache)

    projects._invalidate_project_caches()

    assert cache.resource_list_generation(projects.PROJECT_LIST_CACHE_KIND) is not None
    assert cache.resource_list_generation(projects.PROJECT_USAGE_CACHE_KIND) is not None
    assert cache.get("api_responses", "projects:list:all:scope=all") is None
    assert cache.get(
        "api_responses",
        "project_usage:host=cluster-a:project=project-a:scope=all",
    ) is None
