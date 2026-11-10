"""Project-scope migration preflight: relocate a whole project, or its workloads to a member.

``member_relocation`` keeps the project and every instance identity and moves the workloads to
another member of the same cluster, one at a time. ``project_relocation`` recreates the project
under a new identity (another cluster, or a new name on the same one): the job creates the
target project, clones every profile, and stages each instance through the instance-move
contract. The target project must not exist, so it never holds anything the job did not create.

Readers and shared checks live in ``preflight`` and are looked up at call time.
"""

from __future__ import annotations

from typing import Any, Optional

from . import project_policy
from .preflight_instance import admit_staged
from .preflight_source import device_findings, source_state
from .profile_clone import ProfileClone, clone_profiles, profile_fingerprint
from .security import instance_identity_fingerprint
from .target_admission import read_target_view

def _pf() -> Any:
    from . import preflight

    return preflight


def _owns(record: dict[str, Any], feature: str) -> bool:
    return project_policy.features_enabled(record, feature)


def _dependency_blockers(
    result: Any, *, project: str, record: dict[str, Any], resources: dict[str, list[Any]]
) -> int:
    """Block what a relocation cannot carry. Returns how many cached images it will drop."""
    owned = project_policy.owned_rows(record, resources)
    for kind, rows in owned.items():
        if kind == "placement_groups":
            result.block(
                "unsupported_placement_groups",
                f"Project '{project}' has placement groups that a relocation cannot carry.",
                "Remove the placement groups before relocating the project.",
            )
            continue
        label = project_policy.OWNED_KIND_FEATURE[kind][1]
        names = ", ".join(sorted(str(row.get("name") or "?") for row in rows)[:5])
        result.block(
            f"unsupported_{kind}",
            f"Project '{project}' owns {label} ({names}) that a relocation cannot carry.",
            "Move or remove them first. A relocation recreates profiles and instances only.",
        )
    cached = 0
    if _owns(record, "images"):
        images = [row for row in resources.get("images") or [] if isinstance(row, dict)]
        disposable = project_policy.disposable_images(images, resources.get("instances") or [])
        kept = [row for row in images if str(row.get("fingerprint") or "") not in disposable]
        cached = len(images) - len(kept)
        if kept:
            result.block(
                "unsupported_images",
                f"Project '{project}' holds {len(kept)} image(s) of its own, with an alias or not "
                "used by its instances, that a relocation cannot carry.",
                "Copy the images to the target or delete them, then run preflight again.",
            )
    shared = [
        feature
        for feature in ("profiles", "images", "networks", "storage.volumes")
        if not _owns(record, feature)
    ]
    if shared:
        result.warn(
            "source_project_shares_default_dependencies",
            f"Project '{project}' uses the default project's {', '.join(shared)}. The new "
            "project does the same on the target, so those come from the target's default "
            "project.",
        )
    return cached


def _valid_instances(result: Any, rows: list[Any]) -> list[dict[str, Any]]:
    instances: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("name"):
            result.block(
                "invalid_instance_inventory",
                "The daemon returned an invalid instance inventory entry.",
            )
            continue
        instances.append(row)
    return instances


def _instance_resource(
    instance: dict[str, Any],
    *,
    status: str,
    identity: Optional[str],
    same_identity: bool,
    contract: Any = None,
) -> dict[str, Any]:
    pf = _pf()
    fingerprint = pf._instance_preflight_fingerprint(instance)
    resource = {
        "resource_type": "instance",
        "source_name": str(instance["name"]),
        "target_name": str(instance["name"]),
        "source_fingerprint": fingerprint,
        "source_identity_fingerprint": identity,
        "source_status": status,
        "target_fingerprint": fingerprint if same_identity else None,
        "target_identity_fingerprint": identity if same_identity else None,
        "required": True,
        "state": "planned",
    }
    if contract is not None:
        resource["contract"] = contract.summary()
    return resource


def _source_checks(
    result: Any, instance: dict[str, Any], *, settings: Any, intent: str
) -> tuple[str, Optional[str]]:
    name = str(instance["name"])
    status = source_state(result, instance, name, settings)
    identity = instance_identity_fingerprint(instance)
    if identity is None:
        result.block(
            "source_identity_unavailable",
            f"Instance '{name}' has no authoritative immutable identity evidence.",
            "Refresh the daemon inventory and ensure the instance exposes a creation time or UUID.",
        )
    device_findings(result, instance, name, intent)
    return status, identity


def _transfer_order(instance: dict[str, Any], status: str) -> tuple[int, str]:
    # Stopped workloads go first: a problem then surfaces before any running workload stops.
    return (1 if status == "running" else 0, str(instance.get("name")))


