"""The console's instance gate and the shared one answer every caller the same way.

routers/console.py keeps its own _verify_instance_access because its tests drive it through that
module's names; core/instance_access.py is the copy the terminal, the ticket mint and the
periodic re-authorization use. This test holds the two copies of the rule together across every
scope, grant and flag combination.
"""

import itertools
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import instance_access, org_scope
from app.routers import console

HOST, NAME = "c1", "web-1"

SCOPES = {
    "admin": (None, None),
    "partner-in": ({HOST}, None),
    "partner-out": ({"c2"}, None),
    "tenant-view": ({HOST}, {HOST: {("default", NAME): {"view"}}}),
    "tenant-console": ({HOST}, {HOST: {("default", NAME): {"view", "console"}}}),
    "tenant-console-only": ({HOST}, {HOST: {("default", NAME): {"console"}}}),
    "tenant-other-instance": ({HOST}, {HOST: {("default", "web-2"): {"view", "console"}}}),
}


def _answer(gate, permission):
    try:
        return gate(HOST, "default", NAME, permission)
    except HTTPException as exc:
        return f"refused {exc.status_code}"


@pytest.mark.parametrize(
    "scope, flag, permission",
    list(itertools.product(SCOPES, (False, True), ("view", "console"))),
)
def test_both_gates_answer_alike(monkeypatch, scope, flag, permission):
    settings = SimpleNamespace(tenant_perm_enforcement_enabled=flag)
    monkeypatch.setattr(console, "get_settings", lambda: settings)
    monkeypatch.setattr(instance_access, "get_settings", lambda: settings)
    allowed, acl = SCOPES[scope]
    tokens = (
        org_scope._allowed_clusters_var.set(allowed),
        org_scope._tenant_acl_var.set(acl),
        org_scope._visible_projects_var.set(None if allowed is None else {HOST: {"default"}}),
    )
    try:
        assert _answer(console._verify_instance_access, permission) == _answer(
            instance_access.verify_instance_access, permission
        )
    finally:
        org_scope._visible_projects_var.reset(tokens[2])
        org_scope._tenant_acl_var.reset(tokens[1])
        org_scope._allowed_clusters_var.reset(tokens[0])
