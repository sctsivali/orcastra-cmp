"""Shared clusters expose only project-contained profile and instance capabilities."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import instance_security_policy as isp
from fastapi import HTTPException

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.core.blocking import BlockingTimeout
from app.core.rbac import Role
from app.models.schemas import ProjectCreateRequest, ProjectUpdateRequest
from app.routers import instances as instances_module
from app.routers import profiles as profiles_module
from app.routers import projects as projects_module
from app.services.lxd import projects as lxd_projects_module
from app.services.lxd.projects import ProjectManager
from app.services.instance_security_policy import (
    ClusterOwnership,
    ClusterWritePolicy,
    InstanceSecurityPolicyViolation,
    prepare_project_create_config,
    resolve_cluster_write_policy,
    validate_instance_payload,
    validate_project_update,
)
from tests._profile_daemon import Daemon

HOST = "nuc-shared"
SAFE_SHARED_PROJECT_CONFIG = {
    "restricted": "true",
    "features.images": "true",
    "features.networks": "true",
    "features.profiles": "true",
    "features.storage.volumes": "true",
}


def _user(role: str, suffix: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=f"policy-{suffix}",
        email=f"{suffix}@example.test",
        groups=[f"role_{role}"],
        roles=[f"role_{role}"],
    )


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self.rows


class _Bindings:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.calls = 0

    async def execute(self, statement):
        self.calls += 1
        assert "organization_clusters" in str(statement)
        return _Rows(self.rows)


@pytest.fixture
def held_elsewhere(monkeypatch: pytest.MonkeyPatch) -> set:
    """Which clusters another party holds, as services.cluster_parties would answer (pinned on real
    rows in test_whole_cluster_state_needs_the_whole_cluster)."""
    held: set = set()

    async def _held(_db, _email, cluster_ids):
        return set(cluster_ids) & held

    async def _bought(_db, _email, _cluster_id):
        # Purchase-only reach is pinned on real rows in test_a_buyer_founder_never_holds_a_cluster_exclusively.
        return False

    monkeypatch.setattr(isp.cluster_parties, "clusters_held_by_another_party", _held)
    monkeypatch.setattr(isp.sale_term, "reaches_only_by_purchase", _bought)
    return held


@pytest.mark.asyncio
async def test_current_bindings_choose_exclusive_shared_and_ambiguous_policy(held_elsewhere) -> None:
    partner = _user("partner", "partner-bindings")

    exclusive = await resolve_cluster_write_policy(partner, HOST, _Bindings([(7, None)]))
    shared = await resolve_cluster_write_policy(partner, HOST, _Bindings([(7, None), (9, None)]))
    unbound = await resolve_cluster_write_policy(
        partner, HOST, _Bindings([(None, "partner-bindings@example.test")])
    )
    missing = await resolve_cluster_write_policy(partner, HOST, _Bindings([]))

    assert exclusive.ownership == ClusterOwnership.EXCLUSIVE
    assert exclusive.has_full_access
    assert shared.ownership == ClusterOwnership.SHARED
    assert shared.requires_safe_allowlist
    assert unbound.ownership == ClusterOwnership.AMBIGUOUS
    assert missing.ownership == ClusterOwnership.AMBIGUOUS


@pytest.mark.asyncio
async def test_one_binding_another_party_holds_is_shared_not_exclusive(held_elsewhere) -> None:
    """A partner reaches a cluster another party holds alone through a member row it bought."""
    held_elsewhere.add(HOST)
    policy = await resolve_cluster_write_policy(_user("partner", "partner-bindings"), HOST, _Bindings([(7, None)]))
    assert policy.ownership == ClusterOwnership.SHARED and policy.requires_safe_allowlist


@pytest.mark.asyncio
async def test_admin_does_not_need_an_ownership_query_and_tenant_has_no_write() -> None:
    database = _Bindings([])
    admin_policy = await resolve_cluster_write_policy(
        _user("admin", "admin"), HOST, database
    )
    tenant_policy = await resolve_cluster_write_policy(
        _user("tenant", "tenant"), HOST, database
    )

    assert admin_policy.has_full_access and database.calls == 0
    with pytest.raises(InstanceSecurityPolicyViolation) as caught:
        validate_instance_payload(tenant_policy, config={}, devices={})
    assert caught.value.code == "instance_write_not_allowed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "ownership", "mode", "can_edit", "can_use_advanced"),
    [
        (Role.ADMIN, ClusterOwnership.EXCLUSIVE, "full", True, True),
        (Role.PARTNER, ClusterOwnership.EXCLUSIVE, "full", True, True),
        (Role.PARTNER, ClusterOwnership.SHARED, "safe", True, False),
        (Role.PARTNER, ClusterOwnership.AMBIGUOUS, "safe", True, False),
        (Role.TENANT, ClusterOwnership.AMBIGUOUS, "view_only", False, False),
    ],
)
async def test_instance_write_policy_projection_is_authoritative_and_value_free(
    monkeypatch,
    role: Role,
    ownership: ClusterOwnership,
    mode: str,
    can_edit: bool,
    can_use_advanced: bool,
) -> None:
    events: list[str] = []

    def authorize(host: str) -> None:
        events.append(f"authorize:{host}")

    async def resolve(user, host, db):
        events.append(f"resolve:{host}")
        return ClusterWritePolicy(host, role, ownership)

    monkeypatch.setattr(instances_module, "verify_cluster_access", authorize)
    monkeypatch.setattr(
        instances_module,
        "resolve_cluster_write_policy",
        resolve,
    )

    response = await instances_module.get_instance_write_policy(
        host=HOST,
        user=_user(role.value, f"projection-{role.value}-{ownership.value}"),
        db=object(),
    )

    assert response.model_dump() == {
        "host": HOST,
        "mode": mode,
        "can_edit_specifications": can_edit,
        "can_use_advanced_create": can_use_advanced,
    }
    assert events == [f"authorize:{HOST}", f"resolve:{HOST}"]


@pytest.mark.asyncio
async def test_instance_write_policy_denies_cluster_before_ownership_query(monkeypatch) -> None:
    calls = 0

    def deny(_host: str) -> str:
        raise HTTPException(status_code=403, detail="Access denied")

    async def must_not_resolve(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("ownership must not be read after cluster denial")

    monkeypatch.setattr(instances_module, "verify_cluster_access", deny)
    monkeypatch.setattr(
        instances_module,
        "resolve_cluster_write_policy",
        must_not_resolve,
    )

    with pytest.raises(HTTPException) as caught:
        await instances_module.get_instance_write_policy(
            host=HOST,
            user=_user("partner", "projection-denied"),
            db=object(),
        )

    assert caught.value.status_code == 403
    assert calls == 0


def test_instance_write_policy_literal_precedes_the_instance_name_route() -> None:
    paths = [route.path for route in instances_module.router.routes]

    assert paths.index("/instances/write-policy") < paths.index(
        "/instances/{instance_name}"
    )


@pytest.mark.asyncio
async def test_instance_write_policy_uses_the_real_allowed_cluster_guard(monkeypatch) -> None:
    async def resolve(user, host, db):
        assert host == HOST
        return ClusterWritePolicy(host, Role.PARTNER, ClusterOwnership.EXCLUSIVE)

    monkeypatch.setattr(instances_module, "resolve_cluster_write_policy", resolve)
    scope_token = org_scope._allowed_clusters_var.set({HOST})
    try:
        response = await instances_module.get_instance_write_policy(
            host=HOST,
            user=_user("partner", "projection-real-scope"),
            db=object(),
        )
    finally:
        org_scope._allowed_clusters_var.reset(scope_token)

    assert response.host == HOST
    assert response.mode == "full"


def test_shared_partner_allows_typed_limits_managed_nic_and_project_disk() -> None:
    policy = ClusterWritePolicy(HOST, Role.PARTNER, ClusterOwnership.SHARED)

    validate_instance_payload(
        policy,
        config={
            "limits.cpu": "4",
            "limits.memory": "4GiB",
            "cloud-init.user-data": "#cloud-config",
            "user.owner": "application-team",
        },
        devices={
            "eth0": {"type": "nic", "network": "tenant-net", "ipv4.address": "10.1.2.3"},
            "root": {"type": "disk", "path": "/", "pool": "tenant-pool", "size": "20GiB"},
            "data": {
                "type": "disk",
                "path": "/srv/data",
                "pool": "tenant-pool",
                "source": "data-volume",
            },
        },
    )


def test_shared_partner_refusal_is_structured_and_never_echoes_values() -> None:
    policy = ClusterWritePolicy(HOST, Role.PARTNER, ClusterOwnership.SHARED)
    secret = "super-secret-host-command"

    with pytest.raises(InstanceSecurityPolicyViolation) as caught:
        validate_instance_payload(
            policy,
            config={"raw.lxc": secret, "security.privileged": "true"},
            devices={
                "host": {"type": "disk", "path": "/mnt", "source": "/etc"},
                "gpu0": {"type": "gpu"},
                "eth0": {"type": "nic", "nictype": "physical", "parent": "eno1"},
            },
        )

    detail = caught.value.as_detail()
    assert detail["code"] == "shared_cluster_policy_violation"
    codes = {row["code"] for row in detail["violations"]}
    assert {
        "raw_config_forbidden",
        "host_security_setting_forbidden",
        "host_path_disk_forbidden",
        "host_device_forbidden",
        "unmanaged_nic_forbidden",
    } <= codes
    assert secret not in str(detail)


@pytest.mark.parametrize("role", [Role.ADMIN, Role.PARTNER])
def test_admin_and_exclusive_partner_keep_the_existing_advanced_surface(role: Role) -> None:
    policy = ClusterWritePolicy(HOST, role, ClusterOwnership.EXCLUSIVE)
    validate_instance_payload(
        policy,
        config={"raw.lxc": "lxc.apparmor.profile=unconfined"},
        devices={"gpu0": {"type": "gpu"}},
    )


def test_effective_profile_payload_closes_the_profile_bypass() -> None:
    policy = ClusterWritePolicy(HOST, Role.PARTNER, ClusterOwnership.SHARED)

    with pytest.raises(InstanceSecurityPolicyViolation) as caught:
        validate_instance_payload(
            policy,
            config={"limits.cpu": "2"},
            devices={"gpu-from-profile": {"type": "gpu"}},
        )
    assert caught.value.findings[0].field == "devices.gpu-from-profile"


def test_shared_project_config_is_server_restricted_and_legacy_update_is_not_mutated() -> None:
    policy = ClusterWritePolicy(HOST, Role.PARTNER, ClusterOwnership.SHARED)

    prepared = prepare_project_create_config(
        policy,
        {"features.profiles": "true", "user.cost-center": "platform"},
    )
    assert prepared["restricted"] == "true"
    assert {
        prepared["features.images"],
        prepared["features.networks"],
        prepared["features.profiles"],
        prepared["features.storage.volumes"],
    } == {"true"}

    with pytest.raises(InstanceSecurityPolicyViolation) as disabled:
        prepare_project_create_config(policy, {"features.networks": "false"})
    assert "isolated_project_feature_required" in {
        finding.code for finding in disabled.value.findings
    }

    legacy = {"features.profiles": "true"}
    with pytest.raises(InstanceSecurityPolicyViolation) as caught:
        validate_project_update(policy, legacy)
    assert caught.value.status_code == 409
    assert "restricted" not in legacy, "validation must not silently rewrite an existing project"


def _profile_service(daemon: Daemon):
    class _Profile:
        def __init__(self, name: str) -> None:
            self.name = name

        def get(self, params=None, timeout=None):
            return daemon.get(self.name, params=params, timeout=timeout)

        def put(self, **kwargs):
            return daemon.put(self.name, **kwargs)

    class _Profiles:
        def post(self, **kwargs):
            return daemon.post(**kwargs)

        def __getitem__(self, name):
            return _Profile(name)

    class _Api:
        operations = daemon.ops

        def __getitem__(self, key):
            return {"profiles": _Profiles()}[key]

    client = SimpleNamespace(api=_Api())
    return SimpleNamespace(get_client_with_reconnect=lambda host: client)


def _wire_profile_route(monkeypatch, policy: ClusterWritePolicy) -> None:
    async def shared_policy(*args, **kwargs):
        return policy

    monkeypatch.setattr(profiles_module, "resolve_cluster_write_policy", shared_policy)
    # The sandbox used by this suite cannot start worker threads. Run the same synchronous
    # daemon fake inline; production still uses asyncio.to_thread.
    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    async def bounded_inline(function, *args, **kwargs):
        for option in (
            "timeout",
            "label",
            "executor",
            "max_in_flight",
            "concurrency_key",
            "max_in_flight_per_key",
        ):
            kwargs.pop(option, None)
        return function(*args, **kwargs)

    monkeypatch.setattr(profiles_module.asyncio, "to_thread", inline)
    monkeypatch.setattr(profiles_module, "run_blocking", bounded_inline)
    monkeypatch.setattr(profiles_module, "verify_cluster_access", lambda host: None)
    monkeypatch.setattr(profiles_module, "resolve_owner", lambda *args: "default")
    monkeypatch.setattr(
        profiles_module,
        "list_project_configs",
        lambda client: {"default": dict(SAFE_SHARED_PROJECT_CONFIG)},
    )
    monkeypatch.setattr(profiles_module, "project_is_visible", lambda *args: True)
    monkeypatch.setattr(profiles_module.activity_logger, "log_activity", lambda **kwargs: None)
    monkeypatch.setattr(
        profiles_module,
        "get_cache_manager",
        lambda: SimpleNamespace(invalidate_resource_cache=lambda *args: None),
    )


def _record_configuration_lock(monkeypatch, module, events: list[str], state: dict[str, bool]):
    @asynccontextmanager
    async def lock(db, host):
        assert not state["held"]
        state["held"] = True
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")
            state["held"] = False

    monkeypatch.setattr(module, "serialize_cluster_configuration", lock)


@pytest.mark.asyncio
async def test_profile_create_holds_lock_through_final_policy_and_daemon_submit(
    monkeypatch,
) -> None:
    policy = ClusterWritePolicy("nuc1", Role.PARTNER, ClusterOwnership.SHARED)
    daemon = Daemon()
    _wire_profile_route(monkeypatch, policy)
    events: list[str] = []
    state = {"held": False}
    _record_configuration_lock(monkeypatch, profiles_module, events, state)

    async def resolve_policy(*args, **kwargs):
        assert state["held"]
        events.append("policy")
        return policy

    def project_configs(client):
        assert state["held"]
        events.append("project-config")
        return {"default": dict(SAFE_SHARED_PROJECT_CONFIG)}

    original_isolation = profiles_module.validate_target_project_isolation

    def validate_isolation(*args, **kwargs):
        assert state["held"]
        events.append("project-isolation")
        return original_isolation(*args, **kwargs)

    original_payload = profiles_module.validate_profile_payload

    def validate_payload(*args, **kwargs):
        assert state["held"]
        events.append("payload")
        return original_payload(*args, **kwargs)

    original_submit = profiles_module.create_profile_sync

    def submit(*args, **kwargs):
        assert state["held"]
        events.append("submit")
        return original_submit(*args, **kwargs)

    monkeypatch.setattr(profiles_module, "resolve_cluster_write_policy", resolve_policy)
    monkeypatch.setattr(profiles_module, "list_project_configs", project_configs)
    monkeypatch.setattr(
        profiles_module, "validate_target_project_isolation", validate_isolation
    )
    monkeypatch.setattr(profiles_module, "validate_profile_payload", validate_payload)
    monkeypatch.setattr(profiles_module, "create_profile_sync", submit)

    result = await profiles_module.create_profile(
        request=profiles_module.ProfileCreateRequest(
            name="safe", config={"limits.cpu": "2"}
        ),
        host="nuc1",
        project=None,
        lxd_service=_profile_service(daemon),
        _=None,
        user=_user("partner", "profile-lock-create"),
        db=object(),
    )

    assert result["success"] is True
    assert events == [
        "lock-enter",
        "policy",
        "project-config",
        "project-isolation",
        "payload",
        "submit",
        "lock-exit",
    ]


@pytest.mark.asyncio
async def test_profile_update_holds_lock_through_final_payload_and_daemon_submit(
    monkeypatch,
) -> None:
    policy = ClusterWritePolicy("nuc1", Role.PARTNER, ClusterOwnership.SHARED)
    daemon = Daemon()
    _wire_profile_route(monkeypatch, policy)
    events: list[str] = []
    state = {"held": False}
    _record_configuration_lock(monkeypatch, profiles_module, events, state)

    async def resolve_policy(*args, **kwargs):
        assert state["held"]
        events.append("policy")
        return policy

    def project_configs(client):
        assert state["held"]
        events.append("project-config")
        return {"default": dict(SAFE_SHARED_PROJECT_CONFIG)}

    original_isolation = profiles_module.validate_target_project_isolation

    def validate_isolation(*args, **kwargs):
        assert state["held"]
        events.append("project-isolation")
        return original_isolation(*args, **kwargs)

    original_payload = profiles_module.validate_profile_payload

    def validate_payload(*args, **kwargs):
        assert state["held"]
        events.append("payload")
        return original_payload(*args, **kwargs)

    original_submit = profiles_module.update_profile_sync

    def submit(*args, **kwargs):
        assert state["held"]
        events.append("submit")
        return original_submit(*args, **kwargs)

    monkeypatch.setattr(profiles_module, "resolve_cluster_write_policy", resolve_policy)
    monkeypatch.setattr(profiles_module, "list_project_configs", project_configs)
    monkeypatch.setattr(
        profiles_module, "validate_target_project_isolation", validate_isolation
    )
    monkeypatch.setattr(profiles_module, "validate_profile_payload", validate_payload)
    monkeypatch.setattr(profiles_module, "update_profile_sync", submit)

    result = await profiles_module.update_profile(
        profile_name="web",
        host="nuc1",
        project=None,
        request=profiles_module.ProfileUpdateRequest(description="after"),
        if_match='"abc"',
        lxd_service=_profile_service(daemon),
        _=None,
        user=_user("partner", "profile-lock-update"),
        db=object(),
    )

    assert result["success"] is True
    assert events == [
        "lock-enter",
        "policy",
        "project-config",
        "project-isolation",
        "payload",
        "submit",
        "lock-exit",
    ]


@pytest.mark.asyncio
async def test_profile_create_uses_bounded_per_cluster_offloads(monkeypatch) -> None:
    policy = ClusterWritePolicy("nuc1", Role.PARTNER, ClusterOwnership.SHARED)
    daemon = Daemon()
    _wire_profile_route(monkeypatch, policy)
    calls: list[dict[str, Any]] = []

    async def bounded(function, *args, **kwargs):
        options = {
            key: kwargs.pop(key)
            for key in (
                "timeout",
                "label",
                "executor",
                "max_in_flight",
                "concurrency_key",
                "max_in_flight_per_key",
            )
        }
        calls.append(options)
        return function(*args, **kwargs)

    monkeypatch.setattr(profiles_module, "run_blocking", bounded)

    result = await profiles_module.create_profile(
        request=profiles_module.ProfileCreateRequest(
            name="bounded", config={"limits.cpu": "2"}
        ),
        host="nuc1",
        project=None,
        lxd_service=_profile_service(daemon),
        _=None,
        user=_user("partner", "profile-bounded"),
        db=object(),
    )

    assert result["success"] is True
    assert [call["label"] for call in calls] == [
        "profile.scope[nuc1]",
        "profile.create[nuc1]",
    ]
    assert all(call["executor"] is profiles_module._executor for call in calls)
    assert all(call["max_in_flight"] == profiles_module._PROFILE_MAX_IN_FLIGHT for call in calls)
    assert all(call["concurrency_key"] == "nuc1" for call in calls)
    assert all(
        call["max_in_flight_per_key"]
        == profiles_module._PROFILE_MAX_IN_FLIGHT_PER_CLUSTER
        for call in calls
    )


@pytest.mark.asyncio
async def test_profile_timeout_keeps_share_lock_until_worker_exits(monkeypatch) -> None:
    policy = ClusterWritePolicy("nuc1", Role.PARTNER, ClusterOwnership.SHARED)
    daemon = Daemon()
    _wire_profile_route(monkeypatch, policy)
    worker_started = asyncio.Event()
    worker_released = asyncio.Event()
    share_entered = asyncio.Event()
    lock = asyncio.Lock()

    @asynccontextmanager
    async def configuration_lock(db, host):
        async with lock:
            yield

    async def timed_out_worker(function, *args, **kwargs):
        label = kwargs.pop("label")
        for option in (
            "timeout",
            "executor",
            "max_in_flight",
            "concurrency_key",
            "max_in_flight_per_key",
        ):
            kwargs.pop(option, None)
        if label == "profile.create[nuc1]":
            worker_started.set()
            raise BlockingTimeout(label, 0.01)
        return function(*args, **kwargs)

    monkeypatch.setattr(
        profiles_module, "serialize_cluster_configuration", configuration_lock
    )
    monkeypatch.setattr(profiles_module, "run_blocking", timed_out_worker)
    monkeypatch.setattr(
        profiles_module,
        "in_flight_for_key",
        lambda executor, host: 0 if worker_released.is_set() else 1,
    )

    create_task = asyncio.create_task(
        profiles_module.create_profile(
            request=profiles_module.ProfileCreateRequest(
                name="timeout-safe", config={"limits.cpu": "2"}
            ),
            host="nuc1",
            project=None,
            lxd_service=_profile_service(daemon),
            _=None,
            user=_user("partner", "profile-timeout"),
            db=object(),
        )
    )
    await asyncio.wait_for(worker_started.wait(), timeout=1)

    async def share_cluster() -> None:
        async with configuration_lock(object(), "nuc1"):
            share_entered.set()

    share_task = asyncio.create_task(share_cluster())
    await asyncio.sleep(0)
    assert not share_entered.is_set()

    worker_released.set()
    with pytest.raises(HTTPException) as caught:
        await asyncio.wait_for(create_task, timeout=1)
    await asyncio.wait_for(share_task, timeout=1)

    assert caught.value.status_code == 503
    assert share_entered.is_set()
    assert daemon.posts == []


@pytest.mark.asyncio
async def test_shared_profile_create_rejects_a_direct_passthrough_before_daemon(
    monkeypatch,
) -> None:
    policy = ClusterWritePolicy("nuc1", Role.PARTNER, ClusterOwnership.SHARED)
    daemon = Daemon()
    _wire_profile_route(monkeypatch, policy)

    with pytest.raises(HTTPException) as caught:
        await profiles_module.create_profile(
            request=profiles_module.ProfileCreateRequest(
                name="unsafe", devices={"gpu0": {"type": "gpu"}}
            ),
            host="nuc1",
            project=None,
            lxd_service=_profile_service(daemon),
            _=None,
            user=_user("partner", "profile-create"),
            db=object(),
        )

    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == "shared_cluster_policy_violation"
    assert daemon.posts == []


@pytest.mark.asyncio
async def test_shared_profile_create_rejects_unisolated_effective_project_before_daemon(
    monkeypatch,
) -> None:
    policy = ClusterWritePolicy("nuc1", Role.PARTNER, ClusterOwnership.SHARED)
    daemon = Daemon()
    _wire_profile_route(monkeypatch, policy)
    monkeypatch.setattr(
        profiles_module,
        "list_project_configs",
        lambda client: {
            "tenant-a": {
                **SAFE_SHARED_PROJECT_CONFIG,
                "features.profiles": "false",
            },
            "default": {"restricted": "true"},
        },
    )

    with pytest.raises(HTTPException) as caught:
        await profiles_module.create_profile(
            request=profiles_module.ProfileCreateRequest(
                name="safe-payload", config={"limits.cpu": "2"}
            ),
            host="nuc1",
            project="tenant-a",
            lxd_service=_profile_service(daemon),
            _=None,
            user=_user("partner", "profile-project-isolation"),
            db=object(),
        )

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "shared_cluster_project_not_isolated"
    assert daemon.posts == []


@pytest.mark.asyncio
async def test_shared_profile_update_checks_etag_and_the_final_candidate(monkeypatch) -> None:
    policy = ClusterWritePolicy("nuc1", Role.PARTNER, ClusterOwnership.SHARED)
    daemon = Daemon(
        profile={
            "name": "web",
            "description": "before",
            "config": {"limits.cpu": "2"},
            "devices": {"gpu0": {"type": "gpu"}},
            "used_by": [],
            "project": "default",
        }
    )
    _wire_profile_route(monkeypatch, policy)
    service = _profile_service(daemon)
    common = {
        "profile_name": "web",
        "host": "nuc1",
        "project": None,
        "lxd_service": service,
        "_": None,
        "user": _user("partner", "profile-update"),
        "db": object(),
    }

    with pytest.raises(HTTPException) as missing:
        await profiles_module.update_profile(
            request=profiles_module.ProfileUpdateRequest(description="after"),
            if_match=None,
            **common,
        )
    with pytest.raises(HTTPException) as stale:
        await profiles_module.update_profile(
            request=profiles_module.ProfileUpdateRequest(description="after"),
            if_match='"stale"',
            **common,
        )
    with pytest.raises(HTTPException) as final_payload:
        await profiles_module.update_profile(
            request=profiles_module.ProfileUpdateRequest(config={"limits.cpu": "4"}),
            if_match='"abc"',
            **common,
        )
    with pytest.raises(HTTPException) as unsafe_description_only:
        await profiles_module.update_profile(
            request=profiles_module.ProfileUpdateRequest(description="after"),
            if_match='"abc"',
            **common,
        )

    daemon.profile["devices"] = {}
    description_only = await profiles_module.update_profile(
        request=profiles_module.ProfileUpdateRequest(description="after"),
        if_match='"abc"',
        **common,
    )

    assert missing.value.status_code == 428
    assert stale.value.status_code == 412
    assert final_payload.value.status_code == 403
    assert unsafe_description_only.value.status_code == 403
    assert description_only["success"] is True
    assert len(daemon.puts) == 1, "only a fully safe final payload reaches the daemon"


@pytest.mark.asyncio
async def test_shared_profile_update_fails_closed_when_daemon_has_no_etag(monkeypatch) -> None:
    policy = ClusterWritePolicy("nuc1", Role.PARTNER, ClusterOwnership.SHARED)
    daemon = Daemon(etag=None)
    _wire_profile_route(monkeypatch, policy)

    with pytest.raises(HTTPException) as caught:
        await profiles_module.update_profile(
            profile_name="web",
            host="nuc1",
            project=None,
            request=profiles_module.ProfileUpdateRequest(description="after"),
            if_match='"client-version"',
            lxd_service=_profile_service(daemon),
            _=None,
            user=_user("partner", "profile-no-etag"),
            db=object(),
        )

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "profile_precondition_unavailable"
    assert daemon.puts == []


class _ProjectService:
    def __init__(self, *, restricted: bool = True) -> None:
        self.restricted = restricted
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []

    def get_node_resources(self, host):
        return {"cpu_total": 32, "memory_total": 2**40, "storage_total": 2**44}

    async def create_project(self, **kwargs):
        self.created.append(kwargs)
        return {
            "name": kwargs["name"],
            "host": kwargs["host"],
            "description": kwargs.get("description") or "",
            "config": kwargs["config"],
            "used_by": [],
        }

    async def list_projects(self, host):
        config = dict(SAFE_SHARED_PROJECT_CONFIG) if self.restricted else {}
        return [{"name": "mine", "host": host, "description": "", "config": config, "used_by": []}]

    async def update_project(self, **kwargs):
        self.updated.append(kwargs)
        return {
            "name": kwargs["project_name"],
            "host": kwargs["host"],
            "description": kwargs.get("description") or "",
            "config": dict(SAFE_SHARED_PROJECT_CONFIG),
            "used_by": [],
        }


def _wire_project_route(monkeypatch) -> None:
    async def shared_policy(*args, **kwargs):
        return ClusterWritePolicy(HOST, Role.PARTNER, ClusterOwnership.SHARED)

    monkeypatch.setattr(projects_module, "resolve_cluster_write_policy", shared_policy)
    monkeypatch.setattr(projects_module, "org_to_hold_project_grant", _no_grant)
    monkeypatch.setattr(projects_module, "verify_cluster_access", lambda host: None)
    monkeypatch.setattr(projects_module, "_refuse_a_project_outside_the_bound", lambda *args: None)
    async def bounded_inline(function, *args, **kwargs):
        for option in (
            "timeout",
            "label",
            "executor",
            "max_in_flight",
            "concurrency_key",
            "max_in_flight_per_key",
        ):
            kwargs.pop(option, None)
        return function(*args, **kwargs)

    monkeypatch.setattr(projects_module, "run_blocking", bounded_inline)
    monkeypatch.setattr(projects_module.activity_logger, "log_activity", lambda **kwargs: None)
    monkeypatch.setattr(projects_module, "schedule_orcahub_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        projects_module,
        "get_cache_manager",
        lambda: SimpleNamespace(
            delete=lambda *args: None,
            delete_pattern=lambda *args: None,
            invalidate_resource_cache=lambda *args: None,
        ),
    )


async def _no_grant(*args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_project_capacity_reads_use_bounded_per_cluster_executor(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    async def bounded(function, *args, **kwargs):
        calls.append(dict(kwargs))
        return function(*args)

    monkeypatch.setattr(projects_module, "run_blocking", bounded)
    resources = await projects_module._bounded_node_resources(_ProjectService(), HOST)

    assert resources["cpu_total"] == 32
    assert calls == [
        {
            "timeout": projects_module._PROJECT_RESOURCE_WALL_SECONDS,
            "label": f"project.capacity[{HOST}]",
            "executor": projects_module._projects_executor,
            "max_in_flight": projects_module._PROJECT_RESOURCE_MAX_IN_FLIGHT,
            "concurrency_key": HOST,
            "max_in_flight_per_key": (
                projects_module._PROJECT_RESOURCE_MAX_IN_FLIGHT_PER_CLUSTER
            ),
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "update"])
async def test_project_writes_use_bounded_per_cluster_executor(
    monkeypatch,
    operation: str,
) -> None:
    manager = ProjectManager(SimpleNamespace())
    seen: dict[str, Any] = {}

    async def bounded(function, *args, **kwargs):
        seen.update(
            function=function.__name__,
            timeout=kwargs["timeout"],
            label=kwargs["label"],
            executor=kwargs["executor"],
            max_in_flight=kwargs["max_in_flight"],
            concurrency_key=kwargs["concurrency_key"],
            max_in_flight_per_key=kwargs["max_in_flight_per_key"],
        )
        return {
            "name": "bounded-project",
            "host": HOST,
            "description": "",
            "config": {},
            "used_by": [],
        }

    monkeypatch.setattr(lxd_projects_module, "run_blocking", bounded)

    if operation == "create":
        await manager.create_project(HOST, "bounded-project")
        assert seen["function"] == "_create_project_sync"
        assert seen["label"] == f"project.create[{HOST}]"
    else:
        await manager.update_project(HOST, "bounded-project", description="after")
        assert seen["function"] == "_update_project_sync"
        assert seen["label"] == f"project.update[{HOST}]"

    assert seen["timeout"] == lxd_projects_module._PROJECT_WRITE_WALL_SECONDS
    assert seen["executor"] is lxd_projects_module._projects_executor
    assert seen["max_in_flight"] == lxd_projects_module._PROJECT_LIST_WORKERS
    assert seen["concurrency_key"] == HOST
    assert (
        seen["max_in_flight_per_key"]
        == lxd_projects_module._PROJECT_WRITE_MAX_IN_FLIGHT_PER_CLUSTER
    )


@pytest.mark.asyncio
async def test_project_create_holds_lock_through_final_policy_and_daemon_submit(
    monkeypatch,
) -> None:
    from app.services.webhooks import emitter

    service = _ProjectService()
    _wire_project_route(monkeypatch)
    events: list[str] = []
    state = {"held": False}
    _record_configuration_lock(monkeypatch, projects_module, events, state)
    policy = ClusterWritePolicy(HOST, Role.PARTNER, ClusterOwnership.SHARED)

    async def resolve_policy(*args, **kwargs):
        assert state["held"]
        events.append("policy")
        return policy

    original_isolation = projects_module.validate_target_project_isolation

    def validate_isolation(*args, **kwargs):
        assert state["held"]
        events.append("project-isolation")
        return original_isolation(*args, **kwargs)

    original_create = service.create_project

    async def create_project(**kwargs):
        assert state["held"]
        events.append("submit")
        return await original_create(**kwargs)

    async def no_event(**kwargs):
        return None

    monkeypatch.setattr(projects_module, "resolve_cluster_write_policy", resolve_policy)
    monkeypatch.setattr(
        projects_module, "validate_target_project_isolation", validate_isolation
    )
    monkeypatch.setattr(service, "create_project", create_project)
    monkeypatch.setattr(emitter, "emit_resource_event", no_event)

    result = await projects_module.create_project(
        request=ProjectCreateRequest(
            host=HOST,
            name="locked-create",
            max_cpu=2,
            max_ram="1GiB",
            max_storage="10GiB",
            config={"features.profiles": "true"},
        ),
        lxd_service=service,
        _=None,
        user=_user("partner", "project-lock-create"),
        db=object(),
    )

    assert result.success is True
    assert events == [
        "lock-enter",
        "policy",
        "project-isolation",
        "submit",
        "lock-exit",
    ]


@pytest.mark.asyncio
async def test_project_update_holds_lock_through_exact_read_and_daemon_submit(
    monkeypatch,
) -> None:
    service = _ProjectService()
    _wire_project_route(monkeypatch)
    events: list[str] = []
    state = {"held": False}
    _record_configuration_lock(monkeypatch, projects_module, events, state)
    policy = ClusterWritePolicy(HOST, Role.PARTNER, ClusterOwnership.SHARED)

    async def resolve_policy(*args, **kwargs):
        assert state["held"]
        events.append("policy")
        return policy

    original_list = service.list_projects

    async def list_projects(host):
        assert state["held"]
        events.append("project-config")
        return await original_list(host)

    original_validation = projects_module.validate_project_update

    def validate_project(*args, **kwargs):
        assert state["held"]
        events.append("project-isolation")
        return original_validation(*args, **kwargs)

    original_update = service.update_project

    async def update_project(**kwargs):
        assert state["held"]
        events.append("submit")
        return await original_update(**kwargs)

    monkeypatch.setattr(projects_module, "resolve_cluster_write_policy", resolve_policy)
    monkeypatch.setattr(projects_module, "validate_project_update", validate_project)
    monkeypatch.setattr(service, "list_projects", list_projects)
    monkeypatch.setattr(service, "update_project", update_project)

    result = await projects_module.update_project(
        host=HOST,
        project_name="mine",
        request=ProjectUpdateRequest(description="renamed"),
        lxd_service=service,
        _=None,
        user=_user("partner", "project-lock-update"),
        db=object(),
    )

    assert result.success is True
    assert events == [
        "lock-enter",
        "policy",
        "project-config",
        "project-isolation",
        "submit",
        "lock-exit",
    ]


@pytest.mark.asyncio
async def test_project_create_serializes_a_concurrent_cluster_share(monkeypatch) -> None:
    from app.services.webhooks import emitter

    started = asyncio.Event()
    release = asyncio.Event()
    share_entered = asyncio.Event()

    class BlockingProjectService(_ProjectService):
        async def create_project(self, **kwargs):
            started.set()
            await release.wait()
            return await super().create_project(**kwargs)

    service = BlockingProjectService()
    _wire_project_route(monkeypatch)

    async def no_event(**kwargs):
        return None

    monkeypatch.setattr(emitter, "emit_resource_event", no_event)
    db = object()
    create_task = asyncio.create_task(
        projects_module.create_project(
            request=ProjectCreateRequest(
                host=HOST,
                name="race-safe",
                max_cpu=2,
                max_ram="1GiB",
                max_storage="10GiB",
                config={"features.profiles": "true"},
            ),
            lxd_service=service,
            _=None,
            user=_user("partner", "project-race"),
            db=db,
        )
    )
    started_task = asyncio.create_task(started.wait())
    done, _pending = await asyncio.wait(
        {create_task, started_task}, timeout=1, return_when=asyncio.FIRST_COMPLETED
    )
    if create_task in done:
        await create_task
    assert started_task in done, "project create did not reach daemon submission"

    async def share_cluster() -> None:
        async with projects_module.serialize_cluster_configuration(db, HOST):
            share_entered.set()

    share_task = asyncio.create_task(share_cluster())
    await asyncio.sleep(0)
    assert not share_entered.is_set(), "the ownership transition must wait for daemon submit"

    release.set()
    result = await asyncio.wait_for(create_task, timeout=1)
    await asyncio.wait_for(share_task, timeout=1)

    assert result.success is True
    assert share_entered.is_set()


@pytest.mark.asyncio
async def test_cancelled_project_create_finishes_grant_before_releasing_share_lock(
    monkeypatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    share_entered = asyncio.Event()
    grants: list[tuple[int, str, str]] = []

    class BlockingProjectService(_ProjectService):
        async def create_project(self, **kwargs):
            started.set()
            await release.wait()
            return await super().create_project(**kwargs)

    service = BlockingProjectService()
    _wire_project_route(monkeypatch)

    async def grant_org(*args, **kwargs):
        return 17

    async def record_grant(
        db,
        *,
        organization_id,
        cluster_id,
        project_name,
        created_by,
    ):
        grants.append((organization_id, cluster_id, project_name))
        return ""

    monkeypatch.setattr(projects_module, "org_to_hold_project_grant", grant_org)
    monkeypatch.setattr(
        projects_module.project_grants, "record_created_project", record_grant
    )
    db = object()
    create_task = asyncio.create_task(
        projects_module.create_project(
            request=ProjectCreateRequest(
                host=HOST,
                name="cancel-safe",
                max_cpu=2,
                max_ram="1GiB",
                max_storage="10GiB",
                config={"features.profiles": "true"},
            ),
            lxd_service=service,
            _=None,
            user=_user("partner", "project-cancel"),
            db=db,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    create_task.cancel()
    await asyncio.sleep(0)

    async def share_cluster() -> None:
        async with projects_module.serialize_cluster_configuration(db, HOST):
            share_entered.set()

    share_task = asyncio.create_task(share_cluster())
    await asyncio.sleep(0)
    assert not share_entered.is_set()
    assert grants == []

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await create_task
    await asyncio.wait_for(share_task, timeout=1)

    assert grants == [(17, HOST, "cancel-safe")]
    assert share_entered.is_set()


@pytest.mark.asyncio
async def test_shared_project_create_forces_isolation_and_rejects_raw_config(monkeypatch) -> None:
    from app.services.webhooks import emitter

    service = _ProjectService()
    _wire_project_route(monkeypatch)

    async def no_event(**kwargs):
        return None

    monkeypatch.setattr(emitter, "emit_resource_event", no_event)
    body = {
        "host": HOST,
        "name": "safe-project",
        "max_cpu": 2,
        "max_ram": "1GiB",
        "max_storage": "10GiB",
        "config": {"features.profiles": "true"},
    }

    accepted = await projects_module.create_project(
        request=ProjectCreateRequest(**body),
        lxd_service=service,
        _=None,
        user=_user("partner", "project-create"),
        db=object(),
    )
    with pytest.raises(HTTPException) as rejected:
        await projects_module.create_project(
            request=ProjectCreateRequest(
                **{
                    **body,
                    "name": "unsafe-project",
                    "config": {"raw.idmap": "both 0 0"},
                }
            ),
            lxd_service=service,
            _=None,
            user=_user("partner", "project-create-rejected"),
            db=object(),
        )

    assert accepted.success is True
    assert service.created[0]["config"]["restricted"] == "true"
    assert rejected.value.status_code == 403
    assert len(service.created) == 1


@pytest.mark.asyncio
async def test_shared_legacy_project_update_fails_closed_without_mutation(monkeypatch) -> None:
    service = _ProjectService(restricted=False)
    _wire_project_route(monkeypatch)

    with pytest.raises(HTTPException) as response:
        await projects_module.update_project(
            host=HOST,
            project_name="mine",
            request=ProjectUpdateRequest(description="renamed"),
            lxd_service=service,
            _=None,
            user=_user("partner", "project-update"),
            db=object(),
        )

    assert response.value.status_code == 409
    assert response.value.detail["code"] == "shared_cluster_project_not_isolated"
    assert service.updated == []
