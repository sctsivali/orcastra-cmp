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


__all__ = [
    "MIGRATION_JOB_MARKER",
    "clone_config",
    "created_by_job",
    "features_enabled",
    "policy_config",
    "policy_fingerprint",
    "projects_equivalent",
]
