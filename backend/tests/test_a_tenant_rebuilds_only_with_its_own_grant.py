"""A Tenant may rebuild an instance only with view, operate and rebuild on that exact instance.

Rebuild erases the root disk. It used to be Partner-only; Tenants now reach it through a grant of
its own, never implied by `operate` (which every OrcaHub purchase carries), checked whatever the
tenant-permission rollout flag says, and never in the empty mode a Tenant could not boot from.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core import org_scope
from app.core.config import get_settings
from app.models.db_models import RebuildJob
from app.routers import instance_rebuild as routes
from tests._sqlite_session import sqlite_session, with_json_tables

HOST, PROJECT, NAME = "cluster-a", "cust-a", "vm1"
TENANT = SimpleNamespace(email="buyer@example.test", user_id="buyer")


class _Service:
    def __init__(self):
        self.preflights = []

    async def get_rebuild_preflight(self, name, host, project):
        self.preflights.append((name, host, project))
        return {"status": "Running", "type": "virtual-machine", "snapshots": [], "project": project, "config": {}}


@pytest.fixture
def world(monkeypatch):
    db = with_json_tables(sqlite_session(), RebuildJob)
    audit, isolation, started = [], [], []

    @asynccontextmanager
    async def session_maker():
        yield db

    @asynccontextmanager
    async def no_lock(**kwargs):
        yield

    async def isolation_check(lxd_service, user, host, project, session, tenant=False):
        isolation.append((host, project, tenant))

    async def run(job_id, provisioning):
        started.append(job_id)

    monkeypatch.setattr(routes, "async_session_maker", session_maker)
    monkeypatch.setattr(routes, "guard_instance_mutation", no_lock)
    monkeypatch.setattr(routes, "_refuse_a_rebuild_in_an_unisolated_project", isolation_check)
    monkeypatch.setattr(routes, "_target_engine", lambda service, host: "lxd")
    monkeypatch.setattr(routes, "_validate_image_server", lambda server, engine: None)
    monkeypatch.setattr(routes, "_run_rebuild", run)
    monkeypatch.setattr(routes, "get_cache_manager", lambda: SimpleNamespace(invalidate_instance_cache=lambda: None))
    monkeypatch.setattr(routes.activity_logger, "log_activity", lambda **kw: audit.append(kw))

    tokens = [
        org_scope._allowed_clusters_var.set({HOST}),
        org_scope._visible_projects_var.set({HOST: {PROJECT, "cust-a-2"}}),
    ]
    acl_token = org_scope._tenant_acl_var.set(None)
    settings = get_settings()
    saved = (settings.tenant_perm_enforcement_enabled, settings.tenant_rebuild_per_instance_per_hour,
             settings.tenant_rebuild_concurrent_per_org)
    object.__setattr__(settings, "tenant_perm_enforcement_enabled", True)

    def grant(*perms, instance=NAME, project=PROJECT):
        org_scope._tenant_acl_var.set({HOST: {(project, instance): set(perms)}})

    yield SimpleNamespace(db=db, audit=audit, isolation=isolation, started=started, grant=grant,
                          service=_Service(), settings=settings)

    object.__setattr__(settings, "tenant_perm_enforcement_enabled", saved[0])
    object.__setattr__(settings, "tenant_rebuild_per_instance_per_hour", saved[1])
    object.__setattr__(settings, "tenant_rebuild_concurrent_per_org", saved[2])
    org_scope._tenant_acl_var.reset(acl_token)
    org_scope._visible_projects_var.reset(tokens[1])
    org_scope._allowed_clusters_var.reset(tokens[0])


def _body(**overrides):
    fields = {"host": HOST, "project": PROJECT, "mode": "image", "image": "ubuntu/24.04",
              "image_server": "https://images.lxd.canonical.com", "image_protocol": "simplestreams", "confirm_name": NAME}
    fields.update(overrides)
    return routes.RebuildRequest(**fields)


async def _rebuild(world, name=NAME, **overrides):
    return await routes.rebuild_instance(
        name, _body(**overrides), lxd_service=world.service, _=None, user=TENANT, db=world.db,
    )


async def _refused(world, status, name=NAME, **overrides):
    with pytest.raises(HTTPException) as caught:
        await _rebuild(world, name=name, **overrides)
    assert caught.value.status_code == status, caught.value.detail
    return caught.value


@pytest.mark.parametrize("perms", [("view",), ("view", "operate"), ("view", "rebuild"), ("view", "operate", "console", "snapshot", "file_transfer")])
async def test_a_tenant_without_view_operate_and_rebuild_is_refused_before_the_cluster(world, perms):
    world.grant(*perms)
    await _refused(world, 403)
    assert world.service.preflights == [] and world.isolation == [] and world.started == []


async def test_a_tenant_with_the_grant_starts_a_rebuild_that_is_on_record(world):
    world.grant("view", "operate", "rebuild")
    job = await _rebuild(world)
    await asyncio.sleep(0)  # the worker is a detached task
    assert job["status"] == "pending" and job["project"] == PROJECT
    rows = (await world.db.execute(routes.select(RebuildJob))).scalars().all()
    assert [(r.instance_name, r.requester) for r in rows] == [(NAME, "buyer@example.test")]
    assert world.started == [job["job_id"]]
    assert world.isolation == [(HOST, PROJECT, True)], "the project isolation check runs for a Tenant too"
    requested = [a for a in world.audit if a["action"] == "instance.rebuild.requested"]
    assert requested and requested[0]["details"]["job_id"] == job["job_id"]


async def test_switching_the_rollout_flag_off_does_not_open_rebuild(world):
    object.__setattr__(world.settings, "tenant_perm_enforcement_enabled", False)
    world.grant("view", "operate")
    await _refused(world, 403)


async def test_a_grant_on_one_instance_does_not_reach_its_neighbour(world):
    world.grant("view", "operate", "rebuild", instance=NAME)
    await _refused(world, 403, name="vm2", confirm_name="vm2")


async def test_a_project_outside_the_organization_is_refused(world):
    world.grant("view", "operate", "rebuild", project="*", instance="*")
    await _refused(world, 403, project="cust-b")


async def test_a_tenant_cannot_rebuild_to_an_empty_disk(world):
    world.grant("view", "operate", "rebuild")
    failure = await _refused(world, 422, mode="empty", image=None)
    assert "ISO" in failure.detail
    assert world.service.preflights == []


async def test_a_wrong_confirmation_is_refused_before_anything_reaches_the_cluster(world):
    world.grant("view", "operate", "rebuild")
    await _refused(world, 422, confirm_name="vm1 ")
    assert world.isolation == [] and world.service.preflights == []


async def test_a_partner_keeps_every_mode(world):
    job = await _rebuild(world, mode="empty", image=None)
    assert job["mode"] == "empty"


async def test_a_tenants_concurrent_rebuilds_are_counted_across_its_projects(world):
    world.grant("view", "operate", "rebuild", project="*", instance="*")
    now = datetime.now(timezone.utc)
    for index, project in enumerate((PROJECT, "cust-a-2")):
        world.db.add(RebuildJob(job_id=f"r{index}", cluster_id=HOST, project=project, instance_name=f"other{index}",
                                mode="image", status="rebuilding", created_at=now - timedelta(minutes=2),
                                updated_at=now - timedelta(minutes=1)))
    await world.db.commit()
    failure = await _refused(world, 429)
    assert failure.detail["error"] == "rebuild_concurrency"


async def test_an_operator_can_turn_tenant_rebuild_off(world):
    object.__setattr__(world.settings, "tenant_rebuild_per_instance_per_hour", 0)
    world.grant("view", "operate", "rebuild")
    failure = await _refused(world, 403)
    assert failure.detail["error"] == "rebuild_disabled"


def test_cloud_init_is_bounded():
    with pytest.raises(ValidationError):
        _body(mode="cloud-init", cloud_init="#cloud-config\n" + "x" * routes.CLOUD_INIT_MAX_CHARS)


def test_an_unknown_image_protocol_is_refused():
    with pytest.raises(ValidationError):
        _body(image_server="https://images.lxd.canonical.com", image_protocol="ftp")


def test_rebuild_is_its_own_grant_in_the_catalogue():
    from app.core.tenant_permissions import TENANT_RULE_PERMISSIONS

    assert "rebuild" in TENANT_RULE_PERMISSIONS


def test_the_route_no_longer_requires_a_partner():
    import inspect

    source = inspect.getsource(routes.rebuild_instance)
    assert "require_partner" not in source and "verify_rebuild_access" in source


REAL_RUN = routes._run_rebuild


async def test_a_stop_that_fails_fails_the_job_before_any_rebuild(world, monkeypatch):
    from app.services.lxd_service import LXDOperationError

    calls = []

    class Stubborn:
        async def stop_vm(self, name, host, force, project):
            calls.append("stop")
            raise LXDOperationError("Could not stop VM: device busy")

        async def rebuild_instance(self, *args, **kwargs):
            calls.append("rebuild")
            return "op"

    monkeypatch.setattr(routes, "get_lxd_service", lambda: Stubborn())
    world.db.add(RebuildJob(job_id="j1", cluster_id=HOST, project=PROJECT, instance_name=NAME, mode="image",
                            source={"type": "image", "alias": "x"}, status="pending"))
    await world.db.commit()
    await REAL_RUN("j1", False)
    row = (await world.db.execute(routes.select(RebuildJob).where(RebuildJob.job_id == "j1"))).scalar_one()
    assert calls == ["stop"]
    assert row.status == "failed" and "Could not stop the instance" in row.error
    assert [a["result"] for a in world.audit if a["action"] == "instance.rebuild"] == ["failure"]


FINGERPRINT = "ab" * 32


def _allow_fingerprints(monkeypatch, fingerprints):
    async def allowed(lxd_service):
        return {HOST: set(fingerprints)}
    monkeypatch.setattr("app.routers.images._allowed_image_fingerprints_by_host", allowed)


async def test_a_tenant_cannot_name_a_store_image_by_alias(world, monkeypatch):
    world.grant("view", "operate", "rebuild")
    _allow_fingerprints(monkeypatch, {FINGERPRINT})
    failure = await _refused(world, 403, image="partner-private-gold", image_server=None, image_protocol=None)
    assert failure.detail["error"] == "image_not_allowed"
    assert world.service.preflights == []


async def test_a_tenant_cannot_use_a_fingerprint_none_of_its_instances_runs(world, monkeypatch):
    world.grant("view", "operate", "rebuild")
    _allow_fingerprints(monkeypatch, {FINGERPRINT})
    failure = await _refused(world, 403, image="cd" * 32, image_server=None, image_protocol=None)
    assert failure.detail["error"] == "image_not_allowed"


async def test_a_tenant_may_reinstall_from_an_image_its_instances_run(world, monkeypatch):
    world.grant("view", "operate", "rebuild")
    _allow_fingerprints(monkeypatch, {FINGERPRINT})
    job = await _rebuild(world, image=FINGERPRINT.upper(), image_server=None, image_protocol=None)
    assert job["status"] == "pending"


async def test_a_partner_may_still_name_an_image_by_alias(world):
    job = await _rebuild(world, image="ubuntu/24.04", image_server=None, image_protocol=None)
    assert job["status"] == "pending"


async def test_cloud_init_needs_the_console_grant_for_a_tenant(world):
    world.grant("view", "operate", "rebuild")
    failure = await _refused(world, 403, mode="cloud-init", cloud_init="#cloud-config\n")
    assert failure.detail["error"] == "console_required"
    world.grant("view", "operate", "rebuild", "console")
    job = await _rebuild(world, mode="cloud-init", cloud_init="#cloud-config\n")
    assert job["mode"] == "cloud-init"


async def test_a_tenant_reads_a_failed_job_without_the_clusters_own_text(world):
    world.db.add(RebuildJob(job_id="f1", cluster_id=HOST, project=PROJECT, instance_name=NAME, mode="image",
                            status="failed", error="LXD rebuild failed: 500 /var/snap/lxd/common/lxd/storage-pools/sp0 full"))
    await world.db.commit()
    world.grant("view", "operate", "rebuild")
    tenant_view = await routes.get_rebuild_job(NAME, "f1", db=world.db)
    assert tenant_view["error"] == routes.TENANT_FAILURE_TEXT and "/var/snap" not in json.dumps(tenant_view)
    org_scope._tenant_acl_var.set(None)
    operator_view = await routes.get_rebuild_job(NAME, "f1", db=world.db)
    assert "/var/snap" in operator_view["error"]


def test_a_tenants_cap_covers_the_projects_its_rules_name_within_the_bound(world):
    org_scope._tenant_acl_var.set({HOST: {(PROJECT, NAME): {"view"}, ("cust-b", "x"): {"view"}}})
    assert routes._tenant_projects(HOST, PROJECT) == {PROJECT}
    org_scope._tenant_acl_var.set({HOST: {("*", "*"): {"view"}}})
    assert routes._tenant_projects(HOST, PROJECT) == {PROJECT, "cust-a-2"}
    shared = org_scope._visible_projects_var.set({HOST: {PROJECT, "default"}})
    try:
        assert routes._tenant_projects(HOST, PROJECT) == {PROJECT}, "another holder's default never counts"
    finally:
        org_scope._visible_projects_var.reset(shared)
    bound = org_scope._visible_projects_var.set(None)
    try:
        assert routes._tenant_projects(HOST, PROJECT) is None
    finally:
        org_scope._visible_projects_var.reset(bound)


class _Projects:
    def __init__(self, config):
        self.config = config
        self.reads = 0


@pytest.mark.parametrize("bound,project,config,status", [
    ({HOST: {"cust-a", "default"}}, "cust-a", {}, 409),
    ({HOST: {"cust-a", "default"}}, "cust-a", {"features.images": "true"}, 409),
    ({HOST: {"cust-a", "default"}}, "default", {"restricted": "true"}, 409),
    ({HOST: {"cust-a", "default"}}, "cust-a", {"restricted": "true", "features.images": "true", "features.profiles": "true",
                                               "features.storage.volumes": "true", "features.networks": "true",
                                               "features.storage.buckets": "true"}, None),
    (None, "default", {}, None),
    (None, "cust-a", {}, None),
])
async def test_the_real_isolation_check_holds_a_tenant_on_a_shared_cluster(monkeypatch, bound, project, config, status):
    """A cluster bound to several organizations sets the project bound; only then must the project
    prove isolation, and `default` never may. A tenant's own cluster needs no config read."""
    from app.services import instance_security_policy

    reads = []

    async def project_config(lxd_service, host, wanted):
        reads.append(wanted)
        return config

    monkeypatch.setattr(routes, "_project_config", project_config)
    required = instance_security_policy.shared_project_findings(config) if bound and project != "default" else ()
    if status is None and required:
        pytest.skip(f"isolation needs more than this fixture names: {required}")
    token = org_scope._visible_projects_var.set(bound)
    try:
        call = routes._refuse_a_rebuild_in_an_unisolated_project(None, TENANT, HOST, project, None, tenant=True)
        if status is None:
            await call
        else:
            with pytest.raises(HTTPException) as caught:
                await call
            assert caught.value.status_code == status
            assert caught.value.detail["code"] in ("shared_cluster_project_not_isolated", "shared_default_project")
    finally:
        org_scope._visible_projects_var.reset(token)
    expected_reads = [project] if bound and project != "default" else []
    assert reads == expected_reads, "only a shared cluster's own project config is read"
