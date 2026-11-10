"""The bound on a Partner must not be writable by that Partner.

On a cluster carrying more than one organization, `get_visible_projects` builds a Partner's
allowed project set from `TenantPolicyRule.project_name` rows of their own organizations, and
`POST /tenant-access/policies` lets a Partner write those rows. Without a check, a Partner could
name another organization's project and gain read AND write on its networks, ACLs, profiles and
instance devices.

The guard refuses a rule naming a project the caller cannot already see. `project_is_visible`
answers True when no bound applies, so this is inert for an Admin and on any single-org
cluster, and only engages where the bound is real.
"""

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.routers.tenant_access import _refuse_a_project_the_caller_cannot_see


class Rule:
    def __init__(self, cluster_id, project_name):
        self.cluster_id = cluster_id
        self.project_name = project_name


@pytest.fixture
def bounded():
    """A Partner on a shared cluster, held to the default project."""
    token = org_scope._visible_projects_var.set({"nuc2": {"default"}})
    yield
    org_scope._visible_projects_var.reset(token)


@pytest.fixture
def unbounded():
    """An Admin, or a Partner on a cluster their organization holds alone."""
    token = org_scope._visible_projects_var.set(None)
    yield
    org_scope._visible_projects_var.reset(token)


class TestTheEscalationIsClosed:
    def test_a_foreign_project_is_refused(self, bounded):
        with pytest.raises(HTTPException) as exc:
            _refuse_a_project_the_caller_cannot_see(Rule("nuc2", "orgb-prod"))
        assert exc.value.status_code == 403
        assert "orgb-prod" in exc.value.detail

    def test_the_bound_cannot_be_widened_one_rule_at_a_time(self, bounded):
        """Naming a project they can see adds nothing, so there is no bootstrap path."""
        _refuse_a_project_the_caller_cannot_see(Rule("nuc2", "default"))
        with pytest.raises(HTTPException):
            _refuse_a_project_the_caller_cannot_see(Rule("nuc2", "orgb-staging"))

    @pytest.mark.parametrize("wildcard", ["*", "", None])
    def test_a_wildcard_is_not_a_project_name(self, bounded, wildcard):
        """'*' does not widen the bound either, so it has nothing to refuse."""
        _refuse_a_project_the_caller_cannot_see(Rule("nuc2", wildcard))


class TestItDoesNotBiteWhereNoBoundApplies:
    def test_an_admin_may_name_any_project(self, unbounded):
        _refuse_a_project_the_caller_cannot_see(Rule("nuc2", "orgb-prod"))

    def test_a_cluster_outside_the_map_is_unrestricted(self, bounded):
        """A cluster bound to one organization is absent from the map, so nothing is refused."""
        _refuse_a_project_the_caller_cannot_see(Rule("nuc1", "anything-at-all"))


def test_both_rule_writers_are_guarded():
    """A ratchet: every function that builds a rule must call the project guard."""
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "app" / "routers" / "tenant_access.py"
    tree = ast.parse(src.read_text())
    unguarded = [
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and "TenantPolicyRule(" in ast.unparse(fn)
        and "_refuse_a_project_the_caller_cannot_see" not in ast.unparse(fn)
    ]
    assert unguarded == [], f"these write policy rules without the project guard: {unguarded}"
