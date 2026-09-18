"""A deleted project leaves no policy rule behind, whichever organization wrote it.

Organization A's leftover rule for a project it deleted would put organization B's future
project of the same name inside A's bound, so the delete removes every rule naming the
(cluster, project) pair. A managed policy left empty is dropped; an OrcaHub-owned one keeps
its row because OrcaHub upserts it by external id. A project outside the caller's bound is
not found, and LXD is never asked.
"""

import contextvars
from typing import List

from app.core import org_scope
from tests._project_grant_fakes import GrantSession, Policy, Rule, partner, projects_client

HOST = "nuc2"


class _Service:
    def __init__(self):
        self.deleted: List[str] = []

    async def delete_project(self, host, name):
        self.deleted.append(f"{host}/{name}")
        return True


def _bounded(call, mapping):
    def run():
        org_scope._visible_projects_var.set(mapping)
        org_scope._allowed_clusters_var.set(None)
        return call()

    return contextvars.copy_context().run(run)


def test_every_organizations_rule_for_the_pair_goes_and_the_empty_managed_policy_with_it(monkeypatch):
    mine = Policy(6, "dashboard:nuc2", id=7)
    theirs = Policy(122, "auto:qa-r3", source="orcahub", id=8)
    other = Rule(7, HOST, "qa-other")
    session = GrantSession(
        policies=[mine, theirs],
        rules=[Rule(7, HOST, "qa-r3"), Rule(8, HOST, "qa-r3"), Rule(8, "nuc1", "qa-r3")],
    )
    service = _Service()
    client = projects_client(service, monkeypatch, session, user=partner())

    r = client.delete(f"/projects/{HOST}/qa-r3")

    assert r.status_code == 200, r.text
    assert service.deleted == [f"{HOST}/qa-r3"]
    removed = [d for d in session.deleted if isinstance(d, Rule)]
    assert {(d.policy_id, d.cluster_id) for d in removed} == {(7, HOST), (8, HOST)}
    assert any(d is mine for d in session.deleted), "the managed policy left empty is dropped"
    assert not any(d is theirs for d in session.deleted), "an OrcaHub policy keeps its row"
    assert other not in session.deleted
    assert "could not be removed" not in r.json()["message"]


def test_a_managed_policy_with_rules_left_stays(monkeypatch):
    mine = Policy(6, "dashboard:nuc2", id=7)
    session = GrantSession(policies=[mine], rules=[Rule(7, HOST, "qa-r3"), Rule(7, HOST, "qa-keep")])
    client = projects_client(_Service(), monkeypatch, session, user=partner())

    r = client.delete(f"/projects/{HOST}/qa-r3")

    assert r.status_code == 200, r.text
    assert not any(d is mine for d in session.deleted)


def test_a_project_outside_the_bound_is_not_found_and_lxd_is_not_asked(monkeypatch):
    service = _Service()
    client = projects_client(service, monkeypatch, GrantSession(), user=partner())

    r = _bounded(lambda: client.delete(f"/projects/{HOST}/theirs"), {HOST: {"default", "mine"}})

    assert r.status_code == 404, r.text
    assert service.deleted == []


def test_a_revoke_failure_keeps_the_delete_a_200_and_says_so(monkeypatch):
    from app.services import project_grants

    async def boom(*a, **k):
        raise RuntimeError("database away")

    monkeypatch.setattr(project_grants, "revoke_project_grants", boom)
    service = _Service()
    client = projects_client(service, monkeypatch, GrantSession(), user=partner())

    r = client.delete(f"/projects/{HOST}/qa-r3")

    assert r.status_code == 200, r.text
    assert service.deleted == [f"{HOST}/qa-r3"]
    assert "could not be removed" in r.json()["message"]
