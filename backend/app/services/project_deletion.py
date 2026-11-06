"""Delete an LXD project and forget the access rules that named it, for every caller.

The dashboard route and the integration proxy both delete through here, so a project removed
by either leaves no rule behind for a later same-name project of another organization.

important: grants are revoked only once the project is known to be gone. Before the DELETE goes
out, a follow-up row records the rules captured under the cluster lock (services/project_delete_store.py).
A delete whose outcome is unknown leaves that row to the sweeper (services/project_delete_sweeper.py),
which revokes only those rules once the project reads 404, and nothing at all if it is still there
when the row's deadline passes. The row is in Postgres, so a restart loses none of it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Collection, FrozenSet, Optional, Set

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import project_delete_announce as announcer
from app.services import project_delete_store as store
from app.services import project_grants
from app.services.lxd.exceptions import LXDOperationUnknownError, LXDProjectNotFoundError
from app.services.migration_fence import assert_project_not_migration_fenced
from app.services.shared_cluster_audit import ClusterConfigurationBusy, serialize_cluster_configuration

logger = logging.getLogger(__name__)

CLUSTER_LOCK_WAIT_SECONDS = 5.0


@dataclass(frozen=True)
class ProjectDeleted:
    already_gone: bool
    revoke_note: str
    # True when the project was already gone because an earlier delete of it landed: the rules
    # that delete captured were revoked here, and the deletion is real news to announce.
    settled_pending: bool = False


class ProjectDeletesBacklogged(ClusterConfigurationBusy):
    """Too many earlier deletes are still settling; this one is not sent, so it cannot go untracked."""


class ProjectDeleteUnknown(Exception):
    """LXD may still delete the project; the captured rules wait for the sweeper."""

    def __init__(self, host: str, name: str, rule_ids: FrozenSet[int]) -> None:
        self.host = host
        self.name = name
        self.rule_ids = rule_ids
        super().__init__(f"The delete outcome for project '{name}' could not be confirmed")


_background: Set[asyncio.Task] = set()


def spawn(coro: Awaitable[Any]) -> asyncio.Task:
    """Run work that must outlive its request, holding a reference until it ends."""
    task = asyncio.ensure_future(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def delete_project_and_forget(
    db: AsyncSession,
    lxd_service: Any,
    host: str,
    name: str,
    *,
    origin: str = "route",
    requester: Optional[str] = None,
    scope_project: Optional[str] = None,
    send: Optional[Callable[[], Awaitable[None]]] = None,
) -> ProjectDeleted:
    """Delete under the cluster lock, then revoke and announce; raises ProjectDeleteUnknown, never guesses.

    Raises ClusterConfigurationBusy when another configuration write holds the cluster past the
    wait, and MigrationFenceConflict when a migration owns the project. Everything a delete owes
    happens in here, so a caller that is cancelled after it returns owes nothing more.

    `send` replaces the DELETE itself for a caller that must send its own request (the raw proxy
    keeps LXD's answer for its caller, services/raw_project_delete.py). It raises the same
    outcomes `lxd_service.delete_project` does, and anything else counts as a refusal.
    """
    async with serialize_cluster_configuration(db, host, wait_seconds=CLUSTER_LOCK_WAIT_SECONDS):
        await assert_project_not_migration_fenced(cluster_id=host, project=name, db=db)
        rule_ids = await project_grants.rule_ids_naming(db, cluster_id=host, project_name=name)
        followup = await store.open_intent(
            host, name, rule_ids=rule_ids, origin=origin, requester=requester, scope_project=scope_project
        )
        if followup is None:
            raise ProjectDeletesBacklogged(host)
        held_by: Set[int] = set()
        try:
            if send is not None:
                await send()
            else:
                await lxd_service.delete_project(host, name)
        except LXDProjectNotFoundError:
            if followup.created_here:
                # important: a 404 revokes nothing on its own. Anyone with a cluster-wide delete
                # could otherwise name a project that does not exist and strip every
                # organization's rules staged for that name. Only the rules an earlier delete
                # captured go, because that delete is what made the project disappear.
                await store.give_back(followup)
                return ProjectDeleted(already_gone=True, revoke_note="")
            note = await project_grants.forget_deleted_project(
                db, cluster_id=host, project_name=name, only_rule_ids=followup.rule_ids, held_by=held_by
            )
            await _settled(followup, settled_later=True, reached_organizations=held_by)
            return ProjectDeleted(already_gone=True, revoke_note=note, settled_pending=True)
        except LXDOperationUnknownError as exc:
            await store.mark_pending(followup, merge_ids=rule_ids)
            raise ProjectDeleteUnknown(host, name, rule_ids) from exc
        except Exception:
            await store.give_back(followup)
            raise
        note = await project_grants.forget_deleted_project(db, cluster_id=host, project_name=name, held_by=held_by)
    if not followup.created_here:
        # This call's own delete landed while an earlier one's row was open: announce it as this
        # caller's, under that row's event id so a sweep cannot announce it a second time.
        followup = replace(followup, origin=origin, requester=requester, scope_project=scope_project)
    await _settled(followup, settled_later=False, reached_organizations=held_by)
    return ProjectDeleted(already_gone=False, revoke_note=note)


async def _settled(
    followup: store.Followup, *, settled_later: bool, reached_organizations: Collection[int] = ()
) -> None:
    """The project is gone and its rules are revoked: clear the lists now, announce from the row."""
    try:
        await store.mark_settled(followup.id)
    except Exception:  # noqa: BLE001 - the sweeper re-settles a row still open, idempotently
        logger.warning(
            "Could not mark the delete of %s on %s settled", followup.project_name, followup.cluster_id,
            exc_info=True,
        )
    announcer.clear_caches(followup.origin)
    # Every delete path settles here (browser, both key doors, the sweeper), so a key held to its
    # creator's bound stops listing the name on every worker (services/key_party_bound.py).
    from app.services.key_party_bound import forget_cluster_bounds

    forget_cluster_bounds(followup.cluster_id)
    spawn(announcer.announce_and_finish(
        followup, settled_later=settled_later, reached_organizations=frozenset(reached_organizations)
    ))


async def delete_for_integration(
    lxd_service: Any,
    host: str,
    name: str,
    *,
    requester: Optional[str],
    scope_project: Optional[str],
    send: Optional[Callable[[], Awaitable[None]]] = None,
) -> ProjectDeleted:
    """The integration proxy's delete, start to finish in one task the caller only waits on.

    important: run it through `spawn` and await it shielded, so a caller that disconnects mid-delete
    cannot cut short the rule revoke of a project LXD already removed.
    """
    from app.core.database import async_session_maker

    async with async_session_maker() as db:
        return await delete_project_and_forget(
            db, lxd_service, host, name, origin="proxy", requester=requester, scope_project=scope_project,
            send=send,
        )


async def shutdown() -> None:
    tasks = list(_background)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
