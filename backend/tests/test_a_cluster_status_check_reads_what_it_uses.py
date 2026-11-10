"""A status check reads the cluster-wide instance list only for an answer it keeps.

On a clustered host the members' aggregate replaces the connected member's own figures (its
CPU allocation and its GPU inventory) and lists what it needs itself, and a cluster-wide list
takes seconds on a multi-member cluster. The member's own figures are built only when the
aggregate is not available, from one list shared by both consumers.
"""

from types import SimpleNamespace

import pytest

from app.models.schemas import NodeResources
from app.services.lxd import resources as resources_module
from app.services.lxd.freshness import LiveRead
from app.services.lxd.resources import ResourceManager

AGGREGATE = NodeResources(cpu_total=264, memory_total=768, memory_used=100)


@pytest.fixture
def check(monkeypatch):
    lists = []

    def list_records(client):
        lists.append(1)
        return [], True

    monkeypatch.setattr(resources_module, "list_instance_records", list_records)
    monkeypatch.setattr(
        resources_module,
        "read_resources",
        lambda client, name: LiveRead(body={"cpu": {"total": 8}, "memory": {"total": 64, "used": 8}}, collected_at=None),
    )
    monkeypatch.setattr(
        resources_module,
        "get_node_metadata_store",
        lambda: SimpleNamespace(get_node_display_name=lambda name: name, get_all_node_metadata=dict),
    )

    def run(*, clustered: bool, aggregate):
        monkeypatch.setattr(resources_module, "resolve_clustered", lambda stored, client: clustered)
        monkeypatch.setattr(resources_module, "aggregate_microcloud_resources", aggregate)
        client = SimpleNamespace(
            host_info={"auth": "trusted", "environment": {"server": "lxd", "server_clustered": clustered}},
        )
        return ResourceManager().check_node_status(client, "c", "https://c.invalid:8443", clustered)

    return run, lists


def test_an_aggregating_cluster_lists_no_instances_for_figures_it_discards(check):
    run, lists = check

    status = run(clustered=True, aggregate=lambda **kwargs: AGGREGATE)

    assert status.connected and status.resources.cpu_total == 264
    assert lists == [], "the aggregate lists what it needs itself"


def test_a_failed_aggregate_falls_back_on_one_shared_list(check):
    run, lists = check

    status = run(clustered=True, aggregate=lambda **kwargs: None)

    assert status.connected and status.resources.cpu_total == 8
    assert lists == [1], "CPU allocation and GPU inventory share one read"


def test_a_standalone_host_reads_its_list_once(check):
    run, lists = check

    status = run(clustered=False, aggregate=lambda **kwargs: pytest.fail("standalone never aggregates"))

    assert status.connected and status.resources.memory_total == 64
    assert lists == [1]
