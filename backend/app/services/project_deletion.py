"""Delete an LXD project and forget the access rules that named it, for every caller.

The dashboard route and the integration proxy both delete through here, so a project removed
by either leaves no rule behind for a later same-name project of another organization.

important: grants are revoked only once the project is known to be gone. A delete whose outcome
is unknown (LXD still working when the request had to answer) is followed in the background; the
follow-up revokes only the rules captured under the cluster lock before the delete was sent, and
nothing at all if the project is still there when its window ends.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, FrozenSet, Optional, Set

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import project_grants
from app.services.lxd.exceptions import LXDOperationUnknownError, LXDProjectNotFoundError
from app.services.migration_fence import assert_project_not_migration_fenced
from app.services.shared_cluster_audit import (
    ClusterConfigurationBusy,
    serialize_cluster_configuration,
)

logger = logging.getLogger(__name__)

CLUSTER_LOCK_WAIT_SECONDS = 5.0
FOLLOW_WINDOW_SECONDS = 120.0
FOLLOW_INTERVAL_SECONDS = 5.0
FOLLOW_LOCK_WAIT_SECONDS = 30.0
MAX_FOLLOW_UPS = 16

Finish = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class ProjectDeleted:
    already_gone: bool
    revoke_note: str


class ProjectDeleteUnknown(Exception):
    """LXD may still delete the project; the captured rules wait for the follow-up."""

    def __init__(self, host: str, name: str, rule_ids: FrozenSet[int]) -> None:
        self.host = host
        self.name = name
        self.rule_ids = rule_ids
        super().__init__(f"The delete outcome for project '{name}' could not be confirmed")


async def delete_project_and_forget(
    db: AsyncSession, lxd_service: Any, host: str, name: str
) -> ProjectDeleted:
    """Delete under the cluster lock; raises ProjectDeleteUnknown, never guesses.

    Raises ClusterConfigurationBusy when another configuration write holds the cluster past the
    wait, and MigrationFenceConflict when a migration owns the project.
    """
    async with serialize_cluster_configuration(db, host, wait_seconds=CLUSTER_LOCK_WAIT_SECONDS):
        await assert_project_not_migration_fenced(cluster_id=host, project=name, db=db)
        rule_ids = await project_grants.rule_ids_naming(db, cluster_id=host, project_name=name)
        try:
            await lxd_service.delete_project(host, name)
        except LXDProjectNotFoundError:
            # A delete that ended unknown, then lost its follow-up (restart, other worker), is
            # retried by the operator and meets 404 here. The rules must still go.
            note = await project_grants.forget_deleted_project(db, cluster_id=host, project_name=name)
            return ProjectDeleted(already_gone=True, revoke_note=note)
        except LXDOperationUnknownError as exc:
            raise ProjectDeleteUnknown(host, name, rule_ids) from exc
        note = await project_grants.forget_deleted_project(db, cluster_id=host, project_name=name)
        return ProjectDeleted(already_gone=False, revoke_note=note)


_follow_ups: Set[asyncio.Task] = set()


def follow_unknown_delete(
    lxd_service: Any,
    unknown: ProjectDeleteUnknown,
    finish: Finish,
    session_maker: Optional[Callable[[], Any]] = None,
) -> bool:
    """Start one bounded follow-up; False when the per-worker limit is already reached."""
    if len(_follow_ups) >= MAX_FOLLOW_UPS:
        logger.warning(
            "Not following the unknown delete of project %s on %s: %d follow-ups already running",
            unknown.name, unknown.host, len(_follow_ups),
        )
        return False
    if session_maker is None:
        from app.core.database import async_session_maker as session_maker
    task = asyncio.create_task(_follow(lxd_service, unknown, finish, session_maker))
    _follow_ups.add(task)
    task.add_done_callback(_follow_ups.discard)
    return True


async def _exists(lxd_service: Any, host: str, name: str) -> Optional[bool]:
    try:
        return await lxd_service.project_exists(host, name)
    except LXDOperationUnknownError:
        return None


async def _follow(
    lxd_service: Any,
    unknown: ProjectDeleteUnknown,
    finish: Finish,
    session_maker: Callable[[], Any],
) -> None:
    host, name = unknown.host, unknown.name
    loop = asyncio.get_running_loop()
    deadline = loop.time() + FOLLOW_WINDOW_SECONDS
    try:
        while loop.time() < deadline:
            await asyncio.sleep(FOLLOW_INTERVAL_SECONDS)
            if await _exists(lxd_service, host, name) is not False:
                continue
            try:
                async with session_maker() as db:
                    async with serialize_cluster_configuration(
                        db, host, wait_seconds=FOLLOW_LOCK_WAIT_SECONDS
                    ):
                        # Re-read under the lock: a same-name create may have landed since.
                        present = await _exists(lxd_service, host, name)
                        if present is None:
                            continue
                        if present:
                            logger.info(
                                "Project %s on %s exists again; the earlier delete revokes nothing",
                                name, host,
                            )
                            return
                        await project_grants.forget_deleted_project(
                            db, cluster_id=host, project_name=name, only_rule_ids=unknown.rule_ids
                        )
            except ClusterConfigurationBusy:
                continue
            await finish()
            logger.info("Project %s on %s finished deleting after its request answered", name, host)
            return
        logger.warning(
            "Project %s on %s was still present %ss after its delete; no rule was revoked",
            name, host, int(FOLLOW_WINDOW_SECONDS),
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a follow-up must never take the worker down
        logger.warning("Following the unknown delete of %s on %s failed", name, host, exc_info=True)


async def shutdown() -> None:
    for task in list(_follow_ups):
        task.cancel()
    if _follow_ups:
        await asyncio.gather(*_follow_ups, return_exceptions=True)
