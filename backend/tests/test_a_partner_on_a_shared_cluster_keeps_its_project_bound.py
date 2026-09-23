"""A Partner is scoped by cluster, so widening a list to every project can leak sideways.

`get_tenant_acl_rules` returns None for Partner as well as Admin, so the `used_by` filter that
protects the network, ACL and profile lists never runs for a Partner. That was harmless only
while those lists could not see past the `default` project. The moment they can, a cluster
carrying two organizations hands one partner the other's network config, ACL rule bodies,
profile devices and, through `used_by`, their instance inventory by name.

`nuc2` on this deployment is bound to two organizations today, so this is not hypothetical.

The rule these tests pin: on a cluster bound to more than one organization a Partner keeps the
behaviour they have now, the `default` project, widened only by projects their own policies
name. Everywhere else nothing is restricted, because a Tenant is already filtered by their ACL
and an Admin is gated by break-glass at the cluster level.
"""

import contextvars
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles

from app.core import org_scope
from app.core.org_scope import (
    get_visible_projects,
    project_is_visible,
    visible_projects_for,
)
from app.models.db_models import (
    Organization,
    OrganizationCluster,
    OrganizationMember,
    TenantAccessPolicy,
    TenantPolicyAssignment,
    TenantPolicyRule,
)
from tests._sqlite_session import sqlite_session


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(_element, _compiler, **_kw):  # the policy tables carry a JSONB column
    return "JSON"


PARTNER = "p@example.test"


class FakeUser:
    def __init__(self, email=PARTNER, groups=("role_partner",)):
        self.email = email
        self.groups = list(groups)
        self.roles = list(groups)


def _as(role: str) -> FakeUser:
    return FakeUser(groups=(f"role_{role}",))


async def _world(rules: Optional[Dict[str, List[str]]] = None, shared: bool = True,
                 foreign_rules: Optional[List[str]] = None, assigned: bool = False,
                 expired: bool = False, assigned_to_other: bool = False):
    """The partner runs org 6 on nuc1 and nuc2. Another partner's org 7 holds nuc2 as well unless
    `shared` is False. `rules` are org 6's policy rules per cluster. `foreign_rules` are org 7's
    rules on nuc2, where the partner holds a tenant row, assigned to that row when `assigned`."""
    db = sqlite_session(Organization, OrganizationMember, OrganizationCluster, TenantAccessPolicy,
                        TenantPolicyRule, TenantPolicyAssignment)
    db.add_all([
        Organization(id=6, name="home", slug="home", created_by="system", is_active=True, api_key_id="oak_6"),
        Organization(id=7, name="other", slug="other", created_by="system", is_active=True, api_key_id="oak_7"),
    ])
    await db.commit()
    member = OrganizationMember(organization_id=7, user_email=PARTNER, role="tenant")
    other = OrganizationMember(organization_id=7, user_email="t7@example.test", role="tenant")
    db.add_all([
        OrganizationMember(organization_id=6, user_email=PARTNER, role="partner"),
        OrganizationMember(organization_id=7, user_email="q@example.test", role="partner"),
        OrganizationCluster(organization_id=6, cluster_id="nuc1"),
        OrganizationCluster(organization_id=6, cluster_id="nuc2"),
    ] + ([OrganizationCluster(organization_id=7, cluster_id="nuc2")] if shared else [])
      + ([member, other] if foreign_rules is not None else []))
    await db.commit()
    policies = [(6, cluster, projects) for cluster, projects in (rules or {}).items()]
    if foreign_rules is not None:
        policies.append((7, "nuc2", foreign_rules))
    for n, (org, cluster, projects) in enumerate(policies, start=1):
        db.add(TenantAccessPolicy(id=n, organization_id=org, name=f"p{n}", created_by="system"))
        await db.commit()
        db.add_all([TenantPolicyRule(policy_id=n, cluster_id=cluster, project_name=project, instance_name="*",
                                     permissions=["view"]) for project in projects])
        if org == 7 and assigned:
            ends = datetime.now(timezone.utc) - timedelta(days=1) if expired else None
            db.add(TenantPolicyAssignment(policy_id=n, org_member_id=member.id, assigned_by="q@example.test",
                                          expires_at=ends))
        if org == 7 and assigned_to_other:
            db.add(TenantPolicyAssignment(policy_id=n, org_member_id=other.id, assigned_by="q@example.test"))
        await db.commit()
    return db


