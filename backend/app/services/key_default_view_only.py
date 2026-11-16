"""`default` stays view only for a key a partner created, where another party holds the cluster.

The browser rule (org_scope.refuse_default_operation) reads the request's own org scope, which no
key route carries, so on its own it would let a key change, open and read inside another party's
guests in `default` through the structured proxy, MCP and the raw proxy. A key acts for whoever
created it, so the same rule is judged from that person: the party from the database, the role
from Authentik.

An add-on's key is judged by the person who installed it. Rotating the key rewrites `created_by`
to whoever rotated it, so the installation row is the stable record.

The party is the one a key's bound reads (cluster_parties.key_held_by_another_party: the buyers the
key itself sold onto the cluster join it, a creator who reaches the cluster only by a purchase stays
held), plus the creator's teammate member rows, as the browser rule counts them.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Awaitable, Callable, Dict, Iterable, Mapping, Optional, Tuple

from fastapi import HTTPException

from app.services.authentik_role_reading import CurrentRoleReader
from app.services.creator_role import creator_is_admin

logger = logging.getLogger(__name__)

DEFAULT_PROJECT = "default"
# The same window the key metadata cache already has: a binding change reaches a key within it.
PARTY_SECONDS = 30
DEFAULT_VIEW_ONLY_KEY_DETAIL = (
    "The default project on cluster '{cluster}' is shared with another organization: it is view "
    "only for keys a partner created. Work in the key's own projects there, or ask an "
    "administrator for a key."
)
CREATOR_ROLE_UNAVAILABLE = {
    "error": "identity_provider_unavailable",
    "message": "Who created this key could not be checked because the identity provider did not answer. Retry shortly.",
}

_lock = threading.Lock()
# (creator, cluster, party key id) -> (read at, wall clock then, held)
_held: Dict[Tuple[str, str, str], Tuple[float, float, bool]] = {}


class CreatorRoleUnavailable(Exception):
    """The identity provider did not answer whether the key's creator is an Admin."""


def touches_default(projects: Iterable[Optional[str]]) -> bool:
    """True when any project named is `default`; an unnamed project is `default`, as in LXD."""
    return any(((project or "").strip() or DEFAULT_PROJECT) == DEFAULT_PROJECT for project in projects)


async def _creator(api_key: Mapping[str, Any], db: Any) -> str:
    if api_key.get("extension_install_id") is not None:
        from app.services.extension_scope import load_install_snapshot

        snapshot = await load_install_snapshot(db, str(api_key.get("id") or ""))
        installed_by = (snapshot or {}).get("installed_by")
        if installed_by:
            return str(installed_by)
    return str(api_key.get("created_by") or "")


async def _held_by_another_party(db: Any, creator: str, cluster_id: str, key_id: str) -> bool:
    """`key_id` is cluster_parties.party_key_id: "" for an add-on credential."""
    from app.services.cluster_parties import key_held_by_another_party, member_orgs
    # important: the same shared stamp as the key's bound, published wherever a binding or project
    # on the cluster changes (key_party_bound.forget_cluster_bounds), so a "not held" verdict does
    # not outlive a foreign bind on another worker. Imported here: key_party_bound imports this.
    from app.services.key_party_bound import _changed_since

    cache_key = (creator.strip().lower(), cluster_id, key_id)
    now = time.monotonic()
    with _lock:
        cached = _held.get(cache_key)
    # A cached "not held" verdict is re-read when the stamp cannot be read (fail closed).
    if cached is not None and now - cached[0] < PARTY_SECONDS and not _changed_since(
        cluster_id, cached[1], unreadable_is_change=not cached[2]
    ):
        return cached[2]
    wall = time.time()
    teammates = await member_orgs(db, creator)
    held = await key_held_by_another_party(db, creator, key_id, cluster_id, also_runs=teammates)
    with _lock:
        _held[cache_key] = (now, wall, held)
    return held


async def _judge(api_key: Mapping[str, Any], cluster_id: str, db: Any) -> Tuple[str, bool]:
    from app.services.cluster_parties import party_key_id

    if db is None:
        # Opened only here, so a key route that never touches `default` costs no session.
        from app.core.database import async_session_maker

        async with async_session_maker() as session:
            return await _judge(api_key, cluster_id, session)
    creator = await _creator(api_key, db)
    return creator, await _held_by_another_party(db, creator, cluster_id, party_key_id(api_key))


async def refuse_key_default_operation(
    api_key: Mapping[str, Any],
    cluster_id: str,
    projects: Iterable[Optional[str]],
    *,
    db: Any = None,
    roles: Optional[CurrentRoleReader] = None,
    lands_in: Optional[Callable[[], Awaitable[Iterable[Optional[str]]]]] = None,
) -> None:
    """Refuse a key's change or guest access in a view-only `default`.

    Call it only for what the browser rule refuses: writes, and the reads that open a session or a
    file inside a guest. Plain reads of `default` stay open. 403 for a key whose creator is not an
    Admin, 503 while the creator's role or the parties cannot be read.

    `lands_in` answers where the write lands when that differs from what the key named: LXD
    keeps images, profiles, network ACLs and custom volumes in `default` for a project that does
    not isolate them, and an operation lives in the project of its resources. It costs a cluster
    read, so it is asked only once the cluster is known to be held by another party.
    """
    projects = list(projects)
    if not touches_default(projects) and lands_in is None:
        return
    try:
        creator, held = await _judge(api_key, cluster_id, db)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - an unreadable party is not a verdict either way
        logger.warning("Default view-only check unavailable for a key on %s: %s", cluster_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="Authentication backend unavailable") from None
    if not held:
        return
    if not touches_default(projects):
        try:
            landed = list(await lands_in()) if lands_in is not None else []
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - not knowing where a write lands is not a pass
            logger.warning("Could not tell where a key's write on %s lands: %s", cluster_id, type(exc).__name__)
            raise HTTPException(
                status_code=503, detail="The cluster could not say where this write lands. Retry shortly."
            ) from None
        if not touches_default(landed):
            return
    admin = await creator_is_admin(creator, roles=roles)
    if admin:
        return
    if admin is None:
        raise HTTPException(
            status_code=503, detail=dict(CREATOR_ROLE_UNAVAILABLE), headers={"Retry-After": "30"}
        ) from CreatorRoleUnavailable()
    raise HTTPException(status_code=403, detail=DEFAULT_VIEW_ONLY_KEY_DETAIL.format(cluster=cluster_id))


def forget_cluster_verdicts(cluster_id: str) -> None:
    """Drop this worker's cached verdicts on one cluster (key_party_bound.forget_cluster_bounds)."""
    with _lock:
        for cache_key in [k for k in _held if k[1] == cluster_id]:
            _held.pop(cache_key, None)


def forget_key_default_verdicts() -> None:
    """Drop every cached party verdict on this worker (tests, and key_party_bound's fallback)."""
    with _lock:
        _held.clear()