def inspect_project(
    result: Any,
    *,
    intent: str,
    source_client: Any,
    target_client: Any,
    source_cluster: str,
    source_project: str,
    target_cluster: str,
    target_project: str,
    target_member: Optional[str],
    target_pool: Optional[str],
    settings: Any,
    mappings: dict[str, Any],
    target_identity: dict[str, Any],
    source_project_record: Optional[dict[str, Any]],
    target_project_record: Optional[dict[str, Any]],
) -> Any:
    pf = _pf()
    if source_project_record is None:
        result.block("source_project_missing", "The source project does not exist.")
        return result
    if intent != "move":
        return result
    same_project = source_cluster == target_cluster and source_project == target_project
    if same_project and not target_member:
        result.block(
            "source_target_identical",
            "Source and target resolve to the same project with no target member.",
            "Choose a member to move the workloads to, another cluster, or a new project name.",
        )
        return result
    try:
        resources = pf._known_project_resources(source_client, source_project)
    except pf.InventoryReadError as exc:
        pf._block_inventory_error(result, side="source", error=exc)
        return result
    instances = _valid_instances(result, resources.get("instances") or [])
    limit = int(getattr(settings, "migration_project_max_instances", 25))
    if len(instances) > limit:
        result.block(
            "project_too_large",
            f"Project '{source_project}' has {len(instances)} instances. One relocation moves at "
            f"most {limit}.",
            "Move some instances individually first, or raise MIGRATION_PROJECT_MAX_INSTANCES.",
        )
    if same_project:
        _inspect_member(
            result,
            instances=instances,
            source_client=source_client,
            source_project=source_project,
            target_member=str(target_member),
            target_pool=target_pool,
            settings=settings,
        )
    else:
        _inspect_relocation(
            result,
            instances=instances,
            resources=resources,
            source_client=source_client,
            target_client=target_client,
            source_cluster=source_cluster,
            source_project=source_project,
            target_cluster=target_cluster,
            target_project=target_project,
            target_member=target_member,
            target_pool=target_pool,
            settings=settings,
            mappings=mappings,
            target_identity=target_identity,
            source_project_record=source_project_record,
            target_project_record=target_project_record,
        )
    _dedupe(result)
    return result


def _dedupe(result: Any) -> None:
    for attribute in ("blockers", "warnings"):
        seen: set[tuple[str, str]] = set()
        unique = []
        for item in getattr(result, attribute):
            key = (item.get("code", ""), item.get("message", ""))
            if key not in seen:
                seen.add(key)
                unique.append(item)
        setattr(result, attribute, unique)


def _inspect_member(
    result: Any,
    *,
    instances: list[dict[str, Any]],
    source_client: Any,
    source_project: str,
    target_member: str,
    target_pool: Optional[str],
    settings: Any,
) -> None:
    pf = _pf()
    result.strategy = "member_relocation"
    result.capabilities["rollback"] = False
    moving: list[tuple[dict[str, Any], str, Optional[str]]] = []
    for instance in instances:
        name = str(instance["name"])
        location = str(instance.get("location") or "").strip()
        if not location:
            result.block(
                "source_member_unverified",
                f"The current member for instance '{name}' could not be read.",
                "Refresh cluster inventory before selecting a relocation member.",
            )
            continue
        if location == target_member:
            result.warn(
                "instance_already_on_member",
                f"Instance '{name}' is already on '{target_member}' and is left as it is.",
            )
            continue
        status, identity = _source_checks(result, instance, settings=settings, intent="relocate")
        moving.append((instance, status, identity))
    if not moving:
        result.block(
            "empty_project_relocation_noop",
            f"No workload in the project needs to move to '{target_member}'.",
            "Choose another member, or a project with workloads elsewhere.",
        )
    pools: dict[str, list[dict[str, Any]]] = {}
    for instance, _status, _identity in moving:
        pools.setdefault(str(_root_pool(instance) or ""), []).append(instance)
    for group in pools.values():
        pf._member_relocation_checks(
            result,
            client=source_client,
            project=source_project,
            target_member=target_member,
            target_pool=target_pool,
            instances=group,
        )
    if len(pools) != 1:
        result.capabilities["member_relocation_root_pool"] = None
    result.downtime = "cold" if any(status == "running" for _i, status, _d in moving) else "none"
    result.resources.append(
        {
            "resource_type": "project",
            "source_name": source_project,
            "target_name": source_project,
            "required": True,
            "state": "planned",
        }
    )
    for instance, status, identity in sorted(
        moving, key=lambda item: _transfer_order(item[0], item[1])
    ):
        result.resources.append(
            _instance_resource(instance, status=status, identity=identity, same_identity=True)
        )


def _root_pool(instance: dict[str, Any]) -> Optional[str]:
    from .target_contract import effective_root_pool

    return effective_root_pool(instance)


