"""An in-memory LXD for project relocation runner tests.

It answers the executor, preflight, and profile calls the planner and the runners make, so a test
plans with the real planner and drives the real runners against the same state: projects with
their features, profiles per namespace, instances with local and expanded config, status,
location, snapshots, and cached images. Mutations can be refused (a 4xx) per action and name.
"""

from __future__ import annotations

import copy
import itertools
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator, Optional

import pytest

from app.models.migration_schemas import MigrationPlanCreateRequest
from app.services.migrations import (
    activation,
    executor,
    executor_project,
    preflight,
    preparation,
    project_cleanup,
    project_member_relocation,
    project_preparation,
    project_relocation,
    project_rollback,
    retention,
    rollback,
    source_fence,
    transfer,
)
from app.services.migrations.plan_contracts import _preflight_payload
from app.services.migrations.repository import RepositoryConflict
from app.services.migrations.project_policy import features_enabled
from app.services.migrations.security import canonical_json_hash
from app.services.migrations.target_contract import merge_profiles

_COUNTER = itertools.count(1)


class Refused(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.response = SimpleNamespace(status_code=400)


class FakeCluster:
    def __init__(self, name: str, *, networks: Optional[list[str]] = None, members: tuple[str, ...] = ("m1",)) -> None:
        self.name = name
        self.members = members
        self.networks = [
            {"name": network, "managed": True, "type": "bridge", "status": "Created"}
            for network in (networks or ["lxdbr0"])
        ]
        self.pools = ["default", "alt"]
        self.projects: dict[str, dict[str, Any]] = {"default": {"name": "default", "description": "", "config": {}}}
        self.profiles: dict[tuple[str, str], dict[str, Any]] = {
            ("default", "default"): {
                "name": "default",
                "description": "Default LXD profile",
                "config": {},
                "devices": {
                    "root": {"type": "disk", "path": "/", "pool": "default"},
                    "eth0": {"type": "nic", "network": "lxdbr0", "name": "eth0"},
                },
            }
        }
        self.instances: dict[tuple[str, str], dict[str, Any]] = {}
        self.images: dict[str, list[dict[str, Any]]] = {}
        self.refuse: set[tuple[str, str]] = set()
        # (action, name) whose operation is accepted, then fails, as LXD's start validation does.
        self.fail_async: set[tuple[str, str]] = set()
        self.calls: list[tuple[str, str]] = []
        self.client = SimpleNamespace(cluster=self, api=SimpleNamespace(networks=SimpleNamespace(cluster=self)))

    # namespaces ---------------------------------------------------------------------------
    def profile_project(self, project: str) -> str:
        record = self.projects.get(project)
        return project if record is not None and features_enabled(record, "profiles") else "default"

    def add_project(self, name: str, config: dict[str, Any], description: str = "") -> None:
        self.projects[name] = {"name": name, "description": description, "config": dict(config)}
        if features_enabled(self.projects[name], "profiles"):
            self.profiles[(name, "default")] = {"name": "default", "description": "Default LXD profile", "config": {}, "devices": {}}

    def add_instance(self, project: str, name: str, *, status: str = "Stopped", location: str = "m1", profiles: tuple[str, ...] = ("default",), config: Optional[dict[str, Any]] = None, snapshots: tuple[str, ...] = ("snap0",)) -> None:
        number = next(_COUNTER)
        self.instances[(project, name)] = {
            "name": name,
            "type": "container",
            "architecture": "x86_64",
            "profiles": list(profiles),
            "status": status,
            "location": location,
            "created_at": f"2026-10-16T00:00:{number:02d}Z",
            "config": {
                "volatile.uuid": f"uuid-{self.name}-{name}-{number}",
                "volatile.eth0.hwaddr": f"00:16:3e:00:00:{number % 256:02x}",
                **(config or {}),
            },
            "devices": {},
            "snapshots": list(snapshots),
        }

    def expanded(self, project: str, record: dict[str, Any]) -> dict[str, Any]:
        namespace = self.profile_project(project)
        profile_config, profile_devices = merge_profiles(
            [self.profiles.get((namespace, name)) for name in record["profiles"]]
        )
        result = copy.deepcopy(record)
        result.pop("snapshots", None)
        result["expanded_config"] = {**profile_config, **record["config"]}
        result["expanded_devices"] = {
            name: device
            for name, device in {**profile_devices, **copy.deepcopy(record["devices"])}.items()
            if device.get("type") != "none"
        }
        return result

    def check(self, action: str, name: str) -> None:
        self.calls.append((action, name))
        if (action, name) in self.refuse:
            raise Refused(f"{action} of {name} refused")


def _cluster(client: Any) -> FakeCluster:
    return client.cluster


def _op() -> str:
    return f"op-{next(_COUNTER)}"


# operation id -> the daemon's error, for operations that settle as failed.
_FAILED_OPS: dict[str, str] = {}


# executor -----------------------------------------------------------------------------------
def read_instance(client: Any, *, project: str, name: str) -> Optional[dict[str, Any]]:
    record = _cluster(client).instances.get((project, name))
    return _cluster(client).expanded(project, record) if record else None


def read_project(client: Any, *, project: str) -> Optional[dict[str, Any]]:
    record = _cluster(client).projects.get(project)
    return copy.deepcopy(record) if record else None


def submit_create_project(source_client: Any, target_client: Any, *, source_project: str, target_project: str, job_id: Optional[str] = None) -> None:
    payload = executor._reviewed_project_payload(read_project(source_client, project=source_project) or {}, target_project, job_id=job_id)
    target = _cluster(target_client)
    target.check("create_project", target_project)
    target.add_project(target_project, payload["config"], payload["description"])
    return None


def delete_project(client: Any, *, project: str) -> None:
    cluster = _cluster(client)
    cluster.check("delete_project", project)
    if any(key[0] == project for key in cluster.instances) or any(
        key[0] == project and key[1] != "default" for key in cluster.profiles
    ) or cluster.images.get(project):
        raise RuntimeError(f"Project '{project}' is not empty")
    cluster.projects.pop(project, None)
    cluster.profiles.pop((project, "default"), None)


def snapshot_inventory_fingerprint(client: Any, *, project: str, name: str) -> str:
    record = _cluster(client).instances[(project, name)]
    return canonical_json_hash(sorted(record["snapshots"]))


def _create_from(source: FakeCluster, source_project: str, source_name: str, target: FakeCluster, target_project: str, target_name: str, definition: dict[str, Any], member: Optional[str]) -> None:
    origin = source.instances[(source_project, source_name)]
    number = next(_COUNTER)
    config = dict(definition["config"])
    config.setdefault("volatile.uuid", f"uuid-{target.name}-{target_name}-{number}")
    target.instances[(target_project, target_name)] = {
        "name": target_name,
        "type": definition.get("type") or "container",
        "architecture": definition.get("architecture"),
        "profiles": list(definition["profiles"]),
        "status": "Stopped",
        "location": member or target.members[0],
        "created_at": f"2026-10-16T01:00:{number:02d}Z",
        "config": config,
        "devices": copy.deepcopy(definition["devices"]),
        "snapshots": list(origin["snapshots"]),
    }


def submit_copy_instance(source_client: Any, target_client: Any, *, source_cluster: str, source_project: str, source_name: str, target_cluster: str, target_project: str, target_name: str, target_member: Optional[str], target_pool: Optional[str], definition: dict[str, Any]) -> tuple[str, str, Any]:
    cluster = _cluster(source_client)
    cluster.check("copy", source_name)
    _create_from(cluster, source_project, source_name, cluster, target_project, target_name, definition, target_member)
    return "copy", _op(), source_client


def prepare_cross_cluster_source(source_client: Any, *, source_project: str, source_name: str) -> tuple[dict[str, Any], str]:
    _cluster(source_client).check("migrate_source", source_name)
    return {"cluster": _cluster(source_client), "project": source_project, "name": source_name}, _op()


def submit_cross_cluster_target(target_client: Any, *, target_project: str, target_name: str, target_member: Optional[str], target_pool: Optional[str], handoff: dict[str, Any], definition: dict[str, Any]) -> str:
    target = _cluster(target_client)
    target.check("pull", handoff["name"])
    _create_from(handoff["cluster"], handoff["project"], handoff["name"], target, target_project, target_name, definition, target_member)
    return _op()


def _nic_networks(cluster: FakeCluster, project: str, record: dict[str, Any]) -> set[str]:
    devices = cluster.expanded(project, record)["expanded_devices"]
    return {str(device.get("network")) for device in devices.values() if device.get("type") == "nic"}


def submit_instance_state(client: Any, *, project: str, name: str, action: str, timeout: int = 60, force: bool = False, action_timeout: Optional[int] = None) -> str:
    cluster = _cluster(client)
    cluster.check(action, name)
    record = cluster.instances[(project, name)]
    if (action, name) in cluster.fail_async:
        op = _op()
        _FAILED_OPS[op] = f'Failed start validation for device "eth0": MAC address already defined on another NIC ({name})'
        return op
    if action == "start":
        # LXD's checkAddressConflict: another instance on the same member and network with the
        # same name blocks a start, stopped or in another project.
        ours = _nic_networks(cluster, project, record)
        for (other_project, other_name), other in cluster.instances.items():
            if (other_project, other_name) == (project, name) or other_name != name:
                continue
            if other["location"] == record["location"] and ours & _nic_networks(cluster, other_project, other):
                raise Refused(f'Instance DNS name "{name}" already used on network')
    record["status"] = "Running" if action == "start" else "Stopped"
    return _op()


def replace_instance_devices(client: Any, *, project: str, name: str, devices: dict[str, Any], expected_devices: dict[str, Any], config_updates: Optional[dict[str, str]] = None) -> None:
    cluster = _cluster(client)
    cluster.check("replace_devices", name)
    record = cluster.instances[(project, name)]
    if record["devices"] != expected_devices:
        raise RuntimeError("devices changed since the read")
    had = {n for n, d in cluster.expanded(project, record)["expanded_devices"].items() if d.get("type") == "nic"}
    record["devices"] = copy.deepcopy(devices)
    nics = {n for n, d in cluster.expanded(project, record)["expanded_devices"].items() if d.get("type") == "nic"}
    # Like LXD: a MAC for a NIC added in this same write is ignored and a new one generated.
    record["config"].update(
        {key: value for key, value in (config_updates or {}).items() if key.split(".")[1] in had or not key.endswith(".hwaddr")}
    )
    # Like LXD: a device that is gone takes its generated MAC with it; a new one gets a new MAC.
    for key in [k for k in record["config"] if k.startswith("volatile.") and k.endswith(".hwaddr")]:
        if key.split(".")[1] not in nics:
            record["config"].pop(key)
    for nic in nics:
        record["config"].setdefault(f"volatile.{nic}.hwaddr", f"00:16:3e:{next(_COUNTER):06x}"[:17])
    return None


def submit_delete_instance(client: Any, *, project: str, name: str) -> str:
    cluster = _cluster(client)
    cluster.check("delete", name)
    cluster.instances.pop((project, name), None)
    return _op()


def patch_instance_config(client: Any, *, project: str, name: str, config: dict[str, str]) -> None:
    record = _cluster(client).instances[(project, name)]
    for key, value in config.items():
        if value == "":
            record["config"].pop(key, None)
        else:
            record["config"][key] = value
    return None


def submit_member_relocation(client: Any, *, project: str, name: str, target_member: str) -> str:
    cluster = _cluster(client)
    cluster.check("relocate", name)
    record = cluster.instances[(project, name)]
    if record["status"] != "Stopped":
        raise Refused("instance must be stopped")
    record["location"] = target_member
    return _op()


# executor_project ---------------------------------------------------------------------------
def read_profile(client: Any, *, project: str, name: str) -> Optional[dict[str, Any]]:
    # Like LXD: a project without its own profiles is answered from the default project's.
    cluster = _cluster(client)
    record = cluster.profiles.get((cluster.profile_project(project), name))
    return copy.deepcopy(record) if record else None


def list_profiles(client: Any, *, project: str) -> list[dict[str, Any]]:
    cluster = _cluster(client)
    namespace = cluster.profile_project(project)
    return [copy.deepcopy(row) for (owner, _name), row in cluster.profiles.items() if owner == namespace]


def create_profile(client: Any, *, project: str, record: dict[str, Any]) -> None:
    cluster = _cluster(client)
    cluster.check("create_profile", record["name"])
    cluster.profiles[(cluster.profile_project(project), record["name"])] = copy.deepcopy(record)


def replace_profile(client: Any, *, project: str, record: dict[str, Any]) -> None:
    cluster = _cluster(client)
    cluster.check("replace_profile", record["name"])
    cluster.profiles[(cluster.profile_project(project), record["name"])] = copy.deepcopy(record)


def delete_profile(client: Any, *, project: str, name: str) -> None:
    cluster = _cluster(client)
    cluster.check("delete_profile", name)
    cluster.profiles.pop((cluster.profile_project(project), name), None)


def list_images(client: Any, *, project: str) -> list[dict[str, Any]]:
    return copy.deepcopy(_cluster(client).images.get(project, []))


def submit_delete_image(client: Any, *, project: str, fingerprint: str) -> None:
    cluster = _cluster(client)
    cluster.images[project] = [row for row in cluster.images.get(project, []) if row["fingerprint"] != fingerprint]
    return None


# preflight readers --------------------------------------------------------------------------
def known_project_resources(client: Any, project: str) -> dict[str, list[Any]]:
    cluster = _cluster(client)
    empty = ("custom_volumes", "storage_buckets", "network_acls", "network_zones", "network_address_sets", "placement_groups", "network_forwards", "network_load_balancers")
    # Like LXD: a project without its own networks lists the default project's, host
    # interfaces included.
    record = cluster.projects.get(project)
    networks = [] if features_enabled(record, "networks") else [
        *copy.deepcopy(cluster.networks),
        {"name": "enp5s0", "managed": False, "type": "physical"},
    ]
    return {
        "instances": [cluster.expanded(project, row) for (owner, _n), row in cluster.instances.items() if owner == project],
        "profiles": list_profiles(client, project=project),
        "images": list_images(client, project=project),
        "networks": networks,
        **{kind: [] for kind in empty},
    }


def install(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch every daemon, store, and context seam. Returns the recorded job updates."""
    updates: list[dict[str, Any]] = []

    async def blocking(fn: Any, *args: Any, label: str, timeout: Any = None, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    async def settled(*args: Any, **kwargs: Any) -> dict[str, Any]:
        operation_id = kwargs.get("operation_id", args[1] if len(args) > 1 else None)
        if operation_id in _FAILED_OPS:
            return {"settled": True, "failed": True, "err": _FAILED_OPS[operation_id]}
        return {"settled": True, "failed": False}

    async def lease() -> None:
        return None

    modules = (activation, preparation, project_cleanup, project_member_relocation, project_preparation, project_relocation, project_rollback, retention, rollback, source_fence, transfer)
    for module in modules:
        for name, value in (("_blocking", blocking), ("await_operation", settled), ("_assert_mutation_lease", lease)):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, value)
        if hasattr(module, "active_instance_operations"):
            monkeypatch.setattr(module, "active_instance_operations", lambda *_a, **_k: set())
    for name in ("read_instance", "read_project", "submit_create_project", "delete_project", "snapshot_inventory_fingerprint", "submit_copy_instance", "prepare_cross_cluster_source", "submit_cross_cluster_target", "submit_instance_state", "submit_delete_instance", "patch_instance_config", "submit_member_relocation", "replace_instance_devices"):
        monkeypatch.setattr(executor, name, globals()[name])
    for name in ("read_profile", "list_profiles", "create_profile", "replace_profile", "delete_profile", "list_images", "submit_delete_image"):
        monkeypatch.setattr(executor_project, name, globals()[name])

    def get_project(client: Any, project: str) -> Optional[dict[str, Any]]:
        return read_project(client, project=project)

    monkeypatch.setattr(preflight, "_get_project", get_project)
    monkeypatch.setattr(preflight, "_known_project_resources", known_project_resources)
    monkeypatch.setattr(preflight, "_instance", lambda client, project, name: read_instance(client, project=project, name=name))
    monkeypatch.setattr(preflight, "_profile", lambda client, project, name: read_profile(client, project=project, name=name))
    monkeypatch.setattr(preflight, "_get_collection", lambda node, **_k: copy.deepcopy(node.cluster.networks))
    monkeypatch.setattr(preflight, "_storage_pools", lambda client: list(_cluster(client).pools))
    monkeypatch.setattr(preflight, "_instance_transfer_bytes", lambda *_a: 100)
    monkeypatch.setattr(preflight, "_check_active_operations", lambda *_a, **_k: None)
    monkeypatch.setattr(preflight, "_member_relocation_checks", lambda *_a, **_k: None)
    monkeypatch.setattr(preflight.mobility, "pool_headroom", lambda *_a: {"total": 10**9, "used": 0, "free": 10**9})
    monkeypatch.setattr(preflight, "_member_pool_headroom", lambda *_a: {"total": 10**9, "used": 0, "free": 10**9})
    monkeypatch.setattr(preflight, "daemon_identity", lambda client: {"engine": "lxd", "version": "5.21.8", "extensions": ["cluster_internal_copy", "container_copy_project"], "architectures": ["x86_64"]})
    monkeypatch.setattr(preflight, "approved_build", lambda *_a: True)
    monkeypatch.setattr(preflight, "api_endpoint", lambda client: f"https://{_cluster(client).name}.test:8443")
    return updates


def plan(
    source: FakeCluster,
    target: FakeCluster,
    *,
    source_project: str,
    target_project: str,
    member: Optional[str] = None,
    mappings: Optional[dict[str, Any]] = None,
) -> tuple[SimpleNamespace, list[SimpleNamespace], MigrationPlanCreateRequest]:
    """Plan with the real planner and build the job and its resource journal from it."""
    request = MigrationPlanCreateRequest.model_validate(
        {
            "scope": "project",
            "intent": "move",
            "source": {"cluster_id": source.name, "project": source_project},
            "target": {"cluster_id": target.name, "project": target_project, "member": member},
            "mappings": mappings or {},
        }
    )
    result = preflight.inspect(
        scope="project",
        intent="move",
        source_client=source.client,
        target_client=target.client,
        source_cluster=source.name,
        source_project=source_project,
        source_name=None,
        target_cluster=target.name,
        target_project=target_project,
        target_name=None,
        target_member=member,
        target_pool=None,
        settings=SimpleNamespace(migration_running_source_enabled=True, migration_project_max_instances=25, migration_auto_cleanup_enabled=True),
        mappings=request.mappings.model_dump(mode="json"),
    )
    assert result.blockers == [], result.blockers
    payload = _preflight_payload(result, request)
    job = SimpleNamespace(
        job_id=f"job-{next(_COUNTER)}",
        scope="project",
        intent="move",
        strategy=payload["strategy"],
        phase="queued",
        status="running",
        fence_token=1,
        retention_hours=24,
        safe_result={"reason": "test", "acknowledgements": []},
        source_cluster_id=source.name,
        target_cluster_id=target.name,
        source_project=source_project,
        target_project=target_project,
        source_resource_name=None,
        target_resource_name=None,
        source_member=None,
        target_member=member,
        source_cluster_fingerprint="fp-source",
        target_cluster_fingerprint="fp-target",
        source_organization_id=1,
        requester_role="admin",
        actor_issuer="issuer",
        actor_subject="subject",
        actor_email="ops@example.test",
        actor_provider_uuid="uuid",
        external_cutover_required=False,
    )
    resources = [
        SimpleNamespace(
            resource_id=f"res-{ordinal}",
            resource_type=row["resource_type"],
            ordinal=ordinal,
            state="pending",
            source_ref=row["source"],
            target_ref=row["target"],
            safe_result={},
            operation_refs=[],
        )
        for ordinal, row in enumerate(payload["resources"])
    ]
    return job, resources, request


def wire(monkeypatch: pytest.MonkeyPatch, job: SimpleNamespace, resources: list[SimpleNamespace], request: MigrationPlanCreateRequest) -> dict[str, list[Any]]:
    """Point the context and store seams at this job. Returns what each store call received."""
    calls: dict[str, list[Any]] = {"updates": [], "retained": [], "finished": [], "refused": [], "cleaned": [], "fence_conflict": []}
    by_id = {resource.resource_id: resource for resource in resources}

    async def resource_update(resource_id: str, **values: Any) -> None:
        resource = by_id[resource_id]
        if "state" in values:
            resource.state = values["state"]
        if "safe_result" in values:
            resource.safe_result = copy.deepcopy(values["safe_result"])
        if "operation_ref" in values:
            resource.operation_refs = [*resource.operation_refs, values["operation_ref"]]

    async def list_resources(_job_id: str) -> list[SimpleNamespace]:
        return list(resources)

    async def job_update(job_: Any, _worker_id: str, **values: Any) -> bool:
        calls["updates"].append(values)
        for key in ("phase", "status", "safe_result"):
            if key in values:
                setattr(job_, key, values[key])
        return True

    async def job_plan(_job: Any) -> Any:
        return SimpleNamespace(request_payload=request.model_dump(mode="json"), preflight_payload={}, checkpoint_requested=False)

    async def fresh(*_args: Any) -> dict[str, Any]:
        return {"blockers": [], "resources": []}

    async def admit(*_args: Any) -> None:
        return None

    @asynccontextmanager
    async def session() -> AsyncIterator[Any]:
        yield SimpleNamespace(commit=_noop)

    async def retained(_db: Any, job_: Any, **values: Any) -> dict[str, int]:
        calls["retained"].append(values)
        job_.phase, job_.status = "source_retained", "waiting"
        return {}

    async def finished(_db: Any, job_: Any, **values: Any) -> dict[str, int]:
        calls["finished"].append(values)
        job_.phase, job_.status = values["phase"], values["status"]
        return {}

    async def refused(_db: Any, job_: Any, **values: Any) -> None:
        calls["refused"].append(values)
        job_.phase, job_.status = "cleanup_blocked", "waiting"

    async def cleaned(_db: Any, job_: Any, **values: Any) -> None:
        calls["cleaned"].append(values)
        job_.phase, job_.status = "completed", "success"

    async def fence(*_args: Any, **_kwargs: Any) -> None:
        if calls.get("fence_conflict"):
            raise RepositoryConflict(str(calls["fence_conflict"][0]))
        return None

    @asynccontextmanager
    async def serialized(*_args: Any, **_kwargs: Any) -> AsyncIterator[None]:
        yield

    modules = (activation, preparation, project_cleanup, project_member_relocation, project_preparation, project_relocation, project_rollback, retention, rollback, source_fence, transfer)
    for module in modules:
        for name, value in (
            ("_resource_update", resource_update),
            ("_resources", list_resources),
            ("_fenced_job_update", job_update),
            ("_job_plan", job_plan),
            ("_fresh_worker_preflight", fresh),
            ("_admit_worker_run", admit),
            ("async_session_maker", session),
            ("enter_project_retention", retained),
            ("finish_project_rollback", finished),
            ("return_to_retention", refused),
            ("finish_project_cleanup", cleaned),
            ("fence_project_for_recovery", fence),
            ("serialize_cluster_configuration", serialized),
        ):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, value)
    return calls


async def _noop() -> None:
    return None
