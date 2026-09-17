"""A clustered host's resource read counts the member that answered, and the facade
aggregates it for the readers that want the cluster.

/1.0/resources on a clustered host answers for whichever member the connection landed on.
The read then summed every running instance on the whole cluster against that one member's
threads, which is how a MicroCloud with 307 vCPUs allocated across three members reported
"available CPU (-227)" on one of them and refused every project create. The read now counts
only the instances placed on the answering member, clamps the remainder at zero, and reports
the allocation ratio; the facade hands the project pre-flight and the server resources route
the cluster aggregate, the same one the /nodes card shows.
"""

from types import SimpleNamespace

import pytest

from app.models.schemas import NodeResources
from app.services.lxd import node_resources as nr_module
from app.services.lxd import resources as resources_module
from app.services.lxd.lxd_service import LXDService
from app.services.lxd.resources import ResourceManager


class _Client:
    def __init__(self, clustered: bool, member: str = "mc1") -> None:
        # `auth: trusted` because resolve_clustered only believes a trusted /1.0 body
        self.host_info = {"auth": "trusted", "environment": {"server_clustered": clustered, "server_name": member}}
        self.storage_pools = SimpleNamespace(all=lambda: [])


def _rows():
    return [
        SimpleNamespace(location="mc1", status="Running", config={}, expanded_config={"limits.cpu": "4"}),
        SimpleNamespace(location="mc1", status="Running", config={"limits.cpu": "4"}, expanded_config={"limits.cpu": "4"}),
        SimpleNamespace(location="mc2", status="Running", config={"limits.cpu": "64"}, expanded_config={"limits.cpu": "64"}),
        SimpleNamespace(location="mc3", status="Running", config={"limits.cpu": "64"}, expanded_config={"limits.cpu": "64"}),
    ]


def _live(cpu_total: int):
    return SimpleNamespace(body={"cpu": {"total": cpu_total, "sockets": [{}]}, "memory": {"total": 8, "used": 2}}, collected_at=None)


def test_a_clustered_read_counts_the_answering_member_only(monkeypatch):
    monkeypatch.setattr(resources_module, "list_instance_records", lambda client: (_rows(), True))
    got = ResourceManager().get_node_resources(_Client(clustered=True), "mc-lab", resources=_live(2))

    assert got["cpu_used"] == 8, "two instances on mc1 at 4 vCPUs each, mc2 and mc3 not counted"
    assert got["cpu_available"] == 0, "clamped: 8 allocated on 2 threads is over-commit, not -6"
    assert got["cpu_allocation_ratio"] == 4.0
    assert got["cpu_overcommitted"] is True


def test_a_standalone_read_counts_every_instance(monkeypatch):
    rows = [
        SimpleNamespace(location="", status="Running", config={"limits.cpu": "2"}, expanded_config={"limits.cpu": "2"}),
        SimpleNamespace(location="none", status="Running", config={}, expanded_config={"limits.cpu": "2"}),
        SimpleNamespace(location="", status="Stopped", config={"limits.cpu": "16"}, expanded_config={"limits.cpu": "16"}),
    ]
    monkeypatch.setattr(resources_module, "list_instance_records", lambda client: (rows, True))
    got = ResourceManager().get_node_resources(_Client(clustered=False), "nuc1", resources=_live(12))

    assert got["cpu_used"] == 4 and got["cpu_available"] == 8
    assert got["cpu_overcommitted"] is False


def test_a_failed_instance_read_reports_nothing_allocated_not_a_crash(monkeypatch):
    def _boom(client):
        raise RuntimeError("cluster busy")

    monkeypatch.setattr(resources_module, "list_instance_records", _boom)
    got = ResourceManager().get_node_resources(_Client(clustered=False), "nuc1", resources=_live(12))
    assert got["cpu_used"] == 0 and got["cpu_available"] == 12


def _service(client, monkeypatch, aggregate):
    svc = LXDService.__new__(LXDService)
    svc.connection_manager = SimpleNamespace(get_client=lambda node: client)
    svc.resource_manager = SimpleNamespace(
        get_node_resources=lambda client, node, resources=None: {"cpu_total": 2, "cpu_used": 8, "cpu_available": 0, "memory_total": 4}
    )
    calls = []

    def _aggregate(client, node_name, base_resources):
        calls.append(node_name)
        return aggregate

    # dev's layout: the clustered decision lives in node_resources.resources_for_host, which
    # both the dict facade and the detail read go through
    monkeypatch.setattr(nr_module, "aggregate_microcloud_resources", _aggregate)
    return svc, calls


def test_the_facade_hands_a_clustered_host_the_cluster_aggregate(monkeypatch):
    aggregate = NodeResources(cpu_total=80, cpu_used=307, cpu_available=0, cpu_allocated=307, memory_total=12)
    svc, calls = _service(_Client(clustered=True), monkeypatch, aggregate)
    got = svc.get_node_resources("mc-lab")
    assert calls == ["mc-lab"]
    assert got["cpu_total"] == 80 and got["cpu_used"] == 307 and got["cpu_available"] == 0
    assert got["memory_total"] == 12


def test_the_facade_leaves_a_standalone_host_as_its_own_body(monkeypatch):
    svc, calls = _service(_Client(clustered=False), monkeypatch, NodeResources(cpu_total=99))
    got = svc.get_node_resources("nuc1")
    assert calls == []
    assert got["cpu_total"] == 2


def test_a_failed_aggregate_falls_back_to_the_member_body(monkeypatch):
    svc, calls = _service(_Client(clustered=True), monkeypatch, None)
    got = svc.get_node_resources("mc-lab")
    assert calls == ["mc-lab"] and got["cpu_total"] == 2