async def test_an_admin_is_not_project_restricted_because_break_glass_gates_the_cluster():
    assert await get_visible_projects(_as("admin"), await _world()) is None


async def test_a_tenant_is_not_project_restricted_here_because_their_acl_already_filters():
    assert await get_visible_projects(_as("tenant"), await _world()) is None


async def test_a_partner_is_unrestricted_when_no_cluster_is_shared():
    assert await get_visible_projects(_as("partner"), await _world(shared=False)) is None


async def test_a_partner_on_a_shared_cluster_is_held_to_default_plus_their_own_policies():
    db = await _world({"nuc2": ["home-prod", "home-uat"]})
    assert await get_visible_projects(_as("partner"), db) == {"nuc2": {"default", "home-prod", "home-uat"}}


async def test_a_partner_with_no_policies_on_a_shared_cluster_sees_only_default():
    """Exactly the behaviour they have today, which is the point: no regression, no leak."""
    assert await get_visible_projects(_as("partner"), await _world()) == {"nuc2": {"default"}}


async def test_a_wildcard_rule_does_not_lift_the_bound():
    """This test asserted the opposite until a security review pointed out that it was
    encoding the vulnerability rather than the rule.

    `TenantPolicyRule.project_name` defaults to `"*"` on the model AND on the create schema,
    so treating a wildcard as "this organization administers everything" switched the control
    off for any organization that had ever written a single policy. A wildcard also cannot
    answer the question being asked, which is WHICH projects belong to this organization; on a
    shared cluster it names the other organization's projects too.
    """
    assert await get_visible_projects(_as("partner"), await _world({"nuc2": ["*"]})) == {"nuc2": {"default"}}


async def test_a_wildcard_beside_named_projects_contributes_nothing_of_its_own():
    db = await _world({"nuc2": ["*", "home-prod"]})
    assert await get_visible_projects(_as("partner"), db) == {"nuc2": {"default", "home-prod"}}


async def test_only_the_shared_cluster_is_restricted_not_the_partners_own_one():
    mapping = await get_visible_projects(_as("partner"), await _world({"nuc1": ["home-dev"]}))
    assert set(mapping) == {"nuc2"}
    assert visible_projects_for_in(mapping, "nuc1") is None


async def test_another_partners_policies_do_not_widen_the_bound():
    """The partner reaches nuc2 through a tenant row in org 7 too. Org 7's policies name its own
    tenants' projects, and none of them is the partner's unless assigned to the partner's row."""
    db = await _world(foreign_rules=["other-web", "other-db"])
    assert await get_visible_projects(_as("partner"), db) == {"nuc2": {"default"}}


async def test_an_expired_assignment_does_not_widen_it():
    db = await _world(foreign_rules=["other-web"], assigned=True, expired=True)
    assert await get_visible_projects(_as("partner"), db) == {"nuc2": {"default"}}


async def test_another_users_assignment_does_not_widen_it():
    db = await _world(foreign_rules=["other-web"], assigned_to_other=True)
    assert await get_visible_projects(_as("partner"), db) == {"nuc2": {"default"}}


async def test_a_policy_assigned_to_the_partners_own_row_widens_it():
    db = await _world(foreign_rules=["other-web"], assigned=True)
    assert await get_visible_projects(_as("partner"), db) == {"nuc2": {"default", "other-web"}}


def visible_projects_for_in(mapping, cluster):
    """visible_projects_for, but against a mapping supplied directly."""
    return mapping.get(cluster)


@pytest.mark.parametrize(
    "mapping,cluster,project,expected",
    [
        (None, "nuc2", "someone-else", True),
        ({}, "nuc2", "someone-else", True),
        ({"nuc2": {"default"}}, "nuc2", "default", True),
        ({"nuc2": {"default"}}, "nuc2", "someone-else", False),
        ({"nuc2": {"default"}}, "nuc2", None, True),
        ({"nuc2": {"default"}}, "nuc1", "anything", True),
    ],
)
def test_the_gate_answers_per_cluster_and_defaults_to_open_where_nothing_is_set(
    mapping, cluster, project, expected
):
    def check():
        org_scope._visible_projects_var.set(mapping)
        return project_is_visible(cluster, project)

    assert contextvars.copy_context().run(check) is expected


