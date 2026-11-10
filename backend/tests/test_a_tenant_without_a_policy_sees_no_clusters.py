"""A tenant holding no active policy assignment has an empty cluster scope, never an org-wide one.

`org_scope_dependency` narrows a tenant's clusters to org scope intersected with the clusters
its ACL grants. An empty ACL means no access, as `check_tenant_permission` reads it. Skipping the
intersection for it would let `unreachable_nodes` on /instances and /vms name every down cluster
in the partner's organisation, and let every power route whose per-project check sits behind
TENANT_PERM_ENFORCEMENT_ENABLED (off by default) pass `verify_cluster_access`.

`set_org_scope_for_ws` (console, terminal and events sockets) must narrow the same way, so both
setters call one shared helper; the AST tests at the bottom pin that.
"""

import ast
import asyncio
import inspect
from pathlib import Path
from typing import Optional

import pytest

from app.core import org_scope
from app.core.auth import AuthenticatedUser


def _run(allowed: Optional[set], acl: Optional[dict], groups: list[str]) -> Optional[set]:
    """Drive the real dependency with only its two database resolvers replaced."""
    user = AuthenticatedUser(user_id="u1", email="someone@example.com", groups=groups)

    async def fake_allowed(_user, _db):
        return allowed

    async def fake_acl(_user, _db):
        return acl

    async def fake_tenant_scope(_user, _db):
        return acl, None, None

    async def fake_org_names(_user, _db):
        return []

    async def fake_visible_projects(_user, _db, tenant_grants=None, reachable=None):
        return None

    real = (
        org_scope.get_user_allowed_clusters,
        org_scope.get_tenant_acl_rules,
        org_scope.get_tenant_scope,
        org_scope.get_visible_projects,
        org_scope._resolve_user_org_names,
    )
    org_scope.get_user_allowed_clusters = fake_allowed
    org_scope.get_tenant_acl_rules = fake_acl
    org_scope.get_tenant_scope = fake_tenant_scope
    org_scope.get_visible_projects = fake_visible_projects
    org_scope._resolve_user_org_names = fake_org_names

    # important: read the scope inside the coroutine. asyncio.run executes in a copied context,
    # so from the caller the contextvar reads None, the "unrestricted" sentinel, whatever the
    # dependency did.
    async def drive():
        await org_scope.org_scope_dependency(user=user, db=None)
        return org_scope.get_current_allowed_clusters()

    try:
        return asyncio.run(drive())
    finally:
        (
            org_scope.get_user_allowed_clusters,
            org_scope.get_tenant_acl_rules,
            org_scope.get_tenant_scope,
            org_scope.get_visible_projects,
            org_scope._resolve_user_org_names,
        ) = real


def test_an_empty_acl_empties_the_cluster_scope() -> None:
    scope = _run(allowed={"nuc1", "nuc2"}, acl={}, groups=["role_tenant"])
    # important: compare with `set()`; apply_cluster_filter reads None as "no filter" and would
    # hand back the whole fleet.
    assert scope == set()


def test_a_tenant_with_a_policy_still_intersects() -> None:
    scope = _run(
        allowed={"nuc1", "nuc2"},
        acl={"nuc1": {("default", "*"): {"view"}}},
        groups=["role_tenant"],
    )
    assert scope == {"nuc1"}


def test_a_grant_outside_the_organisation_widens_nothing() -> None:
    """An ACL row naming a cluster the org does not own must not add it."""
    scope = _run(
        allowed={"nuc1"},
        acl={"nuc9": {("default", "*"): {"view"}}},
        groups=["role_tenant"],
    )
    assert scope == set()


def test_a_partner_keeps_its_organisation_scope() -> None:
    scope = _run(allowed={"nuc1", "nuc2"}, acl=None, groups=["role_partner"])
    assert scope == {"nuc1", "nuc2"}


def test_an_admin_keeps_the_unrestricted_scope() -> None:
    """An admin's scope stays None (unrestricted); narrowing it would hide the fleet from admins."""
    scope = _run(allowed=None, acl=None, groups=["role_admin"])
    assert scope is None


def _setter_node(name: str) -> ast.AST:
    """The AST node for one scope setter in org_scope.py."""
    source = Path(inspect.getfile(org_scope)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in org_scope.py")


def _calls(node: ast.AST) -> set:
    """Every name called inside `node`, direct or via an attribute.

    This walks for ast.Call rather than searching the source text: a comment naming the helper
    would satisfy a text search after the call itself was deleted.
    """
    called = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            called.add(func.id)
        elif isinstance(func, ast.Attribute):
            called.add(func.attr)
    return called


@pytest.mark.parametrize("setter", ["org_scope_dependency", "set_org_scope_for_ws"])
def test_both_scope_setters_narrow_through_the_shared_helper(setter: str) -> None:
    assert "_narrow_by_tenant_acl" in _calls(_setter_node(setter)), (
        f"{setter} does not call the shared helper, so the HTTP and WebSocket paths can "
        "disagree about what a tenant may see"
    )


@pytest.mark.parametrize("setter", ["org_scope_dependency", "set_org_scope_for_ws"])
def test_neither_scope_setter_intersects_on_its_own(setter: str) -> None:
    assert "intersection" not in _calls(_setter_node(setter)), (
        f"{setter} intersects inline. That is how the empty-ACL fallback survived: two copies "
        "of the same block, one of which nobody edited."
    )
