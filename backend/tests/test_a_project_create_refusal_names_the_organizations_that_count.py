"""A Partner refused a project create is told which organizations count.

The rule (org_to_hold_project_grant) counts only an organization the user created or holds as
Partner, so a plain member of the organization bound to the cluster is refused too. Telling that
account it is "not a member" would contradict its state.
"""

import contextvars

from app.core import org_scope
from app.routers import projects as projects_module
from tests._project_grant_fakes import GrantSession, partner, projects_client

HOST = "console-lab"


class _Service:
    def __init__(self):
        self.created = []

    def get_node_resources(self, host):
        return {"cpu_total": 64, "memory_total": 2**40, "storage_total": 2**44}

    async def create_project(self, **kwargs):
        self.created.append(kwargs)
        return {"name": kwargs["name"], "host": kwargs["host"], "description": "", "config": {}, "used_by": []}


def test_the_refusal_names_the_organizations_that_count(monkeypatch):
    async def _no_run_org(email, host, db):
        return None

    monkeypatch.setattr(projects_module, "org_to_hold_project_grant", _no_run_org)
    service = _Service()
    client = projects_client(service, monkeypatch, GrantSession(), user=partner())
    body = {"host": HOST, "name": "qa-f13", "max_cpu": 2, "max_ram": "1GiB", "max_storage": "6GiB"}

    def run():
        org_scope._visible_projects_var.set({HOST: {"default"}})
        org_scope._allowed_clusters_var.set(None)
        return client.post("/projects", json=body)

    r = contextvars.copy_context().run(run)

    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "not a member" not in detail
    assert "None of the organizations you created or hold as Partner is bound to cluster 'console-lab'" in detail
    assert "A member role in another organization does not count." in detail
    assert "Settings > Integrations > Synced Organizations > Manage clusters" in detail, "where to bind one"
    assert service.created == []
