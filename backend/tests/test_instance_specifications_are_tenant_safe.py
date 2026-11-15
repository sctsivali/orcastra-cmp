"""Instance specifications expose allocations, never another tenant's inventory."""

import asyncio
import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest
from fastapi import HTTPException
from requests import exceptions as requests_exceptions
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core import org_scope
from app.core.blocking import BlockingTimeout
from app.core.auth import AuthenticatedUser
from app.models.schemas import (
    GPUCardInfo,
    NodeResources,
    NodesStatusResponse,
    NodeStatus,
    VMInfo,
    VMStatus,
)
from app.routers import instances, nodes, vms
from app.services.lxd import lxd_service as lxd_service_module
from app.core.rate_sampler import LocalSampleStore
from app.services.lxd import instance_metrics as metrics_module
from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.instance import InstanceManager
from app.services.lxd.utils import find_instance_in_projects


HOST = "shared-cluster"


def _vm(name: str, project: str, **overrides) -> VMInfo:
    values = {
        "name": name,
        "status": VMStatus.RUNNING,
        "status_code": 103,
        "host": HOST,
        "host_endpoint": "https://control-plane.invalid:8443",
        "project": project,
        "type": "virtual-machine",
        "architecture": "x86_64",
        "config": {},
        "devices": {},
        "expanded_devices": {},
        "effective_limits_known": True,
        "expanded_devices_known": True,
    }
    values.update(overrides)
    return VMInfo(**values)


def test_the_projection_names_explicit_inherited_default_and_gpu_allocations():
    vm = _vm(
        "owned",
        "acme",
        config={"limits.cpu": "2"},
        devices={"root": {"type": "disk", "path": "/"}},
        expanded_devices={
            "root": {"type": "disk", "path": "/", "pool": "fast", "size": "40GiB"},
            "accelerator": {"type": "gpu", "id": "0"},
        },
        effective_cpu_limit="2",
        effective_memory_limit="4GiB",
    )

    result = instances._instance_specification(vm)

    assert result.cpu.model_dump() == {"value": "2", "source": "explicit"}
    assert result.memory.model_dump() == {"value": "4GiB", "source": "inherited"}
    assert result.root_disk.model_dump() == {
        "value": "40GiB",
        "pool": "fast",
        "source": "inherited",
    }
    assert result.gpu_count == 1
    assert result.gpu_devices == ["accelerator"]


def test_vm_defaults_and_an_unsized_root_are_reported_without_inventing_host_capacity():
    result = instances._instance_specification(
        _vm(
            "daemon-defaults",
            "acme",
            expanded_devices={"root": {"type": "disk", "path": "/", "pool": "default"}},
        )
    )

    assert result.cpu.model_dump() == {"value": "1", "source": "default"}
    assert result.memory.model_dump() == {"value": "1GiB", "source": "default"}
    assert result.root_disk.source == "pool_default"
    assert result.root_disk.pool == "default"


def test_absent_container_limits_are_unlimited():
    result = instances._instance_specification(
        _vm("unlimited", "acme", type="container")
    )

    assert result.cpu.model_dump() == {"value": None, "source": "unlimited"}
    assert result.memory.model_dump() == {"value": None, "source": "unlimited"}


def test_vm_state_disk_size_is_not_reported_as_root_allocation():
    result = instances._instance_specification(
        _vm(
            "stateful",
            "acme",
            devices={
                "root": {"type": "disk", "path": "/", "size.state": "8GiB"}
            },
            expanded_devices={
                "root": {
                    "type": "disk",
                    "path": "/",
                    "pool": "default",
                    "size.state": "8GiB",
                }
            },
        )
    )

    assert result.root_disk.value is None
    assert result.root_disk.source == "pool_default"
    assert result.root_disk.pool == "default"


def test_a_data_disk_named_root_cannot_shadow_the_actual_root_device():
    result = instances._instance_specification(
        _vm(
            "two-disks",
            "acme",
            devices={
                "root": {
                    "type": "disk",
                    "path": "/data",
                    "pool": "bulk",
                    "size": "2TiB",
                },
                "os": {
                    "type": "disk",
                    "path": "/",
                    "pool": "fast",
                    "size": "40GiB",
                },
            },
            expanded_devices={
                "root": {
                    "type": "disk",
                    "path": "/data",
                    "pool": "bulk",
                    "size": "2TiB",
                },
                "os": {
                    "type": "disk",
                    "path": "/",
                    "pool": "fast",
                    "size": "40GiB",
                },
            },
        )
    )

    assert result.root_disk.model_dump() == {
        "value": "40GiB",
        "pool": "fast",
        "source": "explicit",
    }


