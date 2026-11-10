"""What an installation may reach, decided at install time and again at request time.

At install time the reach is the manifest's request, intersected with the installing partner's own
cluster scope and with the clusters the target organization is bound to. `granted_scope` on the
row records that consent.

At request time, suspension and uninstall are checked on every request against a short-lived
snapshot that shares the key cache's cross-worker tombstone. An organization losing a cluster is
slower, so the reconcile sweep (extension_reconcile) narrows the stored grant instead of a join on
the authentication path.
"""

import logging
import time
import threading
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.api_key_capabilities import GUEST_ACCESS_CAPABILITIES
from app.core.api_key_permissions import PERMISSION_LEVELS, granted_level
from app.core.cache import get_cache_manager
from app.core.org_scope import (
    get_current_allowed_clusters,
    purchase_only_clusters,
    resolve_cluster_orgs,
)
from app.models.extension_models import Extension, ExtensionInstallation
from app.services.api_key_grant_bound import refuse_a_grant_past_the_project_bound
from app.services.extension_credential_guard import orgs_the_user_runs

logger = logging.getLogger(__name__)

# Mirrors `_REVOKED_NS` in the integrations router: an installation suspended by one uvicorn
# worker must stop working on every other worker at once, not after their own TTL.
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

    Six checks in this order, cheapest and most decisive first, plus one condition on reaching
    inside a guest.
    """
    requested_clusters = {getattr(g, "cluster_id", None) or g["cluster_id"] for g in grants}

    # 1. The partner's own cluster scope, the same guard `create_api_key` applies to an ordinary
    #    key. org_scope_dependency already resolved it for this request.
    allowed = get_current_allowed_clusters()
    if allowed is not None:
        outside = sorted(requested_clusters - allowed)
        if outside:
            raise ScopeRefused(
                "This installation would reach clusters outside your own access: "
                + ", ".join(outside)
            )
    # A cluster the partner reaches only by purchase carries no grant it may hand on.
    purchased = sorted(requested_clusters & purchase_only_clusters())
    if purchased:
        raise ScopeRefused(
            "This installation would reach clusters you reach only through a purchase: " + ", ".join(purchased)
        )

    # 2. The target organization has to be one the operator runs.
    if not is_admin:
        own_orgs = await orgs_the_user_runs(db, user.email)
        if organization_id not in own_orgs:
            # 404 rather than 403, matching how an out-of-scope key id is answered: an
            # organization the caller cannot see must not be distinguishable from one that does
            # not exist.
            raise ScopeRefused("Organization not found", status_code=404)

    # 3. Every granted cluster has to be bound to that organization.
    #
    #    important: resolve_cluster_orgs, the plural. The singular takes the lowest bound id,
    #    which is fine for a label and wrong for a permission on a cluster several organizations
    #    share.
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

    # 6. The partner's project bound, the same rule an ordinary API key is held to: on a cluster
    #    another party also holds, no '*' and no project the partner cannot see there. A manifest
    #    asking for "all" projects does not lift it.
    await refuse_a_grant_past_the_project_bound(db, user.email, grants)

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
    """Reaching inside a guest needs a standing organization consent, not only the capability.

    important: `desktop.*` is gated at request time by every organization on the cluster
    consenting, a live break-glass grant and a per-instance budget that fails closed, while
    `instance.exec` and `instance.files` have only the explicit-capability check. Without this, a
    box ticked during an install would be all that stands between a third-party add-on and root
    inside a tenant's guest. The consent consulted is the existing desktop policy, so an
    organization does not decide twice.
    """
    wanted = sorted(set(capabilities) & GUEST_ACCESS_CAPABILITIES)
    if not wanted:
        return

    from app.services.desktop_policy import consent_for_cluster

    for cluster_id in sorted(clusters):
        # important: keep the second element unused. It names the first organization that withheld
        # consent, and on a shared cluster telling the installer would reveal who else is on that
        # hardware. It is audit-only by the contract of `consent_for_cluster`; the desktop router
        # drops it too.
        consent, _refused_by = await consent_for_cluster(db, cluster_id)
        # important: use the accessors, not the raw fields. `may_view` and `may_input` keep
        # `allow_input` alone from authorizing input on a row that is disabled overall. The raw
        # fields are safe only because `consent_for_cluster` pre-composes them; a `DesktopConsent`
        # from `get_consent` does not, and a disabled organization would authorize guest access.
        if not consent.may_guest_access:
            raise ScopeRefused(
                "This add-on asks to reach inside guests ("
                + ", ".join(wanted)
                + f"), which an organization sharing {cluster_id} has not consented to. An "
                "administrator of every organization on that cluster enables guest access in the "
                "organization's settings first."
            )
        # Typing into a live session is a larger power than running a command, and the desktop
        # policy separates them, so the install keeps the same split.
        if "desktop.input" in wanted and not consent.may_input:
            raise ScopeRefused(
                f"Cluster {cluster_id} consents to viewing a desktop but not to controlling one, "
                "so desktop.input cannot be granted here."
            )


def effective_capabilities(
    consented: Sequence[str], requested_by_manifest: Sequence[str]
) -> List[str]:
    """What an install may be granted: never more than the manifest asked for."""
    return sorted(set(consented) & set(requested_by_manifest))


# ── request time ───────────────────────────────────────────────────────────────────────────────

def _install_revoked_elsewhere(api_key_id: str) -> bool:
    try:
        return get_cache_manager().get(_INSTALL_REVOKED_NS, api_key_id) is not None
    except Exception:  # a cache hiccup must never break authentication
        return False


def evict_install_cache(api_key_id: str) -> None:
    """Publish that an installation changed, so every worker drops its snapshot on next use.

    Suspend changes only the row, so the key cache's own tombstone would not notice. Re-scope,
    uninstall and rotate change the Vault key as well and publish to both.
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

    # important: a rotation's superseded key must resolve too. It is retired by shortening its
    # expiry, so it stays usable inside the grace window until `validate_api_key` ends it; matching
    # only `api_key_id` would end the grace window at the rotation. The add-on's own status is
    # joined in so that disabling an add-on stops the installations it already has.
    result = await db.execute(
        select(ExtensionInstallation, Extension.status)
        .join(Extension, Extension.id == ExtensionInstallation.extension_id)
        .where(
            or_(
                ExtensionInstallation.api_key_id == api_key_id,
                ExtensionInstallation.previous_api_key_id == api_key_id,
            )
        )
    )
    row = result.first()
    if row is None:
        return None
    install, extension_status = row

    snapshot = {
        "id": install.id,
        "extension_id": install.extension_id,
        "extension_status": extension_status,
        "organization_id": install.organization_id,
        "status": install.status,
        "suspend_reason": install.suspend_reason,
        "granted_capabilities": list(install.granted_capabilities or []),
        "granted_scope": list(install.granted_scope or []),
        # The stable creator: a rotation rewrites the key's `created_by` (key_default_view_only).
        "installed_by": getattr(install, "installed_by", None),
    }
    with _install_cache_lock:
        _install_cache[api_key_id] = (time.monotonic(), snapshot)
    return snapshot


