"""A grant of one project must not reach a resource that no project contains.

`projects: ["tenant-a"]` is enforced by binding the request to that project, which bounds
instances, images, volumes and snapshots. It does not bound a resource whose identity is a
project name, nor one that lives above projects entirely.

Authorising only the ambient project fails when the target comes from elsewhere. On the operation
proxy the target is `params["name"]`, so `delete_project` with `project: "tenant-a"` and
`params: {"name": "tenant-b"}` would pass the cluster, project and admin-level checks and destroy
another tenant's project. On the raw proxy the target is in the path, as in
`DELETE /1.0/projects/tenant-b` carrying `project=tenant-a`. A storage pool is cluster-wide and
its definition carries a source path on the LXD host, which runs as root. Both proxies apply the
same rule.
"""

import ast
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.core.api_key_capabilities import (
    _PROJECT_UNBOUNDABLE_PREFIXES,
    assert_project_scope_allows_path,
    project_grant_is_unrestricted,
)
from app.services.lxd.proxy_operations import (
    CLUSTER_WIDE_OPERATIONS,
    PROXY_OPERATION_LEVELS,
)

ROUTER = Path(__file__).resolve().parents[1] / "app" / "routers" / "integrations.py"

NARROW = ["tenant-a"]
WILDCARD = ["*"]


# ── The raw proxy door ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/1.0/projects/tenant-b",
        "/1.0/projects",
        "/1.0/storage-pools/default",
        "/1.0/networks/br0",
        "/1.0/profiles/default",
        # The check decodes before matching, the way the traversal guard beside it does.
        "/1.0/%70rojects/tenant-b",
    ],
)
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_narrow_grant_cannot_change_what_no_project_contains(path: str, method: str) -> None:
    with pytest.raises(HTTPException) as raised:
        assert_project_scope_allows_path(path, method, NARROW)
    assert raised.value.status_code == 403
    assert "tenant-a" in raised.value.detail, "the refusal should name what the key does hold"


@pytest.mark.parametrize("path", ["/1.0/projects/tenant-b", "/1.0/storage-pools/default"])
def test_a_wildcard_grant_still_reaches_them(path: str) -> None:
    """`*` means every project on the cluster, so no single project bounds it."""
    assert_project_scope_allows_path(path, "DELETE", WILDCARD)


@pytest.mark.parametrize("path", ["/1.0/projects", "/1.0/networks", "/1.0/storage-pools"])
def test_reading_topology_is_still_allowed(path: str) -> None:
    """Refusing GET would break integrations that legitimately enumerate the cluster.

    Disclosure is a smaller harm than destruction, so a read stays open for every grant. The
    operation proxy narrows its own `list_projects` result, which the raw proxy cannot do without
    rewriting LXD's response.
    """
    assert_project_scope_allows_path(path, "GET", NARROW)


# The narrow-key write rule is an allowlist, so a lookalike such as `/storage-pools-not-really`
# is refused like any resource outside it. The prefix-boundary case (`/instances-archive`) lives
# in test_project_limited_keys_are_bounded.py.
@pytest.mark.parametrize(
    "path", ["/1.0/instances/web-1", "/1.0/images/abc", "/1.0/operations/abc"]
)
def test_a_project_bounded_resource_is_untouched(path: str) -> None:
    """The rule must not swallow the ordinary write surface a narrow grant is meant to have."""
    assert_project_scope_allows_path(path, "DELETE", NARROW)


def test_an_absent_projects_field_is_treated_as_narrow() -> None:
    """ClusterPermission defaults to `["default"]`, so absence is not a wildcard."""
    assert not project_grant_is_unrestricted(None)
    assert not project_grant_is_unrestricted([])
    with pytest.raises(HTTPException):
        assert_project_scope_allows_path("/1.0/projects/other", "DELETE", None)


# ── The operation proxy door ───────────────────────────────────────────────────────────────


def test_every_unboundable_operation_is_declared_cluster_wide() -> None:
    """A new operation on one of these resources must not quietly inherit the project check."""
    unboundable = {
        operation
        for operation in PROXY_OPERATION_LEVELS
        if any(
            operation.endswith(prefix.lstrip("/").rstrip("s"))
            or operation.endswith(prefix.lstrip("/").rstrip("s") + "_pool")
            for prefix in _PROJECT_UNBOUNDABLE_PREFIXES
        )
        and not operation.startswith(("list_", "get_"))
    }
    missing = sorted(unboundable - CLUSTER_WIDE_OPERATIONS)
    assert not missing, (
        "these change a resource no project contains but are not declared cluster-wide, so the "
        f"ambient project check is the only thing in front of them: {missing}"
    )


def test_the_operation_handler_enforces_the_cluster_wide_rule() -> None:
    """Structural: `proxy_operation` consults the cluster-wide set and the unrestricted check."""
    tree = ast.parse(ROUTER.read_text())
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "proxy_operation"
    )
    statements = handler.body[1:] if ast.get_docstring(handler) else handler.body
    body = "\n".join(ast.unparse(statement) for statement in statements)
    assert "CLUSTER_WIDE_OPERATIONS" in body, (
        "proxy_operation does not consult CLUSTER_WIDE_OPERATIONS, so delete_project takes its "
        "target from params with only the ambient project authorised"
    )
    assert "project_grant_is_unrestricted" in body


def test_the_raw_proxy_handler_enforces_the_same_rule() -> None:
    tree = ast.parse(ROUTER.read_text())
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "raw_proxy"
    )
    body = ast.unparse(handler)
    assert "assert_project_scope_allows_path" in body, (
        "raw_proxy refuses only the paths nobody may reach, not the paths THIS grant may not, "
        "so DELETE /1.0/projects/<other> passes with the ambient project authorised"
    )


def test_every_cluster_wide_operation_costs_admin() -> None:
    """Cluster-wide operations also cost `admin`, so the project rule is not their only guard."""
    cheap = {
        operation: PROXY_OPERATION_LEVELS[operation]
        for operation in CLUSTER_WIDE_OPERATIONS
        if PROXY_OPERATION_LEVELS.get(operation) != "admin"
    }
    assert not cheap, f"cluster-wide operations priced below admin: {cheap}"