class _Cache:
    def __init__(self):
        self.values = {}
        self.writes = []
        self.generation = 0

    def get(self, namespace, key):
        return self.values.get((namespace, key))

    def set(self, namespace, key, value, ttl):
        self.values[(namespace, key)] = value
        self.writes.append((namespace, key, ttl))

    def instance_list_generation(self):
        return self.generation


class _Fleet:
    def __init__(self, rows, incomplete=None):
        self.rows = rows
        self.incomplete = incomplete or []

    async def list_all_vms_with_failures(self):
        return self.rows, [HOST], self.incomplete

    def get_node_names(self):
        raise AssertionError("specifications repeated the synchronous registry read")


def test_specifications_apply_partner_and_tenant_bounds_before_serializing(monkeypatch):
    cache = _Cache()
    monkeypatch.setattr(instances, "get_cache_manager", lambda: cache)
    org_scope.set_allowed_clusters({HOST, "offline-cluster"})
    visible_token = org_scope._visible_projects_var.set({HOST: {"acme"}})
    acl_token = org_scope._tenant_acl_var.set(
        {HOST: {("acme", "owned"): {"view"}, ("acme", "not-viewable"): {"operate"}}}
    )
    try:
        result = asyncio.run(
            instances.list_instance_specifications(
                lxd_service=_Fleet(
                    [
                        _vm("owned", "acme"),
                        _vm("not-viewable", "acme"),
                        _vm("foreign", "other-org"),
                    ],
                    incomplete=["offline-cluster"],
                ),
                fresh=True,
            )
        )
    finally:
        org_scope._tenant_acl_var.reset(acl_token)
        org_scope._visible_projects_var.reset(visible_token)
        org_scope.set_allowed_clusters(None)

    assert [item.name for item in result.instances] == ["owned"]
    assert result.unreachable_nodes == ["offline-cluster"]
    assert result.collected_at.tzinfo == timezone.utc
    assert cache.writes and cache.writes[0][2] == 30
    # Scope material is hashed; raw project or instance names never become Redis keys.
    assert "owned" not in cache.writes[0][1]
    assert "acme" not in cache.writes[0][1]


def test_specifications_name_a_cluster_with_an_incomplete_project_read(monkeypatch):
    monkeypatch.setattr(instances, "get_cache_manager", lambda: _Cache())
    org_scope.set_allowed_clusters({HOST})
    try:
        result = asyncio.run(
            instances.list_instance_specifications(
                lxd_service=_Fleet([_vm("visible", "default")], incomplete=[HOST]),
                fresh=True,
            )
        )
    finally:
        org_scope.set_allowed_clusters(None)

    assert [item.name for item in result.instances] == ["visible"]
    assert HOST in result.unreachable_nodes


def test_specifications_do_not_recache_a_snapshot_that_raced_an_invalidation(monkeypatch):
    cache = _Cache()

    class _ChangingFleet(_Fleet):
        async def list_all_vms_with_failures(self):
            result = await super().list_all_vms_with_failures()
            cache.generation += 1
            return result

    monkeypatch.setattr(instances, "get_cache_manager", lambda: cache)
    org_scope.set_allowed_clusters({HOST})
    try:
        result = asyncio.run(
            instances.list_instance_specifications(
                lxd_service=_ChangingFleet([_vm("visible", "default")]),
                fresh=True,
            )
        )
    finally:
        org_scope.set_allowed_clusters(None)

    assert [item.name for item in result.instances] == ["visible"]
    assert cache.writes == []


