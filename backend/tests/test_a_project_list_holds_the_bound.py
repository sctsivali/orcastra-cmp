"""The project lists answer only what the single-project read would, and create respects the party.

The single-project read refused a project outside the Partner's bound, while both lists showed it
with its config and used_by, so a Partner on a cluster shared with another organization read that
organization's projects through the list. A create on such a cluster made a project the Partner
could then neither see nor name in a policy.
"""

from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.core.auth import AuthenticatedUser
from app.routers import projects
from app.services import cluster_parties
from tests.instance_access_fakes import CLUSTER, partner, scoped, tenant

USER = AuthenticatedUser(user_id="partner-p", email="partner@example.test", groups=["role_partner"])


def _project(name: str) -> Dict[str, Any]:
    return {"name": name, "description": "", "config": {"features.images": "true"},
            "used_by": [f"/1.0/instances/web?project={name}"], "host": CLUSTER}


class _Service:
    def __init__(self) -> None:
        self.calls: List[str] = []

    def get_node_names(self) -> List[str]:
        return [CLUSTER]

    async def list_projects(self, host: str) -> List[Dict[str, Any]]:
        self.calls.append(host)
        return [_project(n) for n in ("default", "own-web", "other-org")]


class _Cache:
    def get(self, *_a: Any) -> None:
        return None

    def set(self, *_a: Any, **_k: Any) -> None:
        return None


@pytest.fixture(autouse=True)
def no_cache(monkeypatch):
    monkeypatch.setattr(projects, "get_cache_manager", lambda: _Cache())


async def test_the_host_list_leaves_out_another_orgs_project() -> None:
    with partner({"default", "own-web"}):
        answer = await projects.list_projects(CLUSTER, lxd_service=_Service())
    assert [p.name for p in answer.projects] == ["default", "own-web"]


async def test_the_fleet_list_leaves_out_another_orgs_project() -> None:
    with partner({"default", "own-web"}):
        answer = await projects.list_all_projects(lxd_service=_Service(), fresh=True)
    assert [p.name for p in answer[0].projects] == ["default", "own-web"]


async def test_an_unbounded_caller_lists_every_project() -> None:
    with scoped(allowed={CLUSTER}):
        answer = await projects.list_projects(CLUSTER, lxd_service=_Service())
    assert [p.name for p in answer.projects] == ["default", "own-web", "other-org"]


async def test_the_tenant_acl_still_applies() -> None:
    with tenant({("own-web", "*"): {"view"}}):
        answer = await projects.list_projects(CLUSTER, lxd_service=_Service())
    assert [p.name for p in answer.projects] == ["own-web"]


class _CreateService:
    def __init__(self) -> None:
        self.asked: List[str] = []

    def get_node_resources(self, host: str) -> Any:
        self.asked.append(host)
        raise RuntimeError("reached the cluster")


def _create_request() -> Any:
    return projects.ProjectCreateRequest(
        host=CLUSTER, name="qa-new", max_cpu=1, max_ram="1GiB", max_storage="1GiB"
    )


async def test_a_create_on_a_cluster_another_party_holds_is_refused(monkeypatch) -> None:
    async def held(_db: Any, _email: str, clusters: Any) -> set:
        return set(clusters)

    monkeypatch.setattr(cluster_parties, "clusters_held_by_another_party", held)
    service = _CreateService()
    with partner({"default"}):
        with pytest.raises(HTTPException) as caught:
            await projects.create_project(_create_request(), lxd_service=service, _=None, _rbac=USER, db=None)
    assert caught.value.status_code == 403
    assert service.asked == []


async def test_a_create_where_only_the_partners_buyers_share_goes_on(monkeypatch) -> None:
    async def held(_db: Any, _email: str, _clusters: Any) -> set:
        return set()

    monkeypatch.setattr(cluster_parties, "clusters_held_by_another_party", held)
    service = _CreateService()
    with partner({"default"}):
        with pytest.raises(Exception):
            await projects.create_project(_create_request(), lxd_service=service, _=None, _rbac=USER, db=None)
    assert service.asked == [CLUSTER]
