"""A caller held to a project bound cannot reach the host through instance or profile config.

The project bound keeps one organization out of another's projects on a cluster they both hold,
and the host is under every project. A privileged container, raw.* or idmap config, a host-path
disk or a host device type would carry a bounded partner past it. The bound exists only where
another party holds the cluster, so a partner on its own cluster and an admin are unaffected, and
a GPU device stays allowed.
"""

from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.core.instance_access import refuse_host_reach_where_bounded
from app.routers import instances, profiles, vms

HOST = "cluster-shared"
PARTNER = AuthenticatedUser(user_id="p", email="partner@example.test", groups=["role_partner"])

REACHING = [
    ({"config": {"security.privileged": "true"}}, "security.privileged"),
    ({"config": {"raw.lxc": "lxc.apparmor.profile=unconfined"}}, "raw.lxc"),
    ({"config": {"security.idmap.isolated": "false"}}, "security.idmap"),
    ({"devices": {"h": {"type": "disk", "source": "/", "path": "/mnt/h"}}}, "host path"),
    ({"devices": {"s": {"type": "proxy", "listen": "tcp:0.0.0.0:1", "connect": "unix:/var/lib/x"}}}, "proxy"),
    ({"devices": {"u": {"type": "usb"}}}, "usb"),
]


@pytest.fixture
def bounded():
    token = org_scope._visible_projects_var.set({HOST: {"default", "mine"}})
    allowed = org_scope._allowed_clusters_var.set({HOST, "cluster-own"})
    yield
    org_scope._visible_projects_var.reset(token)
    org_scope._allowed_clusters_var.reset(allowed)


@pytest.mark.parametrize("body,reason", REACHING)
def test_a_bounded_caller_is_refused_what_reaches_the_host(bounded, body: Dict[str, Any], reason: str) -> None:
    with pytest.raises(HTTPException) as caught:
        refuse_host_reach_where_bounded(HOST, body)
    assert caught.value.status_code == 403 and reason in caught.value.detail


def test_a_gpu_and_ordinary_settings_stay_allowed(bounded) -> None:
    refuse_host_reach_where_bounded(HOST, {"config": {"limits.cpu": "2", "security.nesting": "true"},
                                          "devices": {"g": {"type": "gpu"}, "eth0": {"type": "nic", "network": "br0"},
                                                      "root": {"type": "disk", "path": "/", "pool": "default"}}})


@pytest.mark.parametrize("body,reason", REACHING)
def test_an_unbounded_cluster_or_caller_is_untouched(bounded, body: Dict[str, Any], reason: str) -> None:
    refuse_host_reach_where_bounded("cluster-own", body)  # the partner's own cluster carries no bound
    token = org_scope._visible_projects_var.set(None)  # an admin carries no bound anywhere
    try:
        refuse_host_reach_where_bounded(HOST, body)
    finally:
        org_scope._visible_projects_var.reset(token)


# -- the four routes --------------------------------------------------------------------------


class _Service:
    def __init__(self) -> None:
        self.created: List[str] = []

    async def create_instance(self, **kwargs: Any) -> Dict[str, Any]:
        self.created.append(kwargs["name"])
        return {"operation_id": "op"}

    def get_node_names(self) -> List[str]:
        return [HOST]


async def test_the_instance_route_refuses_before_creating(bounded) -> None:
    service = _Service()
    request = instances.InstanceCreateRequest(name="vm1", host=HOST, image="alpine", project="mine",
                                              config={"security.privileged": "true"})
    with pytest.raises(HTTPException) as caught:
        await instances.create_instance(request, lxd_service=service, _=None, _rbac=PARTNER)
    assert caught.value.status_code == 403 and service.created == []


async def test_the_vm_route_refuses_before_creating(bounded) -> None:
    service = _Service()
    request = vms.VMCreateRequest(name="vm1", host=HOST, image="alpine", config={"raw.qemu": "-device x"})
    with pytest.raises(HTTPException) as caught:
        await vms.create_vm(request, lxd_service=service, _=None, _rbac=PARTNER)
    assert caught.value.status_code == 403 and service.created == []


class _ProfileClient:
    pass


class _ProfileService:
    def __init__(self) -> None:
        self.writes: List[str] = []

    def get_client_with_reconnect(self, _host: str) -> Any:
        return _ProfileClient()


@pytest.mark.parametrize("route", ["create", "update"])
async def test_the_profile_routes_refuse_before_writing(bounded, monkeypatch, route: str) -> None:
    written: List[str] = []
    monkeypatch.setattr(profiles, "resolve_owner", lambda *_a: "mine")
    monkeypatch.setattr(profiles, "create_profile_sync", lambda *a, **k: written.append("create") or ({}, "e"))
    monkeypatch.setattr(profiles, "update_profile_sync", lambda *a, **k: written.append("update") or ({}, "e"))
    devices = {"h": {"type": "disk", "source": "/", "path": "/mnt/h"}}
    with pytest.raises(HTTPException) as caught:
        if route == "create":
            await profiles.create_profile(profiles.ProfileCreateRequest(name="p1", devices=devices), host=HOST,
                                          project="mine", lxd_service=_ProfileService(), _=None, _rbac=PARTNER)
        else:
            await profiles.update_profile("p1", host=HOST, project="mine",
                                          request=profiles.ProfileUpdateRequest(devices=devices), if_match=None,
                                          lxd_service=_ProfileService(), _=None, _rbac=PARTNER)
    assert caught.value.status_code == 403 and written == []
