"""Who may change a guest's logins, and the ratchets that keep it so as the routes grow."""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import org_scope
from app.core.config import get_settings
from app.core.instance_access import verify_guest_access
from app.routers import guest_credentials
from app.services import activity_catalogue
from app.services.guest_credentials import audit as cred_audit
from app.services.guest_credentials import gates
from app.services.task_journal.vocabulary import LOG_SUPPRESSED_ACTIONS

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"
PACKAGE = Path(gates.__file__).resolve().parent
HOST = "shared-cluster"
INSTANCE = "vm1"
OWN_PROJECT = "tenant-project"


@pytest.fixture
def partner_scope():
    allowed = org_scope._allowed_clusters_var.set({HOST})
    projects = org_scope._visible_projects_var.set({HOST: {"default", OWN_PROJECT}})
    acl = org_scope._tenant_acl_var.set(None)
    try:
        yield
    finally:
        org_scope._tenant_acl_var.reset(acl)
        org_scope._visible_projects_var.reset(projects)
        org_scope._allowed_clusters_var.reset(allowed)


def _tenant_with(monkeypatch, permissions):
    def verify(cluster_id, project_name="default", instance_name=None, required_permission="view"):
        if required_permission not in permissions:
            raise HTTPException(status_code=403, detail="no permission")

    monkeypatch.setattr("app.core.instance_access.verify_tenant_permission", verify)


def test_a_tenant_with_console_gets_the_bound_project(partner_scope, monkeypatch):
    _tenant_with(monkeypatch, {"view", "console"})
    assert verify_guest_access(HOST, OWN_PROJECT, INSTANCE) == OWN_PROJECT


@pytest.mark.parametrize("enforcement", [True, False])
def test_a_viewer_is_refused_whatever_the_enforcement_flag_says(partner_scope, monkeypatch, enforcement):
    # That flag relaxes the console-grant rollout. Honouring it here would let every viewer set
    # root's password once an operator switches it off.
    monkeypatch.setattr(get_settings(), "tenant_perm_enforcement_enabled", enforcement, raising=False)
    _tenant_with(monkeypatch, {"view", "operate", "file_transfer", "snapshot"})
    with pytest.raises(HTTPException) as refused:
        verify_guest_access(HOST, OWN_PROJECT, INSTANCE)
    assert refused.value.status_code == 403


def test_a_foreign_project_or_cluster_is_refused(partner_scope):
    with pytest.raises(HTTPException) as refused:
        verify_guest_access(HOST, "other-org", INSTANCE)
    assert refused.value.status_code in (403, 404)
    with pytest.raises(HTTPException):
        verify_guest_access("not-mine", OWN_PROJECT, INSTANCE)


def test_an_admin_without_a_grant_never_reaches_the_guest(monkeypatch):
    opened = []

    async def refuse(user, host):
        raise HTTPException(status_code=403, detail={"error": "break_glass_required"})

    monkeypatch.setattr(gates, "sensitive_gate", refuse)
    monkeypatch.setattr(gates, "_open", lambda *args: opened.append(args))

    async def run():
        async with gates.admitted(SimpleNamespace(user_id="admin"), HOST, "default", INSTANCE, kind="read"):
            raise AssertionError("admitted without a grant")

    with pytest.raises(HTTPException) as refused:
        asyncio.run(run())
    assert refused.value.detail["error"] == "break_glass_required"
    assert opened == []


def test_a_fenced_instance_answers_with_its_own_code(monkeypatch):
    from app.services.migration_fence import MigrationFenceConflict

    async def fenced(**kwargs):
        raise MigrationFenceConflict("Migration Center owns this instance")

    async def no_grant(user, host):
        return None

    monkeypatch.setattr(gates, "sensitive_gate", no_grant)
    monkeypatch.setattr(gates, "assert_instance_not_migration_fenced", fenced)

    async def run():
        async with gates.admitted(None, HOST, "default", INSTANCE, kind="write"):
            pass

    with pytest.raises(HTTPException) as refused:
        asyncio.run(run())
    assert (refused.value.status_code, refused.value.detail["error"]) == (409, "migration_fenced")


def test_a_disabled_deployment_answers_503(monkeypatch):
    monkeypatch.setattr(get_settings(), "guest_credentials_enabled", False, raising=False)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(guest_credentials.require_credentials_enabled())
    assert (refused.value.status_code, refused.value.detail["error"]) == (503, "feature_disabled")


