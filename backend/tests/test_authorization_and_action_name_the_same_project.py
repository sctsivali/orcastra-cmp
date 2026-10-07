"""A route must act on the project it authorized, not on whichever one holds the name.

An instance name is unique per project, not per host. Three power routes checked the tenant ACL
against `request.project or "default"` and then handed `request.project` to the service, which
searches EVERY project when it is None and returns the first hit. A tenant holding `operate` on
`default` was therefore authorized there while the action ran against whichever project LXD
listed first, which on a cluster hosting several tenants is somebody else's instance.

The metrics route was worse: no tenant check at all, and the reader behind it took a `project`
argument and never passed it to the resolver.

Both are the shape of the `VMInfo.project` defect fixed alongside them: a value is resolved
correctly in one place and the thing that acts on it trusts a different one.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.lxd import instance as instance_module
from app.services.lxd import instance_metrics as metrics_module

HOST = "nuc1"
NAME = "web-01"
# The tenant holds operate on `default` only. `acme` belongs to somebody else.
TENANT_ACL = {HOST: {("default", "*"): {"view", "operate"}}}


class _Recorder:
    """Stands in for the resolver and remembers which project the route asked it for."""

    def __init__(self):
        self.projects = []

    def __call__(self, client, name, project=None):
        self.projects.append(project)
        stub = type(
            "Stub",
            (),
            {
                "name": name, "status": "Stopped", "status_code": 102,
                "project": "default", "architecture": "x86_64", "config": {}, "devices": {},
                "expanded_devices": {}, "profiles": ["default"], "description": "",
                "created_at": None, "last_used_at": None, "location": "none", "type": "container",
                "start": lambda self, **k: None, "stop": lambda self, **k: None,
                "restart": lambda self, **k: None, "sync": lambda self, **k: None,
                "state": lambda self: type("S", (), {
                    "network": None, "memory": None, "cpu": None, "disk": None, "processes": 0,
                })(),
            },
        )()
        return stub, (project or "default")


def _app(
    acl,
    flag_on: bool,
    recorder: _Recorder,
    module_name: str = "instances",
    visible_projects=None,
) -> TestClient:
    from app.core import org_scope
    from app.core.config import get_settings
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.services.lxd_service import get_lxd_service

    class _Conn:
        def get_client(self, host, *a, **k):
            return object()

        def get_node_names(self):
            return [HOST]

    from app.services.lxd.lxd_service import LXDService
    from app.services.lxd.projects import ProjectManager
    from app.services.lxd.resources import ResourceManager

    svc = LXDService.__new__(LXDService)
    conn = _Conn()
    svc.connection_manager = conn
    svc.resource_manager = ResourceManager()
    svc.cert_manager = None
    svc.project_manager = ProjectManager(conn, None)
    svc.instance_manager = instance_module.InstanceManager(conn, svc.resource_manager, None)

    import importlib

    module = importlib.import_module(f"app.routers.{module_name}")
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None

    settings = get_settings()
    original = settings.tenant_perm_enforcement_enabled
    object.__setattr__(settings, "tenant_perm_enforcement_enabled", flag_on)
    token = org_scope._tenant_acl_var.set(acl)
    project_token = org_scope._visible_projects_var.set(visible_projects)

    client = TestClient(app, raise_server_exceptions=False)
    client._cleanup = lambda: (
        org_scope._tenant_acl_var.reset(token),
        org_scope._visible_projects_var.reset(project_token),
        object.__setattr__(settings, "tenant_perm_enforcement_enabled", original),
    )
    return client


@pytest.fixture
def recorder(monkeypatch):
    r = _Recorder()
    monkeypatch.setattr(instance_module, "find_instance_in_projects", r)
    monkeypatch.setattr(metrics_module, "find_instance_in_projects", r)
    return r


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_a_power_action_addresses_the_project_it_authorized(action, recorder):
    """With no project named, the ACL is checked on `default` and the action must go there too."""
    client = _app(TENANT_ACL, True, recorder)
    try:
        response = client.post(f"/instances/{NAME}/{action}", json={"host": HOST})
    finally:
        client._cleanup()

    assert response.status_code < 400, response.text
    assert recorder.projects, f"{action} never reached the resolver"
    assert None not in recorder.projects, (
        f"{action} asked the resolver to search every project after authorizing only `default`; "
        f"projects requested: {recorder.projects}"
    )
    assert recorder.projects[0] == "default"


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_a_power_action_on_a_project_the_tenant_does_not_hold_is_refused(action, recorder):
    client = _app(TENANT_ACL, True, recorder)
    try:
        response = client.post(
            f"/instances/{NAME}/{action}", json={"host": HOST, "project": "acme"}
        )
    finally:
        client._cleanup()

    assert response.status_code == 403, response.text
    assert not recorder.projects, "a refused action must not have reached the cluster"


def test_metrics_are_refused_for_an_instance_the_tenant_does_not_hold(recorder):
    """This route had no tenant check at all, so any instance on a reachable cluster was readable."""
    client = _app(TENANT_ACL, True, recorder)
    try:
        response = client.get(f"/instances/{NAME}/metrics?host={HOST}&project=acme")
    finally:
        client._cleanup()

    assert response.status_code == 403, response.text
    assert not recorder.projects, "a refused read must not have reached the cluster"


def test_metrics_read_the_project_they_were_given(recorder):
    """The reader took a `project` argument and never passed it to the resolver."""
    client = _app(None, False, recorder)
    try:
        response = client.get(f"/instances/{NAME}/metrics?host={HOST}&project=acme")
    finally:
        client._cleanup()

    assert response.status_code < 500, response.text
    assert recorder.projects, "metrics never reached the resolver"
    assert recorder.projects[0] == "acme", (
        f"the reader searched {recorder.projects[0]!r} for an instance asked for in 'acme'"
    )


def test_an_operator_is_unaffected(recorder):
    """Admin and Partner carry no ACL, so none of this narrows what they can reach."""
    client = _app(None, False, recorder)
    try:
        response = client.post(
            f"/instances/{NAME}/start", json={"host": HOST, "project": "acme"}
        )
    finally:
        client._cleanup()

    assert response.status_code < 400, response.text
    assert recorder.projects[0] == "acme"


@pytest.mark.parametrize("module_name", ["instances", "vms"])
@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_a_partner_cannot_operate_on_another_organizations_same_named_instance(
    module_name, action, recorder
):
    client = _app(
        None,
        False,
        recorder,
        module_name=module_name,
        visible_projects={HOST: {"default", "partner-owned"}},
    )
    try:
        response = client.post(
            f"/{module_name}/{NAME}/{action}",
            json={"host": HOST, "project": "other-org"},
        )
    finally:
        client._cleanup()

    assert response.status_code == 403, response.text
    assert recorder.projects == [], "a refused Partner action reached the instance resolver"


@pytest.mark.parametrize("module_name", ["instances", "vms"])
@pytest.mark.parametrize("project", [None, "", "   "])
def test_power_actions_normalize_omitted_and_blank_projects_once(
    module_name, project, recorder
):
    client = _app(TENANT_ACL, True, recorder, module_name=module_name)
    body = {"host": HOST}
    if project is not None:
        body["project"] = project
    try:
        response = client.post(f"/{module_name}/{NAME}/start", json=body)
    finally:
        client._cleanup()

    assert response.status_code < 400, response.text
    assert recorder.projects[0] == "default"


@pytest.mark.parametrize("module_name", ["instances", "vms"])
def test_disabling_tenant_operation_enforcement_never_disables_view(
    module_name, recorder
):
    operate_without_view = {HOST: {("default", "*"): {"operate"}}}
    client = _app(operate_without_view, False, recorder, module_name=module_name)
    try:
        response = client.post(f"/{module_name}/{NAME}/start", json={"host": HOST})
    finally:
        client._cleanup()

    assert response.status_code == 403, response.text
    assert recorder.projects == []


@pytest.mark.parametrize("module_name", ["instances", "vms"])
def test_partner_delete_is_project_bound_before_the_service(module_name, recorder):
    client = _app(
        None,
        False,
        recorder,
        module_name=module_name,
        visible_projects={HOST: {"default"}},
    )
    try:
        response = client.delete(
            f"/{module_name}/{NAME}?host={HOST}&project=other-org"
        )
    finally:
        client._cleanup()

    assert response.status_code == 403, response.text
    assert recorder.projects == [], "a refused deletion reached the instance resolver"


# ── the same rule, on every surface that addresses one instance ────────────────────────────


def _guarded_routes():
    """Every route that names an instance and is reachable by a tenant, with its guard state.

    A structural check rather than a live one: this is about a rule holding across a dozen
    handlers in four files, which is the kind of coverage an LLM audit is bad at and a parse is
    good at. Each of these leaked something concrete before the guard was added: another tenant's
    live metrics, their console capabilities, their attached ISO, their tags, their snapshot
    names.
    """
    import ast
    import pathlib

    findings = {}
    # important: vms.py is in this list because it is the router the console actually drives, and
    # it is exactly the twin that slipped through the reading pass. Any router that addresses an
    # instance belongs here; leaving one out is how the next one slips.
    for name in ("console.py", "tags.py", "instance_rebuild.py", "instances.py", "vms.py"):
        path = pathlib.Path("app/routers") / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            body = ast.dump(node)
            if "verify_cluster_access" not in body:
                continue
            # Partner-only routes carry no tenant ACL, so a per-instance check there is inert.
            partner_only = "require_partner" in body or "require_admin" in body
            # important: only a REQUIRED instance argument means the route addresses one
            # instance. An optional one is a filter on a listing, and those are authorized by
            # narrowing the cluster set instead, which console.py's session listing documents
            # at length. Counting them here would demand a per-instance check that is the wrong
            # shape for a listing, and the rule would get relaxed to make the noise stop.
            required = node.args.args[: len(node.args.args) - len(node.args.defaults)]
            optional_names = {
                a.arg
                for a, d in zip(node.args.args[len(required):], node.args.defaults)
                if isinstance(d, ast.Call) and "Query" in ast.dump(d) and "None" in ast.dump(d)
            }
            names_instance = any(
                a.arg in ("instance_name", "resource_id")
                for a in node.args.args
                if a.arg not in optional_names
            )
            if not names_instance or partner_only:
                continue
            guarded = (
                "_verify_instance_access" in body
                or "verify_instance_view_access" in body
                or "verify_tenant_permission" in body
                or "_enforce_snapshot_access" in body
            )
            findings[f"{name}::{node.name}"] = guarded
    return findings


def test_every_tenant_reachable_instance_route_checks_the_instance():
    """`verify_cluster_access` answers "may you reach this cluster", never "is this yours"."""
    routes = _guarded_routes()
    assert routes, "the parse found no routes, so this test would pass while checking nothing"

    unguarded = sorted(name for name, guarded in routes.items() if not guarded)
    assert unguarded == [], (
        "these routes name one instance, are reachable by a tenant, and are gated only on the "
        f"cluster: {unguarded}. A cluster check is true of every instance a tenant's neighbours "
        "run on it."
    )


# ── the router the console actually drives ─────────────────────────────────────────────────


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_the_vms_power_routes_address_the_project_they_authorized(action, recorder):
    """`/vms` is the router the console calls, and it carried the same split.

    The frontend reaches start, stop, restart and metrics through `vmAPI` (frontend/src/lib/api.ts),
    which posts to `/vms/...`. The identical handlers under `/instances` are not called by the
    console at all, so fixing only those would have left the live path open while every test went
    green. That is this codebase's most expensive recurring defect, committed while fixing it.
    """
    client = _app(TENANT_ACL, True, recorder, module_name="vms")
    try:
        response = client.post(f"/vms/{NAME}/{action}", json={"host": HOST})
    finally:
        client._cleanup()

    assert response.status_code < 400, response.text
    assert recorder.projects, f"vms {action} never reached the resolver"
    assert None not in recorder.projects, (
        f"vms {action} asked the resolver to search every project after authorizing only "
        f"`default`; projects requested: {recorder.projects}"
    )


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_a_vms_power_action_on_an_unheld_project_is_refused(action, recorder):
    client = _app(TENANT_ACL, True, recorder, module_name="vms")
    try:
        response = client.post(f"/vms/{NAME}/{action}", json={"host": HOST, "project": "acme"})
    finally:
        client._cleanup()

    assert response.status_code == 403, response.text
    assert not recorder.projects, "a refused action must not have reached the cluster"


def test_vms_metrics_are_refused_for_an_instance_the_tenant_does_not_hold(recorder):
    client = _app(TENANT_ACL, True, recorder, module_name="vms")
    try:
        response = client.get(f"/vms/{HOST}/{NAME}/metrics?project=acme")
    finally:
        client._cleanup()

    assert response.status_code == 403, response.text
    assert not recorder.projects, "a refused read must not have reached the cluster"


def test_an_empty_project_does_not_authorize_one_thing_and_resolve_another(recorder):
    """`?project=` is falsy, and the service reads a falsy project as "search every project".

    A helper that normalised with `or "default"` behind the caller's back therefore authorized
    `default` while the action searched everywhere, which is the split these fixes exist to close,
    reintroduced by the fix itself.
    """
    client = _app(TENANT_ACL, True, recorder, module_name="instance_rebuild")
    try:
        response = client.get(f"/instances/{NAME}/rebuild-preflight?host={HOST}&project=")
    finally:
        client._cleanup()

    if response.status_code < 400:
        assert "" not in recorder.projects, (
            "an empty project reached the resolver, which reads it as every project"
        )
