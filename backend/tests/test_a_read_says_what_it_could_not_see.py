"""What the key-authenticated read surface returns, and what it admits it missed.

The instance fan-out walks every project. The pooled pylxd client is bound to the `default`
project, so `client.instances.all()` on it leaves out every other project's instances entirely,
and an integration that provisions into its own project would see an empty fleet and no error.

The grant's `projects` allow-list applies to these reads as it does to the proxy and the raw
proxy, or a key scoped to one project could enumerate all of them. The image upload router has
its own test (test_an_image_import_cannot_leave_its_project.py).

A cluster that does not answer is named in `unreachable` instead of adding an empty list to a
`total` that reads as authoritative. Otherwise an agent polling for the instance it just created
cannot tell an outage from a deletion.
"""

import ast
import asyncio
from pathlib import Path

import pytest

from app.routers.integrations import (
    _fetch_cluster_instances,
    _gather_per_cluster,
    _granted_projects,
    _within_granted_projects,
)

SOURCE = Path(__file__).resolve().parents[1] / "app" / "routers" / "integrations.py"


def _key(projects, cluster_id="nuc2"):
    grant = {"cluster_id": cluster_id, "permissions": ["admin"]}
    if projects is not None:
        grant["projects"] = projects
    return {"cluster_permissions": [grant]}


def _row(project, cluster_id="nuc2", name="vm"):
    return {"name": name, "cluster_id": cluster_id, "project": project}


# ── The project allow-list ─────────────────────────────────────────────────────────────────


def test_a_wildcard_grant_sees_every_project() -> None:
    rows = [_row("default"), _row("tenant-a"), _row("tenant-b")]
    assert _within_granted_projects(_key(["*"]), rows) == rows


def test_a_narrow_grant_sees_only_what_it_was_given() -> None:
    rows = [_row("default"), _row("tenant-a"), _row("tenant-b")]
    kept = _within_granted_projects(_key(["tenant-a"]), rows)
    assert [row["project"] for row in kept] == ["tenant-a"]


def test_an_absent_projects_field_means_default_only() -> None:
    """ClusterPermission declares `["default"]` as the default, so absence is not a wildcard."""
    assert _granted_projects(_key(None), "nuc2") == {"default"}
    kept = _within_granted_projects(_key(None), [_row("default"), _row("tenant-a")])
    assert [row["project"] for row in kept] == ["default"]


def test_a_row_from_a_cluster_outside_the_grant_is_dropped() -> None:
    """A second check: the fan-out already iterates only granted clusters."""
    assert _within_granted_projects(_key(["*"]), [_row("default", cluster_id="pandora")]) == []


def test_the_allow_list_is_per_cluster() -> None:
    key = {
        "cluster_permissions": [
            {"cluster_id": "nuc2", "projects": ["tenant-a"]},
            {"cluster_id": "pandora", "projects": ["*"]},
        ]
    }
    rows = [
        _row("tenant-a", "nuc2"), _row("tenant-b", "nuc2"),
        _row("tenant-b", "pandora"),
    ]
    kept = _within_granted_projects(key, rows)
    assert [(r["cluster_id"], r["project"]) for r in kept] == [
        ("nuc2", "tenant-a"), ("pandora", "tenant-b")
    ]


def test_a_missing_project_on_a_row_counts_as_default() -> None:
    assert _within_granted_projects(_key(["default"]), [{"name": "vm", "cluster_id": "nuc2"}])


# ── The fan-out envelope ───────────────────────────────────────────────────────────────────


def _gather(fn, cluster_ids):
    return asyncio.run(_gather_per_cluster(fn, None, cluster_ids, "test"))


def test_a_complete_answer_says_so() -> None:
    result = _gather(lambda _ctx, cluster_id: [{"cluster_id": cluster_id}], ["a", "b"])
    assert len(result.items) == 2
    assert result.unreachable == []
    assert result.partial is False


def test_a_cluster_that_failed_is_named_rather_than_dropped() -> None:
    def fetch(_ctx, cluster_id):
        if cluster_id == "b":
            raise RuntimeError("certificate not trusted")
        return [{"cluster_id": cluster_id}]

    result = _gather(fetch, ["a", "b"])
    assert [row["cluster_id"] for row in result.items] == ["a"]
    assert result.partial is True
    assert result.unreachable == [{"cluster_id": "b", "reason": "certificate not trusted"}]


def test_the_clusters_that_did_answer_still_answer() -> None:
    """Clusters that fail are reported in `unreachable`; the call itself does not raise."""
    def fetch(_ctx, cluster_id):
        raise RuntimeError("down")

    result = _gather(fetch, ["a", "b"])
    assert result.items == []
    assert {row["cluster_id"] for row in result.unreachable} == {"a", "b"}


def test_extra_arguments_reach_the_blocking_half() -> None:
    """The detail read passes a name and a project through the same fan-out."""
    seen = []

    def fetch(_ctx, cluster_id, name, project):
        seen.append((cluster_id, name, project))
        return []

    asyncio.run(_gather_per_cluster(fetch, None, ["a"], "detail", "vm1", "tenant-a"))
    assert seen == [("a", "vm1", "tenant-a")]


# ── The project blindness ──────────────────────────────────────────────────────────────────


