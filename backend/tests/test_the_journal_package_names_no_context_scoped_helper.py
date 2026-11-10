"""The task journal package runs where every authorization primitive answers "allowed".

The follower, the sink, the seed and the prune loop are lifespan tasks with no request behind
them, so every org-scope ContextVar is unset, and unset is the permissive value. The rule that
`test_a_background_run_does_not_inherit_a_request_scope` holds for `app/services/snapshots`
applies to the whole of `app/services/task_journal`: rows are written as facts, and scope is
applied on read by `app/core/task_scope.py` and the routers, which live outside this package.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
PACKAGE = APP / "services" / "task_journal"

# Every one of these answers "allowed" or "unfiltered" when the ContextVar it reads is unset.
FORBIDDEN = {
    "verify_cluster_access",
    "verify_tenant_permission",
    "check_tenant_permission",
    "apply_cluster_filter",
    "get_current_allowed_clusters",
    "get_scope_cache_key",
    "filter_projects_by_acl",
    "filter_instances_by_acl",
    "tenant_acl_active",
    "project_is_visible",
    "get_tenant_acl",
    # The remaining readers of the scope ContextVars.
    "tenant_may_see_project",
    "visible_projects_for",
}


def _names(tree: ast.AST):
    """Every identifier the module mentions, however it is reached."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.id, node.lineno
        elif isinstance(node, ast.Attribute):
            yield node.attr, node.lineno
        elif isinstance(node, ast.alias):
            yield (node.name.split(".")[-1]), getattr(node, "lineno", 0)


def test_the_journal_package_names_no_context_scoped_helper():
    offenders = []
    checked = 0
    for path in sorted(PACKAGE.rglob("*.py")):
        checked += 1
        tree = ast.parse(path.read_text())
        for name, lineno in _names(tree):
            if name in FORBIDDEN:
                offenders.append(f"{path.relative_to(APP.parent)}:{lineno} names {name}")
    # Without this an empty scan passes and the rule guards nothing.
    assert checked > 0, "the scan found no modules, so it is not actually checking anything"
    assert not offenders, (
        "A background task's org-scope ContextVars are unset, and unset means allowed. "
        "Derive authorization from the database and pass it as an argument instead:\n  "
        + "\n  ".join(offenders)
    )


def test_the_forbidden_list_still_matches_the_helpers_that_exist():
    """Every name on the list must still exist; a renamed helper would slip past the scan."""
    import app.core.org_scope as org_scope
    import app.core.tenant_visibility as tenant_visibility

    live = set(dir(org_scope)) | set(dir(tenant_visibility))
    missing = sorted(FORBIDDEN - live)
    assert not missing, (
        f"{missing} no longer exist, so guarding against them proves nothing. "
        "Update the list to whatever replaced them."
    )


def test_the_scan_covers_the_follower_and_its_neighbours():
    present = {path.name for path in PACKAGE.rglob("*.py")}
    for name in ("follower.py", "follower_writer.py", "seed.py", "prune.py", "sink.py", "repository.py"):
        assert name in present, f"{name} is not under the package the rule scans"