def test_the_budget_holds_without_redis(monkeypatch):
    monkeypatch.setattr(gates, "_shared_allow", lambda *args: None)
    monkeypatch.setattr(gates, "_budget", {})
    user = SimpleNamespace(user_id="u1")
    for _ in range(gates.READS_PER_MINUTE):
        gates.spend(user, HOST, "default", INSTANCE, "read")
    with pytest.raises(Exception) as refused:
        gates.spend(user, HOST, "default", INSTANCE, "read")
    assert refused.value.code == "rate_limited" and refused.value.extra["retry_after"] == 60
    gates.spend(SimpleNamespace(user_id="u2"), HOST, "default", INSTANCE, "read")


def _handlers():
    tree = ast.parse((ROUTERS / "guest_credentials.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and any("router." in ast.unparse(d) for d in node.decorator_list):
            yield node


def _first_statement(node):
    body = node.body[1:] if isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) else node.body
    return ast.unparse(body[0])


def test_every_handler_opens_with_the_guest_gate():
    handlers = list(_handlers())
    assert len(handlers) == 9
    for node in handlers:
        assert _first_statement(node).startswith("bound = verify_guest_access(host, project, instance_name)"), node.name


def test_every_change_refuses_in_read_only_mode_and_reads_do_not_need_to():
    reads = {"overview", "ssh_server", "list_keys"}
    for node in _handlers():
        decorator = "".join(ast.unparse(d) for d in node.decorator_list)
        assert ("check_read_only_mode" in decorator) == (node.name not in reads), node.name


def test_no_handler_declares_a_body_fastapi_would_echo():
    for node in _handlers():
        for arg in node.args.args:
            annotation = ast.unparse(arg.annotation) if arg.annotation else ""
            assert not annotation.endswith("Body"), f"{node.name} declares {arg.arg}: {annotation}"
        if node.name not in ("overview", "ssh_server"):
            assert "read_json_body(request" in ast.unparse(node), node.name


def test_nothing_sensitive_travels_in_a_query():
    for node in _handlers():
        names = {a.arg for a in node.args.args + node.args.kwonlyargs}
        assert names <= {"request", "host", "project", "instance_name", "user"}, node.name


def test_the_router_carries_org_scope_a_role_and_the_switch():
    calls = [getattr(dep.dependency, "__qualname__", "") for dep in guest_credentials.router.dependencies]
    assert "org_scope_dependency" in calls
    assert any(name.startswith("require_roles") for name in calls)
    assert "release_request_db" in calls
    assert "require_credentials_enabled" in calls


def test_no_cluster_lock_is_held_across_a_guest_round_trip():
    for path in [ROUTERS / "guest_credentials.py", *PACKAGE.glob("*.py")]:
        tree = ast.parse(path.read_text())
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "guard_instance_mutation" not in names, path.name


def test_pylxd_execute_and_the_shared_exec_pools_are_never_used():
    # pylxd's exec sockets do not check the cluster's certificate (pinned_exec.py says why).
    for path in [ROUTERS / "guest_credentials.py", *PACKAGE.glob("*.py")]:
        source = path.read_text()
        tree = ast.parse(source)
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert "execute" not in attrs, path.name
        assert not {"exec_executor", "desktop_executor", "exec_instance", "_session_executor"} & (attrs | names), path.name


def test_every_action_is_catalogued_and_none_borrows_my_keys_names():
    for action in cred_audit.ACTIONS:
        assert action in activity_catalogue.ACTION_CATEGORIES, action
        assert not action.startswith("user.ssh_key")
    writes = set(cred_audit.ACTIONS) - {"guest.account.view"}
    assert writes <= activity_catalogue.HIGH_SEVERITY_ACTIONS
    assert "guest.account.view" in LOG_SUPPRESSED_ACTIONS


def test_audit_details_never_carry_a_word_the_logger_redacts():
    for key in cred_audit.ALLOWED_DETAILS:
        assert not any(word in key for word in ("password", "secret", "token", "credential")), key
    assert cred_audit.allowed({"user": "u", "password": "x", "public_key": "y", "stdout": "z"}) == {"user": "u"}


def test_one_person_cannot_hold_every_thread_of_the_pool():
    from app.services.guest_credentials import runtime

    assert gates.SLOTS["user"] < runtime.POOL_WORKERS
