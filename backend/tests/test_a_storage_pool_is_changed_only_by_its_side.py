"""A storage pool serves every project on its cluster, so its writes follow the whole-cluster rule.

A Partner held to a project bound on a cluster another party holds could create, reconfigure or
delete a pool the other party's projects use. Where only the partner's own buyer organizations
share the cluster, nothing changes.
"""

from typing import Any, List

import pytest
from fastapi import HTTPException

from app.core.auth import AuthenticatedUser
from app.routers import storage_pools
from app.services import cluster_parties
from tests.instance_access_fakes import CLUSTER, partner

USER = AuthenticatedUser(user_id="partner-sp", email="partner@example.test", groups=["role_partner"])


class _Service:
    def __init__(self) -> None:
        self.asked: List[str] = []

    def __getattr__(self, name: str) -> Any:
        def reached(*args: Any, **_kwargs: Any) -> Any:
            self.asked.append(name)
            raise RuntimeError("reached the cluster")
        return reached


@pytest.fixture
def held(monkeypatch):
    state = {"held": True}

    async def fake(_db: Any, _email: str, clusters: Any) -> set:
        return set(clusters) if state["held"] else set()

    monkeypatch.setattr(cluster_parties, "clusters_held_by_another_party", fake)
    return state


def _writes(service: _Service) -> List[Any]:
    return [
        lambda: storage_pools.create_storage_pool(
            storage_pools.StoragePoolCreateRequest(name="p1", driver="dir"),
            host=CLUSTER, lxd_service=service, _=None, _rbac=USER, db=None,
        ),
        lambda: storage_pools.update_storage_pool(
            "p1", host=CLUSTER, config={"size": "1GiB"}, description=None,
            lxd_service=service, _=None, _rbac=USER, db=None,
        ),
        lambda: storage_pools.delete_storage_pool("p1", host=CLUSTER, lxd_service=service, _=None, _rbac=USER, db=None),
    ]


@pytest.mark.parametrize("index", range(3), ids=["create", "update", "delete"])
async def test_a_pool_write_on_a_cluster_another_party_holds_is_refused(held, index: int) -> None:
    service = _Service()
    with partner({"default"}):
        with pytest.raises(HTTPException) as caught:
            await _writes(service)[index]()
    assert caught.value.status_code == 403
    assert service.asked == []


@pytest.mark.parametrize("index", range(3), ids=["create", "update", "delete"])
async def test_a_pool_write_where_only_the_partners_buyers_share_goes_on(held, index: int) -> None:
    held["held"] = False
    service = _Service()
    with partner({"default"}):
        with pytest.raises(Exception) as caught:
            await _writes(service)[index]()
    assert not (isinstance(caught.value, HTTPException) and caught.value.status_code == 403)
