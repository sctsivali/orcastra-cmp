"""Whether an add-on installation hears about a project: the webhook side of the party bound.

An event's project must be in the installation's grant for that cluster, held to the installer's
bound where another party holds the cluster (services/key_party_bound.py), judged when the event
is queued (emitter.py). Routing by cluster alone would let an add-on hear every project on a
cluster it reaches, another party's included. Delivery re-checks the stored grant (dispatcher.py);
the delivery log and redeliver judge the current bound (routers/extension_webhooks.py).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple

from app.services.key_party_bound import held_to, party_bound

logger = logging.getLogger(__name__)


# How long one installer's reach may take to judge before it is treated as not reached, how long
# an event waits for the identity provider before the installer's bound applies, and the one
# deadline every installer of an event shares (the instance create emit has 2 s in all).
REACH_SECONDS = 3
ROLE_SECONDS = 1.0
EVENT_REACH_SECONDS = 1.5
PROJECT_DELETED = "orcastra.project.deleted.v1"

# One bound per installer and cluster for a caller judging many subscriptions or rows.
ReachMemo = Dict[Tuple[str, str], Any]
_UNREADABLE = object()


def _grants_naming(grants: List[Mapping[str, Any]], cluster_id: str, project: Optional[str]) -> bool:
    wanted = (project or "").strip() or "default"
    for grant in grants:
        if grant.get("cluster_id") != cluster_id:
            continue
        projects = grant.get("projects", ["default"])
        if "*" in projects or wanted in projects:
            return True
    return False


def _grants_on(install: Any, cluster_id: str) -> List[Mapping[str, Any]]:
    return [
        g for g in (getattr(install, "granted_scope", None) or [])
        if isinstance(g, dict) and g.get("cluster_id") == cluster_id
    ]


def install_grant_names(install: Any, cluster_id: str, project: Optional[str]) -> bool:
    """Whether the installation's stored grant names `project` on `cluster_id` (no bound, no I/O)."""
    return _grants_naming(_grants_on(install, cluster_id), cluster_id, project)


# One bound read per (installer, cluster) in flight on a worker, and at most this many at once, so
# a burst of events on a cold cluster does not take a pool connection per event and installer.
BOUND_READS_AT_ONCE = 8
_bound_reads: Dict[Tuple[str, str], "asyncio.Task[Optional[FrozenSet[str]]]"] = {}
_slots: Optional[Tuple[Any, "asyncio.Semaphore"]] = None


def _bound_slots() -> "asyncio.Semaphore":
    global _slots
    loop = asyncio.get_running_loop()
    if _slots is None or _slots[0] is not loop:
        _slots = (loop, asyncio.Semaphore(BOUND_READS_AT_ONCE))
    return _slots[1]


def _bound_read_done(key: Tuple[str, str], task: "asyncio.Task[Any]") -> None:
    if _bound_reads.get(key) is task:
        _bound_reads.pop(key, None)
    if not task.cancelled():
        task.exception()  # read, so a failure is not reported as never retrieved


async def _read_installer_bound(as_key: Dict[str, str], cluster_id: str) -> Optional[FrozenSet[str]]:
    # The slot covers the database read alone: an own-side read never queues behind held ones
    # waiting on the identity provider.
    bound, _unknown = await asyncio.wait_for(
        party_bound(as_key, cluster_id, db=None, role_seconds=ROLE_SECONDS, db_gate=_bound_slots()), REACH_SECONDS
    )
    return bound


async def _installer_bound(install: Any, cluster_id: str, memo: Optional[ReachMemo]) -> Optional[FrozenSet[str]]:
    """The installer's bound on `cluster_id`, None for none; raises when it cannot be read."""
    key = (str(getattr(install, "installed_by", "") or ""), cluster_id)
    if memo is not None and key in memo:
        known = memo[key]
        if known is _UNREADABLE:
            raise RuntimeError("the installer's bound could not be read")
        return known
    loop = asyncio.get_running_loop()
    task = _bound_reads.get(key)
    if task is None or task.done() or task.get_loop() is not loop:
        as_key = {"id": str(getattr(install, "api_key_id", "") or ""), "created_by": key[0]}
        task = loop.create_task(_read_installer_bound(as_key, cluster_id))
        _bound_reads[key] = task
        task.add_done_callback(lambda done, k=key: _bound_read_done(k, done))
    try:
        # Shielded: a caller that stops waiting leaves the read to fill the cache for the next one.
        bound = await asyncio.shield(task)
    except Exception:
        if memo is not None:
            memo[key] = _UNREADABLE
        raise
    if memo is not None:
        memo[key] = bound
    return bound


