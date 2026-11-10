"""A Tenant sees a reference only when it names an instance the Tenant may see.

A reference that names no instance (a profile, a network, an ACL, a custom volume, an image)
parses to (project, None), which `check_tenant_permission` would pass on any rule in that
project. A Tenant granted one instance in a project it shares with another party would then read
that party's profiles, ACL bodies and volumes, and a project list would carry the whole project
inventory plus its config.

A Tenant keeps instance references only; a record reached only through a profile or a network
disappears from its view. Partners and Admins (no ACL) keep every reference, still held to the
Partner project bound.
"""

from app.core import org_scope
from app.core.host_detail import network_for_tier, network_state_for_tier, project_for_tier
from app.core.tenant_visibility import (
    filter_visible_lxd_references,
    tenant_acl_can_view_lxd_reference,
    tenant_can_view_lxd_reference,
)
from app.models.schemas import ProjectInfo
from app.routers.networks import NetworkInfo

HOST = "cluster-a"
# One rule on one instance in project p, which the Tenant shares with another party's instance,
# profile, ACL and volume.
ACL = {HOST: {("p", "mine"): {"view", "console"}}}
WIDE = {HOST: {("p", "*"): {"view"}}}

MINE = "/1.0/instances/mine?project=p"
MINE_ROOT = "/1.0/storage-pools/default/volumes/container/mine?project=p&target=member-2"
THEIRS = "/1.0/instances/theirs?project=p"
PROFILE = "/1.0/profiles/their-profile?project=p"
NETWORK = "/1.0/networks/their-net?project=p"
ACL_REF = "/1.0/network-acls/their-acl?project=p"
VOLUME = "/1.0/storage-pools/default/volumes/custom/their-data?project=p"
IMAGE = "/1.0/images/abc123?project=p"
NON_INSTANCE = [PROFILE, NETWORK, ACL_REF, VOLUME, IMAGE]


def _as(acl, fn):
    allowed = org_scope._allowed_clusters_var.set({HOST})
    tenant = org_scope._tenant_acl_var.set(acl)
    try:
        return fn()
    finally:
        org_scope._tenant_acl_var.reset(tenant)
        org_scope._allowed_clusters_var.reset(allowed)


def test_a_tenant_never_sees_a_reference_that_names_no_instance() -> None:
    for acl in (ACL, WIDE):
        for reference in NON_INSTANCE:
            assert not _as(acl, lambda: tenant_can_view_lxd_reference(HOST, reference)), reference
            assert not tenant_acl_can_view_lxd_reference(acl, HOST, reference), reference


def test_a_tenant_sees_its_own_instance_and_its_root_volume_only() -> None:
    assert _as(ACL, lambda: tenant_can_view_lxd_reference(HOST, MINE))
    assert _as(ACL, lambda: tenant_can_view_lxd_reference(HOST, MINE_ROOT))
    assert not _as(ACL, lambda: tenant_can_view_lxd_reference(HOST, THEIRS))
    assert tenant_acl_can_view_lxd_reference(ACL, HOST, MINE)
    assert not tenant_acl_can_view_lxd_reference(ACL, HOST, THEIRS)


def test_the_filter_keeps_instance_references_and_drops_the_member_name() -> None:
    kept = _as(ACL, lambda: filter_visible_lxd_references(HOST, [MINE, MINE_ROOT, THEIRS, *NON_INSTANCE]))
    assert kept == [MINE, "/1.0/storage-pools/default/volumes/container/mine?project=p"]
    assert not any("member-2" in reference for reference in kept)


def test_partners_and_admins_keep_every_reference() -> None:
    every = [MINE, MINE_ROOT, THEIRS, *NON_INSTANCE]
    assert _as(None, lambda: filter_visible_lxd_references(HOST, every)) == every
    assert tenant_acl_can_view_lxd_reference(None, HOST, PROFILE)


