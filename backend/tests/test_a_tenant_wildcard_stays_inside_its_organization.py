"""A tenant's '*' project rule must not reach another organization's projects on a shared cluster.

`TenantPolicyRule.project_name` defaults to '*', and '*' matched every LXD project. On a cluster
bound to several organizations that named the neighbours' projects too, and a tenant was not
given the Partner project bound at all (`get_visible_projects` answers None for a tenant). So a
tenant holding a wildcard listed, read and, with the enforcement flag off, powered another
customer's instances.

The fix expands each wildcard with its own organization's projects before grants are merged,
and gives a tenant on a shared cluster a project bound of its own through `_project_bound_for`,
which both scope setters use.
"""

import ast
import asyncio
import contextvars
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.core.tenant_wildcard import (
    RuleGrant,
    confine,
    confine_wildcards,
    tenant_project_bound,
)

SHARED = "cluster-shared"
OWN = "cluster-a"


def _grant(org: int, cluster: str, project: str, perms: tuple[str, ...] = ("view",)) -> RuleGrant:
    return RuleGrant(
        organization_id=org,
        cluster_id=cluster,
        project_name=project,
        instance_name="*",
        permissions=perms,
    )


# -- pure expansion --------------------------------------------------------------------------


def test_a_wildcard_on_a_shared_cluster_becomes_default_plus_its_organizations_projects() -> None:
    confined = confine([_grant(1, SHARED, "*")], {SHARED}, {(1, SHARED): {"own-web"}})
    assert {g.project_name for g in confined} == {"default", "own-web"}
    assert all(g.organization_id == 1 and g.permissions == ("view",) for g in confined)


def test_a_wildcard_on_an_unshared_cluster_keeps_its_meaning() -> None:
    grant = _grant(1, OWN, "*")
    assert confine([grant], {SHARED}, {}) == [grant]


def test_a_concrete_project_is_never_rewritten() -> None:
    grant = _grant(1, SHARED, "own-web")
    assert confine([grant], {SHARED}, {(1, SHARED): {"own-db"}}) == [grant]


def test_two_organizations_each_expand_with_their_own_projects() -> None:
    confined = confine(
        [_grant(1, SHARED, "*", ("view",)), _grant(2, SHARED, "*", ("view", "operate"))],
        {SHARED},
        {(1, SHARED): {"own-web"}, (2, SHARED): {"other-db"}},
    )
    by_org = {(g.organization_id, g.project_name) for g in confined}
    assert by_org == {(1, "default"), (1, "own-web"), (2, "default"), (2, "other-db")}


def test_no_query_runs_without_a_wildcard() -> None:
    async def load_shared(_db: Any) -> set:
        raise AssertionError("the shared-cluster query ran for a grant list with no wildcard")

    grants = [_grant(1, SHARED, "own-web")]
    assert asyncio.run(confine_wildcards(object(), grants, load_shared)) == grants


# -- the tenant bound -----------------------------------------------------------------------


def test_the_tenant_bound_names_default_plus_the_acl_projects_on_shared_clusters_only() -> None:
    acl = {
        SHARED: {("own-web", "*"): {"view"}, ("own-db", "vm1"): {"view"}},
        OWN: {("*", "*"): {"view"}},
    }
    assert tenant_project_bound(acl, {SHARED}) == {SHARED: {"default", "own-web", "own-db"}}


def test_the_tenant_bound_is_none_where_nothing_is_shared() -> None:
    assert tenant_project_bound({OWN: {("*", "*"): {"view"}}}, {SHARED}) is None
    assert tenant_project_bound({}, {SHARED}) is None


def test_a_leftover_wildcard_never_widens_the_tenant_bound() -> None:
    """A cluster that became shared between two reads still bounds to named projects only."""
    assert tenant_project_bound({SHARED: {("*", "*"): {"view"}}}, {SHARED}) == {
        SHARED: {"default"}
    }


# -- get_tenant_acl_rules end to end over a table-keyed fake session --------------------------


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeSession:
    """Answers by which tables the statement reads, not by call order."""

    def __init__(
        self,
        rules: list[tuple[int, str, str, list[str]]],
        org_projects: list[tuple[int, str, str]],
        shared: list[str],
    ) -> None:
        self.rules = rules
        self.org_projects = org_projects
        self.shared = shared
        self.queries: list[str] = []

    async def execute(self, stmt: Any) -> _Result:
        sql = str(stmt)
        self.queries.append(sql)
        if "tenant_policy_rules" in sql and "organization_clusters" in sql:
            return _Result(self.org_projects)
        if "tenant_policy_rules" in sql:
            return _Result(
                [
                    (
                        SimpleNamespace(
                            cluster_id=cluster,
                            project_name=project,
                            instance_name="*",
                            permissions=perms,
                        ),
                        org,
                    )
                    for org, cluster, project, perms in self.rules
                ]
            )
        if "tenant_policy_assignments" in sql:
            future = datetime.now(timezone.utc) + timedelta(days=1)
            return _Result([SimpleNamespace(policy_id=7, expires_at=future)])
        if "organization_members" in sql:
            return _Result([41])
        if "organization_clusters" in sql:
            return _Result(self.shared)
        raise AssertionError(f"unexpected query: {sql[:120]}")


