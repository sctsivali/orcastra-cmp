"""`default` stays view only for a key a partner created, where another party holds the cluster.

The browser rule (org_scope.refuse_default_operation) reads the request's own org scope, which no
key route carries, so it answered nothing for a key: a key minted before that rule kept changing,
opening and reading inside another party's guests in `default` through the structured proxy, MCP
and the raw proxy (RC5 acceptance FU-1, 2026-11-06). A key acts for whoever created it, so the
same rule is judged from that person: the party from the database, the role from Authentik.

note: an add-on's key is judged by the person who installed it. Rotating the key rewrites
`created_by` to whoever rotated it, so the installation row is the stable record.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

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
_held: Dict[Tuple[str, str], Tuple[float, bool]] = {}


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


async def _held_by_another_party(db: Any, creator: str, cluster_id: str) -> bool:
    from app.services.cluster_parties import default_view_only_clusters

    cache_key = (creator.strip().lower(), cluster_id)
    now = time.monotonic()
    with _lock:
        cached = _held.get(cache_key)
    if cached is not None and now - cached[0] < PARTY_SECONDS:
        return cached[1]
    held = cluster_id in await default_view_only_clusters(db, creator, {cluster_id})
    with _lock:
        _held[cache_key] = (now, held)
    return held


async def _judge(api_key: Mapping[str, Any], cluster_id: str, db: Any) -> Tuple[str, bool]:
    if db is None:
        # Opened only here, so a key door that never touches `default` costs no session.
        from app.core.database import async_session_maker

        async with async_session_maker() as session:
            return await _judge(api_key, cluster_id, session)
    creator = await _creator(api_key, db)
    return creator, await _held_by_another_party(db, creator, cluster_id)


async def refuse_key_default_operation(
    api_key: Mapping[str, Any],
    cluster_id: str,
    projects: Iterable[Optional[str]],
    *,
    db: Any = None,
    roles: Optional[CurrentRoleReader] = None,
) -> None:
    """Refuse a key's change or guest access in a view-only `default`.

    Call it only for what the browser rule refuses: writes, and the reads that open a session or a
    file inside a guest. Plain reads of `default` stay open. 403 for a key whose creator is not an
    Admin, 503 while the creator's role or the parties cannot be read.
    """
    if not touches_default(projects):
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
    admin = await creator_is_admin(creator, roles=roles)
    if admin:
        return
    if admin is None:
        raise HTTPException(
            status_code=503, detail=dict(CREATOR_ROLE_UNAVAILABLE), headers={"Retry-After": "30"}
        ) from CreatorRoleUnavailable()
    raise HTTPException(status_code=403, detail=DEFAULT_VIEW_ONLY_KEY_DETAIL.format(cluster=cluster_id))


def forget_key_default_verdicts() -> None:
    """Tests only: drop every cached party verdict."""
    with _lock:
        _held.clear()
