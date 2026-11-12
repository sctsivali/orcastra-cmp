"""A key is held to the paid term of the purchase its creator reaches the cluster through.

A buyer founder OrcaHub promoted to role_partner may not grant on a cluster it reaches only by
purchase (org_scope.refuse_grants_on_purchased_clusters), but a key minted before that rule keeps
its grant. The browser holds the founder to the purchase's phase (core/sale_term.py): active acts,
retention reads, ended reaches nothing. A key acts for its creator (the installer for an add-on,
key_default_view_only._creator), so every request holds its grants there to the same phase, in a
copy: `read` in retention, and once ended the grant is left out, so no door lists, reads or writes
that cluster (api_key_permissions.refuse_a_paused_grant answers 403 `purchase_ended`).

Whatever the phase, such a key may not write tenant access there (provision, revoke, policy sync):
an organization a key provisions is stamped with the key's creator, and a creator reaches its own
organizations' clusters outright, so one provision would lift the term for good.
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
# cluster -> phase, for every granted cluster the key's creator reaches only through a purchase.
PURCHASE_CLUSTERS = "_purchase_clusters"
HELD = "_held_cluster_permissions"

_lock = threading.Lock()
# key id (or creator) -> (read at, (creator, {cluster: phase}))
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


def held_to_phase(grant: Mapping[str, Any], phase: str) -> Optional[Dict[str, Any]]:
    """The grant as the phase leaves it: None once ended, `read` in retention."""
    if phase == ENDED:
        return None
    if phase == READ_ONLY:
        return {**grant, "permissions": ["read"] if grant.get("permissions", ["read"]) else []}
    return dict(grant)


async def narrow_to_paid_term(
    api_key: Dict[str, Any], *, db: Any = None, roles: Optional[CurrentRoleReader] = None,
) -> Dict[str, Any]:
    """The key held to the phase of every purchase its creator reaches a granted cluster through.
    Returns the key itself when no grant is on such a cluster, else a copy.

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
    bought = {
        str(g.get("cluster_id")): phases[str(g.get("cluster_id"))]
        for g in grants
        if str(g.get("cluster_id")) in phases
    }
    if not bought:
        return api_key
    admin = await creator_is_admin(creator, roles=roles)
    if admin:
        return api_key
    if admin is None:
        logger.warning("Creator role unknown for key %s; held to the purchase's paid term", api_key.get("id"))
    narrowed: List[Dict[str, Any]] = []
    for grant in grants:
        cluster_id = str(grant.get("cluster_id"))
        if cluster_id not in bought:
            narrowed.append(grant)
            continue
        held = held_to_phase(grant, bought[cluster_id])
        if held is not None:
            narrowed.append(held)
    return {
        **api_key,
        "cluster_permissions": narrowed,
        HELD: api_key.get(HELD) or grants,
        PURCHASE_CLUSTERS: bought,
    }


def reached_by_purchase(api_key: Mapping[str, Any], cluster_id: str) -> Optional[str]:
    """The purchase phase through which the key's creator reaches `cluster_id`, or None."""
    return (api_key.get(PURCHASE_CLUSTERS) or {}).get(cluster_id)


def refuse_tenant_writes_on_a_purchase(api_key: Mapping[str, Any], cluster_id: str) -> None:
    """403 when the key would provision, revoke or sync tenant access on a cluster its creator
    reaches only through a purchase (see the module note)."""
    if reached_by_purchase(api_key, cluster_id) is not None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "cluster_reached_by_purchase",
                "message": (
                    f"Cluster '{cluster_id}' reaches this key's creator only through a purchase, so "
                    "the key cannot provision or change tenant access there."
                ),
            },
        )


def refuse_guest_reach_in_retention(api_key: Mapping[str, Any], cluster_id: str) -> None:
    """403 when the key would reach inside a guest (exec, files, a desktop, a data export) on a
    cluster its creator holds through a purchase past its paid term. The browser refuses its
    console, terminal and file sockets there too: in retention the buyer may look, not reach in."""
    if reached_by_purchase(api_key, cluster_id) == READ_ONLY:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "purchase_read_only",
                "message": (
                    f"The purchase that gives this key cluster '{cluster_id}' is past its paid term: "
                    "it may read the cluster, not reach inside a guest."
                ),
            },
        )


async def installer_phase(installer: str, cluster_id: str) -> Optional[str]:
    """For an add-on's events: the phase through which its installer reaches `cluster_id`, or None.

    Read in its own session, cached per installer for PAID_TERM_SECONDS. Raises when unreadable.
    """
    if not installer or not get_settings().tenant_term_enforcement_enabled:
        return None
    probe = {"id": f"installer:{installer.strip().lower()}", "created_by": installer}
    _creator_name, phases = await _creator_phases(probe, None)
    return phases.get(cluster_id)


def forget_paid_terms() -> None:
    """Tests only: drop every cached phase."""
    with _lock:
        _phases.clear()
