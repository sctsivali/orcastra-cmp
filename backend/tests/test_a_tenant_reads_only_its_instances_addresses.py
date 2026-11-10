"""A Tenant's IPAM list keeps its own instances' addresses only.

Network, forward and load-balancer rows name no instance. Passed on any rule in their project,
they would show a Tenant granted one instance in a shared project the other party's forward
listen addresses (often public IPs) and its OVN networks and subnets.
"""

from app.core import org_scope
from app.routers.ipam import IPAMAllocation, _visible_allocation

HOST = "c1"
ACL = {HOST: {("p", "mine"): {"view"}}}


def _row(kind, used_by, project="p"):
    return IPAMAllocation(address="203.0.113.7", type=kind, used_by=used_by, network="uplink", node=HOST, project=project)


def _as(acl, row):
    allowed = org_scope._allowed_clusters_var.set({HOST})
    tenant = org_scope._tenant_acl_var.set(acl)
    try:
        return _visible_allocation(row)
    finally:
        org_scope._tenant_acl_var.reset(tenant)
        org_scope._allowed_clusters_var.reset(allowed)


def test_a_tenant_keeps_its_instance_and_loses_everything_that_names_no_instance() -> None:
    assert _as(ACL, _row("instance", "mine"))
    assert not _as(ACL, _row("instance", "theirs"))
    for kind in ("network", "network-forward", "network-load-balancer"):
        assert not _as(ACL, _row(kind, "uplink")), kind


def test_partners_keep_network_and_forward_rows() -> None:
    for kind in ("instance", "network", "network-forward"):
        assert _as(None, _row(kind, "theirs")), kind