def _project() -> ProjectInfo:
    return ProjectInfo(
        name="p",
        host=HOST,
        description="shared with another organization",
        config={"features.images": "false", "limits.cpu": "4", "limits.memory": "3GiB", "user.owner": "b"},
        tags=["env_prod"],
        used_by=[MINE, THEIRS, PROFILE, VOLUME],
    )


def test_a_tenant_reads_a_project_as_its_limits_and_its_own_instances() -> None:
    seen = _as(ACL, lambda: project_for_tier(_project(), HOST, "tenant"))
    assert seen.config == {"limits.cpu": "4", "limits.memory": "3GiB"}
    assert seen.used_by == [MINE]
    assert seen.description == "" and seen.tags == []
    # A copy: the cached object a Partner reads next must keep everything.
    original = _project()
    _as(ACL, lambda: project_for_tier(original, HOST, "tenant"))
    assert original.used_by == [MINE, THEIRS, PROFILE, VOLUME]


def test_a_partner_reads_the_project_unchanged() -> None:
    project = _project()
    assert _as(None, lambda: project_for_tier(project, HOST, "partner")) is project


def _network() -> NetworkInfo:
    return NetworkInfo(
        name="lxdbr0", description="uplink to the provider's VLAN 40", type="bridge", managed=True,
        config={
            "ipv4.address": "10.66.3.1/24", "ipv4.nat": "true", "ipv6.address": "none",
            "bridge.mtu": "1500", "security.acls": "web", "dns.domain": "lab",
            "bridge.external_interfaces": "enp5s0", "tunnel.uplink.remote": "203.0.113.9",
            "bgp.peers.isp.password": "s3cret", "ovn.ingress_mode": "routed", "raw.dnsmasq": "x",
            "parent": "enp6s0", "user.note": "rack 4",
        },
        used_by=[MINE], status="Created", node=HOST, project="default",
    )


def test_a_tenant_reads_a_network_without_its_host_side() -> None:
    seen = network_for_tier(_network(), "tenant")
    assert seen.config == {
        "ipv4.address": "10.66.3.1/24", "ipv4.nat": "true", "ipv6.address": "none",
        "bridge.mtu": "1500", "security.acls": "web", "dns.domain": "lab",
    }
    assert seen.description == ""
    assert network_for_tier(_network(), "partner").config == _network().config


def test_a_tenant_reads_network_state_without_member_and_host_interfaces() -> None:
    state = {
        "addresses": [{"family": "inet", "address": "10.66.3.1", "netmask": "24", "scope": "global"}],
        "counters": {"bytes_received": 10, "bytes_sent": 20},
        "hwaddr": "00:16:3e:aa:bb:cc", "mtu": 1500, "state": "up", "type": "broadcast",
        "bridge": {"id": "8000.00163e", "upper_devices": ["enp5s0", "veth1"]},
        "ovn": {"chassis": "member-2"}, "bond": None, "vlan": None,
    }
    seen = network_state_for_tier(state, "tenant")
    assert seen == {
        "addresses": state["addresses"], "counters": state["counters"],
        "mtu": 1500, "state": "up", "type": "broadcast",
    }
    assert network_state_for_tier(state, "admin") is state


class _Projects:
    async def list_projects(self, host):
        return [{"name": "p", "host": host, "description": "", "used_by": [MINE, THEIRS, PROFILE],
                 "config": {"features.images": "false", "limits.cpu": "2"}}]


def test_the_project_list_route_hands_a_tenant_only_its_limits_and_instances() -> None:
    import asyncio

    from app.routers.projects import list_projects

    listed = _as(ACL, lambda: asyncio.run(list_projects(host=HOST, lxd_service=_Projects())))
    assert [p.used_by for p in listed.projects] == [[MINE]]
    assert [p.config for p in listed.projects] == [{"limits.cpu": "2"}]
    whole = _as(None, lambda: asyncio.run(list_projects(host=HOST, lxd_service=_Projects())))
    assert whole.projects[0].used_by == [MINE, THEIRS, PROFILE]
