"""A schedule runs where every authorization primitive answers "allowed".

Helpers such as `check_tenant_permission`, `verify_cluster_access` and `apply_cluster_filter` read
request-scoped ContextVars. `run_blocking` copies the caller's context into its worker, but a task
the lifespan started has no caller, so those ContextVars are unset, and unset is the permissive
value: calling one from the scheduler would remove the check silently.

So no module under app/services/snapshots may name any of them. test_cluster_scope_ratchet.py walks
only app/routers and test_a_declared_gate_is_a_reached_gate.py watches services/desktop, mcp and
services/webhooks, so neither covers this.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
PACKAGE = APP / "services" / "snapshots"

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
    "default_is_view_only",
    "refuse_default_operation",
    "verify_project_access",
    "project_is_visible",
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


def test_the_scheduler_package_names_no_context_scoped_helper():
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
    """Every forbidden name still exists in org_scope or tenant_visibility.

    A rename then fails here instead of leaving the ratchet guarding a removed name.
    """
    import app.core.org_scope as org_scope
    import app.core.tenant_visibility as tenant_visibility

    live = set(dir(org_scope)) | set(dir(tenant_visibility))
    missing = sorted(FORBIDDEN - live)
    assert not missing, (
        f"{missing} no longer exist, so guarding against them proves nothing. "
        "Update the list to whatever replaced them."
    )


def test_the_package_declares_the_same_list_it_is_held_to():
    """The list schedule_authority declares beside the code stays within the one enforced here."""
    from app.services.snapshots.schedule_authority import forbidden_scope_helpers

    declared = set(forbidden_scope_helpers())
    assert declared <= FORBIDDEN, sorted(declared - FORBIDDEN)


def test_the_runner_authorizes_from_the_database_not_from_a_context():
    """The runner replaces the banned helpers with checks that read the database."""
    source = (PACKAGE / "schedule_runner.py").read_text()
    assert "authority.db_refusal_for(db, schedule)" in source
    assert "authority.default_view_only_refusal(db, schedule)" in source
    assert "authority.cluster_refusal_for(" in source
    assert "_stored_names_are_addressable(schedule)" in source
