"""Instance-scope migration preflight: copy, staged move, and cluster-member relocation.

Readers and shared checks live in ``preflight`` and are looked up at call time. The staged
paths (copy, or a move to another project or cluster) build a target contract and admit it
against the target's profiles, networks, restrictions, quotas, capacity, and member. Member
relocation keeps the instance identity and uses the native relocation checks.
"""

from __future__ import annotations

from typing import Any, Optional

from . import project_policy
from .preflight_source import (
    active_instance_operations,
    check_instance_operations,
    device_findings,
    source_state,
    source_status,
)
from .security import instance_identity_fingerprint
from .target_admission import (
    contract_for,
    member_findings,
    quota_findings,
    read_target_view,
    resolve_root_pool,
    restriction_findings,
    static_address_findings,
)

def _pf() -> Any:
    from . import preflight

    return preflight


def _apply(result: Any, findings: list[tuple[str, str, str]]) -> None:
    for code, message, remediation in findings:
        result.block(code, message, remediation)


def _shared_dependency_warnings(result: Any, *, side: str, project: str, record: Any) -> None:
    shared = _pf()._shared_dependency_features(project, record)
    if shared:
        result.warn(
            f"{side}_project_shares_default_dependencies",
            f"The {side} project uses the default project's {', '.join(shared)}. The plan pins "
            "the exact profiles and networks it resolved, and execution re-checks them.",
        )


def inspect_instance(
    result: Any,
    *,
    intent: str,
    source_client: Any,
    target_client: Any,
    source_cluster: str,
    source_project: str,
    source_name: Optional[str],
    target_cluster: str,
    target_project: str,
    target_name: Optional[str],
    target_member: Optional[str],
    target_pool: Optional[str],
    settings: Any,
    mappings: dict[str, Any],
    source_identity: dict[str, Any],
    target_identity: dict[str, Any],
    source_project_record: Optional[dict[str, Any]],
    target_project_record: Optional[dict[str, Any]],
) -> Any:
    pf = _pf()
    same_cluster = source_cluster == target_cluster
    same_project = same_cluster and source_project == target_project
    member_relocation = intent == "move" and same_project and bool(target_member)
    if not source_name:
        result.block("source_name_required", "An instance name is required.")
        return result
    try:
        source = pf._instance(source_client, source_project, source_name)
    except pf.InventoryReadError as exc:
        pf._block_inventory_error(result, side="source", error=exc)
        return result
    if source is None:
        result.block("source_not_found", "The source instance does not exist.")
        return result
    if source_project_record is None:
        result.block("source_project_missing", "The source project does not exist.")
        return result

    status = source_state(result, source, source_name, settings)
    result.capabilities["source_status"] = status
    result.capabilities["stopped_source_required"] = not bool(
        getattr(settings, "migration_running_source_enabled", True)
    )
    result.downtime = "cold" if status == "running" else "none"
    identity = instance_identity_fingerprint(source)
    if identity is None:
        result.block(
            "source_identity_unavailable",
            f"Instance '{source_name}' has no authoritative immutable identity evidence.",
            "Refresh the daemon inventory and ensure the instance exposes a creation time or UUID.",
        )
    destination = target_name or source_name
    if same_project and not target_member:
        if destination == source_name:
            result.block(
                "source_target_identical",
                "Source and target resolve to the same instance identity.",
                "Choose a distinct copy name, another project, another cluster, or a member.",
            )
        elif intent == "move":
            result.block(
                "rename_in_place_unsupported",
                "A same-project move cannot be used as an in-place rename.",
                "Use a dedicated rename workflow or choose another project, cluster, or member.",
            )
    # A member relocation keeps the identity, so there is no retained source to fence.
    device_findings(result, source, source_name, "relocate" if member_relocation else intent)
    check_instance_operations(
        result,
        source_client=source_client,
        target_client=target_client,
        same_cluster=same_cluster,
        source_project=source_project,
        source_name=source_name,
        target_project=target_project,
        target_name=destination,
    )
    architecture = source.get("architecture")
    if architecture and target_identity["architectures"] and architecture not in target_identity[
        "architectures"
    ]:
        result.block(
            "architecture_mismatch",
            f"The target cannot run architecture '{architecture}'.",
            "Choose a target cluster with the same CPU architecture.",
        )

    if member_relocation:
        _inspect_member_relocation(
            result,
            source=source,
            source_client=source_client,
            source_project=source_project,
            source_name=source_name,
            target_name=target_name,
            target_member=str(target_member),
            target_pool=target_pool,
            identity=identity,
            status=status,
        )
        return result

    _inspect_staged(
        result,
        intent=intent,
        source=source,
        source_client=source_client,
        target_client=target_client,
        source_project=source_project,
        source_name=source_name,
        target_project=target_project,
        destination=destination,
        target_member=target_member,
        target_pool=target_pool,
        mappings=mappings,
        identity=identity,
        status=status,
        same_project=same_project,
        source_project_record=source_project_record,
        target_project_record=target_project_record,
    )
    return result