async def read_bounds(pairs: Iterable[Tuple[Any, str]], memo: ReachMemo, seconds: float) -> None:
    """Read the bound of every distinct (installer, cluster) at once, under one deadline.

    A pair still unread at the deadline counts as unreadable, so one slow installer does not cost
    the others their events, and a page of the delivery log waits once.
    """
    tasks: Dict[Tuple[str, str], "asyncio.Task[Any]"] = {}
    for install, cluster_id in pairs:
        key = (str(getattr(install, "installed_by", "") or ""), cluster_id)
        if key in memo or key in tasks:
            continue
        tasks[key] = asyncio.ensure_future(_installer_bound(install, cluster_id, memo))
    if not tasks:
        return
    late: set = set(tasks.values())
    try:
        done, late = await asyncio.wait(tasks.values(), timeout=seconds)
        for task in done:
            task.exception()  # read, so a failure is not reported as never retrieved
    finally:
        # Also when the caller is cancelled (the instance create emit has 2 s): no waiter is left.
        for task in late:
            task.cancel()
        await asyncio.gather(*late, return_exceptions=True)
    for key, task in tasks.items():
        if task in late:
            memo[key] = _UNREADABLE


async def install_reaches_project(
    install: Any, cluster_id: str, project: Optional[str], *,
    deleted_and_held: Optional[bool] = None, memo: Optional[ReachMemo] = None,
) -> bool:
    """Whether an add-on installation may hear about `project` on `cluster_id`.

    The event's project must be in the installation's grant for that cluster, and where another
    party holds the cluster for the installer, that grant is held to the installer's bound exactly
    as its key is. The bound is read in its own session, once per installer for a `memo`, and waits
    ROLE_SECONDS at most for the identity provider. Raises when it cannot be read.

    `deleted_and_held`: for a project delete, whether the delete named the installation's
    organization as one whose policies held the project. The project is in no bound by then, so
    a held one is judged by the stored grant; on a cluster another party holds, one not held is not
    heard, so a party that reuses the name is not told its own project went away.
    """
    grants = _grants_on(install, cluster_id)
    if not _grants_naming(grants, cluster_id, project):
        return False
    if deleted_and_held:
        return True
    bound = await _installer_bound(install, cluster_id, memo)
    if bound is None:
        return True
    if deleted_and_held is False:
        return False
    return _grants_naming([held_to(g, bound) for g in grants], cluster_id, project)


def payload_reach(payload: Mapping[str, Any]) -> Optional[Tuple[str, str, bool]]:
    """(cluster, project, is a project delete) an instance, volume or project payload is about;
    None for an event about the installation itself; ("", "", False) for one that names none."""
    data = (payload or {}).get("data") or {}
    resource_type = data.get("resource_type")
    if resource_type not in ("instance", "volume", "project"):
        return None
    cluster_id = data.get("cluster_id")
    project = data.get("resource_name") if resource_type == "project" else data.get("project")
    if not cluster_id or not project:
        return ("", "", False)
    return (str(cluster_id), str(project), (payload or {}).get("type") == PROJECT_DELETED)


async def payload_reaches(install: Any, payload: Mapping[str, Any], memo: Optional[ReachMemo] = None) -> bool:
    """Whether a queued webhook payload is about something this installation reaches now.

    An instance or volume event names its project; one that names none is not reached. A project
    event is about the project it names; a queued project delete is judged as one the delete did
    not name as held (the queue does not keep that), so on a cluster another party holds it is not
    reached. An event about the installation itself always is. A bound that cannot be read is not
    reached.
    """
    about = payload_reach(payload)
    if about is None:
        return True
    cluster_id, project, deleted = about
    if not cluster_id:
        return False
    try:
        return await install_reaches_project(
            install, cluster_id, project, deleted_and_held=False if deleted else None, memo=memo
        )
    except Exception as exc:  # noqa: BLE001 - an unknown reach is not a delivery
        logger.warning("Could not tell whether an installation reaches %s on %s: %s", project, cluster_id, type(exc).__name__)
        return False