async def assert_install_is_live(
    db: AsyncSession, api_key_id: str, *, cluster_id: Optional[str] = None
) -> Dict[str, Any]:
    """Refuse a credential whose add-on is disabled or whose installation is suspended, gone, or
    does not reach `cluster_id`.

    Without this, a Vault key that outlives its installation would still authenticate.
    """
    snapshot = await load_install_snapshot(db, api_key_id)
    if snapshot is None:
        raise ScopeRefused("This credential's installation no longer exists", status_code=401)
    # important: the add-on's status as well as the installation's. Withdrawing an add-on is
    # refused while anything is installed, so `disabled` is the only way to stop the
    # installations it already has.
    if snapshot.get("extension_status") == "disabled":
        raise ScopeRefused("This add-on has been disabled")
    if snapshot["status"] != "active":
        # The stored reason travels with the refusal; after a sweep suspension it is the only
        # explanation the add-on can reach. This shows operator-typed text to a third party on
        # purpose, and the form that collects it says so.
        reason = (snapshot.get("suspend_reason") or "").strip()
        detail = f"This installation is {snapshot['status']}"
        if reason:
            detail = f"{detail}: {reason}"
        raise ScopeRefused(detail, status_code=403)
    if cluster_id is not None:
        reachable = {
            grant.get("cluster_id") for grant in snapshot["granted_scope"] if isinstance(grant, dict)
        }
        if cluster_id not in reachable:
            raise ScopeRefused("This installation does not reach that cluster")
    return snapshot