def test_an_unset_context_restricts_nothing():
    def check():
        return visible_projects_for("nuc2")

    assert contextvars.copy_context().run(check) is None


# ── every list that spans projects must apply the bound ────────────────────────────────────────

def test_every_project_spanning_list_applies_the_bound():
    """A ratchet, because the instance list is how this was missed.

    The networks, profile and ACL lists were given `project_is_visible` when they widened. The
    instance list already spanned every project before that work, so it was never part of the
    change and never got the gate: on a cluster carrying two organizations a Partner saw the
    other organization's instances by name. Found by driving a shared cluster, not by reading.

    `check_tenant_permission` does not cover this. `get_tenant_acl_rules` returns None for both
    Admin and Partner, so it passes everything for them, and a Partner is scoped by cluster
    rather than by project.
    """
    import ast
    import pathlib

    routers = pathlib.Path(__file__).resolve().parents[1] / "app" / "routers"
    expected = {
        "networks.py": "list_networks",
        "profiles.py": "list_profiles",
        "network_acls.py": "list_network_acls",
        "instances.py": "list_instances",
        # /vms is a SECOND live instance list. Naming the four above by hand is what let it
        # ship unbounded after the other four were fixed, which is why the discovery check
        # below exists as well.
        "vms.py": "list_vms",
        # volumes and warnings span every project since the hotfix1 re-cut
        "storage_volumes.py": "list_storage_volumes",
        "lxd_warnings.py": "list_warnings",
    }
    missing = []
    for filename, func in expected.items():
        tree = ast.parse((routers / filename).read_text())
        found = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
                found = True
                if "project_is_visible" not in ast.unparse(node):
                    missing.append(f"{filename}:{func}")
        if not found:
            missing.append(f"{filename}:{func} (handler not found, rename?)")
    assert missing == [], f"these span projects without applying the bound: {missing}"


# Handlers that filter with the tenant ACL alone and are NOT a bound violation, each with the
# reason it is exempt. Read before adding to this: the bar is that the handler emits no
# per-row identity, only a total. A handler that names an instance, a network or a project
# does not belong here.
AGGREGATE_ONLY = {
    # Returns None for anyone without a tenant ACL, so it never narrows a Partner in the first
    # place, and it answers with image fingerprints, which are a cluster-level artifact.
    "_allowed_image_fingerprints_by_host",
    # These three sum counters. get_network_metrics keys its totals by interface name (eth0),
    # not by instance, and the dashboard and node metrics emit host-level figures. A Partner on
    # a shared cluster sees totals that include another organization's traffic, which is a
    # property of the host being shared, not a disclosure of what runs on it.
    "get_monitoring_dashboard",
    "get_node_metrics",
    "get_network_metrics",
}


def test_the_tenant_acl_is_never_the_only_project_filter():
    """Discovery, not a list, because a hand-kept list is how /vms was missed.

    `check_tenant_permission` returns True whenever the tenant ACL is None, and it is always
    None for an Admin and for a Partner (`org_scope.get_tenant_acl_rules`). So a handler that
    filters rows with it ALONE applies no project bound to a Partner at all. Any such handler
    must pair it with `project_is_visible`.
    """
    import ast
    import pathlib

    app = pathlib.Path(__file__).resolve().parents[1] / "app"
    lonely = []
    for path in sorted(app.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.unparse(node)
            if "check_tenant_permission(" not in body:
                continue
            # a per-row filter, not a single-resource authorization check
            if not any(k in body for k in ("for vm in", "for instance in", "for row in")):
                continue
            if "project_is_visible" not in body:
                if node.name in AGGREGATE_ONLY:
                    continue
                lonely.append(f"{path.relative_to(app.parent)}:{node.lineno} {node.name}")
    assert lonely == [], (
        "these filter rows with the tenant ACL alone, which is no bound for a Partner: "
        + ", ".join(lonely)
    )
