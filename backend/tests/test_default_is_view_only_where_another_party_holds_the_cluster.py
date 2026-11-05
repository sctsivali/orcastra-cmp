"""`default` on a cluster another party holds is view only for a Partner (owner decision 2026-11-05).

A Partner bound to its projects on a cluster another party also holds always keeps `default`
visible (org_scope.get_visible_projects), and a Partner carries no tenant ACL, so every
instance-level gate (`verify_instance_access`, `verify_file_access`, `verify_guest_access`, the
power and snapshot checks) passed for that party's instances in `default`: a root shell, the guest's
filesystem and its passwords. Security review of v1.0.0-RC4..c5199ea1, finding 1.

Now, like a cluster past its paid term, `default` there answers reads and refuses every change, an
interactive session, and any instance grant above view. The Partner's own projects, a cluster its
party holds whole, Tenants (who act only on instances a policy names) and Admins are unchanged.
"""

import asyncio
import contextlib

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.instance_access import verify_file_access, verify_guest_access, verify_instance_access
from app.core.request_context import RequestContext, set_request_context

HELD = "shared"
WHOLE = "mine"


@contextlib.contextmanager
def _partner(method="GET", path="/api/v1/instances", *, interactive=False, view_only=frozenset({HELD})):
    tokens = [
        (org_scope._allowed_clusters_var, org_scope._allowed_clusters_var.set({HELD, WHOLE})),
        (org_scope._tenant_acl_var, org_scope._tenant_acl_var.set(None)),
        (org_scope._visible_projects_var, org_scope._visible_projects_var.set({HELD: {"default", "own"}})),
        (org_scope._default_view_only_var, org_scope._default_view_only_var.set(view_only)),
        (org_scope._interactive_var, org_scope._interactive_var.set(interactive)),
    ]
    set_request_context(RequestContext(method=method, path=path))
    try:
        yield
    finally:
        set_request_context(None)
        for var, token in reversed(tokens):
            var.reset(token)


def _refused(fn) -> bool:
    try:
        fn()
    except HTTPException as exc:
        assert exc.status_code == 403
        return True
    return False


def test_reading_default_on_a_held_cluster_still_works() -> None:
    with _partner("GET"):
        assert org_scope.verify_project_access(HELD, "default") == "default"
        assert org_scope.check_tenant_permission(HELD, "default", "their-vm", "view")
        assert verify_instance_access(HELD, "default", "their-vm", "view") == "default"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_change_in_default_on_a_held_cluster_is_refused(method) -> None:
    with _partner(method, "/api/v1/vms/their-vm/start"):
        assert _refused(lambda: org_scope.verify_project_access(HELD, "default"))


def test_an_interactive_session_in_default_is_refused() -> None:
    with _partner("GET", "/api/v1/terminal/ws", interactive=True):
        assert _refused(lambda: org_scope.verify_project_access(HELD, "default"))


@pytest.mark.parametrize("permission", ["console", "operate", "snapshot", "file_transfer"])
def test_no_grant_above_view_reaches_default_on_a_held_cluster(permission) -> None:
    with _partner("POST", "/api/v1/vms/their-vm/start"):
        assert not org_scope.check_tenant_permission(HELD, "default", "their-vm", permission)


def test_a_console_ticket_files_and_credentials_are_refused_whatever_the_rollout_flag(monkeypatch) -> None:
    from app.core import instance_access

    monkeypatch.setattr(
        instance_access, "get_settings", lambda: type("S", (), {"tenant_perm_enforcement_enabled": False})()
    )
    # ws-tickets is a read POST, so the ticket door is the instance gate itself.
    with _partner("POST", "/api/v1/ws-tickets"):
        assert _refused(lambda: verify_instance_access(HELD, "default", "their-vm", "console"))
        assert _refused(lambda: verify_file_access(HELD, "default", "their-vm"))
        assert _refused(lambda: verify_guest_access(HELD, "default", "their-vm"))


def test_the_partners_own_projects_and_whole_clusters_are_unchanged() -> None:
    with _partner("POST", "/api/v1/vms/web/start"):
        assert org_scope.verify_project_access(HELD, "own") == "own"
        assert org_scope.verify_project_access(WHOLE, "default") == "default"
        assert verify_file_access(HELD, "own", "web") == "own"
        assert org_scope.check_tenant_permission(WHOLE, "default", "vm", "console")


def test_an_admin_is_unchanged() -> None:
    tokens = [
        (org_scope._allowed_clusters_var, org_scope._allowed_clusters_var.set(None)),
        (org_scope._default_view_only_var, org_scope._default_view_only_var.set(frozenset({HELD}))),
    ]
    set_request_context(RequestContext(method="POST", path="/api/v1/vms/x/start"))
    try:
        assert org_scope.verify_project_access(HELD, "default") == "default"
        assert org_scope.check_tenant_permission(HELD, "default", "x", "console")
    finally:
        set_request_context(None)
        for var, token in reversed(tokens):
            var.reset(token)