def test_the_instance_fan_out_does_not_read_only_the_default_project() -> None:
    """`client.instances.all()` on the pooled client reads only the `default` project.

    Structural, because the defect is invisible wherever every instance lives in `default`, as
    on most developer machines.
    """
    tree = ast.parse(SOURCE.read_text())
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_fetch_cluster_instances"
    )
    # `_fetch_cluster_instances`'s docstring names the forbidden shape, so strip it before
    # matching.
    statements = function.body[1:] if ast.get_docstring(function) else function.body
    body = "\n".join(ast.unparse(statement) for statement in statements)
    assert "instances.all()" not in body, (
        "_fetch_cluster_instances is reading the pooled default-project client again. Every "
        "instance outside `default` disappears from the response with no error."
    )
    assert "fetch_node_instances" in body


def test_every_project_bearing_read_narrows_to_the_grant() -> None:
    """Every handler that fans out over clusters and returns per-project rows calls the filter.

    A test of `_within_granted_projects` alone stays green while an endpoint never calls it.
    """
    # Rows here are cluster-level, not project-level, so there is no project to narrow to. The
    # failover read answers server settings of the cluster itself, which no project owns.
    exempt = {"list_integration_clusters", "list_integration_cluster_failover"}

    tree = ast.parse(SOURCE.read_text())
    offenders = []
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in exempt:
            continue
        # Route handlers only. The blocking fetches and the fan-out helper itself mention the
        # name in prose, and neither is where the narrowing belongs.
        if not any("router." in ast.unparse(d) for d in node.decorator_list):
            continue
        statements = node.body[1:] if ast.get_docstring(node) else node.body
        body = "\n".join(ast.unparse(statement) for statement in statements)
        if "_gather_per_cluster" not in body:
            continue
        checked += 1
        if "_within_granted_projects" not in body:
            offenders.append(f"{node.name}:{node.lineno}")
    assert checked, "the scan found no fan-out handlers, so it is not checking anything"
    assert not offenders, (
        "these fan out across clusters and return the rows without narrowing them to the key's "
        "project allow-list, so a key scoped to one project enumerates all of them:\n  "
        + "\n  ".join(offenders)
    )


def test_an_unreachable_cluster_raises_instead_of_looking_empty() -> None:
    class _Manager:
        def fetch_node_instances(self, _cluster_id, _endpoint=""):
            return [], False

    class _Service:
        instance_manager = _Manager()

    with pytest.raises(Exception) as raised:
        _fetch_cluster_instances(_Service(), "nuc2")
    assert getattr(raised.value, "status_code", None) == 503


def test_a_project_row_names_itself_in_name_not_project() -> None:
    """A project row names its project in `name`; an instance row uses `project`.

    `_fetch_cluster_projects` returns `{"name": "<project>"}` while `_fetch_cluster_instances`
    returns `{"name": "<instance>", "project": "<project>"}`. Filtering a project listing on
    `project` would read None on every row, default it to "default", and match a default-scoped
    grant every time, so the filter would look applied and keep every project.
    """
    rows = [
        {"name": "default", "cluster_id": "nuc2"},
        {"name": "tenant-a", "cluster_id": "nuc2"},
    ]
    kept = _within_granted_projects(_key(["default"]), rows, project_field="name")
    assert [row["name"] for row in kept] == ["default"]

    # The default field still reads the instance shape.
    assert _within_granted_projects(_key(["default"]), [_row("tenant-a")]) == []


def test_every_project_listing_states_which_field_names_the_project() -> None:
    """Structural, because the wrong field fails open and no response looks different."""
    # Key on where the rows came from, not on the substring "project" in the argument's
    # source text: `GET /integrations/projects` passes `fanout.items`, which does not contain
    # it, so a substring match would skip the one handler this rule exists for.
    tree = ast.parse(SOURCE.read_text())
    offenders = []
    for enclosing in ast.walk(tree):
        if not isinstance(enclosing, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # Names holding rows that came from the projects fetch. sync_data filters two listings
        # in one body, and only the projects one needs the field stated, so tie each call to
        # where its rows came from rather than to the function it sits in.
        from_projects = {
            target.id
            for node in ast.walk(enclosing)
            if isinstance(node, ast.Assign)
            and "_fetch_cluster_projects" in ast.unparse(node.value)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        if not from_projects:
            continue

        for node in ast.walk(enclosing):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) != "_within_granted_projects":
                continue
            rows = ast.unparse(node.args[1]) if len(node.args) > 1 else ""
            if rows.split(".")[0] not in from_projects:
                continue
            if not any(kw.arg == "project_field" for kw in node.keywords):
                offenders.append(f"{enclosing.name}:{node.lineno} filters {rows}")
    checked = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and "_fetch_cluster_projects" in ast.unparse(node)
        and "_within_granted_projects" in ast.unparse(node)
    ]
    assert len(checked) >= 2, (
        f"expected both the projects listing and sync to be scanned, saw {checked}"
    )
    assert not offenders, (
        "these filter a project listing without saying which field names the project, so the "
        "filter reads `project` on a row that has none and keeps everything:\n  "
        + "\n  ".join(offenders)
    )


def test_the_proxy_list_projects_is_narrowed_too() -> None:
    """The operation proxy authorises the project it runs in, which does not bound this one.

    `list_projects` answers about the whole cluster, so without narrowing a key granted only
    `default` would read every tenant project name back, and those names carry customer slugs.
    """
    tree = ast.parse(SOURCE.read_text())
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "proxy_operation"
    )
    statements = handler.body[1:] if ast.get_docstring(handler) else handler.body
    body = "\n".join(ast.unparse(statement) for statement in statements)
    assert "list_projects" in body and "_granted_projects" in body, (
        "proxy_operation returns list_projects without narrowing it to the key's grant"
    )
