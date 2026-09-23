"""A caller held to a project bound cannot reach the host through instance or profile config.

The project bound keeps one organization out of another's projects on a cluster they both hold,
and the host is under every project. A privileged container, raw.* config, an idmap base or size,
a disk from outside a storage pool (a host path, ceph: or cephfs:) or a host device type would
carry a bounded partner past it. The bound exists only where another party holds the cluster, so
a partner on its own cluster and an admin are unaffected, and a GPU device stays allowed.

Values are judged, not only keys: the create form sends security.privileged "false" for Deny and
security.idmap.isolated "true" for a unique map. And a profile edit is judged by what it adds or
changes, since the editor sends both maps whole.
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
    ({"config": {"Security.Privileged": " YES "}}, "Security.Privileged"),
    ({"config": {"security.privileged": "garbage"}}, "security.privileged"),
    ({"config": {"raw.lxc": "lxc.apparmor.profile=unconfined"}}, "raw.lxc"),
    ({"config": {"security.idmap.base": "0"}}, "security.idmap.base"),
    ({"config": {"security.idmap.size": "65536"}}, "security.idmap.size"),
    ({"config": {"security.syscalls.intercept.mount.allowed": "ext4"}}, "intercept"),
    ({"config": {"linux.kernel_modules": "overlay"}}, "kernel_modules"),
    ({"devices": {"h": {"type": "disk", "source": "/", "path": "/mnt/h"}}}, "host path"),
    ({"devices": {"r": {"type": "disk", "source": "ceph:rbd-pool/other-project_vol", "path": "/r"}}}, "storage pool"),
    ({"devices": {"f": {"type": "disk", "source": "cephfs:fs/some/path", "path": "/f"}}}, "storage pool"),
    ({"devices": {"s": {"type": "proxy", "listen": "tcp:0.0.0.0:1", "connect": "unix:/var/lib/x"}}}, "proxy"),
    ({"devices": {"u": {"type": "usb"}}}, "usb"),
]
SAFE = [
    {"config": {"security.privileged": "false"}},
    {"config": {"security.privileged": "0", "security.syscalls.intercept.mknod": "false"}},
    {"config": {"security.idmap.isolated": "true"}},
    {"config": {"security.idmap.isolated": "false", "raw.lxc": ""}},
    {"devices": {"g": {"type": "gpu", "gputype": "physical"}}},
    {"devices": {"data": {"type": "disk", "pool": "default", "source": "vol1", "path": "/data"}}},
    {"devices": {"cloud": {"type": "disk", "source": "cloud-init:config"}}},
    {"devices": {"agent": {"type": "disk", "source": "agent:config"}}},
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


@pytest.mark.parametrize("body", SAFE)
def test_a_setting_that_does_not_reach_the_host_passes_whatever_its_key(bounded, body: Dict[str, Any]) -> None:
    refuse_host_reach_where_bounded(HOST, body)


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


class _Profiles:
    """client.api["profiles"][name], holding one profile and recording what is PUT."""

    def __init__(self, current: Dict[str, Any]) -> None:
        self.current = current
        self.puts: List[Dict[str, Any]] = []

    def __getitem__(self, _name: str) -> "_Profiles":
        return self

    def get(self, **_kwargs: Any) -> Any:
        body = {"metadata": dict(self.current)}
        return type("R", (), {"json": lambda _self: body, "headers": {"ETag": "e1"}})()

    def put(self, json: Dict[str, Any], **_kwargs: Any) -> Any:
        self.puts.append(json)
        self.current = {**self.current, **json}
        return type("R", (), {"json": lambda _self: {"type": "sync", "metadata": {}}})()


class _ProfileClient:
    def __init__(self, current: Dict[str, Any]) -> None:
        self.profiles = _Profiles(current)
        self.api = {"profiles": self.profiles}


class _ProfileService:
    def __init__(self, client: Any = None) -> None:
        self.client = client

    def get_client_with_reconnect(self, _host: str) -> Any:
        return self.client


PORT_FORWARD = {"web": {"type": "proxy", "listen": "tcp:0.0.0.0:80", "connect": "tcp:127.0.0.1:80"}}
NESTING = {"security.nesting": "true", "security.syscalls.intercept.mknod": "true"}


async def _update(monkeypatch, current: Dict[str, Any], **change: Any) -> _ProfileClient:
    monkeypatch.setattr(profiles, "resolve_owner", lambda *_a: "mine")
    client = _ProfileClient(current)
    await profiles.update_profile("p1", host=HOST, project="mine", request=profiles.ProfileUpdateRequest(**change),
                                  if_match=None, lxd_service=_ProfileService(client), _=None, _rbac=PARTNER)
    return client


async def test_a_profile_create_is_refused_before_writing(bounded, monkeypatch) -> None:
    written: List[str] = []
    monkeypatch.setattr(profiles, "resolve_owner", lambda *_a: "mine")
    monkeypatch.setattr(profiles, "create_profile_sync", lambda *a, **k: written.append("create") or ({}, "e"))
    devices = {"h": {"type": "disk", "source": "/", "path": "/mnt/h"}}
    with pytest.raises(HTTPException) as caught:
        await profiles.create_profile(profiles.ProfileCreateRequest(name="p1", devices=devices), host=HOST,
                                      project="mine", lxd_service=_ProfileService(_ProfileClient({})), _=None,
                                      _rbac=PARTNER)
    assert caught.value.status_code == 403 and written == []


async def test_a_profile_editor_resave_keeps_what_the_profile_already_carries(bounded, monkeypatch) -> None:
    """The editor sends both maps whole. Changing the description must not trip on an existing entry."""
    current = {"description": "old", "config": dict(NESTING), "devices": dict(PORT_FORWARD)}
    client = await _update(monkeypatch, current, description="new text", config=dict(NESTING),
                           devices=dict(PORT_FORWARD))
    assert len(client.profiles.puts) == 1 and client.profiles.puts[0]["description"] == "new text"


@pytest.mark.parametrize(
    "change",
    [
        {"devices": {**PORT_FORWARD, "sock": {"type": "proxy", "listen": "tcp:0.0.0.0:2", "connect": "unix:/run/x"}}},
        {"devices": {"web": {**PORT_FORWARD["web"], "connect": "unix:/var/lib/lxd/unix.socket"}}},
        {"config": {**NESTING, "security.privileged": "true"}},
        {"config": {**NESTING, "security.syscalls.intercept.mount.allowed": "ext4"}},
    ],
    ids=["added-device", "changed-device", "added-switch", "added-intercept"],
)
async def test_a_profile_edit_that_adds_or_changes_host_reach_is_refused(bounded, monkeypatch, change) -> None:
    current = {"description": "old", "config": dict(NESTING), "devices": dict(PORT_FORWARD)}
    with pytest.raises(HTTPException) as caught:
        await _update(monkeypatch, current, **change)
    assert caught.value.status_code == 403


async def test_an_unbounded_profile_edit_is_untouched(monkeypatch) -> None:
    client = await _update(monkeypatch, {"config": {}, "devices": {}},
                           devices={"sock": {"type": "proxy", "listen": "tcp:0.0.0.0:2", "connect": "unix:/run/x"}})
    assert len(client.profiles.puts) == 1