def test_tenant_acl_query_revokes_inactive_unbound_and_cross_org_grants():
    rule = SimpleNamespace(
        cluster_id=HOST,
        project_name="owned",
        instance_name="mine",
        permissions=["view"],
    )

    class _Result:
        def __init__(self, rows=None, scalar_rows=None):
            self._rows = rows or []
            self._scalar_rows = scalar_rows or []

        def scalars(self):
            return _Result(rows=self._scalar_rows)

        def unique(self):
            return self

        def all(self):
            return self._rows

    class _DB:
        statements = None

        async def execute(self, statement):
            if self.statements is None:
                self.statements = []
            self.statements.append(statement)
            if len(self.statements) == 1:
                return _Result(rows=[(rule, 7, "tenant-policy", "local", None, None, "local", "partner@example.test", None)])
            return _Result(scalar_rows=[])

    db = _DB()
    user = AuthenticatedUser(
        user_id="tenant-1",
        email="tenant@example.test",
        groups=["role_tenant"],
        roles=["role_tenant"],
    )

    acl = asyncio.run(org_scope.get_tenant_acl_rules(user, db))
    sql = " ".join(str(db.statements[0]).split())

    assert acl == {HOST: {("owned", "mine"): {"view"}}}
    assert (
        "JOIN organizations ON organizations.id = organization_members.organization_id "
        "AND organizations.id = tenant_access_policies.organization_id"
    ) in sql
    assert (
        "JOIN organization_clusters ON organization_clusters.organization_id = "
        "organizations.id AND organization_clusters.cluster_id = "
        "tenant_policy_rules.cluster_id"
    ) in sql
    assert "organizations.is_active = true" in sql
    assert "tenant_policy_assignments.expires_at IS NULL" in sql
    assert "tenant_access_policies.access_ends_at IS NULL" in sql, "a policy whose access ended grants nothing"