def _inspect_member_relocation(
    result: Any,
    *,
    source: dict[str, Any],
    source_client: Any,
    source_project: str,
    source_name: str,
    target_name: Optional[str],
    target_member: str,
    target_pool: Optional[str],
    identity: Optional[str],
    status: str,
) -> None:
    pf = _pf()
    location = str(source.get("location") or "").strip()
    if not location:
        result.block(
            "source_member_unverified",
            "The source instance member could not be read authoritatively.",
            "Refresh cluster inventory before selecting a relocation member.",
        )
    elif location == target_member:
        result.block(
            "target_member_is_source",
            f"Instance '{source_name}' is already on member '{target_member}'.",
            "Choose a different target member.",
        )
    if target_name and target_name != source_name:
        result.block(
            "member_relocation_rename_unsupported",
            "Member relocation preserves the instance name.",
            "Use the source name as the destination name, or copy instead.",
        )
    pf._member_relocation_checks(
        result,
        client=source_client,
        project=source_project,
        target_member=target_member,
        target_pool=target_pool,
        instances=[source],
    )
    result.capabilities["rollback"] = False
    fingerprint = pf._instance_preflight_fingerprint(source)
    result.resources.append(
        {
            "resource_type": "instance",
            "source_name": source_name,
            "target_name": source_name,
            "source_fingerprint": fingerprint,
            "source_identity_fingerprint": identity,
            "source_status": status,
            "target_fingerprint": fingerprint,
            "target_identity_fingerprint": identity,
            "required": True,
            "state": "planned",
        }
    )


def admit_staged(
    result: Any,
    *,
    intent: str,
    source: dict[str, Any],
    source_name: str,
    view: Any,
    mappings: dict[str, Any],
    target_pool: Optional[str],
    target_member: Optional[str],
    target_client: Any,
    target_project: str,
    subject: str = "",
) -> tuple[Any, Optional[str], str]:
    """Contract and target admission for one staged instance: copy or identity-changing move.

    Returns the contract, the resolved root pool, and how it was resolved. ``subject`` prefixes
    findings when one plan admits several instances.
    """
    profile_mapping = dict(mappings.get("profiles") or {})
    profile_names = [
        str(profile_mapping.get(name, name)) for name in (source.get("profiles") or [])
    ]
    root_pool, basis = resolve_root_pool(
        source=source, mapped_pool=target_pool, view=view, profile_names=profile_names
    )
    if root_pool is None:
        result.block(
            "target_pool_mapping_required",
            f"{subject}No target storage pool could be chosen for the root disk.",
            "Pick a destination storage pool.",
        )
    elif root_pool not in view.pools:
        result.block(
            "target_pool_missing", f"{subject}Target storage pool '{root_pool}' does not exist."
        )

    contract = contract_for(
        source=source, view=view, mappings=mappings, root_pool=root_pool, intent=intent
    )
    result.contracts[source_name] = contract
    for name in contract.missing_profiles:
        result.block(
            "target_profile_missing",
            f"{subject}Profile '{name}' does not exist in the target project.",
            "Map the profile to an existing target profile, or create it first.",
        )
    if contract.unmapped_networks:
        candidates = ", ".join(
            name for name in view.usable_networks if not name.startswith("parent:")
        ) or "none"
        for item in contract.unmapped_networks:
            if item.get("mapped"):
                result.block(
                    "network_mapping_invalid",
                    f"{subject}NIC '{item['device']}' is mapped to '{item['mapped']}', which the "
                    f"target project cannot use. Available networks: {candidates}.",
                    "Map the network to one of the available target networks.",
                )
                continue
            result.block(
                "network_mapping_required",
                f"{subject}NIC '{item['device']}' uses '{item['source']}', which the target "
                f"project cannot use. Available networks: {candidates}.",
                "Map the network to one of the available target networks.",
            )
    if contract.profile_added_config:
        result.warn(
            "target_profile_adds_config",
            f"{subject}The target profiles add settings the source does not have: "
            f"{', '.join(sorted(contract.profile_added_config))}.",
        )
    instance_type = str(source.get("type") or "container")
    findings = restriction_findings(
        project_config=view.project_config,
        contract=contract,
        instance_type=instance_type,
        target_member=target_member,
    ) + static_address_findings(contract=contract, view=view)
    if target_member:
        findings += member_findings(
            target_client=target_client,
            target_project=target_project,
            target_member=target_member,
            root_pool=root_pool,
            contract=contract,
            architecture=source.get("architecture"),
        )
    _apply(result, [(code, f"{subject}{message}", fix) for code, message, fix in findings])
    return contract, root_pool, basis


