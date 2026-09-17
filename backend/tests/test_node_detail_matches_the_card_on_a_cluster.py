"""GET /nodes/{name}/resources must agree with the /nodes card on a clustered host.

/nodes aggregates every member of a MicroCloud; the detail endpoint built its body from
/1.0/resources, which a cluster answers for whichever member the connection landed on. The
node dialog opened from a card saying 6 cores and 12 GiB then showed 2 cores and 4 GiB on
every tab but System, and a project quota was bounded by one member's free memory. On a
clustered host the detail now returns the same aggregate the card does; a standalone host is
unchanged.
"""

from types import SimpleNamespace

from app.models.schemas import NodeResources
from app.services.lxd import lxd_service as svc_module
from app.services.lxd import node_resources as nr_module
from app.services.lxd.lxd_service import LXDService


class _Client:
    def __init__(self, clustered: bool) -> None:
        # `auth: trusted` because resolve_clustered only believes a trusted /1.0 body
        self.host_info = {"auth": "trusted", "environment": {"server_clustered": clustered, "server_name": "mc1"}}
        self.api = SimpleNamespace(resources=SimpleNamespace(get=lambda: SimpleNamespace(
            status_code=200, json=lambda: {"metadata": {"cpu": {"total": 2}, "memory": {"total": 4}}}
        )))


def _service(client, monkeypatch, aggregate):
    svc = LXDService.__new__(LXDService)
    svc.connection_manager = SimpleNamespace(get_client=lambda node: client)
    svc.resource_manager = SimpleNamespace(
        get_node_resources=lambda client, node, resources=None: {"cpu_total": 2, "memory_total": 4}
    )
    calls = []

    def _aggregate(client, node_name, base_resources):
        calls.append(node_name)
        return aggregate

    # dev's layout: the clustered decision lives in node_resources.resources_for_host, which
    # both the dict facade and the detail read go through
    monkeypatch.setattr(nr_module, "aggregate_microcloud_resources", _aggregate)
    monkeypatch.setattr(svc_module, "build_gpu_inventory", lambda body, records: None)
    monkeypatch.setattr(svc_module, "list_instance_records", lambda client: ([], True))
    monkeypatch.setattr(nr_module, "build_node_resources", lambda data, gpu: NodeResources(cpu_total=data["cpu_total"]))
    monkeypatch.setattr(svc_module, "read_resources", lambda client, node: SimpleNamespace(body={"cpu": {"total": 2}}))
    return svc, calls


def test_a_clustered_host_answers_with_the_cluster_aggregate(monkeypatch) -> None:
    aggregate = NodeResources(cpu_total=6, memory_total=12)
    svc, calls = _service(_Client(clustered=True), monkeypatch, aggregate)
    got = svc.get_node_resources_detail("mc-lab")
    assert got is aggregate, "the dialog must read what the card read"
    assert calls == ["mc-lab"]


def test_a_standalone_host_is_built_from_its_own_body(monkeypatch) -> None:
    svc, calls = _service(_Client(clustered=False), monkeypatch, NodeResources(cpu_total=99))
    got = svc.get_node_resources_detail("nuc2")
    assert got.cpu_total == 2
    assert calls == [], "no cluster read on a standalone host"


def test_a_failed_aggregate_falls_back_to_the_member_view(monkeypatch) -> None:
    svc, calls = _service(_Client(clustered=True), monkeypatch, None)
    got = svc.get_node_resources_detail("mc-lab")
    assert got.cpu_total == 2, "conservative and visible, not a 500"
    assert calls == ["mc-lab"]
