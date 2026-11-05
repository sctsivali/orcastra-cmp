"""Which registration of a cluster id an API key grant may reach (models/cluster_registration_models.py).

A grant carries the `registration_id` it was given for and matches only while that registration is
active. Registering an id that is not registered right now starts a new registration, so a grant
written for an earlier one, possibly another organization's, reaches nothing until someone grants
it again. An owner registering a live id again (a new endpoint, say) keeps the registration, so the
grants follow it.

important: the server stamps every grant it writes and ignores a stamp a caller sends. A stamp is
authority, and a request body is not where authority comes from.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Collection, Dict, Iterable, List, Mapping, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.cluster_registration_models import ClusterRegistration, ClusterRegistrationAdoption

logger = logging.getLogger(__name__)

STAMP = "registration_id"
# Fields only ever computed for a reader. A grant write drops them along with any stamp it was sent.
REPORTED_ONLY = ("registered", "rebindable")
CACHE_SECONDS = 15.0
# How old a remembered state may be when the database cannot answer. Past it, a deregistration
# another worker committed may be missing from it, so the answer is unavailable instead.
LAST_KNOWN_MAX_SECONDS = 60.0
_EPOCH_NS = "cluster_generations"
_EPOCH_KEY = "epoch"

# Tests point this at SQLite. None means the application's own session maker.
session_maker: Optional[Callable[[], Any]] = None


class GenerationsUnavailable(Exception):
    """No registration state could be read, and none was known before."""


def _maker() -> Callable[[], Any]:
    if session_maker is not None:
        return session_maker
    from app.core.database import async_session_maker

    return async_session_maker


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Generation:
    cluster_id: str
    registration_id: str
    legacy: bool


def _generation(row: ClusterRegistration) -> Generation:
    return Generation(row.cluster_id, row.registration_id, bool(row.legacy))


@dataclass(frozen=True)
class Generations:
    """The active registration per cluster id. `closed` is False until the legacy backfill ran."""

    active: Mapping[str, Generation]
    closed: bool

    def matches(self, grant: Mapping[str, Any]) -> bool:
        generation = self.active.get(grant.get("cluster_id"))
        if generation is None:
            # Before adoption closes, an id without a row is one registered before generations
            # existed and not yet adopted: its grants keep matching as they did.
            return not self.closed
        stamp = grant.get(STAMP)
        if stamp:
            return stamp == generation.registration_id
        return generation.legacy

    def bound(self, grants: Iterable[Any]) -> List[Dict[str, Any]]:
        return [g for g in grants if isinstance(g, dict) and self.matches(g)]

    def rebindable(self, grant: Mapping[str, Any]) -> bool:
        """An inert grant whose id is registered again: its owner can grant it to that registration."""
        return not self.matches(grant) and grant.get("cluster_id") in self.active


# ── reading ──────────────────────────────────────────────────────────────────────────────────

_cached: Optional[Tuple[float, Optional[str], Generations]] = None
_last_known: Optional[Generations] = None
_last_known_at = 0.0


async def _read(db: Any) -> Generations:
    rows = (
        await db.execute(select(ClusterRegistration).where(ClusterRegistration.ended_at.is_(None)))
    ).scalars().all()
    closed = (await db.get(ClusterRegistrationAdoption, 1)) is not None
    return Generations(active={row.cluster_id: _generation(row) for row in rows}, closed=closed)


def _epoch() -> Optional[str]:
    try:
        from app.core.cache import get_cache_manager

        value = get_cache_manager().get(_EPOCH_NS, _EPOCH_KEY)
        return str(value) if value is not None else None
    except Exception:  # noqa: BLE001 - without the shared epoch the local TTL still bounds staleness
        return None


async def current(*, fresh: bool = False) -> Generations:
    """The registration state, cached per worker and re-read whenever another worker changed it.

    `fresh` is for a write that stamps from it. A database that cannot answer serves the last state
    read for up to LAST_KNOWN_MAX_SECONDS, then nothing.
    """
    global _cached, _last_known, _last_known_at
    epoch = _epoch()
    cached = _cached
    if not fresh and cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS and cached[1] == epoch:
        return cached[2]
    try:
        async with _maker()() as db:
            value = await _read(db)
    except Exception as exc:  # noqa: BLE001 - reported to the caller as unavailable, below
        if _last_known is not None and not fresh and time.monotonic() - _last_known_at < LAST_KNOWN_MAX_SECONDS:
            logger.warning("Registration generations unreadable, serving the last known: %s", exc)
            return _last_known
        raise GenerationsUnavailable(str(exc)) from exc
    _cached = (time.monotonic(), epoch, value)
    _last_known = value
    _last_known_at = time.monotonic()
    return value


async def current_or_none() -> Optional[Generations]:
    """For checks that keep working without it (scope, event routing, reports): None means count
    every grant, as before generations; also None with the KEY_GRANT_GENERATIONS kill switch off."""
    from app.core.config import get_settings

    if not get_settings().key_grant_generations:
        return None
    try:
        return await current()
    except GenerationsUnavailable:
        logger.warning("Registration generations unreadable; counting every grant")
        return None


def reachable(grants: Any, generations: Optional[Generations]) -> set:
    """The cluster ids a list of grants still reaches (every one named, when `generations` is None)."""
    dicts = [g for g in (grants or []) if isinstance(g, dict)]
    if generations is not None:
        dicts = generations.bound(dicts)
    return {g.get("cluster_id") for g in dicts}


def known() -> Optional[Generations]:
    """The last state this worker read, for labelling what a reader sees; None before any read.

    note: display only. Every decision about access reads `current()`.
    """
    return _last_known


def changed() -> None:
    """After a registration, a deregistration or the backfill commits: every worker reads again."""
    global _cached
    _cached = None
    try:
        from app.core.cache import get_cache_manager

        get_cache_manager().set(_EPOCH_NS, _EPOCH_KEY, uuid.uuid4().hex, ttl=30 * 86400)
    except Exception:  # noqa: BLE001 - the other workers fall back to their 15 s TTL
        logger.debug("Could not publish a registration generation change")


# ── registration and deregistration ──────────────────────────────────────────────────────────


async def _active_row(db: Any, cluster_id: str) -> Optional[ClusterRegistration]:
    return (
        await db.execute(
            select(ClusterRegistration).where(
                ClusterRegistration.cluster_id == cluster_id, ClusterRegistration.ended_at.is_(None)
            )
        )
    ).scalar_one_or_none()


async def active_registration(db: Any, cluster_id: str) -> Optional[ClusterRegistration]:
    """The cluster id's registration still in force, if any."""
    return await _active_row(db, cluster_id)


