"""A tenant holding no active policy assignment must have an EMPTY cluster scope, not an org-wide one.

`org_scope_dependency` narrows a tenant's clusters to org-scope intersected with the clusters
their ACL grants. It used to skip that intersection when the ACL was empty, and the comment
directly above the skip said the opposite of what the code did:

    # if ACL is empty dict, tenant has no access (newly assigned, no policy yet)
    if acl_clusters:
        allowed = allowed.intersection(acl_clusters)
    # else: keep org-scoped clusters (tenant without ACL assignment has org-level view)

An empty ACL is a real answer, not a missing one, and `check_tenant_permission` already denied
every cluster for it. So a tenant with no policy saw zero instances while `unreachable_nodes`
on /instances and /vms named every down cluster in the partner's organisation, and every power
route whose per-project check sits behind TENANT_PERM_ENFORCEMENT_ENABLED (off by default) still
passed `verify_cluster_access`.

The block was duplicated verbatim in `set_org_scope_for_ws`, which is what the console, terminal
and events sockets use, so a fix applied to one of them would have left the other leaking with
nothing to notice. That is what the AST test at the bottom is for.
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

    async def fake_org_names(_user, _db):
        return []

    real = (
        org_scope.get_user_allowed_clusters,
        org_scope.get_tenant_acl_rules,
        org_scope._resolve_user_org_names,
    )
    org_scope.get_user_allowed_clusters = fake_allowed
    org_scope.get_tenant_acl_rules = fake_acl
    org_scope._resolve_user_org_names = fake_org_names

    # important: read the scope from INSIDE the coroutine. asyncio.run executes in a copied
    # context, so a contextvar the dependency sets is discarded when it returns and reading it
    # from the caller reports None, which is the "unrestricted" sentinel. That reads as a
    # passing admin case no matter what the dependency did.
    async def drive():
        await org_scope.org_scope_dependency(user=user, db=None)
        return org_scope.get_current_allowed_clusters()

    try:
        return asyncio.run(drive())
    finally:
        (
            org_scope.get_user_allowed_clusters,
            org_scope.get_tenant_acl_rules,
            org_scope._resolve_user_org_names,
        ) = real


def test_an_empty_acl_empties_the_cluster_scope() -> None:
    scope = _run(allowed={"nuc1", "nuc2"}, acl={}, groups=["role_tenant"])
    # important: `== set()`, not `is not None`. None is the security inversion here, because
    # apply_cluster_filter reads None as "no filter" and would hand back the whole fleet.
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
    """None, not set(). An over-eager narrowing here would break the admin god-view."""
    scope = _run(allowed=None, acl=None, groups=["role_admin"])
    assert scope is None


class BothScopeSettersNarrowThroughOneHelperTests:
    """Kept as a plain class so pytest collects the functions below, not this docstring."""


def _function_source(name: str) -> str:
    source = Path(inspect.getfile(org_scope)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node)
    raise AssertionError(f"{name} not found in org_scope.py")


@pytest.mark.parametrize("setter", ["org_scope_dependency", "set_org_scope_for_ws"])
def test_both_scope_setters_narrow_through_the_shared_helper(setter: str) -> None:
    body = _function_source(setter)
    assert "_narrow_by_tenant_acl(" in body, (
        f"{setter} does not narrow through the shared helper, so the HTTP and WebSocket paths "
        "can disagree about what a tenant may see"
    )


@pytest.mark.parametrize("setter", ["org_scope_dependency", "set_org_scope_for_ws"])
def test_neither_scope_setter_intersects_on_its_own(setter: str) -> None:
    body = _function_source(setter)
    assert ".intersection(" not in body, (
        f"{setter} intersects inline. That is how the empty-ACL fallback survived: two copies "
        "of the same block, one of which nobody edited."
    )
