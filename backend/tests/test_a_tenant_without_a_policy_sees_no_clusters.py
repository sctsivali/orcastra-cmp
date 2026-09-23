"""A tenant holding no active policy assignment must have an EMPTY cluster scope, not an org-wide one.

`org_scope_dependency` narrows a tenant's clusters to org scope intersected with the clusters
their ACL grants. It used to skip that intersection when the ACL was empty, so a tenant with no
policy (never assigned, every assignment expired, or revoked) kept every cluster of their
organisations. `verify_cluster_access` then passed on that set, the per-project power check sits
behind TENANT_PERM_ENFORCEMENT_ENABLED, and break-glass reused the same set for the terminal.

The block was duplicated verbatim in `set_org_scope_for_ws`, which the console, terminal and
events sockets use, so both setters are driven here and pinned to one helper by an AST test.
"""

import ast
import asyncio
import inspect
from pathlib import Path
from typing import Any, Optional

import pytest

from app.core import org_scope
from app.core.auth import AuthenticatedUser

ORG_CLUSTERS = {"cluster-a", "cluster-b"}


def _user(role: str) -> AuthenticatedUser:
    # note: resolve_roles caches by user_id, so every role gets its own id.
    return AuthenticatedUser(
        user_id=f"{role}-no-policy-probe",
        email=f"{role}@example.test",
        groups=[f"role_{role}"],
    )


@pytest.fixture
def resolvers(monkeypatch: pytest.MonkeyPatch):
    """Replace the database resolvers the setters call. Returns a setter for their answers."""
    answers: dict[str, Any] = {"allowed": None, "acl": None}

    async def fake_allowed(_user: AuthenticatedUser, _db: Any) -> Optional[set]:
        return answers["allowed"]

    async def fake_acl(_user: AuthenticatedUser, _db: Any) -> Optional[dict]:
        return answers["acl"]

    async def fake_none(_user: AuthenticatedUser, _db: Any, _allowed: Any = None) -> None:
        return None

    async def fake_shared(_db: Any) -> set:
        return set()

    async def fake_org_names(_user: AuthenticatedUser, _db: Any) -> list:
        return []

    monkeypatch.setattr(org_scope, "get_user_allowed_clusters", fake_allowed)
    monkeypatch.setattr(org_scope, "get_tenant_acl_rules", fake_acl)
    monkeypatch.setattr(org_scope, "get_visible_projects", fake_none)
    monkeypatch.setattr(org_scope, "_clusters_with_several_orgs", fake_shared)
    monkeypatch.setattr(org_scope, "_resolve_user_org_names", fake_org_names)

    def set_answers(allowed: Optional[set], acl: Optional[dict]) -> None:
        answers["allowed"] = allowed
        answers["acl"] = acl

    return set_answers


class _FakeSessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _http_scope(user: AuthenticatedUser) -> Optional[set]:
    # important: read the scope from INSIDE the coroutine. asyncio.run executes in a copied
    # context, so a contextvar the dependency sets is discarded when it returns, and reading it
    # from the caller reports None, which is the unrestricted sentinel.
    async def drive() -> Optional[set]:
        await org_scope.org_scope_dependency(user=user, db=None)
        return org_scope.get_current_allowed_clusters()

    return asyncio.run(drive())


def _ws_scope(user: AuthenticatedUser, monkeypatch: pytest.MonkeyPatch) -> Optional[set]:
    from app.core import database

    monkeypatch.setattr(database, "async_session_maker", lambda: _FakeSessionContext())

    async def drive() -> Optional[set]:
        await org_scope.set_org_scope_for_ws(user)
        return org_scope.get_current_allowed_clusters()

    return asyncio.run(drive())


@pytest.fixture(params=["http", "ws"])
def scope_of(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    if request.param == "http":
        return _http_scope
    return lambda user: _ws_scope(user, monkeypatch)


def test_an_empty_acl_empties_the_cluster_scope(resolvers, scope_of) -> None:
    resolvers(allowed=set(ORG_CLUSTERS), acl={})
    # important: `== set()`, not `is not None`. None is the security inversion here, because
    # apply_cluster_filter reads None as "no filter" and would hand back the whole fleet.
    assert scope_of(_user("tenant")) == set()


def test_a_tenant_with_a_policy_still_intersects(resolvers, scope_of) -> None:
    resolvers(allowed=set(ORG_CLUSTERS), acl={"cluster-a": {("default", "*"): {"view"}}})
    assert scope_of(_user("tenant")) == {"cluster-a"}


def test_a_grant_outside_the_organisation_widens_nothing(resolvers, scope_of) -> None:
    resolvers(allowed={"cluster-a"}, acl={"cluster-z": {("default", "*"): {"view"}}})
    assert scope_of(_user("tenant")) == set()


def test_a_partner_keeps_its_organisation_scope(resolvers, scope_of) -> None:
    resolvers(allowed=set(ORG_CLUSTERS), acl=None)
    assert scope_of(_user("partner")) == ORG_CLUSTERS


def test_an_admin_keeps_the_unrestricted_scope(resolvers) -> None:
    """None, not set(). An over-eager narrowing here would break the admin view."""
    resolvers(allowed=None, acl=None)
    assert _http_scope(_user("admin")) is None


def test_the_helper_keeps_none_unrestricted() -> None:
    assert org_scope._narrow_by_tenant_acl(None, {}) is None
    assert org_scope._narrow_by_tenant_acl({"cluster-a"}, None) == {"cluster-a"}
    assert org_scope._narrow_by_tenant_acl({"cluster-a"}, {}) == set()


def _function_source(name: str) -> str:
    source = Path(inspect.getfile(org_scope)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
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
        f"{setter} intersects inline. Two copies of one block is how the empty-ACL fallback "
        "survived in one of them."
    )