def _tenant() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="tenant-wildcard-probe", email="tenant@example.test", groups=["role_tenant"]
    )


def _acl_for(session: FakeSession) -> dict:
    acl = asyncio.run(org_scope.get_tenant_acl_rules(_tenant(), session))
    assert acl is not None
    return acl


def _allowed(acl: dict, project: str, perm: str = "view") -> bool:
    def check() -> bool:
        org_scope._tenant_acl_var.set(acl)
        return org_scope.check_tenant_permission(SHARED, project, "x", perm)

    return contextvars.copy_context().run(check)


def test_the_acl_carries_no_wildcard_on_a_shared_cluster() -> None:
    session = FakeSession(
        rules=[(1, SHARED, "*", ["view"]), (1, OWN, "*", ["view"])],
        org_projects=[(1, SHARED, "own-web"), (2, SHARED, "other-db")],
        shared=[SHARED],
    )
    acl = _acl_for(session)

    assert {p for (p, _i) in acl[SHARED]} == {"default", "own-web"}
    assert ("*", "*") in acl[OWN]
    assert _allowed(acl, "own-web") is True
    assert _allowed(acl, "other-db") is False


def test_one_organizations_wildcard_does_not_lend_its_permissions_to_another() -> None:
    """A tenant in two organizations gets each wildcard inside its own organization only."""
    session = FakeSession(
        rules=[(1, SHARED, "*", ["view"]), (2, SHARED, "*", ["view", "operate"])],
        org_projects=[(1, SHARED, "own-web"), (2, SHARED, "other-db")],
        shared=[SHARED],
    )
    acl = _acl_for(session)

    assert _allowed(acl, "other-db", "operate") is True
    assert _allowed(acl, "own-web", "operate") is False
    assert _allowed(acl, "own-web", "view") is True


# -- both setters use the one bound -----------------------------------------------------------


def test_a_partner_bound_still_comes_from_get_visible_projects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = {SHARED: {"default", "partner-proj"}}

    async def fake_visible(_user: Any, _db: Any) -> dict:
        return sentinel

    monkeypatch.setattr(org_scope, "get_visible_projects", fake_visible)
    assert asyncio.run(org_scope._project_bound_for(object(), object(), None)) is sentinel


def test_a_tenant_bound_comes_from_its_acl(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_shared(_db: Any) -> set:
        return {SHARED}

    monkeypatch.setattr(org_scope, "_clusters_with_several_orgs", fake_shared)
    acl = {SHARED: {("own-web", "*"): {"view"}}, OWN: {("*", "*"): {"view"}}}
    bound: Optional[dict] = asyncio.run(org_scope._project_bound_for(object(), object(), acl))
    assert bound == {SHARED: {"default", "own-web"}}


def _function_source(name: str) -> str:
    source = Path(inspect.getfile(org_scope)).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError(f"{name} not found in org_scope.py")


@pytest.mark.parametrize("setter", ["org_scope_dependency", "set_org_scope_for_ws"])
def test_both_setters_take_the_bound_from_one_place(setter: str) -> None:
    body = _function_source(setter)
    assert "_project_bound_for(" in body
    assert "get_visible_projects(" not in body, (
        f"{setter} reads the Partner-only bound directly, so a tenant on a shared cluster "
        "gets no project bound on that path"
    )


class _FakeSessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.mark.parametrize("path", ["http", "ws"])
def test_a_tenant_on_a_shared_cluster_is_bound_on_both_paths(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import database

    acl = {SHARED: {("own-web", "*"): {"view"}}}

    async def fake_allowed(_user: Any, _db: Any) -> set:
        return {SHARED}

    async def fake_acl(_user: Any, _db: Any) -> dict:
        return acl

    async def fake_shared(_db: Any) -> set:
        return {SHARED}

    async def fake_org_names(_user: Any, _db: Any) -> list:
        return []

    monkeypatch.setattr(org_scope, "get_user_allowed_clusters", fake_allowed)
    monkeypatch.setattr(org_scope, "get_tenant_acl_rules", fake_acl)
    monkeypatch.setattr(org_scope, "_clusters_with_several_orgs", fake_shared)
    monkeypatch.setattr(org_scope, "_resolve_user_org_names", fake_org_names)
    monkeypatch.setattr(database, "async_session_maker", lambda: _FakeSessionContext())

    async def drive() -> tuple[bool, bool]:
        if path == "http":
            await org_scope.org_scope_dependency(user=_tenant(), db=object())
        else:
            await org_scope.set_org_scope_for_ws(_tenant())
        return (
            org_scope.project_is_visible(SHARED, "own-web"),
            org_scope.project_is_visible(SHARED, "other-db"),
        )

    assert asyncio.run(drive()) == (True, False)
