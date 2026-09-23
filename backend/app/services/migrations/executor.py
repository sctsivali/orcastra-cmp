"""Bounded synchronous daemon operations used by the migration worker.

This module never persists migration credentials. Cross-cluster pull secrets live only on the
stack between source setup and target acceptance.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.lxd_path import safe_segment
from app.services.lxd import mobility
from app.services.lxd import snapshots as snapshot_service

from .contracts import MigrationUnknown
from .security import (
    api_endpoint,
    canonical_json_hash,
    instance_identity_fingerprint as _instance_identity_fingerprint,
    validate_operation_url,
)


class MigrationOperationUnknown(MigrationUnknown):
    """A mutation was accepted but its final state is not yet known."""


def _metadata(response: Any) -> Any:
    payload = response.json()
    return payload.get("metadata") if isinstance(payload, dict) else None


def _operation(response: Any, client: Any) -> tuple[str, str]:
    payload = response.json()
    operation = payload.get("operation") if isinstance(payload, dict) else None
    if not operation:
        raise MigrationOperationUnknown(
            "The daemon mutation returned without a readable operation identity."
        )
    absolute, operation_id = validate_operation_url(str(operation), api_endpoint(client))
    return absolute, operation_id


def _required_operation_id(operation_url: Optional[str], client: Any) -> str:
    if not operation_url:
        raise MigrationOperationUnknown(
            "The daemon accepted the transfer without a readable operation identity."
        )
    _, operation_id = validate_operation_url(operation_url, api_endpoint(client))
    return operation_id


def read_instance(client: Any, *, project: str, name: str) -> Optional[dict[str, Any]]:
    try:
        metadata = _metadata(
            client.api.instances[safe_segment(name)].get(params={"project": project})
        )
    except Exception as exc:
        response = getattr(exc, "response", None)
        if response is not None and getattr(response, "status_code", None) == 404:
            return None
        raise
    return metadata if isinstance(metadata, dict) else None


def instance_fingerprint(instance: dict[str, Any]) -> str:
    """Hash migration-relevant state without persisting raw config or secret values."""
    config = {
        key: value
        for key, value in (instance.get("expanded_config") or instance.get("config") or {}).items()
        if not str(key).startswith("volatile.")
    }
    devices = instance.get("expanded_devices") or instance.get("devices") or {}
    return canonical_json_hash(
        {
            "type": instance.get("type"),
            "architecture": instance.get("architecture"),
            "profiles": instance.get("profiles") or [],
            "config": config,
            "devices": devices,
        }
    )


def instance_identity_fingerprint(instance: dict[str, Any]) -> Optional[str]:
    """Hash immutable, non-secret identity evidence for destructive cleanup checks."""
    return _instance_identity_fingerprint(instance)


def snapshot_inventory_fingerprint(client: Any, *, project: str, name: str) -> str:
    """Hash snapshot identities without persisting snapshot configuration or guest data."""
    metadata = _metadata(
        client.api.instances[safe_segment(name)].snapshots.get(
            params={"project": project, "recursion": 1}
        )
    )
    if not isinstance(metadata, list):
        raise MigrationOperationUnknown("Source snapshot inventory is unreadable.")
    inventory: list[dict[str, Any]] = []
    for row in metadata:
        if isinstance(row, str):
            snapshot_name = row.rstrip("/").rsplit("/", 1)[-1]
            inventory.append({"name": snapshot_name})
            continue
        if not isinstance(row, dict) or not row.get("name"):
            raise MigrationOperationUnknown("Source snapshot inventory contains an invalid row.")
        inventory.append(
            {
                "name": str(row["name"]),
                "created_at": row.get("created_at"),
                "expires_at": row.get("expires_at"),
                "stateful": bool(row.get("stateful", False)),
            }
        )
    inventory.sort(key=lambda row: str(row.get("name") or ""))
    return canonical_json_hash(inventory)


def instances_equivalent(source: dict[str, Any], target: dict[str, Any]) -> bool:
    """Conservative readback comparison that ignores generated volatile identity."""
    def stable_config(value: dict[str, Any]) -> dict[str, Any]:
        return {
            key: nested
            for key, nested in (
                value.get("expanded_config") or value.get("config") or {}
            ).items()
            if not str(key).startswith("volatile.")
        }

    def stable_devices(value: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        devices = value.get("expanded_devices") or value.get("devices") or {}
        for name, raw_device in devices.items():
            device = dict(raw_device or {})
            if device.get("type") == "disk" and device.get("path") == "/":
                device.pop("pool", None)
            device.pop("hwaddr", None)
            result[str(name)] = {
                key: nested
                for key, nested in device.items()
                if not str(key).startswith("volatile.")
            }
        return result

    return (
        source.get("type") == target.get("type")
        and source.get("architecture") == target.get("architecture")
        and (source.get("profiles") or []) == (target.get("profiles") or [])
        and stable_config(source) == stable_config(target)
        and stable_devices(source) == stable_devices(target)
    )


def read_project(client: Any, *, project: str) -> Optional[dict[str, Any]]:
    try:
        metadata = _metadata(client.api.projects[safe_segment(project)].get())
    except Exception as exc:
        response = getattr(exc, "response", None)
        if response is not None and getattr(response, "status_code", None) == 404:
            return None
        raise
    return metadata if isinstance(metadata, dict) else None


def _reviewed_project_payload(source: dict[str, Any], target_project: str) -> dict[str, Any]:
    """Return the exact project fields approved by preflight for a new namespace."""
    return {
        "name": target_project,
        "description": str(source.get("description") or ""),
        "config": dict(source.get("config") or {}),
    }


def submit_create_project(
    source_client: Any,
    target_client: Any,
    *,
    source_project: str,
    target_project: str,
) -> Optional[str]:
    source = read_project(source_client, project=source_project)
    if source is None:
        raise RuntimeError("The source project no longer exists.")
    response = target_client.api.projects.post(
        json=_reviewed_project_payload(source, target_project)
    )
    _, operation_id = _operation(response, target_client)
    return operation_id


def projects_equivalent(source: dict[str, Any], target: dict[str, Any]) -> bool:
    """Compare project policy that affects isolation, restrictions, and quotas."""
    return dict(source.get("config") or {}) == dict(target.get("config") or {})


def wait_operation(
    client: Any, *, operation_id: Optional[str], project: str, timeout: int = 20
) -> dict[str, Any]:
    if not operation_id:
        raise MigrationOperationUnknown(
            "A daemon mutation cannot be reconciled without an operation identity."
        )
    try:
        return snapshot_service.wait_for_operation(
            client, operation_id, project, timeout=timeout
        )
    except MigrationOperationUnknown:
        raise
    except Exception as exc:  # noqa: BLE001 - submission already crossed the mutation boundary
        raise MigrationOperationUnknown(
            "The daemon operation outcome could not be read and requires reconciliation."
        ) from exc


def change_instance_state(
    client: Any,
    *,
    project: str,
    name: str,
    action: str,
    timeout: int = 60,
) -> tuple[Optional[str], dict[str, Any]]:
    operation_id = submit_instance_state(
        client, project=project, name=name, action=action, timeout=timeout
    )
    return operation_id, wait_operation(
        client, operation_id=operation_id, project=project, timeout=timeout
    )


def submit_instance_state(
    client: Any,
    *,
    project: str,
    name: str,
    action: str,
    timeout: int = 60,
) -> Optional[str]:
    response = client.api.instances[safe_segment(name)].state.put(
        json={"action": action, "timeout": timeout, "force": False},
        params={"project": project},
    )
    _, operation_id = _operation(response, client)
    return operation_id


def create_checkpoint(
    client: Any, *, project: str, name: str, checkpoint_name: str
) -> tuple[Optional[str], dict[str, Any]]:
    operation_id = submit_checkpoint(
        client, project=project, name=name, checkpoint_name=checkpoint_name
    )
    return operation_id, wait_operation(
        client, operation_id=operation_id, project=project, timeout=60
    )


def submit_checkpoint(
    client: Any, *, project: str, name: str, checkpoint_name: str
) -> Optional[str]:
    response = client.api.instances[safe_segment(name)].snapshots.post(
        json={"name": checkpoint_name, "stateful": False},
        params={"project": project},
    )
    _, operation_id = _operation(response, client)
    return operation_id


def copy_instance(
    source_client: Any,
    target_client: Any,
    *,
    source_cluster: str,
    source_project: str,
    source_name: str,
    target_cluster: str,
    target_project: str,
    target_name: str,
    target_member: Optional[str],
    target_pool: Optional[str],
) -> tuple[str, Optional[str], dict[str, Any]]:
    transport, operation_id, operation_client = submit_copy_instance(
        source_client,
        target_client,
        source_cluster=source_cluster,
        source_project=source_project,
        source_name=source_name,
        target_cluster=target_cluster,
        target_project=target_project,
        target_name=target_name,
        target_member=target_member,
        target_pool=target_pool,
    )
    outcome = wait_operation(
        operation_client,
        operation_id=operation_id,
        project=target_project,
        timeout=20,
    )
    return transport, operation_id, outcome


def submit_copy_instance(
    source_client: Any,
    target_client: Any,
    *,
    source_cluster: str,
    source_project: str,
    source_name: str,
    target_cluster: str,
    target_project: str,
    target_name: str,
    target_member: Optional[str],
    target_pool: Optional[str],
) -> tuple[str, str, Any]:
    transport = mobility.choose_transport(source_cluster, target_cluster)
    if transport == mobility.TRANSPORT_COPY:
        operation_url = mobility.copy_within_cluster(
            source_client,
            source_name=source_name,
            source_project=source_project,
            target_name=target_name,
            target_project=target_project,
            target_member=target_member,
            target_pool=target_pool,
        )
        operation_client = source_client
    else:
        certificate = mobility.server_certificate(source_client)
        definition = mobility.read_instance_definition(
            source_client, source_name, source_project
        )
        source_operation, secrets = mobility.begin_source_migration(
            source_client,
            instance_name=source_name,
            project=source_project,
        )
        validate_operation_url(source_operation, api_endpoint(source_client))
        operation_url = mobility.accept_migration_on_target(
            target_client,
            target_name=target_name,
            target_project=target_project,
            source_operation_url=source_operation,
            source_certificate=certificate,
            secrets=secrets,
            target_member=target_member,
            target_pool=target_pool,
            definition=definition,
        )
        operation_client = target_client
    operation_id = _required_operation_id(operation_url, operation_client)
    return transport, operation_id, operation_client


def prepare_cross_cluster_source(
    source_client: Any,
    *,
    source_project: str,
    source_name: str,
) -> tuple[dict[str, Any], str]:
    """Open the source side and return an in-memory handoff plus its durable op id."""
    certificate = mobility.server_certificate(source_client)
    definition = mobility.read_instance_definition(
        source_client, source_name, source_project
    )
    source_operation, secrets = mobility.begin_source_migration(
        source_client,
        instance_name=source_name,
        project=source_project,
    )
    _, source_operation_id = validate_operation_url(
        source_operation, api_endpoint(source_client)
    )
    return (
        {
            "source_operation_url": source_operation,
            "source_certificate": certificate,
            "definition": definition,
            "secrets": secrets,
        },
        source_operation_id,
    )


def submit_cross_cluster_target(
    target_client: Any,
    *,
    target_project: str,
    target_name: str,
    target_member: Optional[str],
    target_pool: Optional[str],
    handoff: dict[str, Any],
) -> str:
    """Accept a prepared pull handoff; callers must never persist ``handoff``."""
    operation_url = mobility.accept_migration_on_target(
        target_client,
        target_name=target_name,
        target_project=target_project,
        source_operation_url=str(handoff["source_operation_url"]),
        source_certificate=str(handoff["source_certificate"]),
        secrets=dict(handoff["secrets"]),
        target_member=target_member,
        target_pool=target_pool,
        definition=dict(handoff["definition"]),
    )
    return _required_operation_id(operation_url, target_client)


def relocate_member(
    client: Any,
    *,
    project: str,
    name: str,
    target_member: str,
) -> tuple[Optional[str], dict[str, Any]]:
    operation_id = submit_member_relocation(
        client, project=project, name=name, target_member=target_member
    )
    return operation_id, wait_operation(
        client, operation_id=operation_id, project=project, timeout=20
    )


def submit_member_relocation(
    client: Any, *, project: str, name: str, target_member: str
) -> Optional[str]:
    response = client.api.instances[safe_segment(name)].post(
        json={"migration": True, "live": False},
        params={"project": project, "target": target_member},
    )
    _, operation_id = _operation(response, client)
    return operation_id


def delete_instance(
    client: Any, *, project: str, name: str
) -> tuple[Optional[str], dict[str, Any]]:
    operation_id = submit_delete_instance(client, project=project, name=name)
    return operation_id, wait_operation(
        client, operation_id=operation_id, project=project, timeout=60
    )


def submit_delete_instance(client: Any, *, project: str, name: str) -> Optional[str]:
    response = client.api.instances[safe_segment(name)].delete(params={"project": project})
    _, operation_id = _operation(response, client)
    return operation_id


def restore_checkpoint(
    client: Any, *, project: str, name: str, checkpoint_name: str
) -> tuple[Optional[str], dict[str, Any]]:
    operation_id = submit_restore_checkpoint(
        client, project=project, name=name, checkpoint_name=checkpoint_name
    )
    return operation_id, wait_operation(
        client, operation_id=operation_id, project=project, timeout=60
    )


def submit_restore_checkpoint(
    client: Any, *, project: str, name: str, checkpoint_name: str
) -> Optional[str]:
    response = client.api.instances[safe_segment(name)].put(
        json={"restore": checkpoint_name}, params={"project": project}
    )
    _, operation_id = _operation(response, client)
    return operation_id


def read_operation(client: Any, *, operation_id: str, project: str) -> dict[str, Any]:
    metadata = _metadata(
        client.api.operations[safe_segment(operation_id)].get(params={"project": project})
    )
    if not isinstance(metadata, dict):
        raise MigrationOperationUnknown("Operation metadata is unreadable.")
    status = metadata.get("status")
    raw_status_code = metadata.get("status_code")
    try:
        status_code = int(raw_status_code) if raw_status_code is not None else None
    except (TypeError, ValueError):
        status_code = None
    if status_code in {200, 400, 401}:
        settled = True
        failed = status_code in {400, 401}
    elif status_code is not None:
        settled = False
        failed = False
    else:
        # Compatibility only for older daemons that omit the numeric operation contract.
        settled = status in {"Success", "Failure", "Cancelled", "Canceled"}
        failed = status in {"Failure", "Cancelled", "Canceled"}
    return {
        "status": status,
        "status_code": status_code,
        "err": metadata.get("err") or None,
        "settled": settled,
        "failed": failed,
    }
