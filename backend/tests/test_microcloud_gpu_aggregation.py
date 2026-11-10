"""MicroCloud aggregation sums per-member GPU inventories, and scopes usage to the member.

`aggregate_microcloud_resources` (per-member inventory, one hoisted instance fetch, class
breakdown carried through) is driven against a fake client, since it needs a clustered target.
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from app.services.lxd import node_resources as nr

from tests.gpu_fixtures import (
    DISCRETE_RTX_CARD,
    JETSON_ORIN_NX_RESOURCES,
    FakeInstance,
    resources_with,
)


class _Response:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _Endpoint:
    def __init__(self, handler) -> None:
        self._handler = handler

    def get(self, params: Dict[str, Any] = None, timeout: Any = None) -> _Response:
        # The real _APINode takes a timeout, and the all-projects read passes one explicitly.
        return _Response(self._handler(params or {}))


class FakeClient:
    """The subset of pylxd.Client the aggregation touches."""

    def __init__(self, members: List[Dict[str, Any]], per_member: Dict[str, Any], instances: List[Any]) -> None:
        self.host_info = {
            "environment": {"server_clustered": True, "server_name": members[0]["server_name"]},
            # Lets the aggregation read every project in one request.
            "api_extensions": ["instance_all_projects"],
        }
        self._members = members
        self._per_member = per_member
        self._instances = list(instances)
        self.instance_reads = 0
        self.recursions: List[int] = []
        self.api = _Api(self)

    def _instance_rows(self, params: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
        """What GET /1.0/instances?recursion=1&all-projects=true returns."""
        self.instance_reads += 1
        self.recursions.append(int((params or {}).get("recursion", 0)))
        return [
            {
                "name": i.name,
                "project": getattr(i, "project", "default"),
                "location": i.location,
                "status": i.status,
                "config": i.config,
                "devices": i.devices,
                "expanded_config": getattr(i, "expanded_config", i.config),
                "expanded_devices": getattr(i, "expanded_devices", i.devices),
            }
            for i in self._instances
        ]


class _Api:
    def __init__(self, client: "FakeClient") -> None:
        self._client = client
        self.cluster = SimpleNamespace(
            members=_Endpoint(lambda params: {"metadata": client._members})
        )
        self.resources = _Endpoint(
            lambda params: {"metadata": client._per_member[params["target"]]}
        )

    def __getitem__(self, key: str) -> _Endpoint:
        if key == "instances":
            return _Endpoint(lambda params: {"metadata": self._client._instance_rows(params)})
        # Storage pools and other paths: nothing this test asserts on.
        return _Endpoint(lambda params: {"metadata": []})


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    """The aggregation caches its result; every case here must recompute."""

    class _NullCache:
        def get(self, *_a, **_kw):
            return None

        def set(self, *_a, **_kw):
            return None

    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: _NullCache())


def _cluster(instances=None):
    members = [
        {"server_name": "jetson", "status": "Online", "url": "https://jetson:8443"},
        {"server_name": "workstation", "status": "Online", "url": "https://ws:8443"},
        {"server_name": "offline-one", "status": "Offline", "url": "https://dead:8443"},
    ]
    per_member = {
        "jetson": JETSON_ORIN_NX_RESOURCES,
        "workstation": resources_with([DISCRETE_RTX_CARD], cpu={"total": 16}, memory={"total": 68719476736}),
        "offline-one": resources_with([]),
    }
    return FakeClient(members, per_member, instances or [])


def test_a_cluster_sums_compute_and_cards_across_members() -> None:
    result = nr.aggregate_microcloud_resources(_cluster(), "microcloud", {})

    assert result is not None
    assert result.gpu_total == 2, "one Tegra compute GPU plus one discrete card"
    assert result.gpu_cards_total == 3, "the Tegra display controller is still reported"
    assert result.gpu_display_total == 1
    assert result.gpu_available == 2


def test_an_offline_member_contributes_nothing() -> None:
    """Only Online members are queried, so a dead member cannot inflate or zero the totals."""
    result = nr.aggregate_microcloud_resources(_cluster(), "microcloud", {})

    assert result.gpu_cards_total == 3, "offline-one has no cards and was not queried"


def test_usage_is_charged_to_the_member_that_holds_it() -> None:
    """A member's consumers are charged against that member's cards only."""
    instances = [
        FakeInstance("cuda-job", location="jetson", config={"nvidia.runtime": "true"}),
        FakeInstance("idle", location="workstation"),
    ]

    result = nr.aggregate_microcloud_resources(_cluster(instances), "microcloud", {})

    assert result.gpu_used == 1, "one member holds a GPU, the other does not"
    assert result.gpu_available == 1


def test_usage_never_exceeds_capacity_per_member() -> None:
    instances = [
        FakeInstance(f"job{i}", location="jetson", config={"nvidia.runtime": "true"}) for i in range(4)
    ]

    result = nr.aggregate_microcloud_resources(_cluster(instances), "microcloud", {})

    assert result.gpu_used == 1, "the Tegra member has one compute GPU, so it can hold one"
    assert result.gpu_available == 1, "the other member's GPU is still free"


def test_card_indices_stay_unique_across_members() -> None:
    """Two members both start their card list at 0; the aggregate must not collide."""
    result = nr.aggregate_microcloud_resources(_cluster(), "microcloud", {})

    indices = [card.index for card in result.gpu_cards]
    assert sorted(indices) == [0, 1, 2], f"indices collided: {indices}"


def test_vendors_and_models_merge_without_duplicates() -> None:
    result = nr.aggregate_microcloud_resources(_cluster(), "microcloud", {})

    assert result.gpu_vendors == ["NVIDIA", "NVIDIA Corporation"]
    assert len(result.gpu_models) == 2


def test_an_unclustered_node_is_refused_rather_than_guessed() -> None:
    client = _cluster()
    client.host_info = {"environment": {"server_clustered": False}}

    assert nr.aggregate_microcloud_resources(client, "standalone", {}) is None


def test_the_instance_list_is_fetched_once_for_the_whole_cluster() -> None:
    """One all-projects read for the cluster, however many members it has.

    pylxd's instances.all() sees only the default project, so the read goes to the raw API.
    """
    client = _cluster()

    nr.aggregate_microcloud_resources(client, "microcloud", {})

    assert client.instance_reads == 1, (
        f"instance list read {client.instance_reads} times"
    )


def test_the_instance_list_is_fetched_with_recursion() -> None:
    """At recursion=0 the list is names only and every `.location` read becomes its own GET.

    recursion=1 already carries expanded_config and expanded_devices, which is everything
    build_gpu_inventory duck-types; recursion=2 adds state, snapshots and backups at several
    times the cost."""
    client = _cluster()
    nr.aggregate_microcloud_resources(client, "microcloud", {})
    assert client.recursions == [1], f"expected one recursion=1 call, got {client.recursions}"
