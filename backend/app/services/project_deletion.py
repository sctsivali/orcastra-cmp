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
from typing import Any, Awaitable, Callable, Dict, FrozenSet, Optional, Set, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import project_grants
from app.services.lxd import health
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
MAX_FOLLOW_UPS_PER_HOST = 2

Finish = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class ProjectDeleted:
    already_gone: bool
    revoke_note: str
    # True when the project was already gone because a delete this worker was following landed:
    # the rules it captured were revoked here, and the deletion is real news to announce.
    settled_pending: bool = False


class ProjectDeleteUnknown(Exception):
    """LXD may still delete the project; the captured rules wait for the follow-up."""

    def __init__(self, host: str, name: str, rule_ids: FrozenSet[int]) -> None:
        self.host = host
        self.name = name
        self.rule_ids = rule_ids
        super().__init__(f"The delete outcome for project '{name}' could not be confirmed")


@dataclass
class _FollowUp:
    rule_ids: Set[int]
    task: Optional[asyncio.Task] = None


# One follow-up per (cluster, project): a repeated unknown delete adds its captured rules to it.
_pending: Dict[Tuple[str, str], _FollowUp] = {}
_background: Set[asyncio.Task] = set()


def spawn(coro: Awaitable[Any]) -> asyncio.Task:
    """Run work that must outlive its request, holding a reference until it ends."""
    task = asyncio.ensure_future(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


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
            # important: a 404 revokes nothing on its own. Anyone with a cluster-wide delete could
            # otherwise name a project that does not exist and strip every organization's rules
            # staged for that name. Only the rules a pending follow-up captured go, because that
            # follow-up's delete is what made the project disappear.
            pending = _pending.pop((host, name), None)
            if pending is None:
                return ProjectDeleted(already_gone=True, revoke_note="")
            if pending.task is not None:
                pending.task.cancel()
            note = await project_grants.forget_deleted_project(
                db, cluster_id=host, project_name=name, only_rule_ids=frozenset(pending.rule_ids)
            )
            return ProjectDeleted(already_gone=True, revoke_note=note, settled_pending=True)
        except LXDOperationUnknownError as exc:
            raise ProjectDeleteUnknown(host, name, rule_ids) from exc
        note = await project_grants.forget_deleted_project(db, cluster_id=host, project_name=name)
        return ProjectDeleted(already_gone=False, revoke_note=note)


async def delete_for_integration(lxd_service: Any, host: str, name: str, finish: Finish) -> ProjectDeleted:
    """The integration proxy's delete, start to finish in one task the caller only waits on.

    important: run it through `spawn` and await it shielded. Everything a delete owes (the rule
    revoke, the follow-up of an unknown outcome, the announcement) happens in here, so a caller
    that disconnects mid-delete cannot strand the rules of a project LXD already removed.
    """
    from app.core.database import async_session_maker

    async with async_session_maker() as db:
        try:
            outcome = await delete_project_and_forget(db, lxd_service, host, name)
        except ProjectDeleteUnknown as unknown:
            follow_unknown_delete(lxd_service, unknown, finish)
            raise
    if not outcome.already_gone or outcome.settled_pending:
        await finish()
    return outcome


def follow_unknown_delete(
    lxd_service: Any,
    unknown: ProjectDeleteUnknown,
    finish: Finish,
    session_maker: Optional[Callable[[], Any]] = None,
) -> bool:
    """Follow one unknown delete to its end; False when it could not be followed.

    A repeat for the same project joins the running follow-up. Bounded per worker and per
    cluster, so a cluster that never answers its probes cannot take every slot.
    """
    key = (unknown.host, unknown.name)
    current = _pending.get(key)
    if current is not None:
        current.rule_ids |= set(unknown.rule_ids)
        return True
    on_host = sum(1 for (host, _name) in _pending if host == unknown.host)
    if len(_pending) >= MAX_FOLLOW_UPS or on_host >= MAX_FOLLOW_UPS_PER_HOST:
        logger.warning(
            "Not following the unknown delete of project %s on %s: %d follow-ups running, %d on it",
            unknown.name, unknown.host, len(_pending), on_host,
        )
        return False
    if session_maker is None:
        from app.core.database import async_session_maker as session_maker
    record = _FollowUp(rule_ids=set(unknown.rule_ids))
    _pending[key] = record
    record.task = spawn(_follow(lxd_service, key, record, finish, session_maker))
    record.task.add_done_callback(lambda _t: _pending.pop(key, None) if _pending.get(key) is record else None)
    return True


async def _exists(lxd_service: Any, host: str, name: str) -> Optional[bool]:
    if health.is_tripped(host):
        # A cluster the breaker has given up on would only hold a probe thread; wait for it.
        return None
    try:
        return await lxd_service.project_exists(host, name)
    except LXDOperationUnknownError:
        return None


async def _follow(
    lxd_service: Any,
    key: Tuple[str, str],
    record: _FollowUp,
    finish: Finish,
    session_maker: Callable[[], Any],
) -> None:
    host, name = key
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
                            db, cluster_id=host, project_name=name, only_rule_ids=frozenset(record.rule_ids)
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
    tasks = list(_background)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _pending.clear()
