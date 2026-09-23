"""An empty allowed-cluster set must deny everything, never fall open to everything.

`apply_cluster_filter` reads None as "no filter", which is how an admin gets the whole fleet.
An empty set has to mean the opposite. This is the consumer half of
`test_a_tenant_without_a_policy_sees_no_clusters.py`: that file proves a policy-less tenant now
gets an empty set, and this one proves an empty set is honoured by the filter, the gate, the
fan-out and the cache key.
"""

from typing import Iterator, List, Optional

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.org_scope import (
    apply_cluster_filter,
    get_scope_cache_key,
    verify_cluster_access,
)

FLEET = ["cluster-a", "cluster-b", "cluster-c"]


@pytest.fixture
def empty_scope() -> Iterator[None]:
    token = org_scope._allowed_clusters_var.set(set())
    acl_token = org_scope._tenant_acl_var.set({})
    try:
        yield
    finally:
        org_scope._tenant_acl_var.reset(acl_token)
        org_scope._allowed_clusters_var.reset(token)


def test_the_filter_returns_nothing(empty_scope: None) -> None:
    assert apply_cluster_filter(FLEET) == []


def test_every_cluster_is_refused(empty_scope: None) -> None:
    for cluster_id in FLEET:
        with pytest.raises(HTTPException) as raised:
            verify_cluster_access(cluster_id)
        assert raised.value.status_code == 403


def test_the_fan_out_has_no_nodes_to_ask(empty_scope: None) -> None:
    """`get_node_names` is the denominator of every unreachable_nodes computation.

    An empty scope has to produce an empty list, or a caller with no clusters is told which
    clusters did not answer.
    """
    from app.services.lxd.lxd_service import LXDService

    class _CertManager:
        def list_clusters(self) -> List[str]:
            return list(FLEET)

    service = LXDService.__new__(LXDService)
    service.cert_manager = _CertManager()

    assert service.get_node_names() == []


def _key_under(allowed: Optional[set], acl: Optional[dict]) -> str:
    token = org_scope._allowed_clusters_var.set(allowed)
    acl_token = org_scope._tenant_acl_var.set(acl)
    try:
        return get_scope_cache_key("instances:list")
    finally:
        org_scope._tenant_acl_var.reset(acl_token)
        org_scope._allowed_clusters_var.reset(token)


def test_an_empty_scope_gets_its_own_cache_key() -> None:
    """A tenant whose scope shrank must not read the payload written under the wider one."""
    empty = _key_under(set(), {})
    org_wide = _key_under(set(FLEET), {})
    unrestricted = _key_under(None, None)

    assert empty == "instances:list:scope=empty"
    assert empty != org_wide
    assert empty != unrestricted
    assert org_wide != unrestricted