async def _ever_registered(db: Any, cluster_id: str) -> bool:
    return (
        await db.execute(select(ClusterRegistration.id).where(ClusterRegistration.cluster_id == cluster_id).limit(1))
    ).scalar_one_or_none() is not None


async def _adoption_closed(db: Any) -> bool:
    return (await db.get(ClusterRegistrationAdoption, 1)) is not None


async def begin_registration(db: Any, cluster_id: str, *, live: bool) -> Generation:
    """The registration a register call establishes; in its transaction, under the cluster's lock.

    `live` is whether the registry, read from Vault itself just now, already lists the id. Only
    then is this an owner registering a cluster again, and the active registration (and every grant
    made for it) carries on. Otherwise an active row is left over from a deregistration that did
    not finish, and it ends here.
    """
    now = _now()
    active = await _active_row(db, cluster_id)
    if active is not None:
        if live:
            return _generation(active)
        active.ended_at = now
        active.ended_reason = "replaced"
        await db.flush()
    elif live and not await _adoption_closed(db) and not await _ever_registered(db, cluster_id):
        # A cluster registered before generations existed, registered again before the backfill
        # reached it: adopt it, so the grants written for it keep working.
        row = ClusterRegistration(cluster_id=cluster_id, registration_id=uuid.uuid4().hex, legacy=True, registered_at=now)
        db.add(row)
        await db.flush()
        return _generation(row)
    row = ClusterRegistration(cluster_id=cluster_id, registration_id=uuid.uuid4().hex, legacy=False, registered_at=now)
    db.add(row)
    await db.flush()
    return _generation(row)


async def end_registration(db: Any, cluster_id: str) -> Generation:
    """End the id's registration; in the deregistration's transaction, under the cluster's lock.

    An id never adopted (registered before generations, deregistered before the backfill) gets an
    ended legacy row, so its unstamped grants match nothing and the backfill cannot adopt it later.
    """
    now = _now()
    active = await _active_row(db, cluster_id)
    if active is None:
        active = ClusterRegistration(
            cluster_id=cluster_id, registration_id=uuid.uuid4().hex, legacy=True, registered_at=now
        )
        db.add(active)
    active.ended_at = now
    active.ended_reason = "deregistered"
    await db.flush()
    return _generation(active)


async def backfill_legacy(read_registry: Callable[[], Any]) -> Optional[int]:
    """Adopt every registered id that has no registration row as legacy, then close adoption.

    Returns how many were adopted, 0 when adoption was already closed, or None when it could not
    run (the registry was not read from Vault itself). Idempotent: every worker may run it.
    """
    from app.services.shared_cluster_audit import serialize_cluster_configuration

    maker = _maker()
    async with maker() as db:
        if await _adoption_closed(db):
            return 0
    read = await asyncio.to_thread(read_registry)
    if getattr(read, "source", None) != "vault":
        return None
    adopted = 0
    for cluster_id in sorted(read.cluster_ids):
        async with maker() as db:
            async with serialize_cluster_configuration(db, cluster_id, wait_seconds=30):
                if not await _ever_registered(db, cluster_id):
                    db.add(ClusterRegistration(cluster_id=cluster_id, registration_id=uuid.uuid4().hex, legacy=True))
                    adopted += 1
                await db.commit()
    async with maker() as db:
        db.add(ClusterRegistrationAdoption(id=1, adopted=adopted))
        try:
            await db.commit()
        except IntegrityError:
            # Another worker closed it first.
            await db.rollback()
    changed()
    return adopted


