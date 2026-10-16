"""Bounded synchronous daemon calls for project-level relocation: profiles, images, projects.

Profile writes are synchronous on LXD. An image delete returns an operation. A 404 on a read
means the object is gone and is returned as None, never as an error.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.lxd_path import safe_segment

from .executor import _is_not_found, _metadata, _operation, _sync_success


def read_profile(client: Any, *, project: str, name: str) -> Optional[dict[str, Any]]:
    try:
        metadata = _metadata(
            client.api.profiles[safe_segment(name)].get(params={"project": project})
        )
    except Exception as exc:
        if _is_not_found(exc):
            return None
        raise
    return metadata if isinstance(metadata, dict) else None


def list_profiles(client: Any, *, project: str) -> list[dict[str, Any]]:
    metadata = _metadata(client.api.profiles.get(params={"project": project, "recursion": 1}))
    if not isinstance(metadata, list):
        raise RuntimeError("The daemon returned an unreadable profile list.")
    return [row for row in metadata if isinstance(row, dict)]


def create_profile(client: Any, *, project: str, record: dict[str, Any]) -> None:
    response = client.api.profiles.post(
        json={
            "name": record["name"],
            "description": record.get("description") or "",
            "config": dict(record.get("config") or {}),
            "devices": dict(record.get("devices") or {}),
        },
        params={"project": project},
    )
    if not _sync_success(response):
        raise RuntimeError(f"Profile '{record['name']}' create did not answer synchronously.")


def replace_profile(client: Any, *, project: str, record: dict[str, Any]) -> None:
    """Set a profile's whole definition. Used for the default profile LXD creates itself."""
    response = client.api.profiles[safe_segment(str(record["name"]))].put(
        json={
            "description": record.get("description") or "",
            "config": dict(record.get("config") or {}),
            "devices": dict(record.get("devices") or {}),
        },
        params={"project": project},
    )
    if not _sync_success(response):
        raise RuntimeError(f"Profile '{record['name']}' update did not answer synchronously.")


def delete_profile(client: Any, *, project: str, name: str) -> None:
    try:
        client.api.profiles[safe_segment(name)].delete(params={"project": project})
    except Exception as exc:
        if _is_not_found(exc):
            return
        raise


def list_images(client: Any, *, project: str) -> list[dict[str, Any]]:
    metadata = _metadata(client.api.images.get(params={"project": project, "recursion": 1}))
    if not isinstance(metadata, list):
        raise RuntimeError("The daemon returned an unreadable image list.")
    return [row for row in metadata if isinstance(row, dict)]


def submit_delete_image(client: Any, *, project: str, fingerprint: str) -> Optional[str]:
    try:
        response = client.api.images[safe_segment(fingerprint)].delete(
            params={"project": project}
        )
    except Exception as exc:
        if _is_not_found(exc):
            return None
        raise
    if _sync_success(response):
        return None
    _, operation_id = _operation(response, client)
    return operation_id


__all__ = [
    "create_profile",
    "delete_profile",
    "list_images",
    "list_profiles",
    "read_profile",
    "replace_profile",
    "submit_delete_image",
]
