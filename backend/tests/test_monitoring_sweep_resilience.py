"""One slow cluster must not take the monitoring dashboard down with it.

The fleet list raises when any cluster's read runs past its wall or finds the pool full, which is
right for the instance pages: a partial list cached as complete would hide that cluster's
instances. For the dashboard that would turn one hung cluster into a 503 for every viewer, so the
sweep reads the fleet through a variant that names such a cluster, and the dashboard shows the
rest with that cluster marked partial.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.core import org_scope
from app.core.blocking import BlockingPoolFull, BlockingTimeout
from app.models.schemas import NodeResources, NodeStatus
from app.services import monitoring_sweep
from app.services.lxd import instance as instance_module
from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.instance import FleetInventory, InstanceManager
from app.services.monitoring_sweep import SweepTimer, inventory_note, read_fleet
from tests._monitoring_usage_fakes import row


def _manager(clusters):
    manager = InstanceManager.__new__(InstanceManager)
    manager.cert_manager = SimpleNamespace(list_clusters=lambda: list(clusters))
    manager.connection_manager = SimpleNamespace(get_node_names=lambda: [])
    return manager


def _bounded(answers):
    async def run(fn, *args, **kwargs):
        if kwargs["label"] == "instance.inventory.registry":
            return fn(*args)
        answer = answers[args[0]]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return run


@pytest.fixture
def scoped():
    org_scope.set_allowed_clusters(None)
    yield
    org_scope.set_allowed_clusters(None)


@pytest.mark.asyncio
async def test_a_cluster_out_of_time_is_named_and_the_rest_of_the_fleet_answers(monkeypatch, scoped):
    answers = {
        "fast": (["vm-fast"], True, True),
        "hung": BlockingTimeout("instance.inventory[hung]", 30.0),
        "full": BlockingPoolFull("instance.inventory[full]", 20),
        "off": ([], False, False),
    }
    monkeypatch.setattr(instance_module, "run_blocking", _bounded(answers))

    fleet = await _manager(answers).read_fleet_inventory()

    assert fleet.vms == ["vm-fast"] and fleet.connected == ["fast"]
    assert fleet.timed_out == {"hung": "inventory", "full": "busy"}
    assert set(fleet.incomplete) == {"hung", "full", "off"}, "an offline cluster is incomplete, not timed out"


@pytest.mark.asyncio
async def test_the_instance_pages_keep_refusing_a_list_with_a_hole_in_it(monkeypatch, scoped):
    answers = {"fast": (["vm-fast"], True, True), "hung": BlockingTimeout("instance.inventory[hung]", 30.0)}
    monkeypatch.setattr(instance_module, "run_blocking", _bounded(answers))

    with pytest.raises(LXDConnectionError):
        await _manager(answers).list_all_vms_with_failures()


@pytest.mark.asyncio
async def test_a_registry_that_cannot_be_read_still_raises(monkeypatch, scoped):
    async def registry_times_out(fn, *args, **kwargs):
        raise BlockingTimeout(kwargs["label"], 5.0)

    monkeypatch.setattr(instance_module, "run_blocking", registry_times_out)

    with pytest.raises(LXDConnectionError):
        await _manager(["a"]).read_fleet_inventory()


class _Service:
    def __init__(self, inventory: FleetInventory, statuses):
        self.inventory = inventory
        self.statuses = {status.name: status for status in statuses}

    def get_node_names(self):
        return list(self.statuses)

    async def read_fleet_inventory(self):
        return self.inventory

    def check_node_status(self, name):
        return self.statuses[name]


def _online(name: str) -> NodeStatus:
    return NodeStatus(
        name=name,
        endpoint=f"https://{name}.invalid:8443",
        connected=True,
        reachable=True,
        resources=NodeResources(cpu_total=8, cpu_used=2, memory_total=1000, memory_used=400),
    )


@pytest.mark.asyncio
async def test_the_sweep_merges_inventory_and_status_timeouts():
    service = _Service(
        FleetInventory([], ["a"], ["b"], {"b": "inventory"}),
        [_online("a"), _online("b")],
    )

    fleet = await read_fleet(service, SweepTimer())

    assert fleet.timed_out == {"b": "inventory"}
    assert [status.name for status in fleet.statuses] == ["a", "b"]


def test_the_status_wall_defaults_well_above_the_slowest_measured_cluster():
    """A clustered status read, its members read in parallel, finishes well inside this wall."""
    assert monitoring_sweep.STATUS_WALL_SECONDS == 20.0


def test_a_partial_cluster_explains_itself():
    assert "not counted until the next one" in inventory_note("inventory")
    assert "earlier collection" in inventory_note("busy")


def test_the_dashboard_answers_with_a_partial_cluster_marked(monkeypatch):
    from app.routers import monitoring
    from tests.test_monitoring_member_cache_generation_fences import (
        _GenerationCache,
        _MetadataStore,
        _scoped,
        _stored_points,
    )

    cache = _GenerationCache()
    running = row("web", host="a")
    service = _Service(
        FleetInventory([running], ["a"], ["b"], {"b": "inventory"}),
        [_online("a"), _online("b")],
    )
    monkeypatch.setattr(monitoring, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(monitoring, "get_scope_cache_key", _scoped)
    monkeypatch.setattr(monitoring, "get_node_metadata_store", lambda: _MetadataStore())
    monkeypatch.setattr(monitoring, "tenant_acl_active", lambda: False)
    monkeypatch.setattr(monitoring, "_last_history_store_times", {})

    async def no_memory(lxd_service, usages):
        return list(usages), {}

    monkeypatch.setattr(monitoring, "apply_member_memory", no_memory)

    result = asyncio.run(monitoring.get_monitoring_dashboard(lxd_service=service, fresh=True))

    by_name = {node.node_name: node for node in result.nodes}
    assert by_name["a"].collection_timed_out is False and by_name["a"].running_instances == 1
    assert by_name["b"].is_online and by_name["b"].collection_timed_out is True
    assert by_name["b"].status_message == inventory_note("inventory")
    assert _stored_points(cache) == 1, "the chart keeps its point"
