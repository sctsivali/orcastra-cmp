"""An instance action authorizes one project and then acts on exactly that project.

The power, delete, metrics and single-read routes checked `request.project or "default"` and
then handed the raw value to the service, where None meant "search every project and take the
first hit". On a cluster bound to two organizations a Partner could start, stop, delete or read
the metrics of the other organization's same-named instance, and the metrics route had no
tenant check at all.

These tests pin the resolve-then-authorize model: a named project is authorized as given, an
omitted one is searched only among the projects the caller may view, and the service always
receives the concrete project that was authorized, never None.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import instance_access
from app.core.instance_access import (
    normalize_project,
    resolve_instance_project,
    verify_instance_view_access,
    verify_project_access,
)
from app.models.schemas import VMActionRequest
from app.routers import instances, vms
from app.services.lxd.resources import ResourceManager
from tests.instance_access_fakes import (
    CLUSTER,
    AsyncRecorder,
    FakeClient,
    FakeLXDService,
    Recorder,
    partner,
    scoped,
    tenant,
)

BOTH = FakeClient({"other-db": {"web01"}, "own-web": {"web01"}}, order=["other-db", "own-web"])


# -- the helpers -------------------------------------------------------------------------------

def test_a_blank_project_means_default():
    assert normalize_project(None) == "default"
    assert normalize_project("   ") == "default"
    assert normalize_project(" own-web ") == "own-web"
    with scoped():
        assert verify_project_access(CLUSTER, "") == "default"


def test_a_named_project_outside_the_bound_is_refused():
    with partner({"default", "own-web"}):
        with pytest.raises(HTTPException) as exc:
            verify_project_access(CLUSTER, "other-db")
    assert exc.value.status_code == 403


def test_a_tenant_without_view_is_refused_whatever_the_flag_says():
    with tenant({("own-web", "*"): {"operate"}}):
        with pytest.raises(HTTPException) as exc:
            verify_instance_view_access(CLUSTER, "own-web", "web01")
    assert exc.value.status_code == 403


async def test_a_named_project_is_authorized_as_given_with_no_search():
    service = FakeLXDService(client=None)
    with partner({"default", "own-web"}):
        assert await resolve_instance_project(service, CLUSTER, "web01", "own-web") == "own-web"
        with pytest.raises(HTTPException) as exc:
            await resolve_instance_project(service, CLUSTER, "web01", "other-db")
    assert exc.value.status_code == 403


async def test_an_omitted_project_is_searched_only_inside_the_bound():
    """other-db is listed first and holds a same-named instance, and must never be chosen."""
    with partner({"default", "own-web"}):
        found = await resolve_instance_project(FakeLXDService(BOTH), CLUSTER, "web01", None)
    assert found == "own-web"


async def test_an_omitted_project_is_searched_only_where_the_tenant_may_view():
    with tenant({("own-web", "web01"): {"view"}}):
        found = await resolve_instance_project(FakeLXDService(BOTH), CLUSTER, "web01", None)
    assert found == "own-web"


async def test_an_admin_takes_the_first_project_holding_the_instance():
    with scoped():
        found = await resolve_instance_project(FakeLXDService(BOTH), CLUSTER, "web01", "")
    assert found == "other-db"


async def test_an_instance_only_in_a_hidden_project_is_a_404_not_a_403():
    hidden = FakeClient({"other-db": {"web01"}, "own-web": set()}, order=["other-db", "own-web"])
    with partner({"default", "own-web"}):
        with pytest.raises(HTTPException) as exc:
            await resolve_instance_project(FakeLXDService(hidden), CLUSTER, "web01", None)
    assert exc.value.status_code == 404


async def test_an_unreachable_cluster_is_a_503():
    with scoped():
        with pytest.raises(HTTPException) as exc:
            await resolve_instance_project(FakeLXDService(None), CLUSTER, "web01", None)
    assert exc.value.status_code == 503
    assert CLUSTER in exc.value.detail


async def test_the_search_runs_in_a_thread_that_carries_the_scope(monkeypatch):
    """run_in_executor drops the ContextVars, so the search there would ignore the bound."""
    seen = []
    real = asyncio.to_thread

    async def spy(fn, *args, **kwargs):
        seen.append(fn.__name__)
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(instance_access.asyncio, "to_thread", spy)
    with partner({"default", "own-web"}):
        found = await resolve_instance_project(FakeLXDService(BOTH), CLUSTER, "web01", None)
    assert seen == ["_first_viewable_project"]
    assert found == "own-web"


# -- the routes --------------------------------------------------------------------------------

@pytest.fixture
def quiet_routers(monkeypatch):
    """Cache, audit, webhook and flag stubs, so a handler runs with no Redis, DB or network."""
    for module in (vms, instances):
        monkeypatch.setattr(module, "get_cache_manager", lambda: Recorder())
        monkeypatch.setattr(module, "activity_logger", Recorder())
        monkeypatch.setattr(
            module, "get_settings", lambda: SimpleNamespace(tenant_perm_enforcement_enabled=False)
        )
    webhooks = Recorder()
    monkeypatch.setattr(instances, "schedule_orcahub_event", webhooks.emit)
    return webhooks


def _power(module, action):
    handler = {
        (vms, "start"): vms.start_vm, (vms, "stop"): vms.stop_vm, (vms, "restart"): vms.restart_vm,
        (instances, "start"): instances.start_instance,
        (instances, "stop"): instances.stop_instance,
        (instances, "restart"): instances.restart_instance,
    }[(module, action)]

    async def call(service, project):
        name = "vm_name" if module is vms else "instance_name"
        request = VMActionRequest(host=CLUSTER, project=project)
        return await handler(**{name: "web01"}, request=request, lxd_service=service, _=None)

    return call


def _delete(module):
    async def call(service, project):
        if module is vms:
            return await vms.delete_vm(
                vm_name="web01", host=CLUSTER, force=False, project=project,
                lxd_service=service, tag_service=AsyncRecorder(), _=None, _rbac=None,
            )
        return await instances.delete_instance(
            instance_name="web01", host=CLUSTER, project=project, force=False,
            lxd_service=service, tag_service=AsyncRecorder(), _=None, _rbac=None,
        )

    return call


def _metrics(module):
    async def call(service, project):
        if module is vms:
            return await vms.get_instance_metrics(
                host=CLUSTER, instance_name="web01", project=project, lxd_service=service
            )
        return await instances.get_instance_metrics(
            instance_name="web01", host=CLUSTER, project=project, lxd_service=service
        )

    return call


def _single_read(module):
    async def call(service, project):
        if module is vms:
            return await vms.get_vm(vm_name="web01", host=CLUSTER, project=project, lxd_service=service)
        return await instances.get_instance(
            instance_name="web01", host=CLUSTER, project=project, lxd_service=service
        )

    return call


HANDLERS = {
    "vms.start": (_power(vms, "start"), "start"),
    "vms.stop": (_power(vms, "stop"), "stop"),
    "vms.restart": (_power(vms, "restart"), "restart"),
    "instances.start": (_power(instances, "start"), "start"),
    "instances.stop": (_power(instances, "stop"), "stop"),
    "instances.restart": (_power(instances, "restart"), "restart"),
    "vms.delete": (_delete(vms), "delete"),
    "instances.delete": (_delete(instances), "delete"),
    "vms.metrics": (_metrics(vms), "metrics"),
    "instances.metrics": (_metrics(instances), "metrics"),
    "vms.get": (_single_read(vms), "get"),
    "instances.get": (_single_read(instances), "get"),
}


@pytest.mark.parametrize("label", sorted(HANDLERS))
async def test_the_service_receives_the_resolved_project_never_none(label, quiet_routers):
    call, action = HANDLERS[label]
    service = FakeLXDService(BOTH)
    with partner({"default", "own-web"}):
        await call(service, None)
    assert service.calls == [(action, "own-web")]


@pytest.mark.parametrize("label", sorted(HANDLERS))
async def test_a_foreign_project_is_refused_before_the_service_is_called(label, quiet_routers):
    call, _action = HANDLERS[label]
    service = FakeLXDService(BOTH)
    with partner({"default", "own-web"}):
        with pytest.raises(HTTPException) as exc:
            await call(service, "other-db")
    assert exc.value.status_code == 403
    assert service.calls == []


@pytest.mark.parametrize("label", ["vms.metrics", "instances.metrics", "vms.get", "instances.get"])
async def test_a_tenant_read_of_an_unviewable_instance_is_refused_with_the_flag_off(
    label, quiet_routers
):
    call, _action = HANDLERS[label]
    service = FakeLXDService(BOTH)
    with tenant({("own-web", "other"): {"view"}}):
        with pytest.raises(HTTPException) as exc:
            await call(service, "own-web")
    assert exc.value.status_code == 403
    assert service.calls == []


async def test_a_delete_names_the_resolved_project_in_the_webhook_and_tag_cleanup(quiet_routers):
    service = FakeLXDService(BOTH)
    tags = AsyncRecorder()
    with partner({"default", "own-web"}):
        await instances.delete_instance(
            instance_name="web01", host=CLUSTER, project=None, force=False,
            lxd_service=service, tag_service=tags, _=None, _rbac=None,
        )
    assert tags.calls[0][2]["resource_project"] == "own-web"
    assert quiet_routers.calls[0][1] == ("instance.deleted", CLUSTER, "own-web", "web01")


# -- the metrics reader ------------------------------------------------------------------------

def test_the_metrics_reader_reads_the_project_it_was_given(monkeypatch):
    from app.services.lxd import resources

    asked = []

    def fake_find(client, name, project=None):
        asked.append(project)
        return None, None

    monkeypatch.setattr(resources, "find_instance_in_projects", fake_find)
    with pytest.raises(Exception):
        ResourceManager().get_instance_metrics(object(), CLUSTER, "web01", "own-web")
    assert asked == ["own-web"]


def test_two_projects_keep_separate_cpu_history(monkeypatch):
    from app.services.lxd import resources

    class _Instance:
        status = "Running"
        status_code = 103
        config: dict = {}
        last_used_at = None

        def state(self):
            return SimpleNamespace(
                cpu={"usage": 1_000}, memory=None, disk=None, network=None, processes=0
            )

    monkeypatch.setattr(
        resources, "find_instance_in_projects", lambda client, name, project=None: (_Instance(), project)
    )
    manager = ResourceManager()
    manager.get_instance_metrics(object(), CLUSTER, "web01", "own-web")
    manager.get_instance_metrics(object(), CLUSTER, "web01", "other-db")
    assert {f"{CLUSTER}:own-web:web01", f"{CLUSTER}:other-db:web01"} <= set(manager._cpu_history)