def _inspect_relocation(
    result: Any,
    *,
    instances: list[dict[str, Any]],
    resources: dict[str, list[Any]],
    source_client: Any,
    target_client: Any,
    source_cluster: str,
    source_project: str,
    target_cluster: str,
    target_project: str,
    target_member: Optional[str],
    target_pool: Optional[str],
    settings: Any,
    mappings: dict[str, Any],
    target_identity: dict[str, Any],
    source_project_record: dict[str, Any],
    target_project_record: Optional[dict[str, Any]],
) -> None:
    pf = _pf()
    result.strategy = "project_relocation"
    if source_project == "default":
        result.block(
            "default_project_relocation_unsupported",
            "The default project cannot be relocated. It always exists on every cluster.",
            "Move its instances individually, or relocate a named project.",
        )
        return
    if source_cluster != target_cluster and str(
        (source_project_record.get("config") or {}).get("restricted.cluster.groups") or ""
    ).strip():
        result.warn(
            "project_cluster_groups_cloned",
            "The project restricts placement to cluster groups. The target cluster must have "
            "groups with the same names, or creating the project there fails and nothing moves.",
        )
    cached_images = _dependency_blockers(
        result, project=source_project, record=source_project_record, resources=resources
    )
    if target_project_record is not None:
        result.block(
            "target_project_exists",
            f"Project '{target_project}' already exists on the target. A relocation creates the "
            "target project and never merges into an existing one.",
            "Choose a new project name, or move the instances individually into it.",
        )
        return
    if cached_images:
        result.warn(
            "cached_images_not_carried",
            f"{cached_images} cached or base image(s) are not copied. Instances do not need them "
            "after creation, the target downloads images again when it needs them, and Finalize "
            "deletes them with the source project.",
        )
    profiles_owned = _owns(source_project_record, "profiles")
    profile_names = sorted(
        {
            str(name)
            for instance in instances
            for name in (instance.get("profiles") or [])
        }
    )
    try:
        view = read_target_view(
            target_client=target_client,
            target_project=target_project,
            target_project_record=None,
            source_project_record=source_project_record,
            profile_names=[] if profiles_owned else profile_names,
        )
    except pf.InventoryReadError as exc:
        pf._block_inventory_error(result, side="target", error=exc)
        return
    clones: list[ProfileClone] = []
    if profiles_owned:
        clones = clone_profiles(
            [row for row in resources.get("profiles") or [] if isinstance(row, dict)],
            mappings=mappings,
            usable_networks=view.usable_networks,
            pools=view.pools,
        )
        for clone in clones:
            for code, message, remediation in clone.findings:
                result.block(code, message, remediation)
        view.profile_records = {clone.name: clone.record() for clone in clones}
    result.capabilities["target_project_create"] = True
    result.capabilities["target_project_fingerprint"] = project_policy.policy_fingerprint(
        {"config": view.project_config}
    )
    result.capabilities["rollback"] = True
    result.capabilities["profiles_cloned"] = len(clones)

    staged: list[tuple[dict[str, Any], str, Optional[str], Any]] = []
    pools: dict[str, list[str]] = {}
    for instance in instances:
        name = str(instance["name"])
        status, identity = _source_checks(result, instance, settings=settings, intent="move")
        architecture = instance.get("architecture")
        if (
            architecture
            and target_identity.get("architectures")
            and architecture not in target_identity["architectures"]
        ):
            result.block(
                "architecture_mismatch",
                f"The target cannot run instance '{name}' ({architecture}).",
                "Choose a target cluster with the same CPU architecture.",
            )
        contract, root_pool, _basis = admit_staged(
            result,
            intent="move",
            source=instance,
            source_name=name,
            view=view,
            mappings=mappings,
            target_pool=target_pool,
            target_member=target_member,
            target_client=target_client,
            target_project=target_project,
            subject=f"Instance '{name}': ",
        )
        if root_pool:
            pools.setdefault(root_pool, []).append(name)
        staged.append((instance, status, identity, contract))
    for pool, names in sorted(pools.items()):
        pf._check_capacity(
            result,
            source_client=source_client,
            target_client=target_client,
            source_project=source_project,
            instance_names=names,
            target_pool=pool,
            target_member=target_member,
        )
    result.downtime = "cold" if any(status == "running" for _i, status, _d, _c in staged) else "none"
    if not instances:
        result.block(
            "empty_project_relocation_noop",
            f"Project '{source_project}' has no instances to relocate.",
            "Create the project on the target from Projects instead.",
        )
    result.resources.append(
        {
            "resource_type": "project",
            "source_name": source_project,
            "target_name": target_project,
            "required": True,
            "state": "planned",
        }
    )
    source_profiles = {
        str(row.get("name")): row
        for row in resources.get("profiles") or []
        if isinstance(row, dict) and row.get("name")
    }
    for clone in clones:
        result.resources.append(
            {
                "resource_type": "profile",
                "source_name": clone.name,
                "target_name": clone.name,
                "source_fingerprint": profile_fingerprint(source_profiles.get(clone.name)),
                "target_fingerprint": clone.fingerprint,
                "contract": clone.summary(),
                "required": True,
                "state": "planned",
            }
        )
    for instance, status, identity, contract in sorted(
        staged, key=lambda item: _transfer_order(item[0], item[1])
    ):
        result.resources.append(
            _instance_resource(
                instance, status=status, identity=identity, same_identity=False, contract=contract
            )
        )


__all__ = ["inspect_project"]
