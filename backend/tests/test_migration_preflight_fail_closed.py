"""Fail-closed regression coverage for migration planning inventory."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services.migrations import preflight, preflight_source


_ISOLATED_CONFIG = {
    feature: "true" for feature in preflight._PROJECT_DEPENDENCY_FEATURES
}


def _instance(*, status: str = "Stopped") -> dict[str, Any]:
    return {
        "name": "workload",
        "type": "container",
        "architecture": "x86_64",
        "profiles": ["default"],
        "status": status,
        "location": "member-a",
        "created_at": "2026-09-23T00:00:00Z",
        "config": {"volatile.uuid": "source-uuid"},
        "expanded_config": {"volatile.uuid": "source-uuid"},
        "devices": {"root": {"type": "disk", "path": "/", "pool": "fast"}},
        "expanded_devices": {
            "root": {"type": "disk", "path": "/", "pool": "fast"}
        },
    }


def _empty_resources(instance: dict[str, Any] | None = None) -> dict[str, list[Any]]:
    return {
        "instances": [instance] if instance is not None else [],
        "profiles": [{"name": "default", "config": {}, "devices": {}}],
        "networks": [],
        "images": [],
        "custom_volumes": [],
        "storage_buckets": [],
        "network_acls": [],
        "network_zones": [],
        "network_address_sets": [],
        "placement_groups": [],
        "network_forwards": [],
        "network_load_balancers": [],
    }


def _patch_inspect(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str = "Stopped",
    source_config: dict[str, str] | None = None,
    target_config: dict[str, str] | None = None,
) -> tuple[Any, Any]:
    source_client = SimpleNamespace(api=SimpleNamespace(networks=object(), operations=object()))
    target_client = SimpleNamespace(api=SimpleNamespace(networks=object(), operations=object()))
    source_instance = _instance(status=status)
    source_project = {
        "name": "source-project",
        "config": (
            dict(_ISOLATED_CONFIG) if source_config is None else source_config
        ),
    }
    target_project = {
        "name": "target-project",
        "config": (
            dict(_ISOLATED_CONFIG) if target_config is None else target_config
        ),
    }
    identity = {
        "engine": "lxd",
        "version": "6.10",
        "extensions": ["cluster_internal_copy", "container_copy_project"],
        "architectures": ["x86_64"],
    }
    monkeypatch.setattr(preflight, "daemon_identity", lambda _client: identity)
    monkeypatch.setattr(preflight, "approved_build", lambda *_args: True)
    monkeypatch.setattr(preflight, "_check_active_operations", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        preflight,
        "_get_project",
        lambda client, project: (
            source_project
            if client is source_client or project == "source-project"
            else target_project
        ),
    )
    monkeypatch.setattr(
        preflight,
        "_instance",
        lambda client, project, _name: (
            source_instance
            if client is source_client or project == "source-project"
            else None
        ),
    )
    monkeypatch.setattr(
        preflight, "_profile", lambda *_args: {"config": {}, "devices": {}}
    )
    monkeypatch.setattr(
        preflight,
        "_known_project_resources",
        lambda client, _project: (
            _empty_resources(source_instance)
            if client is source_client
            else _empty_resources()
        ),
    )
    monkeypatch.setattr(preflight, "_storage_pools", lambda _client: ["fast"])
    monkeypatch.setattr(preflight, "_instance_transfer_bytes", lambda *_args: 100)
    monkeypatch.setattr(
        preflight_source, "active_instance_operations", lambda _client, _wanted: set()
    )
    monkeypatch.setattr(
        preflight,
        "_get_collection",
        lambda _node, *, project, resource: [
            {"name": "lxdbr0", "managed": True, "type": "bridge", "status": "Created"}
        ],
    )
    monkeypatch.setattr(
        preflight.mobility,
        "pool_headroom",
        lambda *_args: {"total": 1_000, "used": 100, "free": 900},
    )
    monkeypatch.setattr(
        preflight,
        "api_endpoint",
        lambda client: (
            "https://source.example.test:8443"
            if client is source_client
            else "https://target.example.test:8443"
        ),
    )
    return source_client, target_client


def _inspect(
    source_client: Any,
    target_client: Any,
    *,
    intent: str,
    same_cluster: bool = False,
    same_project: bool = False,
    target_member: str | None = None,
    scope: str = "instance",
) -> preflight.PreflightResult:
    return preflight.inspect(
        scope=scope,
        intent=intent,
        source_client=source_client,
        target_client=target_client,
        source_cluster="cluster-a",
        source_project="source-project",
        source_name="workload",
        target_cluster="cluster-a" if same_cluster else "cluster-b",
        target_project="source-project" if same_project else "target-project",
        target_name="workload" if target_member else "workload-copy",
        target_member=target_member,
        target_pool="fast",
        settings=SimpleNamespace(),
        mappings={"storage_pools": {"root": "fast"}},
    )


def _codes(result: preflight.PreflightResult) -> set[str]:
    return {issue["code"] for issue in result.blockers}


def test_identity_changing_move_is_admitted_like_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = _patch_inspect(monkeypatch)

    moved = _inspect(source, target, intent="move")
    copied = _inspect(source, target, intent="copy")

    for result in (moved, copied):
        assert "instance_shadow_metadata_cutover_unsupported" not in _codes(result)
        assert result.strategy == "cross_cluster_pull"
        assert "workload" in result.contracts
    assert moved.capabilities["rollback"] is True
    assert copied.capabilities["rollback"] is False


def test_same_project_member_move_is_not_identity_cutover_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _target = _patch_inspect(monkeypatch)

    result = _inspect(
        source,
        source,
        intent="move",
        same_cluster=True,
        same_project=True,
        target_member="member-b",
    )

    assert "instance_shadow_metadata_cutover_unsupported" not in _codes(result)


def test_project_member_relocation_is_blocked_until_partial_recovery_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _target = _patch_inspect(monkeypatch)

    result = _inspect(
        source,
        source,
        intent="move",
        same_cluster=True,
        same_project=True,
        target_member="member-b",
        scope="project",
    )

    assert "project_member_relocation_recovery_unavailable" in _codes(result)


def test_copy_to_an_explicit_member_fails_closed_on_unreadable_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = _patch_inspect(monkeypatch)

    result = _inspect(source, target, intent="copy", target_member="member-b")

    assert "targeted_copy_member_validation_unsupported" not in _codes(result)
    assert "target_member_unverified" in _codes(result)


def test_a_running_source_is_admitted_for_a_cold_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = _patch_inspect(monkeypatch, status="Running")

    result = _inspect(source, target, intent="copy")

    assert "source_instance_not_stopped" not in _codes(result)
    assert result.capabilities["source_status"] == "running"
    assert result.downtime == "cold"


def test_a_running_source_is_refused_when_running_migration_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = _patch_inspect(monkeypatch, status="Running")

    result = preflight.inspect(
        scope="instance",
        intent="copy",
        source_client=source,
        target_client=target,
        source_cluster="cluster-a",
        source_project="source-project",
        source_name="workload",
        target_cluster="cluster-b",
        target_project="target-project",
        target_name="workload-copy",
        target_member=None,
        target_pool="fast",
        settings=SimpleNamespace(migration_running_source_enabled=False),
        mappings={"storage_pools": {"root": "fast"}},
    )

    assert "source_instance_not_stopped" in _codes(result)


@pytest.mark.parametrize("status", ["", "Frozen", "Error", "Starting"])
def test_a_transitional_or_failed_source_is_never_migrated(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    source, target = _patch_inspect(monkeypatch, status=status)

    result = _inspect(source, target, intent="copy")

    assert "source_instance_state_unsupported" in _codes(result)


@pytest.mark.parametrize(
    "source_config",
    [
        {**_ISOLATED_CONFIG, "features.profiles": "false"},
        {},
    ],
)
def test_shared_default_dependencies_are_a_warning_for_one_instance(
    monkeypatch: pytest.MonkeyPatch, source_config: dict[str, str]
) -> None:
    source, target = _patch_inspect(monkeypatch, source_config=source_config)

    result = _inspect(source, target, intent="copy")

    assert "source_shared_project_dependencies_unlocked" not in _codes(result)
    warning = next(
        issue
        for issue in result.warnings
        if issue["code"] == "source_project_shares_default_dependencies"
    )
    assert "features.profiles" in warning["message"]


def test_shared_default_dependencies_still_block_a_whole_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = _patch_inspect(
        monkeypatch, source_config={**_ISOLATED_CONFIG, "features.profiles": "false"}
    )

    result = _inspect(source, target, intent="move", scope="project")

    blocker = next(
        issue
        for issue in result.blockers
        if issue["code"] == "source_shared_project_dependencies_unlocked"
    )
    assert "canonical locks" in blocker["message"]


class _Response:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def json(self) -> Any:
        return self.payload


class _Collection:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def get(self, **_kwargs: Any) -> _Response:
        return _Response(self.payload)


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"metadata": None}, {"metadata": {}}, {"metadata": ["/1.0/instances/a"]}],
)
def test_malformed_collection_metadata_is_never_empty(payload: Any) -> None:
    with pytest.raises(preflight.InventoryReadError):
        preflight._get_collection(
            _Collection(payload), project="source-project", resource="instance"
        )


def test_target_inventory_failure_is_a_hard_blocker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = _patch_inspect(monkeypatch)

    def networks(_node: Any, *, project: str, resource: str) -> list[dict[str, Any]]:
        raise preflight.InventoryReadError(resource)

    monkeypatch.setattr(preflight, "_get_collection", networks)

    result = _inspect(source, target, intent="copy")

    assert "target_inventory_unverified" in _codes(result)
    issue = next(
        blocker
        for blocker in result.blockers
        if blocker["code"] == "target_inventory_unverified"
    )
    assert "never treated as empty" in issue["remediation"]


def test_active_daemon_operation_is_derived_from_authoritative_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(
        {
            "metadata": {
                "running": [
                    {
                        "status_code": 103,
                        "resources": {
                            "instances": [
                                "/1.0/instances/workload?project=source-project"
                            ]
                        },
                    }
                ],
                "success": [],
                "failure": [],
            }
        }
    )
    client = SimpleNamespace(
        api=SimpleNamespace(operations=_Collection(response.payload))
    )
    monkeypatch.setattr(
        preflight,
        "daemon_identity",
        lambda _client: {
            "extensions": ["operations_get_query_all_projects"],
        },
    )

    assert preflight._read_active_operation_projects(
        client, {"source-project"}
    ) == {"source-project"}


def test_ambiguous_active_operation_inventory_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(
        api=SimpleNamespace(
            operations=_Collection(
                {
                    "metadata": {
                        "running": [
                            {
                                "status_code": 103,
                                "resources": {"cluster": ["/1.0/cluster"]},
                            }
                        ]
                    }
                }
            )
        )
    )
    monkeypatch.setattr(
        preflight,
        "daemon_identity",
        lambda _client: {
            "extensions": ["operations_get_query_all_projects"],
        },
    )

    with pytest.raises(preflight.InventoryReadError):
        preflight._read_active_operation_projects(client, {"source-project"})


def test_active_or_unreadable_daemon_operation_inventory_blocks_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = preflight.PreflightResult(strategy="same_cluster_copy", downtime="none")
    monkeypatch.setattr(
        preflight,
        "_read_active_operation_projects",
        lambda _client, _projects: {"source-project"},
    )

    preflight._check_active_operations(
        result,
        source_client=object(),
        target_client=object(),
        source_cluster="cluster-a",
        source_project="source-project",
        target_cluster="cluster-a",
        target_project="target-project",
    )

    assert "source_active_daemon_operation" in _codes(result)
    assert result.capabilities["daemon_operations_quiescent"] is False

    result = preflight.PreflightResult(strategy="same_cluster_copy", downtime="none")

    def unreadable(_client: Any, _projects: set[str]) -> set[str]:
        raise preflight.InventoryReadError("active operation")

    monkeypatch.setattr(preflight, "_read_active_operation_projects", unreadable)
    preflight._check_active_operations(
        result,
        source_client=object(),
        target_client=object(),
        source_cluster="cluster-a",
        source_project="source-project",
        target_cluster="cluster-a",
        target_project="target-project",
    )

    assert "source_inventory_unverified" in _codes(result)
    assert result.capabilities["daemon_operation_inventory_verified"] is False


def test_member_relocation_pool_mapping_cannot_validate_an_unrelated_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = preflight.PreflightResult(strategy="member_relocation", downtime="cold")
    client = SimpleNamespace(
        api=SimpleNamespace(
            cluster=SimpleNamespace(
                members=_Collection(
                    {
                        "metadata": [
                            {
                                "server_name": "member-b",
                                "status": "Online",
                                "architecture": "x86_64",
                            }
                        ]
                    }
                )
            )
        )
    )
    checked: list[str | None] = []

    def capacity(_result: Any, **kwargs: Any) -> None:
        checked.append(kwargs["target_pool"])

    monkeypatch.setattr(preflight, "_check_capacity", capacity)

    preflight._member_relocation_checks(
        result,
        client=client,
        project="source-project",
        target_member="member-b",
        target_pool="unrelated-pool",
        instances=[_instance()],
    )

    assert "member_relocation_pool_mapping_unsupported" in _codes(result)
    assert checked == ["fast"]
    assert result.capabilities["member_relocation_root_pool"] == "fast"


def test_member_relocation_requires_exact_online_target_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = preflight.PreflightResult(strategy="member_relocation", downtime="cold")
    client = SimpleNamespace(
        api=SimpleNamespace(
            cluster=SimpleNamespace(
                members=_Collection(
                    {
                        "metadata": [
                            {
                                "server_name": "member-b",
                                "status": "Offline",
                                "architecture": "x86_64",
                            }
                        ]
                    }
                )
            )
        )
    )
    monkeypatch.setattr(preflight, "_check_capacity", lambda *_args, **_kwargs: None)

    preflight._member_relocation_checks(
        result,
        client=client,
        project="source-project",
        target_member="member-b",
        target_pool=None,
        instances=[_instance()],
    )

    assert "target_member_offline" in _codes(result)


def test_unknown_capacity_is_a_hard_blocker(monkeypatch: pytest.MonkeyPatch) -> None:
    result = preflight.PreflightResult(strategy="same_cluster_copy", downtime="none")
    monkeypatch.setattr(preflight, "_storage_pools", lambda _client: ["fast"])
    monkeypatch.setattr(preflight, "_instance_transfer_bytes", lambda *_args: None)
    monkeypatch.setattr(preflight.mobility, "pool_headroom", lambda *_args: None)

    preflight._check_capacity(
        result,
        source_client=object(),
        target_client=object(),
        source_project="source-project",
        instance_names=["workload"],
        target_pool="fast",
    )

    assert "source_footprint_unverified" in _codes(result)
    assert "target_capacity_unverified" in _codes(result)


def test_restricted_incus_pull_is_blocked_when_target_will_be_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = _patch_inspect(
        monkeypatch,
        source_config={**_ISOLATED_CONFIG, "restricted": "true"},
    )
    source_project = {
        "name": "source-project",
        "config": {**_ISOLATED_CONFIG, "restricted": "true"},
    }
    monkeypatch.setattr(
        preflight,
        "daemon_identity",
        lambda client: {
            "engine": "lxd" if client is source else "incus",
            "version": "6.10" if client is source else "7.0",
            "extensions": ["container_copy_project"],
            "architectures": ["x86_64"],
        },
    )
    monkeypatch.setattr(
        preflight,
        "_get_project",
        lambda client, _project: source_project if client is source else None,
    )

    result = _inspect(source, target, intent="copy")

    assert "restricted_incus_pull_target_unsupported" in _codes(result)
