"""How a migration reproduces and compares project configuration.

Every project key outside ``user.*`` is policy: features, limits, restrictions, image and
backup defaults. Policy is copied verbatim to a new target project and must match exactly on an
existing one. ``user.*`` keys are operator metadata (tags, notes) and are copied but never
compared. ``user.orcastra.*`` keys are control-plane markers tied to one daemon object, such as
the create correlation id, so they are neither copied nor compared.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from .security import canonical_json_hash

ORCASTRA_MARKER_PREFIX = "user.orcastra."
MIGRATION_JOB_MARKER = "user.orcastra.migration-job"
# LXD stores these as "true" on a project created without them, so a clone states them.
LXD_DEFAULT_FEATURES = (
    "features.images",
    "features.profiles",
    "features.storage.buckets",
    "features.storage.volumes",
)


def _config(project: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    raw = (project or {}).get("config") if isinstance(project, Mapping) else None
    return dict(raw) if isinstance(raw, Mapping) else {}


def policy_config(config: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in config.items()
        if not str(key).startswith("user.")
    }


def policy_fingerprint(project: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Hash of a project's policy keys, or None when the project does not exist."""
    if project is None:
        return None
    return canonical_json_hash(policy_config(clone_config(project, job_id=None)))


def clone_config(source_project: Mapping[str, Any], *, job_id: Optional[str]) -> dict[str, Any]:
    """Config for a new target project created by a migration job."""
    config = {
        str(key): value
        for key, value in _config(source_project).items()
        if not str(key).startswith(ORCASTRA_MARKER_PREFIX)
    }
    for feature in LXD_DEFAULT_FEATURES:
        config.setdefault(feature, "true")
    if job_id:
        config[MIGRATION_JOB_MARKER] = job_id
    return config


def projects_equivalent(
    source: Optional[Mapping[str, Any]], target: Optional[Mapping[str, Any]]
) -> bool:
    """Whether two projects enforce the same isolation, restrictions, and quotas."""
    if source is None or target is None:
        return False
    expected = policy_config(clone_config(source, job_id=None))
    actual = policy_config(clone_config(target, job_id=None))
    return expected == actual


def created_by_job(project: Optional[Mapping[str, Any]], job_id: str) -> bool:
    return bool(job_id) and _config(project).get(MIGRATION_JOB_MARKER) == job_id


def features_enabled(project: Optional[Mapping[str, Any]], feature: str) -> bool:
    """Whether a project isolates one dependency namespace (``default`` always does)."""
    if project is None:
        return False
    if str(project.get("name") or "") == "default":
        return True
    return str(_config(project).get(f"features.{feature}", "false")).strip().lower() == "true"


# Project resource kinds and the feature that makes them the project's own. With the feature
# off, LXD lists the default project's objects of that kind under the project.
OWNED_KIND_FEATURE = {
    "custom_volumes": ("storage.volumes", "custom storage volumes"),
    "storage_buckets": ("storage.buckets", "storage buckets"),
    "networks": ("networks", "managed networks"),
    "network_acls": ("networks", "network ACLs"),
    "network_address_sets": ("networks", "network address sets"),
    "network_forwards": ("networks", "network forwards"),
    "network_load_balancers": ("networks", "network load balancers"),
    "network_zones": ("networks.zones", "network zones"),
}


def owned_rows(
    record: Optional[Mapping[str, Any]], resources: Mapping[str, list[Any]]
) -> dict[str, list[dict[str, Any]]]:
    """The project's own objects per kind: what its features isolate, managed networks only."""
    owned: dict[str, list[dict[str, Any]]] = {}
    for kind, (feature, _label) in OWNED_KIND_FEATURE.items():
        if not features_enabled(record, feature):
            continue
        rows = [row for row in resources.get(kind) or [] if isinstance(row, dict)]
        if kind == "networks":
            rows = [row for row in rows if row.get("managed")]
        if rows:
            owned[kind] = rows
    placement = [row for row in resources.get("placement_groups") or [] if isinstance(row, dict)]
    if placement:
        owned["placement_groups"] = placement
    return owned


def disposable_images(
    images: list[Mapping[str, Any]], instances: list[Mapping[str, Any]]
) -> set[str]:
    """Fingerprints of project images a relocation may leave behind and Finalize may delete.

    A cached image qualifies, and so does an alias-less image an instance of the project was
    created from (``volatile.base_image``): LXD stores those in a project with its own images,
    sometimes without the cached flag (found live). Any other image was put there on purpose.
    """
    bases = {
        str((instance.get("config") or {}).get("volatile.base_image") or "")
        for instance in instances
        if isinstance(instance, Mapping)
    }
    disposable: set[str] = set()
    for image in images:
        fingerprint = str(image.get("fingerprint") or "")
        if not fingerprint:
            continue
        if image.get("cached") or (not image.get("aliases") and fingerprint in bases):
            disposable.add(fingerprint)
    return disposable


__all__ = [
    "MIGRATION_JOB_MARKER",
    "OWNED_KIND_FEATURE",
    "disposable_images",
    "owned_rows",
    "clone_config",
    "created_by_job",
    "features_enabled",
    "policy_config",
    "policy_fingerprint",
    "projects_equivalent",
]
