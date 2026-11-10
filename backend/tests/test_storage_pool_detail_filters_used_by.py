"""The storage-pool detail route must filter used_by for a tenant, and refuse when nothing is left.

This is the behavioural half of `test_used_by_is_tenant_filtered.py`. The ratchet proves the
handler is shaped correctly; this proves the shape produces the right response.

`org_scope_dependency` narrows a tenant's reachable clusters to org scope intersected with their
ACL clusters (`_narrow_by_tenant_acl` in app/core/org_scope.py), so `verify_cluster_access`
refuses anything outside that set before the handler runs. The case that matters is a cluster the
tenant is granted, carrying projects their ACL does not cover.
"""

import asyncio
from typing import Optional

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.routers.storage_pools import get_storage_pool

HOST = "cluster-a"

# Two projects on one cluster. The tenant is granted alpha only, which is the situation a shared
# cluster creates: their own project, plus somebody else's sitting next to it.
ALPHA_REF = "/1.0/instances/alpha-web?project=alpha"
ALPHA_VOL = "/1.0/storage-pools/default/volumes/custom/alpha-data?project=alpha"
BETA_REF = "/1.0/instances/beta-db?project=beta"
BETA_VOL = "/1.0/storage-pools/default/volumes/custom/beta-backup?project=beta"

TENANT_ACL = {HOST: {("alpha", "*"): {"view"}}}


class _FakePool:
    def __init__(self, used_by):
        self.name = "default"
        self.driver = "zfs"
        self.description = "shared pool"
        self.config = {"source": "tank"}
        self.used_by = list(used_by)
        self.status = "Created"


class _FakeService:
    def __init__(self, pool):
        self._pool = pool

    def get_client_with_reconnect(self, host):
        service = self

        class _Client:
            class storage_pools:
                @staticmethod
                def get(name):
                    return service._pool

        return _Client()


def _call(used_by, acl: Optional[dict]):
    """Run the handler under a given ACL, the way the router dependency would have set it."""
    allowed_token = org_scope._allowed_clusters_var.set({HOST})
    acl_token = org_scope._tenant_acl_var.set(acl)
    try:
        return asyncio.run(
            get_storage_pool(
                pool_name="default",
                host=HOST,
                lxd_service=_FakeService(_FakePool(used_by)),
            )
        )
    finally:
        org_scope._tenant_acl_var.reset(acl_token)
        org_scope._allowed_clusters_var.reset(allowed_token)


def test_tenant_sees_only_its_own_instance_references() -> None:
    result = _call([ALPHA_REF, BETA_REF, ALPHA_VOL, BETA_VOL], TENANT_ACL)
    # A tenant keeps references to its instances only. A custom volume reference names no
    # instance and can name other parties' volumes, so it is withheld even under a project-wide
    # grant like this one.
    assert set(result.used_by) == {ALPHA_REF}
    # The other project's names must not appear at all.
    assert not any("beta" in reference for reference in result.used_by)


def test_tenant_is_refused_a_pool_with_nothing_visible() -> None:
    # The list endpoint drops this pool entirely, so the detail route must not resurrect it.
    with pytest.raises(HTTPException) as excinfo:
        _call([BETA_REF, BETA_VOL], TENANT_ACL)
    assert excinfo.value.status_code == 403


def test_admin_and_partner_keep_the_full_list() -> None:
    # get_tenant_acl_rules returns None for both, and that path must stay untouched.
    every = [ALPHA_REF, BETA_REF, ALPHA_VOL, BETA_VOL]
    result = _call(every, None)
    assert set(result.used_by) == set(every)


def test_node_carries_the_cluster_rather_than_an_empty_string() -> None:
    assert _call([ALPHA_REF], TENANT_ACL).node == HOST
    assert _call([ALPHA_REF], None).node == HOST
