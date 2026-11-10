"""An instance name shared by two projects on one cluster is ambiguous, and says so.

`GET /integrations/instances/{name}` (and MCP `orcastra_get_instance`, which runs it) documents a
409 naming every cluster and project when a name is not unique. That holds within one cluster too:
a search that stops at the first project holding the name would hand a key granted every project
one of the two in silence.
"""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import HTTPException

import app.routers.integrations as integrations
from app.services.lxd import project_scope, utils

CLUSTER = "cluster-a"
HOLDERS = ["default", "tenant-b"]


def _key(projects: List[str]) -> Dict[str, Any]:
    return {
        "id": "oak_test", "app_name": "qa", "capabilities": ["inventory.read"],
        "cluster_permissions": [{"cluster_id": CLUSTER, "permissions": ["read"], "projects": projects}],
    }


@pytest.fixture
def cluster(monkeypatch) -> List[Optional[str]]:
    """A cluster where `web` exists in `default` and in `tenant-b`. Records each project searched."""
    searched: List[Optional[str]] = []

    def _find(client: Any, name: str, project: Optional[str] = None, **_: Any) -> Tuple[Any, Optional[str]]:
        searched.append(project)
        holders = HOLDERS if project is None else [project]
        for holder in holders:
            if name == "web" and holder in HOLDERS:
                return SimpleNamespace(name="web", status="Running", status_code=103, type="container"), holder
        return None, None

    def _listing(client: Any, collection: str, recursion: int = 1, path: Any = None):
        assert collection == "instances"
        rows = [{"name": "web", "project": p} for p in HOLDERS] + [{"name": "db", "project": "default"}]
        return rows, True

    monkeypatch.setattr(utils, "find_instance_in_projects", _find)
    monkeypatch.setattr(project_scope, "list_across_projects", _listing)
    monkeypatch.setattr(integrations, "connect_or_get_client", lambda *_a, **_k: object())
    return searched


async def _read(api_key: Dict[str, Any], project: Optional[str] = None) -> Any:
    return await integrations.get_integration_instance(
        name="web", cluster_id=CLUSTER, project=project, api_key=api_key,
        lxd_service=SimpleNamespace(),
    )


def test_a_name_in_two_projects_is_a_conflict_naming_both(cluster) -> None:
    with pytest.raises(HTTPException) as refused:
        asyncio.run(_read(_key(["*"])))
    assert refused.value.status_code == 409
    assert f"{CLUSTER}/default" in refused.value.detail and f"{CLUSTER}/tenant-b" in refused.value.detail
    # One listing, then one read per holder: never a GET per project on the cluster.
    assert cluster == HOLDERS


def test_naming_the_project_resolves_it(cluster) -> None:
    found = asyncio.run(_read(_key(["*"]), project="tenant-b"))
    assert found["project"] == "tenant-b"


def test_a_key_that_sees_one_holder_gets_that_one(cluster) -> None:
    found = asyncio.run(_read(_key(["tenant-b"])))
    assert found["project"] == "tenant-b"


def test_a_key_granted_both_holders_by_name_is_told_it_is_ambiguous(cluster) -> None:
    with pytest.raises(HTTPException) as refused:
        asyncio.run(_read(_key(["tenant-b", "default"])))
    assert refused.value.status_code == 409


def test_a_listing_failure_falls_back_to_the_search_rather_than_not_found(cluster, monkeypatch) -> None:
    def _broken(*_a: Any, **_k: Any):
        raise RuntimeError("all-projects refused")

    monkeypatch.setattr(project_scope, "list_across_projects", _broken)
    found = asyncio.run(_read(_key(["*"])))
    assert found["project"] == "default"
    assert cluster == [None]
