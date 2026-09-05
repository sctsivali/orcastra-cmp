"""What an installation may reach, decided at install time and again at request time.

The rule the whole platform rests on: an installation's reach is the manifest's request,
intersected with the installing partner's own cluster scope, intersected with the clusters the
target organization is still bound to. It is never read from the stored grant alone.

important: that is why `granted_scope` on the row is a record of consent rather than a licence.
Storing a flattened intersection and trusting it afterwards is how an installation keeps working
against a cluster the partner lost, and on a platform where clusters are rented to paying
customers that means one tenant's add-on reading the next tenant's workload.

The two halves are split by how fast the underlying fact moves. Suspension and uninstall are
instant decisions, so they are checked on every request against a short-lived snapshot with the
same cross-worker tombstone the key cache already uses. A partner losing a cluster is slow, so it
is caught by the reconcile sweep instead; a two-table join on the warm authentication path would
be a worse trade than a bounded window, and the window is named in the documentation rather than
left implicit.
"""

import logging
import time
import threading
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_key_capabilities import GUEST_ACCESS_CAPABILITIES
from app.core.api_key_permissions import PERMISSION_LEVELS, granted_level
from app.core.cache import get_cache_manager
from app.core.org_scope import (
    get_current_allowed_clusters,
    get_user_org_ids,
    resolve_cluster_orgs,
)
from app.models.extension_models import ExtensionInstallation

logger = logging.getLogger(__name__)

# Mirrors `_REVOKED_NS` in the integrations router, and for the same reason: an installation
# suspended by one uvicorn worker must stop working on the other three, not on their own TTL.
_INSTALL_REVOKED_NS = "extension_install_revoked"

# Matches API_KEY_CACHE_TTL so the two caches expire together and a request cannot see a fresh
# key alongside a stale installation.
INSTALL_CACHE_TTL = 30.0

_install_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_install_cache_lock = threading.Lock()


class ScopeRefused(HTTPException):
    """A refusal that names the category and never another tenant's resources."""

    def __init__(self, detail: str, status_code: int = 403) -> None:
        super().__init__(status_code=status_code, detail=detail)


# ── install time ───────────────────────────────────────────────────────────────────────────────

async def assert_install_scope_within_partner(
    db: AsyncSession,
    user,
    *,
    organization_id: int,
    grants: Sequence[Any],
    manifest_scope: Dict[str, Any],
    capabilities: Sequence[str],
    is_admin: bool = False,
) -> None:
    """Refuse an installation that would reach further than the operator installing it.

    Five checks in this order, cheapest and most decisive first, plus one condition on reaching
    inside a guest.
    """
    requested_clusters = {getattr(g, "cluster_id", None) or g["cluster_id"] for g in grants}

    # 1. The partner's own cluster scope. Reuses exactly the guard `create_api_key` applies to an
    #    ordinary key, and it is free: org_scope_dependency already resolved it for this request.
    allowed = get_current_allowed_clusters()
    if allowed is not None:
        outside = sorted(requested_clusters - allowed)
        if outside:
            raise ScopeRefused(
                "This installation would reach clusters outside your own access: "
                + ", ".join(outside)
            )

    # 2. The target organization has to be one the operator actually holds.
    if not is_admin:
        own_orgs = await get_user_org_ids(user, db)
        if organization_id not in own_orgs:
            # 404 rather than 403, matching how an out-of-scope key id is answered: an
            # organization the caller cannot see must not be distinguishable from one that does
            # not exist.
            raise ScopeRefused("Organization not found", status_code=404)

    # 3. Every granted cluster has to be bound to that organization.
    #
    #    important: resolve_cluster_orgs, the plural. Its singular sibling takes the lowest bound
    #    id, which its own docstring calls fine for a label and wrong for a permission, and a
    #    shared cluster is not hypothetical on this deployment.
    for cluster_id in sorted(requested_clusters):
        bound = {oid for oid, _slug in await resolve_cluster_orgs(cluster_id, db)}
        if organization_id not in bound:
            raise ScopeRefused(
                f"Cluster {cluster_id} is not bound to the organization being installed into"
            )

    # 4. The permission level cannot exceed what the manifest declared it needs.
    ceiling = PERMISSION_LEVELS.get(str(manifest_scope.get("permission", "read")), 0)
    for grant in grants:
        permissions = getattr(grant, "permissions", None) or grant["permissions"]
        if granted_level(permissions) > ceiling:
            raise ScopeRefused(
                "This add-on declared it needs "
                f"{manifest_scope.get('permission', 'read')} access, so it cannot be installed "
                "with more"
            )

    # 5. A project wildcard is only available to a manifest that asked for one.
    if manifest_scope.get("projects") != "all":
        for grant in grants:
            projects = getattr(grant, "projects", None) or grant.get("projects") or []
            if "*" in projects:
                raise ScopeRefused(
                    "This add-on declared it needs named projects, so it cannot be installed "
                    "across all of them"
                )

    await _assert_guest_access_is_consented(
        db, organization_id=organization_id, capabilities=capabilities, clusters=requested_clusters
    )


