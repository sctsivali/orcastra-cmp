"""`used_by` names nothing outside the caller's project bound, on every list.

On a cluster shared by two organizations a record in the default project (a profile, a
network, a pool, a volume, an ACL) is used by both organizations' instances. The bound lives in
the shared filter, ahead of the tenant ACL, and every list calls it for every role: a call
inside `if tenant_acl_active():` would never run for a Partner.
"""

import ast
import pathlib

import pytest

from app.core import org_scope
from app.core.tenant_visibility import filter_visible_lxd_references

HOST = "nuc2"
REFS = [
    "/1.0/instances/a?project=mine",
    "/1.0/instances/b?project=theirs",
    "/1.0/instances/c",
    "/1.0/profiles/x?project=theirs",
]


@pytest.fixture
def bounded():
    token = org_scope._visible_projects_var.set({HOST: {"default", "mine"}})
    acl = org_scope._tenant_acl_var.set(None)
    yield
    org_scope._tenant_acl_var.reset(acl)
    org_scope._visible_projects_var.reset(token)


@pytest.fixture
def unbounded():
    token = org_scope._visible_projects_var.set(None)
    acl = org_scope._tenant_acl_var.set(None)
    yield
    org_scope._tenant_acl_var.reset(acl)
    org_scope._visible_projects_var.reset(token)


def test_a_bounded_partner_sees_only_references_inside_the_bound(bounded):
    assert filter_visible_lxd_references(HOST, REFS) == [
        "/1.0/instances/a?project=mine",
        "/1.0/instances/c",  # bare = default, in every bound
    ]


def test_an_unbounded_caller_sees_the_list_unchanged(unbounded):
    assert filter_visible_lxd_references(HOST, REFS) == REFS


def test_the_bound_runs_before_the_tenant_acl(bounded, monkeypatch):
    # A tenant whose ACL excludes `mine`: the bound passes it, the ACL then drops it.
    from app.core import tenant_visibility

    monkeypatch.setattr(tenant_visibility, "tenant_acl_active", lambda: True)
    monkeypatch.setattr(
        tenant_visibility,
        "tenant_can_view_lxd_reference",
        lambda cluster, ref, perm="view": "project=mine" not in ref,
    )
    assert filter_visible_lxd_references(HOST, REFS) == ["/1.0/instances/c"]


def test_every_list_filters_used_by_outside_the_tenant_branch():
    """A filter call nested under the tenant ACL guard leaks used_by to a Partner."""
    routers = pathlib.Path(__file__).resolve().parents[1] / "app" / "routers"
    handlers = {
        "networks.py": "list_networks",
        "network_acls.py": "list_network_acls",
        "storage_pools.py": "list_storage_pools",
        "storage_volumes.py": "list_storage_volumes",
        "profiles.py": "list_profiles",
    }
    offenders = []
    for filename, name in handlers.items():
        tree = ast.parse((routers / filename).read_text())
        fn = next(
            (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name),
            None,
        )
        assert fn is not None, f"{filename}:{name} not found"
        parents = {}
        for node in ast.walk(fn):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        calls = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("filter_visible_lxd_references")
        ]
        if not calls:
            offenders.append(f"{filename}:{name} never filters used_by")
            continue
        for call in calls:
            node = call
            while node in parents:
                node = parents[node]
                if isinstance(node, ast.If) and "tenant_acl_active" in ast.unparse(node.test):
                    offenders.append(f"{filename}:{name} filters used_by only for a tenant (line {call.lineno})")
                    break
    assert offenders == [], offenders
