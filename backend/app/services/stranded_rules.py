"""Access rules a raw-proxy write leaves pointing at a name nothing answers to.

A raw write can remove a project or an instance from under the name a tenant rule holds without
deleting through the dashboard: an instance DELETE, an instance renamed or moved to another
project, a project renamed. The rule would then hand the next object created under that name to
the same tenant. The raw project DELETE has its own path (services/raw_project_delete.py); this
module covers the rest.

Rules are captured before the write goes out and forgotten only once LXD reports success; a
refusal or an unknown outcome leaves them in place for the orphan report.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, FrozenSet, Optional

from fastapi import HTTPException

from app.core.raw_proxy_path import canonical_proxy_path
from app.services import instance_grants, project_grants

logger = logging.getLogger(__name__)

INSTANCE_FAMILIES = ("instances", "containers", "virtual-machines")


@dataclass(frozen=True)
class StrandTarget:
    kind: str  # "instance" or "project"
    project_name: str
    instance_name: Optional[str] = None


def raw_stranding_target(method: str, path: Optional[str], body: Any, project: str) -> Optional[StrandTarget]:
    """What a raw write would strand, or None when it strands nothing.

    - `DELETE /1.0/<instances|containers|virtual-machines>/<n>`: the instance.
    - `POST` to the same path with a body `name` other than `<n>`, or a body `project` other than
      the request's: a rename or a move, which leaves the original (project, name) empty.
    - `POST /1.0/projects/<n>` with a body `name` other than `<n>`: a project rename.
    """
    verb = (method or "").upper()
    if verb not in ("DELETE", "POST"):
        return None
    try:
        canonical = canonical_proxy_path(path)
    except HTTPException:
        return None
    if len(canonical.segments) != 2:
        return None
    family, name = canonical.lowered[0], canonical.segments[1]
    payload = body if isinstance(body, dict) else {}
    if family in INSTANCE_FAMILIES:
        if verb == "DELETE":
            return StrandTarget("instance", project, name)
        renamed = isinstance(payload.get("name"), str) and payload["name"] and payload["name"] != name
        moved = isinstance(payload.get("project"), str) and payload["project"] and payload["project"] != project
        return StrandTarget("instance", project, name) if (renamed or moved) else None
    if family == "projects" and verb == "POST":
        renamed = isinstance(payload.get("name"), str) and payload["name"] and payload["name"] != name
        return StrandTarget("project", name) if renamed else None
    return None


async def _project_rule_ids(cluster_id: str, project_name: str) -> FrozenSet[int]:
    from app.core.database import async_session_maker

    try:
        async with async_session_maker() as db:
            return await project_grants.rule_ids_naming(db, cluster_id=cluster_id, project_name=project_name)
    except Exception:  # noqa: BLE001 - a capture failure leaves the rules for the orphan report
        logger.warning("Could not read the rules naming project %s on %s", project_name, cluster_id, exc_info=True)
        return frozenset()


async def capture(cluster_id: str, target: Optional[StrandTarget]) -> Optional[Callable[[str], Awaitable[None]]]:
    """Capture the rules `target` would strand; returns the callback to run with LXD's outcome."""
    if target is None:
        return None
    if target.kind == "instance":
        rule_ids = await instance_grants.rule_ids_naming_instance(
            cluster_id, target.project_name, target.instance_name or ""
        )
    else:
        rule_ids = await _project_rule_ids(cluster_id, target.project_name)
    if not rule_ids:
        return None

    async def forget(outcome: str) -> None:
        if outcome != "Success":
            return
        if target.kind == "instance":
            await instance_grants.forget_deleted_instance(
                cluster_id=cluster_id,
                project_name=target.project_name,
                instance_name=target.instance_name or "",
                only_rule_ids=rule_ids,
                reason="raw_write",
            )
            return
        from app.core.database import async_session_maker

        async with async_session_maker() as db:
            await project_grants.forget_deleted_project(
                db, cluster_id=cluster_id, project_name=target.project_name, only_rule_ids=rule_ids
            )

    return forget
