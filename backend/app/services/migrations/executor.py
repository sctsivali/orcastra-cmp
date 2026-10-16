"""Bounded synchronous daemon operations used by the migration worker.

This module never persists migration credentials. Cross-cluster pull secrets live only on the
stack between source setup and target acceptance.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.lxd_path import safe_segment
from app.services.lxd import mobility
from app.services.lxd import snapshots as snapshot_service

from . import project_policy
from .contracts import MigrationUnknown
from .security import (
    api_endpoint,
    canonical_json_hash,
    instance_identity_fingerprint as _instance_identity_fingerprint,
    validate_operation_url,
)


class MigrationOperationUnknown(MigrationUnknown):
    """A mutation was accepted but its final state is not yet known."""


class OperationForgotten(MigrationOperationUnknown):
    """The daemon no longer lists the operation, usually because it finished and was purged."""


def _is_not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    return response is not None and getattr(response, "status_code", None) == 404


def is_definitive_rejection(exc: Exception) -> bool:
    """Whether a daemon answered a mutation with a client error, so nothing was accepted.

    A 4xx answer to a POST or PUT is a synchronous refusal: the daemon validated the request and
    created no operation. Timeouts, connection errors and 5xx answers stay ambiguous.
    """
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None) if response is not None else None
    return isinstance(status_code, int) and 400 <= status_code < 500


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


_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?$")


def _whole_second(value: Any) -> Any:
    """A snapshot timestamp at whole-second UTC precision.

    note: a cross-cluster migration carries snapshot times in whole seconds, so the target
    reads ``08:05:55Z`` where the source reads ``08:05:55.928374805Z`` (found live). Comparing
    at second precision still pins every snapshot's identity.
    """
    if not isinstance(value, str):
        return value
    match = _TIMESTAMP.match(value.strip())
    if match is None:
        return value
    base, zone = match.groups()
    moment = datetime.fromisoformat(base + ("+00:00" if zone in (None, "Z") else zone))
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
                "created_at": _whole_second(row.get("created_at")),
                "expires_at": _whole_second(row.get("expires_at")),
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


def _reviewed_project_payload(
    source: dict[str, Any], target_project: str, *, job_id: Optional[str] = None
) -> dict[str, Any]:
    """Return the exact project fields approved by preflight for a new namespace."""
    return {
        "name": target_project,
        "description": str(source.get("description") or ""),
        "config": project_policy.clone_config(source, job_id=job_id),
    }


def _sync_success(response: Any) -> bool:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        return False
    return (
        isinstance(payload, dict)
        and payload.get("type") == "sync"
        and payload.get("status_code") == 200
    )


def submit_create_project(
    source_client: Any,
    target_client: Any,
    *,
    source_project: str,
    target_project: str,
    job_id: Optional[str] = None,
) -> Optional[str]:
    """Create the target project, returning an operation id or None for a synchronous create."""
    source = read_project(source_client, project=source_project)
    if source is None:
        raise RuntimeError("The source project no longer exists.")
    response = target_client.api.projects.post(
        json=_reviewed_project_payload(source, target_project, job_id=job_id)
    )
    # LXD answers a project create synchronously. Only an async daemon returns an operation.
    if _sync_success(response):
        return None
    _, operation_id = _operation(response, target_client)
    return operation_id


def delete_project(client: Any, *, project: str) -> None:
    """Delete an empty project. LXD answers synchronously and refuses a non-empty one."""
    client.api.projects[safe_segment(project)].delete()


def projects_equivalent(source: dict[str, Any], target: dict[str, Any]) -> bool:
    """Compare project policy that affects isolation, restrictions, and quotas."""
    return project_policy.projects_equivalent(source, target)


def wait_operation(
    client: Any,
    *,
    operation_id: Optional[str],
    project: str,
    timeout: int = 20,
    wait_seconds: Optional[int] = None,
) -> dict[str, Any]:
    """Ask the daemon to hold the response until the operation settles or the wait ends.

    note: callers going through ``_blocking`` must pass ``wait_seconds``. ``_blocking`` consumes
    ``timeout`` as its own wall-clock limit, so ``timeout`` never reaches this function there.
    """
    if wait_seconds is not None:
        timeout = wait_seconds
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
        if _is_not_found(exc):
            raise OperationForgotten(
                "The daemon no longer lists this operation. Target readback decides its outcome."
            ) from exc
        # important: LXD answers the wait of a FAILED operation with an error-typed body that
        # carries the operation's own error (verified on LXD 5.21: a VM stop past its timeout).
        # One authoritative read of the operation tells that definitive failure apart from an
        # outcome that really could not be read.
        try:
            outcome = read_operation(client, operation_id=operation_id, project=project)
        except OperationForgotten:
            raise
        except Exception:  # noqa: BLE001
            outcome = None
        if outcome is not None and outcome.get("settled"):
            return outcome
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
    force: bool = False,
    action_timeout: Optional[int] = None,
) -> Optional[str]:
    """Submit a state change. ``action_timeout`` is the guest shutdown budget for a stop.

    note: callers going through ``_blocking`` must use ``action_timeout``, for the same reason
    as ``wait_operation``.
    """
    if action_timeout is not None:
        timeout = action_timeout
    response = client.api.instances[safe_segment(name)].state.put(
        json={"action": action, "timeout": timeout, "force": force},
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
    definition: Optional[dict[str, Any]] = None,
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
            target_pool=None if definition is not None else target_pool,
            definition=definition,
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
    definition: Optional[dict[str, Any]] = None,
) -> str:
    """Accept a prepared pull handoff. Callers must never persist ``handoff``."""
    operation_url = mobility.accept_migration_on_target(
        target_client,
        target_name=target_name,
        target_project=target_project,
        source_operation_url=str(handoff["source_operation_url"]),
        source_certificate=str(handoff["source_certificate"]),
        secrets=dict(handoff["secrets"]),
        target_member=target_member,
        target_pool=None if definition is not None else target_pool,
        definition=dict(definition if definition is not None else handoff["definition"]),
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


def patch_instance_config(
    client: Any, *, project: str, name: str, config: dict[str, str]
) -> Optional[str]:
    """Merge config keys into one instance. Returns an operation id when the daemon gives one."""
    response = client.api.instances[safe_segment(name)].patch(
        json={"config": dict(config)}, params={"project": project}
    )
    if _sync_success(response):
        return None
    _, operation_id = _operation(response, client)
    return operation_id


def replace_instance_devices(
    client: Any,
    *,
    project: str,
    name: str,
    devices: dict[str, Any],
    expected_devices: dict[str, Any],
    config_updates: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """Replace one instance's local devices, keeping everything else as it is.

    A PATCH cannot remove a device, so this is a read then a PUT of the full writable record.
    ``devices`` was computed from ``expected_devices``: the read here must still show exactly
    those, and its ETag guards the PUT, so an edit made in between is refused, never reverted.
    """
    node = client.api.instances[safe_segment(name)]
    response = node.get(params={"project": project})
    current = _metadata(response)
    if not isinstance(current, dict):
        raise MigrationOperationUnknown(f"Instance '{name}' could not be read for a device update.")
    if (current.get("devices") or {}) != expected_devices:
        raise MigrationOperationUnknown(f"The devices of '{name}' changed before they could be updated.")
    etag = (getattr(response, "headers", None) or {}).get("ETag")
    if not etag:
        raise MigrationOperationUnknown(f"Instance '{name}' answered without an ETag; nothing was written.")
    body = {
        key: current.get(key)
        for key in ("architecture", "config", "description", "ephemeral", "profiles", "stateful")
        if key in current
    }
    body["devices"] = devices
    if config_updates:
        body["config"] = {**(current.get("config") or {}), **config_updates}
    result = node.put(json=body, params={"project": project}, headers={"If-Match": etag})
    if _sync_success(result):
        return None
    _, operation_id = _operation(result, client)
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
    try:
        response = client.api.operations[safe_segment(operation_id)].get(
            params={"project": project}
        )
    except Exception as exc:
        if _is_not_found(exc):
            raise OperationForgotten("The daemon no longer lists this operation.") from exc
        raise
    metadata = _metadata(response)
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
