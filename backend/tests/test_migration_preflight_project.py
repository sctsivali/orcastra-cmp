"""Project-scope preflight: relocating a whole project, or its workloads to another member."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from app.services.migrations import preflight

ISOLATED = {
    "features.images": "true",
    "features.networks": "false",
    "features.profiles": "true",
    "features.storage.buckets": "true",
    "features.storage.volumes": "true",
}
NETWORKS = [
    {"name": "lxdbr0", "managed": True, "type": "bridge", "status": "Created"},
    {"name": "lxdbr1", "managed": True, "type": "bridge", "status": "Created"},
]


def _instance(name: str, *, status: str = "Stopped", location: str = "member-a", **config: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": "container",
        "architecture": "x86_64",
        "profiles": ["default"],
        "status": status,
        "location": location,
        "created_at": "2026-10-16T00:00:00Z",
        "config": {"volatile.uuid": f"uuid-{name}", **config},
        "expanded_config": {"volatile.uuid": f"uuid-{name}", **config},
        "devices": {},
        "expanded_devices": {
            "root": {"type": "disk", "path": "/", "pool": "default"},
            "eth0": {"type": "nic", "network": "lxdbr0", "name": "eth0"},
        },
    }


DEFAULT_PROFILE = {
    "name": "default",
    "description": "",
    "config": {},
    "devices": {
        "root": {"type": "disk", "path": "/", "pool": "default"},
        "eth0": {"type": "nic", "network": "lxdbr0", "name": "eth0"},
    },
}


def _resources(instances: list[dict[str, Any]], **extra: list[Any]) -> dict[str, list[Any]]:
    resources: dict[str, list[Any]] = {
        "instances": instances,
        "profiles": [copy.deepcopy(DEFAULT_PROFILE)],
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
    resources.update(extra)
    return resources


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resources: dict[str, list[Any]],
    source_config: Optional[dict[str, str]] = None,
    target_exists: bool = False,
    members: Optional[list[dict[str, Any]]] = None,
) -> tuple[Any, Any]:
    source = SimpleNamespace(api=SimpleNamespace(networks=object(), cluster=None))
    target = SimpleNamespace(api=SimpleNamespace(networks=object()))
    identity = {
        "engine": "lxd",
        "version": "5.21.8",
        "extensions": ["cluster_internal_copy", "container_copy_project"],
        "architectures": ["x86_64"],
    }
    source_project = {"name": "web", "config": dict(ISOLATED if source_config is None else source_config)}

    def get_project(client: Any, project: str) -> Optional[dict[str, Any]]:
        if client is source and project in {"web", "default"}:
            return {**source_project, "name": project}
        if project == "web-new" and target_exists:
            return {"name": "web-new", "config": dict(ISOLATED)}
        return None

    monkeypatch.setattr(preflight, "daemon_identity", lambda _client: identity)
    monkeypatch.setattr(preflight, "approved_build", lambda *_args: True)
    monkeypatch.setattr(preflight, "api_endpoint", lambda _client: "https://10.0.0.1:8443")
    monkeypatch.setattr(preflight, "_check_active_operations", lambda *_a, **_k: None)
    monkeypatch.setattr(preflight, "_get_project", get_project)
    monkeypatch.setattr(preflight, "_known_project_resources", lambda _client, _project: resources)
    monkeypatch.setattr(preflight, "_get_collection", lambda *_a, **_k: copy.deepcopy(NETWORKS))
    monkeypatch.setattr(preflight, "_storage_pools", lambda _client: ["default", "alt"])
    monkeypatch.setattr(preflight, "_profile", lambda *_a: copy.deepcopy(DEFAULT_PROFILE))
    monkeypatch.setattr(preflight, "_instance_transfer_bytes", lambda *_a: 100)
    monkeypatch.setattr(
        preflight.mobility, "pool_headroom", lambda *_a: {"total": 10_000, "used": 0, "free": 10_000}
    )
    if members is not None:
        def get_members(**_kwargs: Any) -> Any:
            return SimpleNamespace(status_code=200, json=lambda: {"metadata": members})

        def network(**_kwargs: Any) -> Any:
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"metadata": {"locations": ["member-a", "member-b"], "status": "Created"}},
            )

        source.api.cluster = SimpleNamespace(members=SimpleNamespace(get=get_members))
        source.api.networks = {"lxdbr0": SimpleNamespace(get=network)}
        monkeypatch.setattr(preflight, "_member_pool_headroom", lambda *_a: {"total": 1000, "used": 0, "free": 1000})
    return source, target


def _inspect(source: Any, target: Any, *, target_cluster: str = "cluster-b", target_project: str = "web-new", member: Optional[str] = None, mappings: Optional[dict[str, Any]] = None) -> Any:
    return preflight.inspect(
        scope="project",
        intent="move",
        source_client=source,
        target_client=target,
        source_cluster="cluster-a",
        source_project="web",
        source_name=None,
        target_cluster=target_cluster,
        target_project=target_project,
        target_name=None,
        target_member=member,
        target_pool=None,
        settings=SimpleNamespace(migration_running_source_enabled=True, migration_project_max_instances=25),
        mappings=mappings or {},
    )


def _codes(result: Any) -> set[str]:
    return {issue["code"] for issue in result.blockers}


def test_a_relocation_plans_project_profiles_then_instances_stopped_first(monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = _patch(
        monkeypatch,
        resources=_resources([_instance("web-1", status="Running"), _instance("db-1")]),
    )

    result = _inspect(source, target)

    assert result.blockers == []
    assert result.strategy == "project_relocation"
    assert result.downtime == "cold"
    assert result.capabilities["target_project_create"] is True
    assert result.capabilities["rollback"] is True
    order = [(row["resource_type"], row["source_name"]) for row in result.resources]
    assert order == [("project", "web"), ("profile", "default"), ("instance", "db-1"), ("instance", "web-1")]
    instance = result.resources[-1]
    assert instance["source_status"] == "running"
    assert instance["contract"]["contract_hash"]
    assert set(result.contracts) == {"web-1", "db-1"}


def test_a_profile_nic_mapping_reaches_the_clone_and_the_instance_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = _patch(monkeypatch, resources=_resources([_instance("web-1")]))

    result = _inspect(source, target, mappings={"networks": {"lxdbr0": "lxdbr1"}})

    assert result.blockers == []
    profile = next(row for row in result.resources if row["resource_type"] == "profile")
    assert profile["contract"]["nic_decisions"][0]["target"] == "lxdbr1"
    contract = result.contracts["web-1"]
    assert contract.expected_devices["eth0"]["network"] == "lxdbr1"
    # The instance inherits the mapped NIC from its cloned profile, so nothing is pinned locally.
    assert "eth0" not in contract.devices


def test_an_existing_target_project_is_never_merged_into(monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = _patch(monkeypatch, resources=_resources([_instance("web-1")]), target_exists=True)

    assert "target_project_exists" in _codes(_inspect(source, target))


def test_the_default_project_cannot_be_relocated(monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = _patch(monkeypatch, resources=_resources([_instance("web-1")]))

    result = preflight.inspect(
        scope="project",
        intent="move",
        source_client=source,
        target_client=target,
        source_cluster="cluster-a",
        source_project="default",
        source_name=None,
        target_cluster="cluster-b",
        target_project="default",
        target_name=None,
        target_member=None,
        target_pool=None,
        settings=SimpleNamespace(),
        mappings={},
    )

    assert "default_project_relocation_unsupported" in _codes(result)


@pytest.mark.parametrize(
    ("kind", "rows", "code"),
    [
        ("custom_volumes", [{"name": "data", "pool": "default"}], "unsupported_custom_volumes"),
        ("storage_buckets", [{"name": "b1"}], "unsupported_storage_buckets"),
        ("images", [{"fingerprint": "abc", "cached": False}], "unsupported_images"),
        ("placement_groups", [{"name": "spread"}], "unsupported_placement_groups"),
    ],
)
def test_what_a_relocation_cannot_carry_is_refused(
    monkeypatch: pytest.MonkeyPatch, kind: str, rows: list[Any], code: str
) -> None:
    source, target = _patch(monkeypatch, resources=_resources([_instance("web-1")], **{kind: rows}))

    assert code in _codes(_inspect(source, target))


def test_project_owned_networks_are_refused_but_shared_ones_are_not(monkeypatch: pytest.MonkeyPatch) -> None:
    owned = {**ISOLATED, "features.networks": "true"}
    rows = [{"name": "ovn0", "managed": True}]
    source, target = _patch(monkeypatch, resources=_resources([_instance("web-1")], networks=rows), source_config=owned)
    assert "unsupported_networks" in _codes(_inspect(source, target))

    # With features.networks off, the listed networks belong to the default project.
    source, target = _patch(monkeypatch, resources=_resources([_instance("web-1")], networks=rows))
    assert "unsupported_networks" not in _codes(_inspect(source, target))


def test_cached_images_are_not_carried_and_only_warned(monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = _patch(
        monkeypatch, resources=_resources([_instance("web-1")], images=[{"fingerprint": "abc", "cached": True}])
    )

    result = _inspect(source, target)

    assert "unsupported_images" not in _codes(result)
    assert "cached_images_not_carried" in {warning["code"] for warning in result.warnings}


def test_an_alias_less_base_image_is_disposable_but_a_named_one_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    """LXD keeps the image an instance was created from without the cached flag."""
    instance = _instance("web-1", **{"volatile.base_image": "base1"})
    base = {"fingerprint": "base1", "cached": False, "aliases": []}
    source, target = _patch(monkeypatch, resources=_resources([instance], images=[base]))
    assert "unsupported_images" not in _codes(_inspect(source, target))

    named = {**base, "aliases": [{"name": "golden"}]}
    source, target = _patch(monkeypatch, resources=_resources([instance], images=[named]))
    assert "unsupported_images" in _codes(_inspect(source, target))


def test_a_project_over_the_size_limit_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = _patch(monkeypatch, resources=_resources([_instance(f"web-{n}") for n in range(3)]))

    result = preflight.inspect(
        scope="project",
        intent="move",
        source_client=source,
        target_client=target,
        source_cluster="cluster-a",
        source_project="web",
        source_name=None,
        target_cluster="cluster-b",
        target_project="web-new",
        target_name=None,
        target_member=None,
        target_pool=None,
        settings=SimpleNamespace(migration_project_max_instances=2),
        mappings={},
    )

    assert "project_too_large" in _codes(result)


def test_an_empty_project_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = _patch(monkeypatch, resources=_resources([]))

    assert "empty_project_relocation_noop" in _codes(_inspect(source, target))


def test_a_same_cluster_rename_is_a_relocation(monkeypatch: pytest.MonkeyPatch) -> None:
    source, _target = _patch(monkeypatch, resources=_resources([_instance("web-1")]))

    result = _inspect(source, source, target_cluster="cluster-a", target_project="web-renamed")

    assert result.strategy == "project_relocation"
    assert result.blockers == []


def test_member_relocation_skips_workloads_already_on_the_member(monkeypatch: pytest.MonkeyPatch) -> None:
    members = [
        {"server_name": "member-a", "status": "Online", "architecture": "x86_64"},
        {"server_name": "member-b", "status": "Online", "architecture": "x86_64"},
    ]
    source, _target = _patch(
        monkeypatch,
        resources=_resources(
            [_instance("web-1", status="Running"), _instance("web-2", location="member-b")]
        ),
        members=members,
    )

    result = _inspect(source, source, target_cluster="cluster-a", target_project="web", member="member-b")

    assert result.strategy == "member_relocation"
    assert result.blockers == []
    assert [row["source_name"] for row in result.resources if row["resource_type"] == "instance"] == ["web-1"]
    assert "instance_already_on_member" in {warning["code"] for warning in result.warnings}
    # Same identity on both sides: there is nothing to retain or fence for autostart.
    assert "source_autostart_fenced" not in {warning["code"] for warning in result.warnings}


def test_member_relocation_with_nothing_to_move_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    members = [{"server_name": "member-b", "status": "Online", "architecture": "x86_64"}]
    source, _target = _patch(
        monkeypatch, resources=_resources([_instance("web-1", location="member-b")]), members=members
    )

    result = _inspect(source, source, target_cluster="cluster-a", target_project="web", member="member-b")

    assert "empty_project_relocation_noop" in _codes(result)