def test_tenant_acl_join_semantically_excludes_inactive_unbound_and_cross_org_rows():
    """Run the real ORM statement over local SQLite tables, without a live DB or new driver."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE organizations (id INTEGER PRIMARY KEY, is_active BOOLEAN NOT NULL, "
                "source TEXT NOT NULL DEFAULT 'local')"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE organization_members "
                "(id INTEGER PRIMARY KEY, organization_id INTEGER NOT NULL, "
                "user_email TEXT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE organization_clusters "
                "(id INTEGER PRIMARY KEY, organization_id INTEGER, cluster_id TEXT NOT NULL, "
                "registered_by TEXT, granted_by TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE tenant_access_policies "
                "(id INTEGER PRIMARY KEY, organization_id INTEGER NOT NULL, "
                "name TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'local', "
                "term_ended_at DATETIME, access_ends_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE tenant_policy_assignments "
                "(id INTEGER PRIMARY KEY, policy_id INTEGER NOT NULL, "
                "org_member_id INTEGER NOT NULL, expires_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE tenant_policy_rules "
                "(id INTEGER PRIMARY KEY, policy_id INTEGER NOT NULL, "
                "cluster_id TEXT NOT NULL, project_name TEXT NOT NULL, "
                "instance_name TEXT NOT NULL, permissions TEXT NOT NULL, "
                "created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text("INSERT INTO organizations (id, is_active) VALUES (:id, :active)"),
            [
                {"id": 1, "active": False},
                {"id": 2, "active": True},
                {"id": 3, "active": True},
                {"id": 4, "active": True},
            ],
        )
        connection.execute(
            text("INSERT INTO organization_members VALUES (:id, :org, :email)"),
            [
                {"id": 11, "org": 1, "email": "tenant@example.test"},
                {"id": 12, "org": 2, "email": "tenant@example.test"},
                {"id": 14, "org": 4, "email": "tenant@example.test"},
            ],
        )
        connection.execute(
            text("INSERT INTO organization_clusters (id, organization_id, cluster_id) VALUES (:id, :org, :cluster)"),
            [
                {"id": 21, "org": 1, "cluster": HOST},
                {"id": 22, "org": 2, "cluster": HOST},
                {"id": 23, "org": 3, "cluster": HOST},
            ],
        )
        connection.execute(
            text("INSERT INTO tenant_access_policies (id, organization_id, name) VALUES (:id, :org, :name)"),
            [
                {"id": 31, "org": 1, "name": "inactive"},
                {"id": 32, "org": 2, "name": "valid"},
                {"id": 33, "org": 3, "name": "cross-org"},
                {"id": 34, "org": 4, "name": "unbound"},
                {"id": 35, "org": 2, "name": "expired"},
            ],
        )
        connection.execute(
            text(
                "INSERT INTO tenant_policy_assignments "
                "VALUES (:id, :policy, :member, NULL)"
            ),
            [
                {"id": 41, "policy": 31, "member": 11},
                {"id": 42, "policy": 32, "member": 12},
                # Policy org 3 assigned to a member of org 4.
                {"id": 43, "policy": 33, "member": 14},
                {"id": 44, "policy": 34, "member": 14},
            ],
        )
        connection.execute(
            text(
                "INSERT INTO tenant_policy_assignments "
                "VALUES (45, 35, 12, '2020-01-01')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO tenant_policy_rules "
                "VALUES (:id, :policy, :cluster, :project, '*', '[\"view\"]', "
                "'2026-01-01')"
            ),
            [
                {"id": 51, "policy": 31, "cluster": HOST, "project": "inactive"},
                {"id": 52, "policy": 32, "cluster": HOST, "project": "valid"},
                {"id": 53, "policy": 33, "cluster": HOST, "project": "cross-org"},
                {"id": 54, "policy": 34, "cluster": "unbound", "project": "unbound"},
                {"id": 55, "policy": 35, "cluster": HOST, "project": "expired"},
            ],
        )

    session = Session(engine)

    class AsyncSessionAdapter:
        async def execute(self, statement):
            return session.execute(statement)

    user = AuthenticatedUser(
        user_id="tenant-1",
        email="tenant@example.test",
        groups=["role_tenant"],
        roles=["role_tenant"],
    )
    try:
        acl = asyncio.run(org_scope.get_tenant_acl_rules(user, AsyncSessionAdapter()))
    finally:
        session.close()
        engine.dispose()

    assert acl == {HOST: {("valid", "*"): {"view"}}}


def test_revoked_tenant_acl_gets_a_different_response_cache_scope():
    org_scope.set_allowed_clusters({HOST})
    token = org_scope._tenant_acl_var.set(
        {HOST: {("owned", "mine"): {"view"}}}
    )
    try:
        active_key = org_scope.get_scope_cache_key("instances:specifications")
    finally:
        org_scope._tenant_acl_var.reset(token)

    token = org_scope._tenant_acl_var.set({})
    try:
        revoked_key = org_scope.get_scope_cache_key("instances:specifications")
    finally:
        org_scope._tenant_acl_var.reset(token)
        org_scope.set_allowed_clusters(None)

    assert active_key != revoked_key


@pytest.mark.parametrize("handler,name_arg", [(instances.get_instance, "instance_name"), (vms.get_vm, "vm_name")])
def test_an_explicit_foreign_project_is_refused_before_lxd(handler, name_arg):
    class _NeverCalled:
        async def get_vm(self, *args, **kwargs):
            raise AssertionError("a refused project reached LXD")

    org_scope.set_allowed_clusters({HOST})
    token = org_scope._visible_projects_var.set({HOST: {"default"}})
    try:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                handler(
                    **{
                        name_arg: "same-name",
                        "host": HOST,
                        "project": "foreign",
                        "lxd_service": _NeverCalled(),
                    }
                )
            )
    finally:
        org_scope._visible_projects_var.reset(token)
        org_scope.set_allowed_clusters(None)

    assert exc.value.status_code == 403


def test_a_projectless_lookup_never_reads_a_filtered_project():
    calls = []

    class _Response:
        def json(self):
            return {"metadata": {"name": "same-name", "status": "Running"}}

    class _InstanceEndpoint:
        def get(self, *, params):
            calls.append(params["project"])
            return _Response()

    class _Instances:
        def __getitem__(self, name):
            assert name == "same-name"
            return _InstanceEndpoint()

    class _Projects:
        def all(self):
            return [type("Project", (), {"name": name})() for name in ("foreign", "owned")]

    client = type(
        "Client",
        (),
        {
            "projects": _Projects(),
            "api": type("API", (), {"instances": _Instances()})(),
        },
    )()

    instance, project = find_instance_in_projects(
        client,
        "same-name",
        project_filter=lambda candidate: candidate == "owned",
    )

    assert project == "owned"
    assert instance is not None
    assert calls == ["owned"]


@pytest.mark.parametrize("successful_projects", [{"default"}, set()])
def test_project_inventory_reports_partial_and_all_project_failures(
    monkeypatch,
    successful_projects,
):
    from app.services.lxd import health
    from app.services.lxd import instance as instance_module

    rows = [
        {
            "name": f"vm-{project}",
            "status": "Stopped",
            "status_code": 102,
            "project": project,
        }
        for project in successful_projects
    ]
    monkeypatch.setattr(
        instance_module,
        "list_across_projects",
        lambda *_args, **_kwargs: (rows, False),
    )
    client = SimpleNamespace()
    manager = InstanceManager.__new__(InstanceManager)
    manager.connection_manager = SimpleNamespace(get_client=lambda _node: client)
    manager.cert_manager = None
    monkeypatch.setattr(health, "is_tripped", lambda _node: False)

    rows, connected, complete = manager._fetch_vms_from_node_detailed(HOST, "unknown")

    assert connected is True
    assert complete is False
    assert {row.project for row in rows} == successful_projects


def test_a_socket_timeout_is_not_retyped_as_instance_not_found():
    class _Instances:
        def __getitem__(self, _name):
            return SimpleNamespace(
                get=lambda **_kwargs: (_ for _ in ()).throw(
                    requests_exceptions.ReadTimeout("read timed out")
                )
            )

    client = SimpleNamespace(
        projects=SimpleNamespace(
            all=lambda: [SimpleNamespace(name="default"), SimpleNamespace(name="other")]
        ),
        api=SimpleNamespace(instances=_Instances()),
    )

    with pytest.raises(requests_exceptions.ReadTimeout):
        find_instance_in_projects(client, "mine")


@pytest.mark.parametrize("handler,name_arg", [(instances.get_instance_metrics, "instance_name"), (vms.get_instance_metrics, "instance_name")])
def test_metrics_view_acl_is_not_disabled_by_the_operation_feature_flag(handler, name_arg):
    class _NeverCalled:
        async def get_instance_metrics(self, *args, **kwargs):
            raise AssertionError("a refused metrics read reached LXD")

    org_scope.set_allowed_clusters({HOST})
    token = org_scope._tenant_acl_var.set({HOST: {("default", "mine"): {"view"}}})
    try:
        with pytest.raises(HTTPException) as exc:
            kwargs = {
                name_arg: "not-mine",
                "host": HOST,
                "project": "default",
                "lxd_service": _NeverCalled(),
            }
            asyncio.run(handler(**kwargs))
    finally:
        org_scope._tenant_acl_var.reset(token)
        org_scope.set_allowed_clusters(None)

    assert exc.value.status_code == 403


def test_metrics_are_offloaded_from_the_event_loop(monkeypatch):
    service = lxd_service_module.LXDService.__new__(lxd_service_module.LXDService)
    call = {}

    def _sample(*args):
        call["worker_args"] = args
        return "sample"

    async def _run_blocking(fn, *args, **kwargs):
        call["options"] = kwargs
        return fn(*args)

    monkeypatch.setattr(service, "_get_instance_metrics_blocking", _sample)
    monkeypatch.setattr(lxd_service_module, "run_blocking", _run_blocking)

    result = asyncio.run(service.get_instance_metrics(HOST, "mine", "default"))

    assert result == "sample"
    assert call["worker_args"] == (HOST, "mine", "default")
    assert call["options"]["timeout"] == 12.0
    assert call["options"]["max_in_flight"] == 10
    # Its own pool, capped per cluster: the node-status sweep cannot starve it and one slow
    # cluster cannot hold every thread.
    assert call["options"]["executor"] is lxd_service_module._instance_metrics_executor
    assert call["options"]["executor"] is not lxd_service_module._blocking_executor
    assert call["options"]["concurrency_key"] == HOST
    assert call["options"]["max_in_flight_per_key"] == 3


@pytest.mark.parametrize("phase", ["lookup", "state"])
def test_a_metrics_socket_timeout_becomes_a_connection_failure(monkeypatch, phase):
    if phase == "lookup":
        lookup = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # noqa: E731
            requests_exceptions.ReadTimeout("lookup timed out")
        )
    else:
        timed_out_instance = SimpleNamespace(
            status="Running",
            status_code=103,
            state=lambda: (_ for _ in ()).throw(
                requests_exceptions.ReadTimeout("state timed out")
            ),
        )
        lookup = lambda *_args, **_kwargs: (  # noqa: E731
            timed_out_instance,
            "default",
        )
    monkeypatch.setattr(metrics_module, "find_instance_in_projects", lookup)

    with pytest.raises(LXDConnectionError):
        metrics_module.read_instance_metrics(object(), HOST, "mine", "default")


def test_concurrent_metrics_reads_share_one_bounded_baseline_store(monkeypatch):
    """Eight threads sampling at once must neither corrupt the store nor grow it unbounded."""

    class _State:
        cpu = {"usage": 1_000_000}
        memory = {}
        disk = {}
        network = {}
        processes = 0
        pid = None
        started_at = None

    class _Instance:
        status = "Running"
        status_code = 103
        type = "virtual-machine"
        config = {}
        expanded_config = None
        last_used_at = None
        location = "none"

        def state(self):
            return _State()

    bounded = LocalSampleStore(max_entries=4)
    monkeypatch.setattr(metrics_module.STATE_SAMPLER, "_local", bounded)
    monkeypatch.setattr(
        metrics_module,
        "find_instance_in_projects",
        lambda *_args, **_kwargs: (_Instance(), "default"),
    )
    barrier = threading.Barrier(8)

    def _sample(index):
        barrier.wait()
        return metrics_module.read_instance_metrics(
            object(),
            HOST,
            f"instance-{index}",
            "default",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        samples = list(executor.map(_sample, range(8)))

    assert [sample.cpu_sample_status for sample in samples] == ["calculating"] * 8
    assert len(bounded._entries) == 4


@pytest.mark.parametrize(
    "handler,name_arg",
    [
        (instances.get_instance_metrics, "instance_name"),
        (vms.get_instance_metrics, "instance_name"),
    ],
)
def test_a_bounded_metrics_timeout_is_reported_as_temporarily_unavailable(handler, name_arg):
    class _TimedOut:
        async def get_instance_metrics(self, *args, **kwargs):
            raise BlockingTimeout("instance metrics", 12.0)

    org_scope.set_allowed_clusters({HOST})
    try:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                handler(
                    **{
                        name_arg: "mine",
                        "host": HOST,
                        "project": "default",
                        "lxd_service": _TimedOut(),
                    }
                )
            )
    finally:
        org_scope.set_allowed_clusters(None)

    assert exc.value.status_code == 503
    assert exc.value.detail == "Instance metrics are temporarily unavailable"


@pytest.mark.parametrize(
    "handler,name_arg",
    [
        (instances.get_instance_metrics, "instance_name"),
        (vms.get_instance_metrics, "instance_name"),
    ],
)
def test_a_metrics_socket_failure_is_503_on_both_router_families(handler, name_arg):
    class _ConnectionFailed:
        async def get_instance_metrics(self, *args, **kwargs):
            raise LXDConnectionError("cluster did not answer")

    org_scope.set_allowed_clusters({HOST})
    try:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                handler(
                    **{
                        name_arg: "mine",
                        "host": HOST,
                        "project": "default",
                        "lxd_service": _ConnectionFailed(),
                    }
                )
            )
    finally:
        org_scope.set_allowed_clusters(None)

    assert exc.value.status_code == 503


def test_tenant_availability_is_a_copy_without_control_plane_metadata():
    from datetime import datetime

    from app.core.auth import AuthenticatedUser
    from app.models.cluster_status import AvailabilityReason, ClusterStatus
    from app.models.schemas import ClusterAvailability, ClusterAvailabilityResponse

    original = ClusterAvailabilityResponse(
        clusters=[
            ClusterAvailability(
                name=HOST,
                display_name="Shared",
                endpoint="https://secret-control-plane.invalid:8443",
                status=ClusterStatus.OFFLINE,
                reason_code=AvailabilityReason.UNREACHABLE,
                reason="This cluster did not answer.",
                node_type="microcloud",
                checked_at=datetime.now(timezone.utc),
            )
        ],
        online_count=0,
        total_count=1,
        source="probe",
        generated_at=datetime.now(timezone.utc),
    )
    tenant = AuthenticatedUser(
        user_id="tenant-1",
        email="tenant@example.test",
        groups=["role_tenant"],
        roles=["role_tenant"],
    )

    projected = nodes._availability_for_user(original, tenant)

    assert projected.clusters[0].endpoint is None
    assert projected.clusters[0].node_type is None
    assert projected.clusters[0].reason is None
    assert original.clusters[0].endpoint == "https://secret-control-plane.invalid:8443"
    assert original.clusters[0].node_type == "microcloud"
    assert original.clusters[0].reason == "This cluster did not answer."


def test_full_node_inventory_handlers_are_partner_gated():
    source = Path(__file__).resolve().parents[1] / "app" / "routers" / "nodes.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    expected = {"get_nodes_status", "get_node_names", "get_node_resources", "get_node_gpus"}
    guarded = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in expected:
            if "Depends(require_partner())" in ast.unparse(node.args):
                guarded.add(node.name)
    assert guarded == expected


def test_partner_gpu_inventory_redacts_unproven_instance_names(monkeypatch):
    card = GPUCardInfo(index=0, in_use=True, used_by="foreign-instance")

    class _Service:
        def get_node_gpus(self, node_name):
            return SimpleNamespace(cards=[card], gpu_cards_total=1, gpu_total=1)

    async def _inline(fn, *args):
        return fn(*args)

    monkeypatch.setattr(nodes.asyncio, "to_thread", _inline)

    org_scope.set_allowed_clusters({HOST})
    token = org_scope._visible_projects_var.set({HOST: {"default"}})
    try:
        result = asyncio.run(
            nodes.get_node_gpus(node_name=HOST, lxd_service=_Service(), _rbac=None)
        )
    finally:
        org_scope._visible_projects_var.reset(token)
        org_scope.set_allowed_clusters(None)

    assert result.gpus[0].used_by is None
    assert card.used_by == "foreign-instance", "redaction must not mutate a shared inventory"


def test_partner_gpu_owners_are_redacted_on_node_cold_cache_and_resource_paths(monkeypatch):
    from datetime import datetime

    card = GPUCardInfo(index=0, in_use=True, used_by="foreign-instance")
    resources = NodeResources(gpu_cards=[card], gpu_cards_total=1)
    status = NodeStatus(
        name=HOST,
        endpoint="https://control-plane.invalid:8443",
        connected=True,
        resources=resources,
    )
    raw_response = NodesStatusResponse(
        nodes=[status],
        all_connected=True,
        collected_at=datetime.now(timezone.utc),
    )

    class _Service:
        calls = 0

        async def check_nodes_status_async(self):
            self.calls += 1
            return [status]

        def get_node_resources_detail(self, _node_name):
            return resources

    async def _inline(fn, *args):
        return fn(*args)

    service = _Service()
    cache = _Cache()
    monkeypatch.setattr(nodes, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(nodes, "get_scope_cache_key", lambda _key: "nodes-key")
    monkeypatch.setattr(nodes.asyncio, "to_thread", _inline)

    org_scope.set_allowed_clusters({HOST})
    token = org_scope._visible_projects_var.set({HOST: {"default"}})
    try:
        cold = asyncio.run(
            nodes.get_nodes_status(lxd_service=service, fresh=True, _rbac=None)
        )

        cache.values[("api_responses", "nodes-key")] = raw_response.model_dump(
            mode="json"
        )
        cached = asyncio.run(
            nodes.get_nodes_status(lxd_service=service, fresh=False, _rbac=None)
        )
        detail = asyncio.run(
            nodes.get_node_resources(
                node_name=HOST,
                lxd_service=service,
                _rbac=None,
            )
        )
    finally:
        org_scope._visible_projects_var.reset(token)
        org_scope.set_allowed_clusters(None)

    assert cold.nodes[0].resources.gpu_cards[0].used_by is None
    assert cached.nodes[0].resources.gpu_cards[0].used_by is None
    assert detail.gpu_cards[0].used_by is None
    assert service.calls == 1
    assert card.used_by == "foreign-instance"
    assert status.resources.gpu_cards[0].used_by == "foreign-instance"
    assert (
        cache.values[("api_responses", "nodes-key")]["nodes"][0]["resources"][
            "gpu_cards"
        ][0]["used_by"]
        == "foreign-instance"
    )
