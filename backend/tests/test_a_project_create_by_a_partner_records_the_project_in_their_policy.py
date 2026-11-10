"""A Partner's new project is inside their own bound from the next request on.

On a cluster shared by two organizations a Partner is held to the default project plus what
their organizations' policies name, and they may not write such a rule for a project they
cannot see. Without a grant they could neither read nor create in a project they had just
created, so the create records the project in the organization's managed policy, for the one
name this request brought into existence.
"""

import contextvars
from typing import Any, Dict, List

from app.core import org_scope
from app.models.db_models import TenantAccessPolicy, TenantPolicyRule
from app.routers import projects as projects_module
from app.services import project_grants
from tests._project_grant_fakes import GrantSession, Policy, Rule, admin, partner, projects_client

HOST = "nuc2"


class _Service:
    def __init__(self):
        self.created: List[Dict[str, Any]] = []

    def get_node_resources(self, host):
        return {"cpu_total": 64, "memory_total": 2**40, "storage_total": 2**44}

    async def create_project(self, **kwargs):
        self.created.append(kwargs)
        return {"name": kwargs["name"], "host": kwargs["host"], "description": "", "config": {}, "used_by": []}


def _body(name="qa-r3"):
    return {"host": HOST, "name": name, "max_cpu": 2, "max_ram": "1GiB", "max_storage": "6GiB"}


def _grant_org(monkeypatch, org_id):
    async def _org(email, host, db):
        return org_id

    monkeypatch.setattr(projects_module, "org_to_hold_project_grant", _org)


def _bounded(call, mapping):
    def run():
        org_scope._visible_projects_var.set(mapping)
        org_scope._allowed_clusters_var.set(None)
        return call()

    return contextvars.copy_context().run(run)


def test_the_project_is_written_into_a_managed_policy_with_one_view_rule(monkeypatch):
    _grant_org(monkeypatch, 6)
    session = GrantSession()
    service = _Service()
    client = projects_client(service, monkeypatch, session, user=partner())

    r = client.post("/projects", json=_body())

    assert r.status_code == 201, r.text
    assert service.created and service.created[0]["name"] == "qa-r3"
    policies = [o for o in session.added if isinstance(o, TenantAccessPolicy)]
    rules = [o for o in session.added if isinstance(o, TenantPolicyRule)]
    assert len(policies) == 1 and policies[0].name == "dashboard:nuc2" and policies[0].source == "local"
    assert policies[0].organization_id == 6 and policies[0].created_by == "p@example.test"
    assert len(rules) == 1
    assert (rules[0].cluster_id, rules[0].project_name, rules[0].instance_name) == (HOST, "qa-r3", "*")
    assert rules[0].permissions == ["view"]
    assert not any(type(o).__name__ == "TenantPolicyAssignment" for o in session.added)
    assert session.commits == 1
    assert "could not be recorded" not in r.json()["message"]


def test_the_grant_is_idempotent_and_reuses_the_managed_policy(monkeypatch):
    _grant_org(monkeypatch, 6)
    existing = Policy(6, "dashboard:nuc2", id=7)
    session = GrantSession(policies=[existing], rules=[Rule(7, HOST, "qa-r3")])
    client = projects_client(_Service(), monkeypatch, session, user=partner())

    r = client.post("/projects", json=_body())

    assert r.status_code == 201, r.text
    assert session.added == [], "nothing new when the policy and the rule already exist"


def test_a_bounded_partner_with_no_bound_organization_is_refused_before_lxd(monkeypatch):
    _grant_org(monkeypatch, None)
    service = _Service()
    client = projects_client(service, monkeypatch, GrantSession(), user=partner())

    r = _bounded(lambda: client.post("/projects", json=_body()), {HOST: {"default"}})

    assert r.status_code == 403, r.text
    assert service.created == [], "the project must not exist for a creator who could not see it"


def test_an_unbounded_partner_with_no_bound_organization_still_creates(monkeypatch):
    # No bound applies on this cluster, so nothing is withheld and nothing is recorded.
    _grant_org(monkeypatch, None)
    service = _Service()
    session = GrantSession()
    client = projects_client(service, monkeypatch, session, user=partner())

    r = client.post("/projects", json=_body())

    assert r.status_code == 201, r.text
    assert service.created and session.added == []


def test_the_request_bound_is_widened_so_the_response_already_sees_the_project(monkeypatch):
    _grant_org(monkeypatch, 6)
    seen: List[Any] = []
    real = project_grants.widen_bound_in_request

    def spy(cluster_id, project_name):
        real(cluster_id, project_name)
        seen.append(org_scope.visible_projects_for(cluster_id))

    monkeypatch.setattr(project_grants, "widen_bound_in_request", spy)
    client = projects_client(_Service(), monkeypatch, GrantSession(), user=partner())

    r = _bounded(lambda: client.post("/projects", json=_body()), {HOST: {"default"}})

    assert r.status_code == 201, r.text
    assert seen == [{"default", "qa-r3"}]


def test_a_grant_failure_keeps_the_create_a_201_and_says_so(monkeypatch):
    _grant_org(monkeypatch, 6)

    async def boom(*a, **k):
        raise RuntimeError("database away")

    monkeypatch.setattr(project_grants, "grant_project_to_creator", boom)
    service = _Service()
    client = projects_client(service, monkeypatch, GrantSession(), user=partner())

    r = client.post("/projects", json=_body())

    assert r.status_code == 201, r.text
    assert service.created, "the LXD project exists; a 500 would make the UI retry into a 409"
    assert "could not be recorded" in r.json()["message"]


def test_an_admin_creator_records_nothing(monkeypatch):
    called = []

    async def _org(email, host, db):
        called.append(email)
        return 6

    monkeypatch.setattr(projects_module, "org_to_hold_project_grant", _org)
    session = GrantSession()
    client = projects_client(_Service(), monkeypatch, session, user=admin())

    r = client.post("/projects", json=_body())

    assert r.status_code == 201, r.text
    assert called == [] and session.added == []


def test_a_malformed_name_is_refused_before_anything_runs(monkeypatch):
    _grant_org(monkeypatch, 6)
    service = _Service()
    client = projects_client(service, monkeypatch, GrantSession(), user=partner())

    r = client.post("/projects", json=_body(name="../x"))

    assert r.status_code == 422, r.text
    assert service.created == []