async def _assert_guest_access_is_consented(
    db: AsyncSession,
    *,
    organization_id: int,
    capabilities: Sequence[str],
    clusters: Set[str],
) -> None:
    """Reaching inside a guest needs a standing organization decision, not just a tick.

    important: this closes a real asymmetry rather than adding ceremony. `desktop.*` is gated at
    request time by three further checks: every organization on the cluster consenting, a live
    break-glass grant, and a per-instance budget that fails closed. `instance.exec` and
    `instance.files` are gated by the explicit-capability check and nothing else. So without this,
    ticking a box during an install would be the only thing standing between a third-party
    application and root inside a renting customer's container.

    The consent consulted is the one that already exists, so no new surface is introduced and an
    organization that has already made this decision does not make it twice.
    """
    wanted = sorted(set(capabilities) & GUEST_ACCESS_CAPABILITIES)
    if not wanted:
        return

    from app.services.desktop_policy import consent_for_cluster

    for cluster_id in sorted(clusters):
        consent, refused_by = await consent_for_cluster(db, cluster_id)
        if not consent.enabled:
            raise ScopeRefused(
                "This add-on asks to reach inside guests ("
                + ", ".join(wanted)
                + f"), which cluster {cluster_id} has not consented to. An organization "
                "administrator enables that in the organization's settings first."
            )
        # Typing into a live session is a larger power than running a command, and the existing
        # policy already separates them, so the install respects the same split.
        if "desktop.input" in wanted and not consent.allow_input:
            raise ScopeRefused(
                f"Cluster {cluster_id} consents to viewing a desktop but not to controlling one, "
                "so desktop.input cannot be granted here."
            )


def effective_capabilities(
    consented: Sequence[str], requested_by_manifest: Sequence[str]
) -> List[str]:
    """What an install may actually be granted: never more than the manifest asked for."""
    return sorted(set(consented) & set(requested_by_manifest))


# ── request time ───────────────────────────────────────────────────────────────────────────────

def _install_revoked_elsewhere(api_key_id: str) -> bool:
    try:
        return get_cache_manager().get(_INSTALL_REVOKED_NS, api_key_id) is not None
    except Exception:  # a cache hiccup must never break authentication
        return False


def evict_install_cache(api_key_id: str) -> None:
    """Publish that an installation changed, so every worker drops its snapshot on next use.

    Suspend and re-scope change the row without touching the Vault key, so the key cache's own
    tombstone would not notice. Uninstall and rotate publish to both, because both stores moved.
    """
    with _install_cache_lock:
        _install_cache.pop(api_key_id, None)
    try:
        get_cache_manager().set(
            _INSTALL_REVOKED_NS, api_key_id, True, ttl=int(INSTALL_CACHE_TTL) + 5
        )
    except Exception as exc:
        logger.warning("Could not publish installation change for %s: %s", api_key_id, exc)


def _cached_install(api_key_id: str) -> Optional[Dict[str, Any]]:
    with _install_cache_lock:
        entry = _install_cache.get(api_key_id)
        if not entry:
            return None
        cached_at, snapshot = entry
        if time.monotonic() - cached_at > INSTALL_CACHE_TTL:
            _install_cache.pop(api_key_id, None)
            return None
    if _install_revoked_elsewhere(api_key_id):
        with _install_cache_lock:
            _install_cache.pop(api_key_id, None)
        return None
    return snapshot


async def load_install_snapshot(db: AsyncSession, api_key_id: str) -> Optional[Dict[str, Any]]:
    """The installation behind a credential, cached for the same window as the key itself."""
    cached = _cached_install(api_key_id)
    if cached is not None:
        return cached

    result = await db.execute(
        select(ExtensionInstallation).where(
            ExtensionInstallation.api_key_id == api_key_id
        )
    )
    install = result.scalar_one_or_none()
    if install is None:
        return None

    snapshot = {
        "id": install.id,
        "extension_id": install.extension_id,
        "organization_id": install.organization_id,
        "status": install.status,
        "granted_capabilities": list(install.granted_capabilities or []),
        "granted_scope": list(install.granted_scope or []),
    }
    with _install_cache_lock:
        _install_cache[api_key_id] = (time.monotonic(), snapshot)
    return snapshot


async def assert_install_is_live(
    db: AsyncSession, api_key_id: str, *, cluster_id: Optional[str] = None
) -> Dict[str, Any]:
    """Refuse a credential whose installation is suspended, gone, or no longer reaches here.

    A credential that outlives its installation is the failure this exists to prevent: the Vault
    key would still authenticate perfectly well with nothing left that says whose it is.
    """
    snapshot = await load_install_snapshot(db, api_key_id)
    if snapshot is None:
        raise ScopeRefused("This credential's installation no longer exists", status_code=401)
    if snapshot["status"] != "active":
        raise ScopeRefused(
            f"This installation is {snapshot['status']}", status_code=403
        )
    if cluster_id is not None:
        reachable = {
            grant.get("cluster_id") for grant in snapshot["granted_scope"] if isinstance(grant, dict)
        }
        if cluster_id not in reachable:
            raise ScopeRefused("This installation does not reach that cluster")
    return snapshot