def _inspect_staged(
    result: Any,
    *,
    intent: str,
    source: dict[str, Any],
    source_client: Any,
    target_client: Any,
    source_project: str,
    source_name: str,
    target_project: str,
    destination: str,
    target_member: Optional[str],
    target_pool: Optional[str],
    mappings: dict[str, Any],
    identity: Optional[str],
    status: str,
    same_project: bool,
    source_project_record: dict[str, Any],
    target_project_record: Optional[dict[str, Any]],
) -> None:
    pf = _pf()
    profile_mapping = dict(mappings.get("profiles") or {})
    profile_names = [
        str(profile_mapping.get(name, name)) for name in (source.get("profiles") or [])
    ]
    try:
        view = read_target_view(
            target_client=target_client,
            target_project=target_project,
            target_project_record=target_project_record,
            source_project_record=source_project_record,
            profile_names=profile_names,
        )
    except pf.InventoryReadError as exc:
        pf._block_inventory_error(result, side="target", error=exc)
        return
    result.capabilities["target_project_create"] = not view.project_exists
    if not view.project_exists:
        # The job creates this exact policy, so approval pins it now and the post-create
        # re-check compares like with like.
        result.capabilities["target_project_fingerprint"] = project_policy.policy_fingerprint(
            {"config": view.project_config}
        )
    _shared_dependency_warnings(
        result, side="source", project=source_project, record=source_project_record
    )
    if not same_project and target_project_record is not None:
        _shared_dependency_warnings(
            result, side="target", project=target_project, record=target_project_record
        )

    contract, root_pool, basis = admit_staged(
        result,
        intent=intent,
        source=source,
        source_name=source_name,
        view=view,
        mappings=mappings,
        target_pool=target_pool,
        target_member=target_member,
        target_client=target_client,
        target_project=target_project,
    )
    result.capabilities["target_root_pool"] = root_pool
    result.capabilities["target_root_pool_basis"] = basis

    target_instance = None
    if view.project_exists:
        try:
            target_instance = pf._instance(target_client, target_project, destination)
        except pf.InventoryReadError as exc:
            pf._block_inventory_error(result, side="target", error=exc)
        if target_instance is not None:
            result.block(
                "target_instance_exists",
                f"Target instance '{destination}' already exists.",
                "Choose another target name or remove the conflicting instance.",
            )
    try:
        _apply(
            result,
            quota_findings(
                target_client=target_client,
                target_project=target_project,
                view=view,
                contract=contract,
                instance_type=str(source.get("type") or "container"),
            ),
        )
    except Exception:  # noqa: BLE001 - unknown quota usage must fail closed
        result.block(
            "target_quota_unverified",
            "The target project's quota usage could not be read.",
            "Restore project usage reporting and run preflight again.",
        )
    if root_pool:
        pf._check_capacity(
            result,
            source_client=source_client,
            target_client=target_client,
            source_project=source_project,
            instance_names=[source_name],
            target_pool=root_pool,
            target_member=target_member,
        )
    result.capabilities["rollback"] = intent == "move"
    if not same_project:
        result.resources.append(
            {
                "resource_type": "project",
                "source_name": source_project,
                "target_name": target_project,
                "required": True,
                "state": "planned",
            }
        )
    result.resources.append(
        {
            "resource_type": "instance",
            "source_name": source_name,
            "target_name": destination,
            "source_fingerprint": pf._instance_preflight_fingerprint(source),
            "source_identity_fingerprint": identity,
            "source_status": status,
            "target_fingerprint": None,
            "target_identity_fingerprint": None,
            "contract": contract.summary(),
            "required": True,
            "state": "planned",
        }
    )


__all__ = ["active_instance_operations", "admit_staged", "inspect_instance", "source_status"]
