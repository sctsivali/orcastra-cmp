"""Stable plan payload and mapping helpers for Migration Center."""

from __future__ import annotations

from typing import Any, Optional

from app.models.migration_schemas import MigrationPlanCreateRequest

from . import preflight
from .security import canonical_json_hash, sanitize_payload

def _preflight_payload(
    result: preflight.PreflightResult,
    request: MigrationPlanCreateRequest,
) -> dict[str, Any]:
    source = request.source
    target = request.target
    intent = str(getattr(request.intent, "value", request.intent))
    instance_scope = str(getattr(request.scope, "value", request.scope)) == "instance"
    rollback_supported = bool(result.capabilities.get("rollback")) and intent == "move"
    resources: list[dict[str, Any]] = []
    project_resource_keys = {
        f"project:{resource.get('source_name')}"
        for resource in result.resources
        if resource.get("resource_type") == "project" and resource.get("source_name")
    }
    for ordinal, resource in enumerate(result.resources):
        source_name = resource.get("source_name")
        target_name = resource.get("target_name")
        resource_type = str(resource.get("resource_type") or "resource")
        resource_key = f"{resource_type}:{source_name or ordinal}"
        planned: dict[str, Any] = {}
        if resource.get("source_status"):
            # Kept outside ``source``: the worker's graph signature hashes ``source``, and a
            # running source is stopped by the job itself.
            planned["source_status"] = resource["source_status"]
        resources.append(
            {
                **planned,
                "resource_key": resource_key,
                "resource_type": resource_type,
                "source": {
                    "cluster_id": source.cluster_id,
                    "project": source.project,
                    "name": source_name,
                    "member": source.member,
                    "definition_fingerprint": resource.get("source_fingerprint"),
                    "identity_fingerprint": resource.get(
                        "source_identity_fingerprint"
                    ),
                    "project_fingerprint": result.capabilities.get(
                        "source_project_fingerprint"
                    ),
                },
                "target": {
                    "cluster_id": target.cluster_id,
                    "project": target.project,
                    "name": target_name,
                    "member": target.member,
                    "definition_fingerprint": resource.get("target_fingerprint"),
                    "identity_fingerprint": resource.get(
                        "target_identity_fingerprint"
                    ),
                    "project_fingerprint": result.capabilities.get(
                        "target_project_fingerprint"
                    ),
                    **(
                        {"contract": resource["contract"]}
                        if isinstance(resource.get("contract"), dict)
                        else {}
                    ),
                },
                "dependency_keys": (
                    []
                    if resource_type == "project"
                    or f"project:{source.project}" not in project_resource_keys
                    else [f"project:{source.project}"]
                ),
            }
        )
    capabilities = [
        {
            "code": "same_engine",
            "supported": result.capabilities["source"]["engine"]
            == result.capabilities["target"]["engine"],
            "reason": None,
        },
        {
            "code": "checkpoint",
            "supported": bool(result.capabilities.get("checkpoint")),
            "reason": None,
        },
        {
            "code": "rollback",
            "supported": rollback_supported,
            "reason": (
                "Copy creates an independent destination and has no rollback window."
                if intent == "copy"
                else "Member relocation keeps every instance identity. A failed move puts the "
                "workloads back automatically; afterwards, relocate them back with a new plan."
                if result.strategy == "member_relocation"
                else "The stopped source is retained, so rollback restores it and removes the "
                "target. Writes made on the target after cutover are discarded."
                if rollback_supported
                else "Rollback is not available for this plan."
            ),
        },
        {
            "code": "live",
            "supported": bool(result.capabilities.get("live")),
            "reason": (
                "A running source is stopped for the transfer and its target is started "
                "afterwards (cold migration)."
            ),
        },
        {
            "code": "incremental_refresh",
            "supported": False,
            "reason": (
                "The daemon advertises a narrow Incus refresh capability, but this release "
                "does not select or execute incremental refresh."
                if result.capabilities.get("incremental_refresh")
                else "The selected topology does not expose an eligible refresh capability."
            ),
        },
        {
            "code": "lxd_native_cross_project_move",
            "supported": False,
            "reason": (
                "Native LXD cross-project move is not applicable to the selected topology."
                if not result.capabilities.get(
                    "lxd_native_cross_project_move_applicable"
                )
                else
                "The exact LXD build passed the GHSA-5g5r-wh97-qcq2 baseline, but this release "
                "still selects staged copy and explicit cleanup instead of native move."
                if result.capabilities.get(
                    "lxd_native_cross_project_move_security_approved"
                )
                else "Native LXD cross-project move is fail-closed until the exact build passes "
                "the dedicated GHSA-5g5r-wh97-qcq2 security baseline."
            ),
        },
        {
            "code": "atomic_shadow_metadata_cutover",
            "supported": bool(
                result.capabilities.get("atomic_shadow_metadata_cutover")
            ),
            "reason": (
                "Tags, snapshot schedules, and tenant access rules follow the instance to its "
                "new identity in the same transaction that retains the source."
                if instance_scope
                else "Project tags, project-wide tenant access rules, and every instance's "
                "tags, schedules, and rules follow the project in the same transaction that "
                "retains the source project."
            ),
        },
        {
            "code": "project_dependency_isolation",
            "supported": bool(
                result.capabilities.get("project_dependencies_locked", False)
            ),
            "reason": (
                "Effective project dependencies are contained by the selected project locks."
                if result.capabilities.get("project_dependencies_locked", False)
                else "One or more dependency namespaces are inherited from the default project "
                "and are outside the migration locks."
            ),
        },
        {
            "code": "daemon_operation_quiescence",
            "supported": bool(
                result.capabilities.get("daemon_operation_inventory_verified", False)
                and result.capabilities.get("daemon_operations_quiescent", False)
            ),
            "reason": (
                "Authoritative daemon operation inventory is readable and quiescent."
                if result.capabilities.get("daemon_operation_inventory_verified", False)
                and result.capabilities.get("daemon_operations_quiescent", False)
                else "Daemon operation inventory is unreadable or an active operation touches "
                "a selected project."
            ),
        },
        {
            "code": "mapping_storage_root",
            "supported": True,
            "reason": "Only storage_pools.root or storage_pools.* is applied.",
        },
        {
            "code": "mapping_networks",
            "supported": instance_scope or result.strategy == "project_relocation",
            "reason": (
                "Each NIC follows an explicit mapping, the target profile, or a network with "
                "the same name."
                if instance_scope
                else "Each NIC of an instance or a cloned profile follows an explicit mapping, "
                "or a network with the same name."
                if result.strategy == "project_relocation"
                else "Member relocation keeps every network as it is."
            ),
        },
        {
            "code": "mapping_profiles",
            "supported": instance_scope,
            "reason": (
                "Profiles may be renamed to existing target profiles."
                if instance_scope
                else "A relocation clones every profile under its own name."
            ),
        },
        {
            "code": "mapping_volumes",
            "supported": False,
            "reason": "Custom volume mapping is not executable in this release.",
        },
        {
            "code": "mapping_members",
            "supported": False,
            "reason": "Member mapping is selected through the destination member only.",
        },
        {
            "code": "mapping_external_dependencies",
            "supported": False,
            "reason": "External dependency mapping requires an operator-managed cutover.",
        },
    ]
    retained = intent == "move" and result.strategy != "member_relocation"
    automatic_cleanup = bool(result.capabilities.get("automatic_cleanup")) and retained
    payload = {
        "strategy": result.strategy,
        "downtime_class": result.downtime,
        "capabilities": capabilities,
        "daemon_capabilities": sanitize_payload(result.capabilities),
        "mappings": sanitize_payload(request.mappings.model_dump(mode="json")),
        "resources": resources,
        "blockers": [
            {**issue, "severity": "blocker"} for issue in result.blockers
        ],
        "warnings": [
            {**issue, "severity": "warning"} for issue in result.warnings
        ],
        "rollback_contract": {
            "supported": rollback_supported,
            "source_retained": retained,
            "retention_hours": request.retention_hours if retained else None,
            "automatic_cleanup": automatic_cleanup,
            "target_writes_discarded_on_rollback": retained,
            "manual_recovery_only": False,
            "reason": (
                "Copy creates an independent destination and has no retained-source rollback."
                if intent == "copy"
                else "Native member relocation has no retained source copy."
                if result.strategy == "member_relocation"
                else (
                    "The source project stays, its instances stopped and fenced, for the retention "
                    "window. Rollback restores it and removes the target project. "
                    + (
                        "Finalize, or the end of retention, deletes the source project."
                        if automatic_cleanup
                        else "Finalize deletes the source project."
                    )
                )
                if result.strategy == "project_relocation"
                else "The source stays stopped and fenced for the retention window. Rollback "
                "restores it and removes the target. Finalize, or the end of retention, deletes it."
                if automatic_cleanup
                else "The source stays stopped and fenced for the retention window. Rollback "
                "restores it and removes the target. Finalize deletes it."
            ),
        },
    }
    payload["approval_hash"] = approval_report_hash(payload)
    return payload