_backfill_task: Optional[asyncio.Task] = None


async def _backfill_until_closed(read_registry: Callable[[], Any], retry_seconds: float) -> None:
    while True:
        try:
            adopted = await backfill_legacy(read_registry)
            if adopted is not None:
                logger.info("Registration generations ready: %s cluster(s) adopted as legacy", adopted)
                return
            logger.warning("Registration generations: registry not readable from Vault; retrying")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - retried; until then unstamped grants match as before
            logger.warning("Registration generation backfill failed; retrying", exc_info=True)
        await asyncio.sleep(retry_seconds)


def start_backfill(read_registry: Callable[[], Any], retry_seconds: float = 60.0) -> None:
    global _backfill_task
    if _backfill_task is None or _backfill_task.done():
        _backfill_task = asyncio.get_running_loop().create_task(
            _backfill_until_closed(read_registry, retry_seconds)
        )


async def stop_backfill() -> None:
    global _backfill_task
    task, _backfill_task = _backfill_task, None
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ── what a key holds, writes, and shows ──────────────────────────────────────────────────────


def bind(metadata: Dict[str, Any], generations: Generations) -> Dict[str, Any]:
    """The key as it may act now: grants for ended registrations removed, in a copy.

    important: a copy. The validation cache stores the document it was handed, and a grant removed
    from it in place would stay removed after the cluster was granted again.
    """
    grants = [g for g in (metadata.get("cluster_permissions") or []) if isinstance(g, dict)]
    bound = generations.bound(grants)
    if len(bound) == len(grants):
        return metadata
    return {**metadata, "cluster_permissions": bound, "_held_cluster_permissions": grants}


def stamp(
    requested: Iterable[Mapping[str, Any]],
    held: Iterable[Any],
    generations: Generations,
    rebind: Collection[str] = (),
) -> List[Dict[str, Any]]:
    """The grants to store, each stamped by the server; a stamp in the request is ignored.

    A grant the key already held for a registration that has ended keeps its old stamp (it stays
    inert) unless its id is in `rebind`: an edit about something else must not hand it the
    cluster now registered under that id. A new grant to an id with no active registration is 422.
    """
    held_by_id = {g.get("cluster_id"): g for g in held if isinstance(g, dict)}
    stored: List[Dict[str, Any]] = []
    for grant in requested:
        clean = {k: v for k, v in grant.items() if k != STAMP and k not in REPORTED_ONLY}
        cluster_id = clean.get("cluster_id")
        before = held_by_id.get(cluster_id)
        if before is not None and not generations.matches(before) and cluster_id not in rebind:
            if before.get(STAMP):
                clean[STAMP] = before[STAMP]
            stored.append(clean)
            continue
        generation = generations.active.get(cluster_id)
        if generation is not None:
            clean[STAMP] = generation.registration_id
        elif generations.closed:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "cluster_not_registered",
                    "message": f"No cluster is registered under: {cluster_id}",
                },
            )
        stored.append(clean)
    return stored


async def stamped(
    requested: Iterable[Mapping[str, Any]], held: Iterable[Any] = (), rebind: Collection[str] = ()
) -> List[Dict[str, Any]]:
    """`stamp` from a fresh read of the registrations, for a grant write; 503 when it cannot read.

    With the KEY_GRANT_GENERATIONS kill switch off, grants are stored unstamped (a caller's stamp
    still dropped), as before generations existed.
    """
    from app.core.config import get_settings

    if not get_settings().key_grant_generations:
        return [{k: v for k, v in g.items() if k != STAMP and k not in REPORTED_ONLY} for g in requested]
    try:
        generations = await current(fresh=True)
    except GenerationsUnavailable:
        raise HTTPException(
            status_code=503, detail="Cluster registrations could not be read. Try again in a moment."
        ) from None
    return stamp(requested, held, generations, rebind)


def annotate(grants: Iterable[Any], generations: Generations, *, for_managers: bool = False) -> List[Any]:
    """Grants as a reader sees them: no stamp, and an inert one marked `registered: false`.

    `rebindable` only `for_managers` (the key list and Edit Access). Told to the key holder, it
    would confirm that an id it once reached is registered again, possibly by someone else.
    """
    shown: List[Any] = []
    for grant in grants:
        if not isinstance(grant, dict):
            shown.append(grant)
            continue
        view = {k: v for k, v in grant.items() if k != STAMP}
        if not generations.matches(grant):
            view["registered"] = False
            if for_managers and generations.rebindable(grant):
                view["rebindable"] = True
        shown.append(view)
    return shown
