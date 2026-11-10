"""Every project route answers inside the Partner bound.

A Partner on a shared cluster must not see other organizations' project names in the list or
the pickers, nor rename or delete a project no policy of theirs names. A project outside the
bound is absent from both lists and not found on PATCH and DELETE; inside the bound, or with no
bound, every route behaves normally.
"""

import contextvars

import pytest

from app.core import org_scope
from tests._project_grant_fakes import GrantSession, partner, projects_client

HOST = "nuc2"


class _Service:
    def __init__(self):
        self.updated = []
        self.deleted = []

    def get_node_names(self):
        return [HOST]

    def get_node_resources(self, host):
        return {"cpu_total": 64, "memory_total": 2**40, "storage_total": 2**44}

    async def list_projects(self, host):
        return [
            {"name": n, "host": host, "description": "", "config": {}, "used_by": []}
            for n in ("default", "mine", "theirs")
        ]

    async def update_project(self, *args, **kwargs):
        self.updated.append(kwargs)
        return {"name": kwargs.get("project_name", "x"), "host": HOST, "description": "", "config": {}, "used_by": []}

    async def delete_project(self, host, name):
        self.deleted.append(name)
        return True


def _under(mapping, call):
    def run():
        org_scope._visible_projects_var.set(mapping)
        org_scope._allowed_clusters_var.set(None)
        return call()

    return contextvars.copy_context().run(run)


BOUND = {HOST: {"default", "mine"}}


@pytest.mark.parametrize("path", ["/projects", f"/projects/{HOST}"])
def test_the_lists_hold_the_bound(monkeypatch, path):
    client = projects_client(_Service(), monkeypatch, GrantSession(), user=partner())

    bounded = _under(BOUND, lambda: client.get(path))
    unbounded = _under(None, lambda: client.get(path))

    assert bounded.status_code == 200, bounded.text
    body = bounded.json()
    hosts = body if isinstance(body, list) else [body]
    names = sorted(p["name"] for h in hosts if h["host"] == HOST for p in h["projects"])
    assert names == ["default", "mine"]
    body = unbounded.json()
    hosts = body if isinstance(body, list) else [body]
    assert sorted(p["name"] for h in hosts if h["host"] == HOST for p in h["projects"]) == ["default", "mine", "theirs"]


def test_update_and_delete_are_not_found_outside_the_bound_and_pass_inside(monkeypatch):
    service = _Service()
    client = projects_client(service, monkeypatch, GrantSession(), user=partner())
    patch = {"description": "renamed"}

    outside_patch = _under(BOUND, lambda: client.patch(f"/projects/{HOST}/theirs", json=patch))
    outside_delete = _under(BOUND, lambda: client.delete(f"/projects/{HOST}/theirs"))
    inside_patch = _under(BOUND, lambda: client.patch(f"/projects/{HOST}/mine", json=patch))
    inside_delete = _under(BOUND, lambda: client.delete(f"/projects/{HOST}/mine"))

    assert outside_patch.status_code == 404 and outside_delete.status_code == 404
    assert service.updated == [] or all(u.get("project_name") != "theirs" for u in service.updated)
    assert "theirs" not in service.deleted
    assert inside_patch.status_code == 200, inside_patch.text
    assert inside_delete.status_code == 200, inside_delete.text
    assert service.deleted == ["mine"]