def approval_report_hash(report: dict[str, Any]) -> str:
    """Hash the full approval report while normalizing presentation-only list order."""
    capabilities = sorted(
        (
            dict(capability)
            for capability in report.get("capabilities") or []
            if isinstance(capability, dict)
        ),
        key=lambda capability: (
            str(capability.get("code") or ""),
            canonical_json_hash(capability),
        ),
    )
    resources = sorted(
        (
            dict(resource)
            for resource in report.get("resources") or []
            if isinstance(resource, dict)
        ),
        key=lambda resource: (
            str(resource.get("resource_key") or ""),
            canonical_json_hash(resource),
        ),
    )

    def issues(name: str) -> list[dict[str, Any]]:
        return sorted(
            (
                dict(issue)
                for issue in report.get(name) or []
                if isinstance(issue, dict)
            ),
            key=lambda issue: (
                str(issue.get("code") or ""),
                canonical_json_hash(issue),
            ),
        )

    projection = {
        key: value for key, value in report.items() if key != "approval_hash"
    }
    projection["capabilities"] = capabilities
    projection["resources"] = resources
    projection["blockers"] = issues("blockers")
    projection["warnings"] = issues("warnings")
    return canonical_json_hash(projection)


def _target_pool(request: MigrationPlanCreateRequest) -> Optional[str]:
    pools = request.mappings.storage_pools
    return pools.get("root") or pools.get("*") or next(iter(pools.values()), None)


def approved_root_pool(plan_payload: dict[str, Any]) -> Optional[str]:
    """The root pool the approved preflight resolved, which the worker must reuse."""
    capabilities = (plan_payload or {}).get("daemon_capabilities") or {}
    pool = capabilities.get("target_root_pool")
    return pool if isinstance(pool, str) and pool else None
