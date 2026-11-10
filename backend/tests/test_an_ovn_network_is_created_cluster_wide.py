"""On a cluster, only member-defined network types get the per-member define step.

A bridge, macvlan, physical or sriov network carries member-specific config, so LXD's clustered
create for it is two-phase: POST ?target=<member> on each member, then one cluster-wide POST.
An OVN network carries none and the daemon refuses the define step ('Network type "ovn" does
not support member specific config'), so it is created with one cluster-wide POST. On a
MicroCloud, OVN is the network type a tenant can create in its own project.
"""

from types import SimpleNamespace
from typing import Any, Dict, List
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import networks as networks_module
from app.core.dependencies import check_read_only_mode
from app.core.org_scope import org_scope_dependency
from app.core.rbac import get_current_user
from app.services.lxd_service import get_lxd_service

MEMBERS = ["mc1", "mc2", "mc3"]


@asynccontextmanager
async def _no_configuration_lock(_db, _host):
    yield


class _Networks:
    """client.api.networks: records every POST and answers the read-back."""

    def __init__(self) -> None:
        self.posts: List[Dict[str, Any]] = []

    def post(self, json: Dict[str, Any], params: Dict[str, Any], timeout=None) -> None:
        self.posts.append({"json": json, "params": dict(params)})

    def __getitem__(self, name: str):
        return SimpleNamespace(get=lambda params=None: SimpleNamespace(
            json=lambda: {"metadata": {"name": name, "type": self.posts[-1]["json"]["type"], "managed": True, "config": {}}}
        ))


def _app(monkeypatch, clustered: bool):
    nets = _Networks()
    client = SimpleNamespace(api=SimpleNamespace(networks=nets))
    svc = SimpleNamespace(get_client_with_reconnect=lambda host: client)

    monkeypatch.setattr(networks_module, "_is_clustered", lambda c: clustered)
    monkeypatch.setattr(networks_module, "_cluster_member_names", lambda c: list(MEMBERS))
    monkeypatch.setattr(networks_module, "list_project_configs", lambda c: {})
    monkeypatch.setattr(networks_module, "resolve_owner", lambda c, kind, project: project or "default")
    monkeypatch.setattr(networks_module, "project_is_visible", lambda host, project: True)
    monkeypatch.setattr(
        networks_module, "_uplink_candidates",
        lambda c, host: {"UPLINK": {"name": "UPLINK", "type": "physical", "managed": True}},
    )
    monkeypatch.setattr(networks_module, "verify_cluster_access", lambda host: None)
    monkeypatch.setattr(
        networks_module,
        "serialize_cluster_configuration",
        _no_configuration_lock,
    )
    monkeypatch.setattr(networks_module.activity_logger, "log_activity", lambda **kw: None)
    monkeypatch.setattr(
        networks_module, "get_cache_manager",
        lambda: SimpleNamespace(invalidate_resource_cache=lambda *a, **k: None,
                                delete_pattern=lambda *a, **k: None),
        raising=False,
    )

    app = FastAPI()
    app.include_router(networks_module.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[check_read_only_mode] = lambda: None
    return TestClient(app, raise_server_exceptions=False), nets


def _targets(posts):
    return [p["params"].get("target") for p in posts]


def test_an_ovn_network_on_a_cluster_is_one_cluster_wide_post(monkeypatch) -> None:
    client, nets = _app(monkeypatch, clustered=True)
    r = client.post("/networks", params={"host": "mc-lab", "project": "UAT-Hermes"}, json={
        "name": "ovn-qa", "type": "ovn", "config": {"network": "UPLINK", "ipv4.address": "192.168.9.1/24"},
    })
    assert r.status_code == 200, r.text
    assert _targets(nets.posts) == [None], "no per-member define for a type without member config"
    assert nets.posts[0]["json"]["config"]["network"] == "UPLINK"
    assert nets.posts[0]["params"]["project"] == "UAT-Hermes"


def test_a_bridge_on_a_cluster_is_defined_per_member_then_created(monkeypatch) -> None:
    client, nets = _app(monkeypatch, clustered=True)
    r = client.post("/networks", params={"host": "mc-lab"}, json={
        "name": "br-qa", "type": "bridge", "config": {"ipv4.address": "10.77.0.1/24"},
    })
    assert r.status_code == 200, r.text
    assert _targets(nets.posts) == MEMBERS + [None]
    for p in nets.posts[:-1]:
        assert "config" not in p["json"], "the define step carries no cluster-wide config"
    assert nets.posts[-1]["json"]["config"] == {"ipv4.address": "10.77.0.1/24"}


def test_a_standalone_host_gets_one_post_whatever_the_type(monkeypatch) -> None:
    for typ, cfg in (("bridge", {}), ("ovn", {"network": "UPLINK"})):
        client, nets = _app(monkeypatch, clustered=False)
        r = client.post("/networks", params={"host": "nuc2"}, json={"name": "n-" + typ, "type": typ, "config": cfg})
        assert r.status_code == 200, r.text
        assert _targets(nets.posts) == [None]


@pytest.mark.parametrize("typ", sorted(networks_module.MEMBER_DEFINED_NETWORK_TYPES))
def test_every_member_defined_type_is_one_lxd_knows(typ: str) -> None:
    assert typ in {"bridge", "macvlan", "physical", "sriov"}


def test_ovn_is_never_member_defined() -> None:
    assert "ovn" not in networks_module.MEMBER_DEFINED_NETWORK_TYPES
