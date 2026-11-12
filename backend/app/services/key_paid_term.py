"""A key is held to the paid term of the purchase its creator reaches the cluster through.

A buyer founder OrcaHub promoted to role_partner may not grant on a cluster it reaches only by
purchase (org_scope.refuse_grants_on_purchased_clusters), but a key minted before that rule keeps
its grant. The browser holds the founder to the purchase's phase (core/sale_term.py): active acts,
retention reads, ended reaches nothing. A key acts for its creator (the installer for an add-on,
key_default_view_only._creator), so every request holds its grants there to the same phase, in a
copy: `read` in retention, an empty grant once ended (a 403 at every door, not "not registered").
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fastapi import HTTPException

from app.core.config import get_settings
from app.core.sale_term import ENDED, READ_ONLY, purchase_phases
from app.services.authentik_role_reading import CurrentRoleReader
from app.services.creator_role import creator_is_admin
from app.services.key_default_view_only import _creator

logger = logging.getLogger(__name__)

# The same window as the key metadata cache: a renewed or ended term reaches a key within it.
PAID_TERM_SECONDS = 30
PAID_TERM_HELD = "_paid_term_clusters"
HELD = "_held_cluster_permissions"

_lock = threading.Lock()
# creator -> (read at, {cluster: phase} for the clusters it reaches only by purchase)
_phases: Dict[str, Tuple[float, Tuple[str, Dict[str, str]]]] = {}


async def _read_phases(api_key: Mapping[str, Any], db: Any) -> Tuple[str, Dict[str, str]]:
    if db is None:
        from app.core.database import async_session_maker

        async with async_session_maker() as session:
            return await _read_phases(api_key, session)
    creator = await _creator(api_key, db)
    return creator, await purchase_phases(db, creator)


async def _creator_phases(api_key: Mapping[str, Any], db: Any) -> Tuple[str, Dict[str, str]]:
    cache_key = str(api_key.get("id") or api_key.get("created_by") or "")
    now = time.monotonic()
    with _lock:
        cached = _phases.get(cache_key)
    if cached is not None and now - cached[0] < PAID_TERM_SECONDS:
        return cached[1]
    read = await _read_phases(api_key, db)
    with _lock:
        _phases[cache_key] = (now, read)
    return read


def held_to_phase(grant: Mapping[str, Any], phase: str) -> Dict[str, Any]:
    if phase == ENDED:
        return {**grant, "permissions": []}
    held = grant.get("permissions", ["read"])
    return {**grant, "permissions": ["read"] if held else []}


async def narrow_to_paid_term(
    api_key: Dict[str, Any], *, db: Any = None, roles: Optional[CurrentRoleReader] = None,
) -> Dict[str, Any]:
    """The key with every grant on a cluster its creator reaches only by a purchase past its paid
    term held to that phase. Returns the key itself when nothing changes, else a copy.

    important: never the cached document. A renewed term must give the grant back.
    """
    if not get_settings().tenant_term_enforcement_enabled:
        return api_key
    grants = [g for g in (api_key.get("cluster_permissions") or []) if isinstance(g, dict)]
    if not grants:
        return api_key
    try:
        creator, phases = await _creator_phases(api_key, db)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - an unreadable term is not a verdict either way
        logger.warning("Paid term unavailable for key %s: %s", api_key.get("id"), type(exc).__name__)
        raise HTTPException(status_code=503, detail="Authentication backend unavailable") from None
    limited = {
        str(g.get("cluster_id")): phases[str(g.get("cluster_id"))]
        for g in grants
        if phases.get(str(g.get("cluster_id"))) in (READ_ONLY, ENDED)
    }
    if not limited:
        return api_key
    admin = await creator_is_admin(creator, roles=roles)
    if admin:
        return api_key
    if admin is None:
        logger.warning("Creator role unknown for key %s; held to the purchase's paid term", api_key.get("id"))
    narrowed: List[Dict[str, Any]] = [
        held_to_phase(g, limited[str(g.get("cluster_id"))]) if str(g.get("cluster_id")) in limited else g
        for g in grants
    ]
    return {
        **api_key,
        "cluster_permissions": narrowed,
        HELD: api_key.get(HELD) or grants,
        PAID_TERM_HELD: limited,
    }


def forget_paid_terms() -> None:
    """Tests only: drop every cached phase."""
    with _lock:
        _phases.clear()
