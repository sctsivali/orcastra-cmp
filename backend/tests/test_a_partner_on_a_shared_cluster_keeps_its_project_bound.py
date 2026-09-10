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

import asyncio
import contextvars
from typing import Any, List

import pytest

from app.core import org_scope
from app.core.org_scope import (
    get_visible_projects,
    project_is_visible,
    visible_projects_for,
)


class _Scalars:
    def __init__(self, rows: List[Any]):
        self._rows = rows

    def all(self) -> List[Any]:
        return list(self._rows)


class _Result:
    def __init__(self, rows: List[Any]):
        self._rows = rows

    def scalars(self) -> _Scalars:
        return _Scalars(self._rows)


class FakeSession:
    """Answers by which table the statement reads, not by call order."""

    def __init__(self, shared_clusters=(), org_ids=(), rules_by_cluster=None):
        self.shared_clusters = list(shared_clusters)
        self.org_ids = list(org_ids)
        self.rules_by_cluster = rules_by_cluster or {}
        self.queries: List[str] = []

    async def execute(self, stmt):
        sql = str(stmt)
        self.queries.append(sql)
        if "tenant_policy_rules" in sql:
            # the cluster is bound into the statement, so recover it from the parameters
            params = stmt.compile().params
            cluster = next(
                (v for k, v in params.items() if str(v) in self.rules_by_cluster), None
            )
            return _Result(list(self.rules_by_cluster.get(cluster, [])))
        if "organization_clusters" in sql:
            return _Result(self.shared_clusters)
        if "organizations" in sql:
            return _Result(self.org_ids)
        raise AssertionError(f"unexpected query: {sql[:120]}")


class FakeUser:
    def __init__(self, email="p@example.test", groups=("role_partner",)):
        self.email = email
        self.groups = list(groups)
        self.roles = list(groups)


def _run(coro):
    return asyncio.run(coro)


def _as(role: str) -> FakeUser:
    return FakeUser(groups=(f"role_{role}",))


def test_an_admin_is_not_project_restricted_because_break_glass_gates_the_cluster():
    session = FakeSession(shared_clusters=["nuc2"])
    assert _run(get_visible_projects(_as("admin"), session)) is None


def test_a_tenant_is_not_project_restricted_here_because_their_acl_already_filters():
    session = FakeSession(shared_clusters=["nuc2"])
    assert _run(get_visible_projects(_as("tenant"), session)) is None


def test_a_partner_is_unrestricted_when_no_cluster_is_shared():
    session = FakeSession(shared_clusters=[])
    assert _run(get_visible_projects(_as("partner"), session)) is None


def test_a_partner_on_a_shared_cluster_is_held_to_default_plus_their_own_policies():
    session = FakeSession(
        shared_clusters=["nuc2"],
        org_ids=[6],
        rules_by_cluster={"nuc2": ["sivali-prod", "sivali-uat"]},
    )
    mapping = _run(get_visible_projects(_as("partner"), session))
    assert mapping == {"nuc2": {"default", "sivali-prod", "sivali-uat"}}


def test_a_partner_with_no_policies_on_a_shared_cluster_sees_only_default():
    """Exactly the behaviour they have today, which is the point: no regression, no leak."""
    session = FakeSession(shared_clusters=["nuc2"], org_ids=[6], rules_by_cluster={})
    assert _run(get_visible_projects(_as("partner"), session)) == {"nuc2": {"default"}}


def test_a_wildcard_rule_means_the_org_administers_the_whole_cluster():
    session = FakeSession(
        shared_clusters=["nuc2"], org_ids=[6], rules_by_cluster={"nuc2": ["*"]}
    )
    assert _run(get_visible_projects(_as("partner"), session)) is None


def test_only_the_shared_cluster_is_restricted_not_the_partners_own_one():
    session = FakeSession(
        shared_clusters=["nuc2"], org_ids=[6], rules_by_cluster={"nuc2": []}
    )
    mapping = _run(get_visible_projects(_as("partner"), session))
    assert set(mapping) == {"nuc2"}
    assert visible_projects_for_in(mapping, "nuc1") is None


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