def test_the_view_only_set_is_the_partners_held_clusters_only(monkeypatch) -> None:
    from app.core import rbac
    from app.services import cluster_parties

    seen = {}

    async def held(_db, _email, clusters, also_runs=frozenset()):
        seen["also_runs"] = set(also_runs)
        return {c for c in clusters if c == HELD} | {"not-reachable"}

    async def member_orgs(_db, email):
        seen["email"] = email
        return {480}

    monkeypatch.setattr(cluster_parties, "clusters_held_by_another_party", held)
    monkeypatch.setattr(cluster_parties, "member_orgs", member_orgs)
    user = type("U", (), {"email": "p@example.com"})()
    monkeypatch.setattr(rbac, "resolve_roles", lambda _u: [rbac.Role.PARTNER])
    assert asyncio.run(org_scope._default_view_only(user, object(), {HELD, WHOLE})) == frozenset({HELD})
    # A teammate's member row in its own organization counts as that partner's side (owner 2026-11-05).
    assert seen["also_runs"] == {480}
    monkeypatch.setattr(rbac, "resolve_roles", lambda _u: [rbac.Role.TENANT])
    assert asyncio.run(org_scope._default_view_only(user, object(), {HELD, WHOLE})) == frozenset()
    assert asyncio.run(org_scope._default_view_only(user, object(), None)) == frozenset()


def test_reads_stay_open_in_a_view_only_default() -> None:
    # Owner decision 2026-11-05: lists and reads stay; only a change or a session is refused.
    with _partner("GET", "/api/v1/instances/their-vm/snapshots"):
        assert org_scope.check_tenant_permission(HELD, "default", "their-vm", "snapshot")
    with _partner("POST", "/api/v1/instance-files/list"):
        assert not org_scope.check_tenant_permission(HELD, "default", "their-vm", "file_transfer")


def test_a_member_row_puts_its_organization_on_the_callers_side() -> None:
    from types import SimpleNamespace

    from app.services import cluster_parties

    class _Rows:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _Db:
        async def execute(self, _statement):
            # One binding: the teammate's own organization 480 holds the cluster.
            return _Rows([("lab", 480, "owner@example.com")])

    async def nothing(*_a, **_k):
        return {}

    async def no_keys(*_a, **_k):
        return set()

    import app.services.organization_owners as owners

    original = (owners.owners_of, cluster_parties.run_orgs, cluster_parties.side_keys)
    owners.owners_of, cluster_parties.run_orgs, cluster_parties.side_keys = nothing, nothing, no_keys
    try:
        held = asyncio.run(cluster_parties.clusters_held_by_another_party(_Db(), "mate@example.com", {"lab"}))
        assert held == {"lab"}, "without the member row the teammate's own cluster reads as foreign"
        kept = asyncio.run(cluster_parties.clusters_held_by_another_party(
            _Db(), "mate@example.com", {"lab"}, also_runs={480}))
        assert kept == set()
    finally:
        owners.owners_of, cluster_parties.run_orgs, cluster_parties.side_keys = original
    del SimpleNamespace


# Every write to a project-scoped resource reaches the `default` gate (security review of the fix
# diff, 2026-11-05: volume delete, image delete, profile writes and PATCH /projects/{host}/default
# went around verify_project_access).
_GATES = {"verify_project_access", "refuse_default_operation", "_profile_write_target",
          "verify_default_project_write", "verify_whole_cluster_access"}
_WRITE_GATE_EXEMPT = {
    ("projects.py", "create_project"): "makes a new project; `default` already exists",
    ("projects.py", "delete_project"): "LXD refuses to delete `default`",
}


def test_every_project_resource_write_reaches_the_default_gate() -> None:
    import ast
    from pathlib import Path

    routers = Path(__file__).resolve().parents[1] / "app" / "routers"
    missing = []
    for name in ("storage_volumes.py", "images.py", "profiles.py", "projects.py", "networks.py", "network_acls.py"):
        tree = ast.parse((routers / name).read_text())
        for fn in tree.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            writes = [d for d in fn.decorator_list if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                      and d.func.attr in ("post", "put", "patch", "delete")]
            if not writes or (name, fn.name) in _WRITE_GATE_EXEMPT:
                continue
            called = {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
                      for c in ast.walk(fn) if isinstance(c, ast.Call)}
            if not called & _GATES:
                missing.append(f"{name}::{fn.name}")
    assert not missing, "these writes skip the default gate:\n  " + "\n  ".join(missing)
