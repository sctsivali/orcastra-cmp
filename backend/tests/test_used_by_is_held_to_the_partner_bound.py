"""A "used by" list is held to the project bound for every role, not only under a tenant ACL.

The lists filtered used_by only when a tenant ACL was active, and a Partner has none. On a
cluster bound to two organizations a Partner's network, ACL, storage pool and volume lists
therefore named the other organization's instances and projects in "used by", and counted them
in "in use by N". The helper now applies the bound first for everyone and the ACL after it,
and the list handlers call it for every row.
"""

import ast
from pathlib import Path

import pytest

from app.core.tenant_visibility import filter_visible_lxd_references
from tests.instance_access_fakes import CLUSTER, partner, scoped

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"

OWN = "/1.0/instances/web01"
FOREIGN_INSTANCE = "/1.0/instances/web02?project=other-db"
FOREIGN_PROFILE = "/1.0/profiles/default?project=other-db"
OWN_VOLUME = "/1.0/storage-pools/pool/volumes/custom/data?project=own-web"
REFS = [OWN, FOREIGN_INSTANCE, FOREIGN_PROFILE, OWN_VOLUME]


def test_a_bounded_partner_sees_only_references_inside_the_bound():
    with partner({"default", "own-web"}):
        assert filter_visible_lxd_references(CLUSTER, REFS) == [OWN, OWN_VOLUME]


def test_an_unbounded_caller_sees_every_reference():
    with scoped():
        assert filter_visible_lxd_references(CLUSTER, REFS) == REFS


def test_the_bound_applies_on_its_own_cluster_only():
    with partner({"default"}):
        assert filter_visible_lxd_references("cluster-b", REFS) == REFS


def test_the_bound_applies_before_the_tenant_acl():
    """A wildcard ACL would admit other-db, and the bound must still keep it out."""
    acl = {CLUSTER: {("*", "*"): {"view"}}}
    with scoped(allowed={CLUSTER}, acl=acl, bound={CLUSTER: {"default"}}):
        assert filter_visible_lxd_references(CLUSTER, REFS) == [OWN]


def test_the_tenant_acl_still_narrows_inside_the_bound():
    acl = {CLUSTER: {("own-web", "*"): {"view"}}}
    with scoped(allowed={CLUSTER}, acl=acl, bound={CLUSTER: {"default", "own-web"}}):
        assert filter_visible_lxd_references(CLUSTER, REFS) == [OWN_VOLUME]


def test_nothing_in_gives_nothing_out():
    with partner({"default"}):
        assert filter_visible_lxd_references(CLUSTER, None) == []


LIST_HANDLERS = [
    ("networks.py", "list_networks"),
    ("network_acls.py", "list_network_acls"),
    ("storage_pools.py", "list_storage_pools"),
    ("storage_volumes.py", "list_storage_volumes"),
]


def _under_tenant_branch(node: ast.AST, parents: dict) -> bool:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.If) and "tenant_acl_active" in ast.unparse(current.test):
            return True
        current = parents.get(current)
    return False


@pytest.mark.parametrize("filename,handler", LIST_HANDLERS)
def test_every_list_filters_used_by_outside_the_tenant_branch(filename, handler):
    tree = ast.parse((ROUTERS / filename).read_text())
    function = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == handler
    )
    parents = {child: parent for parent in ast.walk(function) for child in ast.iter_child_nodes(parent)}
    calls = [
        node for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "filter_visible_lxd_references"
    ]
    assert calls, f"{filename}:{handler} no longer filters used_by at all"
    assert any(not _under_tenant_branch(call, parents) for call in calls), (
        f"{filename}:{handler} filters used_by only for a tenant, so a Partner on a shared "
        "cluster sees the other organization's references"
    )
