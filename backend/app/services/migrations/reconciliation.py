"""Read-only reconciliation of ambiguous daemon operations and state."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from app.core.lxd_path import safe_segment
from app.models.migration_models import MigrationJob
from app.services.lxd.root_disk import root_pool

from . import executor, preflight, project_policy, repository
from .completion import target_completion_events
from .context import _fenced_job_update, _resource_update, _resources
from .contracts import _blocking, logger
from .reconcile_transfer import reconcile_staged
from .security import (
    MigrationSecurityError,
    api_endpoint,
    sanitize_error,
    sanitize_payload,
    validate_operation_url,
)


class OperationInventoryError(RuntimeError):
    """The daemon did not provide unique, trustworthy operation evidence."""


@dataclass(frozen=True)
class _OperationEvidence:
    seen: bool = False
    settled: bool = False
    failed: bool = False
    operation_id: str | None = None
    # The daemon no longer lists the recorded operation, so it is not in flight any more.
    forgotten: bool = False


_OPERATION_DESCRIPTIONS: dict[str, frozenset[str]] = {
    "transfer": frozenset({"Creating instance", "Copying instance", "Migrating instance"}),
    "source_transfer": frozenset({"Migrating instance"}),
    "stop_source": frozenset({"Stopping instance"}),
    "checkpoint": frozenset({"Snapshotting instance"}),
    "cleanup_source": frozenset({"Deleting instance"}),
    "rollback_target_delete": frozenset({"Deleting instance"}),
    "restore_checkpoint": frozenset({"Restoring snapshot"}),
    "restore_source_start": frozenset({"Starting instance"}),
}
_INTENT_TIMESTAMPS = {
    "transfer": "transfer_submission_at",
    "source_transfer": "source_submission_at",
    "stop_source": "stop_submission_at",
    "checkpoint": "checkpoint_submission_at",
    "cleanup_source": "cleanup_submission_at",
    "rollback_target_delete": "rollback_delete_submission_at",
    "restore_checkpoint": "restore_submission_at",
    "restore_source_start": "restore_start_submission_at",
}

_MAX_AUTOMATIC_RECONCILIATIONS = 5
_RECONCILIATION_BASE_DELAY_SECONDS = 30
_RECONCILIATION_MAX_DELAY_SECONDS = 15 * 60


def _reconciliation_backoff_values(
    job: MigrationJob,
    *,
    error_code: str,
    error_message: str,
) -> dict[str, Any]:
    """Return a durable, deterministic not-before transition for unresolved state."""
    safe_result = dict(getattr(job, "safe_result", None) or {})
    raw_streak = safe_result.get("reconciliation_streak", 0)
    streak = (
        raw_streak
        if isinstance(raw_streak, int) and not isinstance(raw_streak, bool)
        else 0
    )
    next_streak = max(0, streak) + 1
    safe_result["reconciliation_streak"] = next_streak
    if next_streak >= _MAX_AUTOMATIC_RECONCILIATIONS:
        safe_result.pop("reconciliation_retry_delay_seconds", None)
        return {
            "phase": "manual_recovery",
            "status": "manual_recovery",
            "locked_by": None,
            "lease_expires_at": None,
            "safe_result": sanitize_payload(safe_result),
            "error_code": "reconciliation_attempts_exhausted",
            "error_message": (
                "Automatic reconciliation stopped after repeated inconclusive authoritative "
                f"readback. Last result: {error_message}"
            ),
            "event_message": (
                "Automatic reconciliation reached its bounded attempt limit; resource locks "
                "remain held for operator recheck."
            ),
        }
    exponential = min(
        _RECONCILIATION_MAX_DELAY_SECONDS,
        _RECONCILIATION_BASE_DELAY_SECONDS * (2 ** (next_streak - 1)),
    )
    digest = hashlib.sha256(
        f"{job.job_id}:{next_streak}".encode("utf-8")
    ).digest()
    jitter_basis_points = int.from_bytes(digest[:2], "big") % 3001 - 1500
    delay_seconds = max(
        1,
        round(exponential * (10_000 + jitter_basis_points) / 10_000),
    )
    safe_result["reconciliation_retry_delay_seconds"] = delay_seconds
    return {
        "phase": "unknown",
        "status": "unknown",
        "locked_by": None,
        "lease_expires_at": repository.utcnow() + timedelta(seconds=delay_seconds),
        "safe_result": sanitize_payload(safe_result),
        "error_code": error_code,
        "error_message": error_message,
        "event_message": (
            "Migration remains active; authoritative reconciliation was deferred with bounded "
            "backoff."
        ),
    }


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise OperationInventoryError("The persisted mutation timestamp is unavailable.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise OperationInventoryError("The persisted mutation timestamp is invalid.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _operation_outcome(metadata: dict[str, Any]) -> dict[str, Any]:
    status_code = metadata.get("status_code")
    if not isinstance(status_code, int):
        raise OperationInventoryError("Operation status metadata is invalid.")
    if status_code in {200, 400, 401}:
        return {"settled": True, "failed": status_code in {400, 401}}
    if 100 <= status_code < 200:
        return {"settled": False, "failed": False}
    raise OperationInventoryError("Operation status metadata is invalid.")


def _inventory_rows(client: Any, *, project: str) -> list[dict[str, Any]]:
    """Read every operation bucket without turning malformed rows into absence."""
    try:
        response = client.api.operations.get(
            params={"project": project, "recursion": 1}
        )
        status_code = getattr(response, "status_code", 200)
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise OperationInventoryError("Operation inventory is unavailable.") from exc
    if status_code != 200 or not isinstance(payload, dict):
        raise OperationInventoryError("Operation inventory is unavailable.")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise OperationInventoryError("Operation inventory is malformed.")
    rows: list[dict[str, Any]] = []
    for bucket, raw_rows in metadata.items():
        if not isinstance(bucket, str) or not isinstance(raw_rows, list):
            raise OperationInventoryError("Operation inventory is malformed.")
        for row in raw_rows:
            if not isinstance(row, dict):
                raise OperationInventoryError("Operation inventory is malformed.")
            operation_id = row.get("id")
            if (
                not isinstance(operation_id, str)
                or not operation_id
                or row.get("class") != "task"
                or not isinstance(row.get("description"), str)
                or not row.get("description")
                or not isinstance(row.get("resources"), dict)
            ):
                raise OperationInventoryError("Operation inventory is malformed.")
            try:
                validate_operation_url(
                    f"/1.0/operations/{operation_id}", api_endpoint(client)
                )
            except MigrationSecurityError as exc:
                raise OperationInventoryError(
                    "Operation inventory contains an invalid operation identity."
                ) from exc
            _operation_outcome(row)
            rows.append(row)
    return rows


def _resource_identity(
    client: Any, operation: dict[str, Any], *, raw_url: Any
) -> tuple[str, str, str, str | None]:
    """Return (kind, project, name, snapshot) for one registered-origin URL."""
    if not isinstance(raw_url, str) or not raw_url or len(raw_url) > 4096:
        raise OperationInventoryError("Operation resource metadata is malformed.")
    endpoint = api_endpoint(client)
    absolute = urljoin(endpoint.rstrip("/") + "/", raw_url)
    base = urlparse(endpoint)
    parsed = urlparse(absolute)
    base_port = base.port or (443 if base.scheme == "https" else 80)
    parsed_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if (
        parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.scheme != base.scheme
        or (parsed.hostname or "").lower() != (base.hostname or "").lower()
        or parsed_port != base_port
    ):
        raise OperationInventoryError(
            "Operation resource does not belong to the registered cluster origin."
        )
    segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
    if (
        len(segments) < 3
        or segments[0] != "1.0"
        or any(segment in {".", ".."} or "/" in segment for segment in segments)
    ):
        raise OperationInventoryError("Operation resource path is malformed.")
    root = segments[1]
    if root not in {"instances", "containers", "virtual-machines", "projects"}:
        raise OperationInventoryError("Operation resource type is not supported.")
    try:
        query = parse_qs(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=16,
        )
    except ValueError as exc:
        raise OperationInventoryError("Operation resource query is malformed.") from exc
    if any(key not in {"project", "target"} for key in query):
        raise OperationInventoryError("Operation resource query is ambiguous.")
    project_values = query.get("project", [])
    if len(project_values) > 1 or (project_values and not project_values[0].strip()):
        raise OperationInventoryError("Operation resource project is ambiguous.")
    operation_project = operation.get("project")
    if operation_project is not None and (
        not isinstance(operation_project, str) or not operation_project.strip()
    ):
        raise OperationInventoryError("Operation project metadata is malformed.")
    project = (
        project_values[0].strip()
        if project_values
        else str(operation_project or "default").strip()
    )
    snapshot: str | None = None
    if root == "projects":
        name = segments[2]
        project = name
    else:
        name = segments[2]
        if len(segments) > 3:
            if len(segments) != 5 or segments[3] not in {"snapshots", "backups"}:
                raise OperationInventoryError("Operation resource path is ambiguous.")
            snapshot = segments[4]
    return root, project, name, snapshot


def _operation_touches_instance(
    client: Any,
    operation: dict[str, Any],
    *,
    project: str,
    name: str,
    expected_resources: set[tuple[str, str]],
    checkpoint_name: str | None = None,
) -> bool:
    observed: set[tuple[str, str]] = set()
    for kind, urls in operation["resources"].items():
        if not isinstance(kind, str) or not isinstance(urls, list) or not urls:
            raise OperationInventoryError("Operation resource metadata is malformed.")
        for raw_url in urls:
            root, owner, resource_name, snapshot = _resource_identity(
                client, operation, raw_url=raw_url
            )
            normalized_kind = kind.replace("_", "-")
            if root == "projects":
                raise OperationInventoryError(
                    "Operation references an unexpected project resource."
                )
            if normalized_kind not in {
                "instances",
                "containers",
                "virtual-machines",
                "instance-snapshots",
                "instance-backups",
            }:
                raise OperationInventoryError("Operation resource kind is ambiguous.")
            if checkpoint_name is not None and snapshot not in {None, checkpoint_name}:
                raise OperationInventoryError(
                    "Operation references a different checkpoint."
                )
            observed.add((owner, resource_name))
    return observed == expected_resources and (project, name) in observed


def _discover_operation(
    client: Any,
    *,
    project: str,
    name: str,
    kind: str,
    intent_at: Any,
    expected_resources: set[tuple[str, str]],
    checkpoint_name: str | None = None,
    target_member: str | None = None,
) -> tuple[str, dict[str, Any]]:
    submitted_at = _parse_timestamp(intent_at)
    candidates: list[dict[str, Any]] = []
    for operation in _inventory_rows(client, project=project):
        if operation["description"] not in _OPERATION_DESCRIPTIONS[kind]:
            continue
        created_at = _parse_timestamp(operation.get("created_at"))
        # A missing or wide causal window is ambiguity, not permission to adopt history.
        if not (
            submitted_at - timedelta(seconds=5)
            <= created_at
            <= submitted_at + timedelta(seconds=90)
        ):
            continue
        if target_member is not None:
            inner_metadata = operation.get("metadata")
            location = operation.get("location")
            if location is None and isinstance(inner_metadata, dict):
                location = inner_metadata.get("location")
            if location != target_member:
                continue
        if _operation_touches_instance(
            client,
            operation,
            project=project,
            name=name,
            expected_resources=expected_resources,
            checkpoint_name=checkpoint_name,
        ):
            candidates.append(operation)
    if len(candidates) != 1:
        raise OperationInventoryError(
            "The mutation operation cannot be correlated uniquely."
        )
    operation = candidates[0]
    return str(operation["id"]), _operation_outcome(operation)


async def _operation_evidence(
    resource: Any,
    *,
    client: Any,
    cluster_id: str,
    project: str,
    name: str,
    kind: str,
    intent_key: str,
    expected_resources: set[tuple[str, str]] | None = None,
    checkpoint_name: str | None = None,
    target_member: str | None = None,
) -> _OperationEvidence:
    state = dict(resource.safe_result or {})
    if not state.get(intent_key):
        return _OperationEvidence()
    refs = [ref for ref in (resource.operation_refs or []) if ref.get("kind") == kind]
    if len(refs) > 1:
        return _OperationEvidence(seen=True)
    if refs:
        ref = refs[0]
        if (
            ref.get("cluster_id") != cluster_id
            or ref.get("project") != project
            or not isinstance(ref.get("operation_id"), str)
            or not ref.get("operation_id")
        ):
            return _OperationEvidence(seen=True)
        try:
            outcome = await _blocking(
                executor.read_operation,
                client,
                operation_id=str(ref["operation_id"]),
                project=project,
                label=f"migration.reconcile_{kind}[{ref['operation_id']}]",
            )
        except executor.OperationForgotten:
            return _OperationEvidence(
                seen=True, operation_id=str(ref["operation_id"]), forgotten=True
            )
        except Exception as exc:  # noqa: BLE001
            logger.info(
                "Could not read migration operation %s during reconciliation: %s",
                ref.get("operation_id"),
                sanitize_error(exc),
            )
            return _OperationEvidence(seen=True)
        return _OperationEvidence(
            seen=True,
            settled=bool(outcome.get("settled")),
            failed=bool(outcome.get("failed")),
            operation_id=str(ref["operation_id"]),
        )
    try:
        operation_id, outcome = await _blocking(
            _discover_operation,
            client,
            project=project,
            name=name,
            kind=kind,
            intent_at=state.get(_INTENT_TIMESTAMPS[kind]),
            expected_resources=expected_resources or {(project, name)},
            checkpoint_name=checkpoint_name,
            target_member=target_member,
            label=f"migration.discover_{kind}[{name}]",
        )
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "Could not correlate %s operation for %s: %s",
            kind,
            name,
            sanitize_error(exc),
        )
        return _OperationEvidence()
    # LXD and Incus transfer submissions do not accept an Orcastra correlation token. A
    # same-project operation with the same generic description can be submitted later through
    # a direct daemon client, so inventory discovery alone cannot adopt a primary data transfer.
    if kind in {"transfer", "source_transfer"}:
        return _OperationEvidence()
    await _resource_update(
        resource.resource_id,
        state=resource.state,
        operation_ref={
            "kind": kind,
            "cluster_id": cluster_id,
            "project": project,
            "operation_id": operation_id,
        },
        safe_result=state,
    )
    return _OperationEvidence(
        seen=True,
        settled=bool(outcome.get("settled")),
        failed=bool(outcome.get("failed")),
        operation_id=operation_id,
    )


def _snapshot_names(client: Any, *, project: str, name: str) -> set[str]:
    try:
        response = client.api.instances[safe_segment(name)].snapshots.get(
            params={"project": project, "recursion": 1}
        )
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        raise OperationInventoryError("Snapshot inventory is unavailable.") from exc
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if not isinstance(metadata, list):
        raise OperationInventoryError("Snapshot inventory is malformed.")
    names: list[str] = []
    for row in metadata:
        raw_name = row.get("name") if isinstance(row, dict) else None
        if not isinstance(raw_name, str) or not raw_name:
            raise OperationInventoryError("Snapshot inventory is malformed.")
        names.append(raw_name)
    if len(names) != len(set(names)):
        raise OperationInventoryError("Snapshot inventory is ambiguous.")
    return set(names)


def _exact_instance(
    resource: Any,
    instance: dict[str, Any] | None,
    *,
    identity_key: str,
    definition_key: str,
    expected_status: str | None = None,
) -> bool:
    state = resource.safe_result or {}
    if instance is None:
        return False
    expected_identity = state.get(identity_key)
    expected_definition = state.get(definition_key)
    if (
        not isinstance(expected_identity, str)
        or not expected_identity
        or executor.instance_identity_fingerprint(instance) != expected_identity
        or not isinstance(expected_definition, str)
        or not expected_definition
        or executor.instance_fingerprint(instance) != expected_definition
    ):
        return False
    return expected_status is None or (
        str(instance.get("status") or "").lower() == expected_status
    )


async def _mark_reconciliation_unknown(
    job: MigrationJob,
    worker_id: str,
    *,
    failed: bool = False,
    message: str = "Source, target, and operation state are not yet safely reconciled.",
) -> None:
    values = _reconciliation_backoff_values(
        job,
        error_code="daemon_operation_failed" if failed else "ambiguous_daemon_state",
        error_message=message,
    )
    await _fenced_job_update(
        job,
        worker_id,
        **values,
    )


async def _reconcile_project_creation(
    job: MigrationJob,
    worker_id: str,
    source_client: Any,
    target_client: Any,
    resources: list[Any],
) -> bool:
    project_resources = [
        resource
        for resource in resources
        if resource.resource_type == "project"
        and (resource.safe_result or {}).get("target_project_create_intent")
    ]
    if not project_resources:
        return False
    if len(project_resources) != 1:
        await _mark_reconciliation_unknown(job, worker_id)
        return True
    resource = project_resources[0]
    try:
        source = await _blocking(
            executor.read_project,
            source_client,
            project=job.source_project,
            label="migration.reconcile_created_source_project",
        )
        target = await _blocking(
            executor.read_project,
            target_client,
            project=job.target_project,
            label="migration.reconcile_created_target_project",
        )
        inventory = await _blocking(
            preflight._known_project_resources,
            target_client,
            job.target_project,
            label="migration.reconcile_created_target_inventory",
        )
        occupied = preflight._occupied_resources(inventory)
    except Exception as exc:  # noqa: BLE001
        logger.info("Could not verify created target project: %s", sanitize_error(exc))
        await _mark_reconciliation_unknown(job, worker_id)
        return True
    approved_source_hash = (resource.source_ref or {}).get("project_fingerprint")
    if (
        source is None
        or target is None
        or not isinstance(approved_source_hash, str)
        or not approved_source_hash
        or preflight._project_fingerprint(source) != approved_source_hash
        or not executor.projects_equivalent(source, target)
        or not project_policy.created_by_job(target, job.job_id)
        or occupied
    ):
        await _resource_update(resource.resource_id, state="unknown")
        await _mark_reconciliation_unknown(job, worker_id)
        return True
    state = {
        **(resource.safe_result or {}),
        "target_project_created": True,
        "target_project_reconciled": True,
        "source_project_hash": approved_source_hash,
    }
    await _resource_update(resource.resource_id, state="ready", safe_result=state)
    await _fenced_job_update(
        job,
        worker_id,
        phase="preparing_target",
        status="running",
        locked_by=None,
        lease_expires_at=None,
        error_code=None,
        error_message=None,
        event_message="Target project creation was reconciled and can continue safely.",
    )
    return True


async def _reconcile_pretransfer(
    job: MigrationJob,
    worker_id: str,
    source_client: Any,
    resources: list[Any],
) -> bool:
    instances = [resource for resource in resources if resource.resource_type == "instance"]
    if not any(
        (resource.safe_result or {}).get("stop_submission_intent")
        or (resource.safe_result or {}).get("checkpoint_submission_intent")
        for resource in instances
    ):
        return False
    if any(
        (resource.safe_result or {}).get(key)
        for resource in instances
        for key in (
            "transfer_submission_intent",
            "source_submission_intent",
            "target_submission_intent",
        )
    ):
        return False
    continuation_phase = "quiescing"
    for resource in instances:
        state = dict(resource.safe_result or {})
        name = str((resource.source_ref or {}).get("name") or "")
        source = await _blocking(
            executor.read_instance,
            source_client,
            project=job.source_project,
            name=name,
            label=f"migration.reconcile_pretransfer_source[{name}]",
        )
        if (
            state.get("stop_submission_intent")
            and state.get("original_status") == "running"
            and _exact_instance(
                resource,
                source,
                identity_key="source_identity_hash",
                definition_key="source_definition_hash",
                expected_status="running",
            )
        ):
            # The stop never took effect (a guest that ignored its shutdown, for example) and no
            # transfer was submitted, so nothing changed. Only a stop still in flight waits.
            evidence = await _operation_evidence(
                resource,
                client=source_client,
                cluster_id=job.source_cluster_id,
                project=job.source_project,
                name=name,
                kind="stop_source",
                intent_key="stop_submission_intent",
            )
            if evidence.seen and not evidence.settled and not evidence.forgotten:
                await _resource_update(resource.resource_id, state="unknown", safe_result=state)
                await _mark_reconciliation_unknown(job, worker_id)
                return True
            await _fenced_job_update(
                job,
                worker_id,
                phase="rolling_back",
                status="running",
                locked_by=None,
                lease_expires_at=None,
                safe_result=sanitize_payload(
                    {
                        **(job.safe_result or {}),
                        "recovery_kind": "abort",
                        "abort_code": "source_stop_failed",
                        "abort_reason": sanitize_error(
                            f"Source '{name}' did not shut down, so nothing was changed."
                        ),
                    }
                ),
                event_message="The source did not stop, so nothing was changed. Ending the migration.",
            )
            return True
        if not _exact_instance(
            resource,
            source,
            identity_key="source_identity_hash",
            definition_key="source_definition_hash",
            expected_status="stopped",
        ):
            await _resource_update(resource.resource_id, state="unknown", safe_result=state)
            await _mark_reconciliation_unknown(job, worker_id)
            return True
        if state.get("stop_submission_intent"):
            evidence = await _operation_evidence(
                resource,
                client=source_client,
                cluster_id=job.source_cluster_id,
                project=job.source_project,
                name=name,
                kind="stop_source",
                intent_key="stop_submission_intent",
            )
            if not evidence.seen or not evidence.settled or evidence.failed:
                await _resource_update(resource.resource_id, state="unknown", safe_result=state)
                await _mark_reconciliation_unknown(
                    job, worker_id, failed=evidence.failed
                )
                return True
            state["stop_reconciled"] = True
        if state.get("checkpoint_submission_intent"):
            continuation_phase = "checkpointing"
            checkpoint_name = state.get("checkpoint_name")
            if not isinstance(checkpoint_name, str) or not checkpoint_name:
                await _mark_reconciliation_unknown(job, worker_id)
                return True
            evidence = await _operation_evidence(
                resource,
                client=source_client,
                cluster_id=job.source_cluster_id,
                project=job.source_project,
                name=name,
                kind="checkpoint",
                intent_key="checkpoint_submission_intent",
                checkpoint_name=checkpoint_name,
            )
            try:
                snapshot_names = await _blocking(
                    _snapshot_names,
                    source_client,
                    project=job.source_project,
                    name=name,
                    label=f"migration.reconcile_checkpoint_inventory[{name}]",
                )
                snapshot_hash = await _blocking(
                    executor.snapshot_inventory_fingerprint,
                    source_client,
                    project=job.source_project,
                    name=name,
                    label=f"migration.reconcile_checkpoint_hash[{name}]",
                )
            except Exception:  # noqa: BLE001
                await _mark_reconciliation_unknown(job, worker_id)
                return True
            if (
                not evidence.seen
                or not evidence.settled
                or evidence.failed
                or checkpoint_name not in snapshot_names
            ):
                await _resource_update(resource.resource_id, state="unknown", safe_result=state)
                await _mark_reconciliation_unknown(
                    job, worker_id, failed=evidence.failed
                )
                return True
            state.update(
                {
                    "checkpoint_reconciled": True,
                    "source_snapshot_inventory_hash": snapshot_hash,
                }
            )
        await _resource_update(resource.resource_id, state="preparing", safe_result=state)
    await _fenced_job_update(
        job,
        worker_id,
        phase=continuation_phase,
        status="running",
        locked_by=None,
        lease_expires_at=None,
        error_code=None,
        error_message=None,
        event_message="Source preparation was reconciled and can continue safely.",
    )
    return True


def _active_operation_touches(
    client: Any,
    *,
    project: str,
    names: set[str],
) -> bool:
    """Fail closed when any active operation names a protected instance."""
    for operation in _inventory_rows(client, project=project):
        outcome = _operation_outcome(operation)
        if outcome["settled"]:
            continue
        resources = operation.get("resources")
        if not isinstance(resources, dict) or not resources:
            raise OperationInventoryError("Active operation ownership is unavailable.")
        for kind, urls in resources.items():
            if not isinstance(kind, str) or not isinstance(urls, list) or not urls:
                raise OperationInventoryError("Active operation ownership is malformed.")
            for raw_url in urls:
                root, owner, name, _snapshot = _resource_identity(
                    client, operation, raw_url=raw_url
                )
                if root != "projects" and owner == project and name in names:
                    return True
    return False


async def _failed_submission_evidence(
    resource: Any,
    *,
    client: Any,
    cluster_id: str,
    project: str,
    name: str,
    kind: str,
    intent_key: str,
) -> bool:
    state = resource.safe_result or {}
    if not state.get(intent_key):
        return True
    refs = [ref for ref in (resource.operation_refs or []) if ref.get("kind") == kind]
    if len(refs) > 1:
        return False
    if refs:
        ref = refs[0]
        if (
            ref.get("cluster_id") != cluster_id
            or ref.get("project") != project
            or not isinstance(ref.get("operation_id"), str)
            or not ref.get("operation_id")
        ):
            return False
        try:
            outcome = await _blocking(
                executor.read_operation,
                client,
                operation_id=str(ref["operation_id"]),
                project=project,
                label=f"migration.reconcile_failed_{kind}[{ref['operation_id']}]",
            )
        except Exception:  # noqa: BLE001
            return False
        return bool(outcome.get("settled") and outcome.get("failed"))
    if kind in {"transfer", "source_transfer"}:
        # Primary transfer APIs do not accept an Orcastra correlation token. A generic
        # same-name failure from operation history is not proof that our submission failed.
        return False
    try:
        _operation_id, outcome = await _blocking(
            _discover_operation,
            client,
            project=project,
            name=name,
            kind=kind,
            intent_at=state.get(_INTENT_TIMESTAMPS[kind]),
            expected_resources={(project, name)},
            label=f"migration.discover_failed_{kind}[{name}]",
        )
    except Exception:  # noqa: BLE001
        return False
    return bool(outcome.get("settled") and outcome.get("failed"))


async def _try_terminalize_failed_copy(
    job: MigrationJob,
    worker_id: str,
    source_client: Any,
    target_client: Any,
    resources: list[Any],
) -> bool:
    """Release copy locks only after a no-target, conclusively failed readback."""
    if job.intent != "copy":
        return False
    instances = [resource for resource in resources if resource.resource_type == "instance"]
    if not instances or not any(
        (resource.safe_result or {}).get("transfer_submission_intent")
        for resource in instances
    ):
        return False
    try:
        source_names: set[str] = set()
        target_names: set[str] = set()
        for resource in instances:
            state = resource.safe_result or {}
            source_name = str((resource.source_ref or {}).get("name") or "")
            target_name = str((resource.target_ref or {}).get("name") or source_name)
            source_names.add(source_name)
            target_names.add(target_name)
            source = await _blocking(
                executor.read_instance,
                source_client,
                project=job.source_project,
                name=source_name,
                label=f"migration.abort_read_source[{source_name}]",
            )
            target = await _blocking(
                executor.read_instance,
                target_client,
                project=job.target_project,
                name=target_name,
                label=f"migration.abort_read_target[{target_name}]",
            )
            approved_identity = state.get("source_identity_hash") or (
                resource.source_ref or {}
            ).get("identity_fingerprint")
            approved_definition = state.get("source_definition_hash") or (
                resource.source_ref or {}
            ).get("definition_fingerprint")
            approved_snapshots = state.get("source_snapshot_inventory_hash")
            if (
                target is not None
                or source is None
                or executor.instance_identity_fingerprint(source) != approved_identity
                or str(source.get("status") or "").lower() != "stopped"
                or not isinstance(approved_definition, str)
                or not approved_definition
                or not isinstance(approved_snapshots, str)
                or not approved_snapshots
            ):
                return False
            current_definition = (
                executor.instance_fingerprint(source)
                if state.get("source_definition_hash")
                else preflight._instance_preflight_fingerprint(source)
            )
            if current_definition != approved_definition:
                return False
            current_snapshots = await _blocking(
                executor.snapshot_inventory_fingerprint,
                source_client,
                project=job.source_project,
                name=source_name,
                label=f"migration.abort_source_snapshots[{source_name}]",
            )
            if current_snapshots != approved_snapshots:
                return False
            if job.source_cluster_id != job.target_cluster_id:
                if not await _failed_submission_evidence(
                    resource,
                    client=source_client,
                    cluster_id=job.source_cluster_id,
                    project=job.source_project,
                    name=source_name,
                    kind="source_transfer",
                    intent_key="source_submission_intent",
                ):
                    return False
                if not await _failed_submission_evidence(
                    resource,
                    client=target_client,
                    cluster_id=job.target_cluster_id,
                    project=job.target_project,
                    name=target_name,
                    kind="transfer",
                    intent_key="target_submission_intent",
                ):
                    return False
            elif not await _failed_submission_evidence(
                resource,
                client=source_client,
                cluster_id=job.source_cluster_id,
                project=job.target_project,
                name=target_name,
                kind="transfer",
                intent_key="transfer_submission_intent",
            ):
                return False

        if job.source_cluster_id == job.target_cluster_id:
            projects: dict[str, set[str]] = {}
            projects.setdefault(job.source_project, set()).update(source_names)
            projects.setdefault(job.target_project, set()).update(target_names)
            for project, names in projects.items():
                if await _blocking(
                    _active_operation_touches,
                    source_client,
                    project=project,
                    names=names,
                    label=f"migration.abort_active_operations[{project}]",
                ):
                    return False
        else:
            if await _blocking(
                _active_operation_touches,
                source_client,
                project=job.source_project,
                names=source_names,
                label="migration.abort_active_source_operations",
            ) or await _blocking(
                _active_operation_touches,
                target_client,
                project=job.target_project,
                names=target_names,
                label="migration.abort_active_target_operations",
            ):
                return False
        for resource in resources:
            if resource.resource_type != "project" or not (
                resource.safe_result or {}
            ).get("target_project_create_intent"):
                continue
            target_project = await _blocking(
                executor.read_project,
                target_client,
                project=job.target_project,
                label="migration.abort_read_created_target_project",
            )
            if target_project is not None:
                return False
    except Exception as exc:  # noqa: BLE001
        logger.info("Copy abort readback remains ambiguous: %s", sanitize_error(exc))
        return False

    for resource in instances:
        await _resource_update(
            resource.resource_id,
            state="failed",
            safe_result=resource.safe_result or {},
            error_code="copy_submission_failed_without_target",
            error_message="The copy failed and no target resource remains.",
        )
    await _fenced_job_update(
        job,
        worker_id,
        phase="failed",
        status="failure",
        safe_result=sanitize_payload(
            {
                **(job.safe_result or {}),
                "verified": True,
                "outcome": "copy_failed_without_target",
            }
        ),
        error_code="copy_submission_failed_without_target",
        error_message="All submitted copy operations failed and no target resource remains.",
        terminal=True,
        event_message=(
            "Reconciliation verified a failed copy with no remaining target resources."
        ),
    )
    return True


async def _reconcile(
    job: MigrationJob,
    worker_id: str,
    source_client: Any,
    target_client: Any,
) -> None:
    resources = await _resources(job.job_id)
    states = [resource.safe_result or {} for resource in resources]
    recovery_phase = (
        "cleaning_up"
        if any(state.get("cleanup_submission_intent") for state in states)
        else "rolling_back"
        if any(
            state.get(key)
            for state in states
            for key in ("rollback_delete_submission_intent", "abort_target_delete_intent")
        )
        else None
    )
    if recovery_phase is not None and job.phase != recovery_phase:
        # A recovery runner was interrupted. Its steps read state first, so resuming the same
        # runner is safe. Locks stay held until that runner reaches its terminal transaction.
        await _fenced_job_update(
            job,
            worker_id,
            phase=recovery_phase,
            status="running",
            locked_by=None,
            lease_expires_at=None,
            event_message="Reconciliation resumed an interrupted recovery step.",
        )
        return
    if any((resource.safe_result or {}).get("restore_submission_intent") for resource in resources):
        await _fenced_job_update(
            job,
            worker_id,
            phase="manual_recovery",
            status="manual_recovery",
            locked_by=None,
            lease_expires_at=None,
            error_code="destructive_recovery_write_history_unverified",
            error_message=(
                "Persisted destructive recovery intent cannot be terminalized until "
                "authoritative target write history is available."
            ),
            event_message=(
                "Recovery reconciliation stopped at the current release safety gate."
            ),
        )
        return
    if await _reconcile_pretransfer(
        job, worker_id, source_client, resources
    ):
        return
    has_transfer_intent = any(
        (resource.safe_result or {}).get(key)
        for resource in resources
        for key in (
            "transfer_submission_intent",
            "source_submission_intent",
            "target_submission_intent",
        )
    )
    if not has_transfer_intent and await _reconcile_project_creation(
        job, worker_id, source_client, target_client, resources
    ):
        return
    if await reconcile_staged(
        job, worker_id, source_client, target_client, resources=resources
    ):
        return
    if await _try_terminalize_failed_copy(
        job, worker_id, source_client, target_client, resources
    ):
        return

    unresolved = False
    failed_operation = False
    activation_recovery_required = False
    validated = 0
    for resource in resources:
        if resource.resource_type == "project":
            state = resource.safe_result or {}
            source_project = await _blocking(
                executor.read_project,
                source_client,
                project=job.source_project,
                label="migration.reconcile_source_project",
            )
            target_project = await _blocking(
                executor.read_project,
                target_client,
                project=job.target_project,
                label="migration.reconcile_target_project",
            )
            if (
                source_project is None
                or target_project is None
                or not executor.projects_equivalent(source_project, target_project)
                or (
                    state.get("target_project_create_intent")
                    and not state.get("target_project_reconciled")
                )
            ):
                unresolved = True
                await _resource_update(resource.resource_id, state="unknown")
            else:
                await _resource_update(resource.resource_id, state="ready")
            continue
        if resource.resource_type != "instance":
            continue
        source_name = str((resource.source_ref or {}).get("name") or "")
        target_name = str((resource.target_ref or {}).get("name") or source_name)
        state = dict(resource.safe_result or {})
        source = await _blocking(
            executor.read_instance,
            source_client,
            project=job.source_project,
            name=source_name,
            label=f"migration.reconcile_source[{source_name}]",
        )
        approved_source_identity = state.get("source_identity_hash") or (
            resource.source_ref or {}
        ).get("identity_fingerprint")
        source_identity = (
            executor.instance_identity_fingerprint(source) if source is not None else None
        )
        if (
            not isinstance(approved_source_identity, str)
            or not approved_source_identity
            or source_identity != approved_source_identity
        ):
            unresolved = True
            await _resource_update(resource.resource_id, state="unknown", safe_result=state)
            continue

        if job.strategy == "member_relocation":
            evidence = await _operation_evidence(
                resource,
                client=source_client,
                cluster_id=job.source_cluster_id,
                project=job.source_project,
                name=source_name,
                kind="transfer",
                intent_key="transfer_submission_intent",
                target_member=job.target_member,
            )
            expected_definition = state.get("source_definition_hash")
            exact_definition = (
                isinstance(expected_definition, str)
                and expected_definition
                and executor.instance_fingerprint(source) == expected_definition
            )
            original_status = str(state.get("original_status") or "").lower()
            current_status = str(source.get("status") or "").lower()
            # A relocated instance is either still stopped, or running again because its
            # pre-migration state was running and activation already started it. Error,
            # Frozen, or transitional states are never adopted.
            exact_stopped_status = current_status == "stopped" or (
                original_status == "running"
                and current_status == "running"
                and bool(state.get("target_start_attempted"))
            )
            exact_member = (
                isinstance(job.target_member, str)
                and bool(job.target_member)
                and source.get("location") == job.target_member
            )
            exact_root_pool = (
                isinstance(state.get("source_root_pool"), str)
                and state.get("source_root_pool")
                and root_pool(source) == state.get("source_root_pool")
            )
            evidence_allows_adoption = not evidence.failed and (
                not evidence.seen or evidence.settled
            )
            if not (
                exact_definition
                and exact_member
                and exact_root_pool
                and exact_stopped_status
                and evidence_allows_adoption
            ):
                unresolved = True
                failed_operation = failed_operation or evidence.failed
                if not exact_stopped_status:
                    activation_recovery_required = True
                await _resource_update(resource.resource_id, state="unknown", safe_result=state)
                continue
            target_identity = state.get("target_identity_hash")
            if target_identity and target_identity != source_identity:
                unresolved = True
                await _resource_update(resource.resource_id, state="unknown", safe_result=state)
                continue
            state.update(
                {
                    "target_definition_hash": executor.instance_fingerprint(source),
                    "target_identity_hash": source_identity,
                    "target_started": current_status == "running",
                }
            )
            await _resource_update(resource.resource_id, state="ready", safe_result=state)
            validated += 1
            continue

        target = await _blocking(
            executor.read_instance,
            target_client,
            project=job.target_project,
            name=target_name,
            label=f"migration.reconcile_target[{target_name}]",
        )
        target_client_for_operation = (
            source_client
            if job.source_cluster_id == job.target_cluster_id
            else target_client
        )
        if job.source_cluster_id != job.target_cluster_id:
            source_evidence = await _operation_evidence(
                resource,
                client=source_client,
                cluster_id=job.source_cluster_id,
                project=job.source_project,
                name=source_name,
                kind="source_transfer",
                intent_key="source_submission_intent",
            )
            target_evidence = await _operation_evidence(
                resource,
                client=target_client,
                cluster_id=job.target_cluster_id,
                project=job.target_project,
                name=target_name,
                kind="transfer",
                intent_key="target_submission_intent",
            )
            transfer_proven = (
                source_evidence.seen
                and source_evidence.settled
                and not source_evidence.failed
                and target_evidence.seen
                and target_evidence.settled
                and not target_evidence.failed
            )
            resource_failed = source_evidence.failed or target_evidence.failed
        else:
            target_evidence = await _operation_evidence(
                resource,
                client=target_client_for_operation,
                cluster_id=job.target_cluster_id,
                project=job.target_project,
                name=target_name,
                kind="transfer",
                intent_key="transfer_submission_intent",
                expected_resources={(job.target_project, target_name)},
            )
            transfer_proven = (
                target_evidence.seen
                and target_evidence.settled
                and not target_evidence.failed
            )
            resource_failed = target_evidence.failed
        target_identity = (
            executor.instance_identity_fingerprint(target) if target is not None else None
        )
        expected_target_identity = state.get("target_identity_hash")
        copy_snapshots_match = True
        if job.intent == "copy" and target is not None:
            approved_definition = state.get("source_definition_hash")
            approved_snapshots = state.get("source_snapshot_inventory_hash")
            try:
                source_snapshots = await _blocking(
                    executor.snapshot_inventory_fingerprint,
                    source_client,
                    project=job.source_project,
                    name=source_name,
                    label=f"migration.reconcile_source_snapshots[{source_name}]",
                )
                target_snapshots = await _blocking(
                    executor.snapshot_inventory_fingerprint,
                    target_client,
                    project=job.target_project,
                    name=target_name,
                    label=f"migration.reconcile_target_snapshots[{target_name}]",
                )
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "Could not verify copy snapshot inventory: %s",
                    sanitize_error(exc),
                )
                copy_snapshots_match = False
            else:
                copy_snapshots_match = bool(
                    isinstance(approved_definition, str)
                    and approved_definition
                    and executor.instance_fingerprint(source) == approved_definition
                    and str(source.get("status") or "").lower() == "stopped"
                    and str(target.get("status") or "").lower() == "stopped"
                    and isinstance(approved_snapshots, str)
                    and approved_snapshots
                    and source_snapshots == approved_snapshots
                    and target_snapshots == approved_snapshots
                )
        if (
            not transfer_proven
            or resource_failed
            or target is None
            or not executor.instances_equivalent(source, target)
            or not copy_snapshots_match
            or not isinstance(target_identity, str)
            or not target_identity
            or (
                expected_target_identity
                and target_identity != expected_target_identity
            )
        ):
            unresolved = True
            failed_operation = failed_operation or resource_failed
            await _resource_update(resource.resource_id, state="unknown", safe_result=state)
            continue
        state["target_definition_hash"] = executor.instance_fingerprint(target)
        if job.intent == "copy":
            state["target_snapshot_inventory_hash"] = state[
                "source_snapshot_inventory_hash"
            ]
        if not expected_target_identity:
            state["target_identity_hash"] = target_identity
        if job.intent == "move":
            if str(source.get("status") or "").lower() != "stopped":
                activation_recovery_required = True
                await _resource_update(resource.resource_id, state="unknown", safe_result=state)
                continue
            target_running = str(target.get("status") or "").lower() == "running"
            expected_running = state.get("original_status") == "running"
            if target_running != expected_running:
                activation_recovery_required = True
                await _resource_update(resource.resource_id, state="unknown", safe_result=state)
                continue
            state["target_started"] = target_running
        await _resource_update(
            resource.resource_id,
            state="completed" if job.intent == "copy" else "retained",
            safe_result=state,
        )
        validated += 1

    instance_count = sum(1 for resource in resources if resource.resource_type == "instance")
    if activation_recovery_required:
        await _fenced_job_update(
            job,
            worker_id,
            phase="manual_recovery",
            status="manual_recovery",
            locked_by=None,
            lease_expires_at=None,
            error_code="target_activation_unverified",
            error_message=(
                "Cutover activation does not match the source's pinned pre-migration state."
            ),
            event_message="Migration paused because target activation needs operator recovery.",
        )
    elif (
        not unresolved
        and not failed_operation
        and validated == instance_count
        and job.strategy == "member_relocation"
    ):
        # Activation and the final barrier finish the job from the adopted state.
        await _fenced_job_update(
            job,
            worker_id,
            phase="target_validating",
            status="running",
            locked_by=None,
            lease_expires_at=None,
            error_code=None,
            error_message=None,
            event_message="Reconciliation verified the relocation. Resuming activation.",
        )
    elif not unresolved and not failed_operation and validated == instance_count:
        completion_events = target_completion_events(
            job,
            include_created_events=job.intent == "copy",
            occurred_at=repository.utcnow().isoformat(),
        )
        if job.intent == "copy" or job.strategy == "member_relocation":
            await _fenced_job_update(
                job,
                worker_id,
                phase="completed",
                status="success",
                safe_result=sanitize_payload(
                    {**(job.safe_result or {}), "verified": True}
                ),
                terminal=True,
                outbox_events=completion_events,
                event_message="Reconciliation verified the completed migration.",
            )
        else:
            await _fenced_job_update(
                job,
                worker_id,
                phase=(
                    "external_cutover_pending"
                    if job.external_cutover_required
                    and job.external_cutover_confirmed_at is None
                    else "source_retained"
                ),
                status="waiting",
                retention_deadline=(
                    None
                    if job.external_cutover_required
                    and job.external_cutover_confirmed_at is None
                    else repository.utcnow() + timedelta(hours=job.retention_hours)
                ),
                locked_by=None,
                lease_expires_at=None,
                outbox_events=completion_events,
                event_message="Reconciliation verified the target and retained source.",
            )
    else:
        await _mark_reconciliation_unknown(
            job, worker_id, failed=failed_operation
        )
