"""
Break-glass grant lifecycle and sensitive-access enforcement.

A platform admin keeps operational visibility of the fleet but cannot open a console
or terminal into a tenant's VM without an explicit, justified, time-boxed grant. This
module creates, lists and revokes grants, answers whether a live grant exists, and holds
the gate that console and terminal session creation calls.

The router and the gate share one liveness rule: a grant is live iff
``revoked_at IS NULL AND expires_at > now()`` (status is advisory).
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING, Any, Optional

from fastapi import HTTPException
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ws_close
from app.core.auth import AuthenticatedUser
from app.core.config import get_settings
from app.core.org_scope import (
    get_current_allowed_clusters,
    get_owned_clusters_ignoring_admin,
    resolve_cluster_org,
)
from app.models.db_models import BreakGlassGrant

if TYPE_CHECKING:
    from app.services.lxd_certs.pinning import PinState

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def effective_status(grant: BreakGlassGrant) -> str:
    """Display status without mutating the row: an active grant past expiry reads as expired."""
    if grant.status == "active" and grant.revoked_at is None and grant.expires_at <= _now():
        return "expired"
    return grant.status


async def _active_grant_for(
    db: AsyncSession, principal_column: Any, principal: str, cluster_id: str
) -> Optional[BreakGlassGrant]:
    """The live grant for one principal on one cluster, whichever kind of principal it is.

    Both principal kinds share this query so the liveness predicate is written once: liveness is
    ``revoked_at IS NULL AND expires_at > now()``; `status` stays advisory so a lazy expiry sweep
    never widens access.
    """
    stmt = (
        select(BreakGlassGrant)
        .where(
            and_(
                principal_column == principal,
                BreakGlassGrant.cluster_id == cluster_id,
                BreakGlassGrant.revoked_at.is_(None),
                BreakGlassGrant.expires_at > _now(),
            )
        )
        .order_by(BreakGlassGrant.expires_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_active_grant(
    db: AsyncSession, admin_email: str, cluster_id: str
) -> Optional[BreakGlassGrant]:
    """The live grant (if any) authorizing admin_email to reach cluster_id."""
    return await _active_grant_for(db, BreakGlassGrant.admin_email, admin_email, cluster_id)


async def get_active_key_grant(
    db: AsyncSession,
    api_key_id: str,
    cluster_id: str,
    *,
    fingerprint: Optional[str] = None,
    pin_state: Optional["PinState"] = None,
) -> Optional[BreakGlassGrant]:
    """The live grant (if any) authorizing an integration key to reach cluster_id.

    A key has no human behind it, so the grant supplies the reason, the expiry and the revocation.

    important: when the caller supplies the cluster's current certificate fingerprint or pin state,
    a grant that recorded a different certificate is refused. `cluster_id` is a free-text Vault key
    with no foreign key, so a cluster deleted and re-registered under another owner matches an old
    grant by string alone. A grant that recorded no fingerprint is not checked.
    """
    grant = await _active_grant_for(db, BreakGlassGrant.api_key_id, api_key_id, cluster_id)
    if grant is None:
        return None
    if pin_state is not None:
        # A public-CA cluster renews its certificate without becoming another registration.
        same_registration = not grant.cluster_fingerprint or pin_state.accepts(
            grant.cluster_fingerprint
        )
    else:
        same_registration = not (
            fingerprint and grant.cluster_fingerprint and grant.cluster_fingerprint != fingerprint
        )
    if not same_registration:
        logger.warning(
            "Refusing break-glass grant %s for key %s on %s: the cluster's certificate no longer "
            "matches the one the grant was issued against, so this is a different registration",
            grant.id, api_key_id, cluster_id,
        )
        return None
    return grant


async def _effective_owned(user: AuthenticatedUser, db: AsyncSession) -> set[str]:
    """
    Clusters the actor owns, whatever its role. Reuses the request-scoped allowed set for
    partner/tenant (resolved by org_scope_dependency / set_org_scope_for_ws); for an admin
    (context var is None) computes the owned set so an admin who is also a partner is not
    locked out of their own organization's clusters.
    """
    allowed = get_current_allowed_clusters()
    if allowed is not None:
        return allowed
    return await get_owned_clusters_ignoring_admin(user, db)


async def inspect_access(
    user: AuthenticatedUser, host: str, db: AsyncSession
) -> dict:
    """
    Read-only pre-flight: whether this actor needs a break-glass grant to open a console or
    terminal on ``host``, and whether they already hold one. Lets the UI ask for a grant
    before connecting instead of failing at the gate. No session is created.
    """
    org_id, org_slug = await resolve_cluster_org(host, db)
    if not get_settings().auth_enabled:
        return {"required": False, "has_active_grant": True,
                "cluster_id": host, "target_organization_id": org_id, "target_organization_slug": org_slug}
    owned = await _effective_owned(user, db)
    if host in owned:
        return {"required": False, "has_active_grant": True,
                "cluster_id": host, "target_organization_id": org_id, "target_organization_slug": org_slug}
    grant = await get_active_grant(db, user.email, host)
    return {
        "required": True,
        "has_active_grant": grant is not None,
        "cluster_id": host,
        "target_organization_id": org_id,
        "target_organization_slug": org_slug,
        "grant_expires_at": grant.expires_at.isoformat() if grant else None,
    }


async def enforce_sensitive_access(
    user: AuthenticatedUser, host: str, db: AsyncSession
) -> Optional[BreakGlassGrant]:
    """
    Gate a sensitive (console/terminal) access on cluster ``host``.

    Returns None when the actor owns the cluster (in-scope self-service).
    Returns the live grant when access is authorized under break-glass.
    Raises HTTPException(403, break_glass_required) when out-of-scope without a grant.
    Local dev (auth disabled) bypasses the gate.
    """
    if not get_settings().auth_enabled:
        return None

    owned = await _effective_owned(user, db)
    if host in owned:
        return None  # in-scope -> no grant needed

    grant = await get_active_grant(db, user.email, host)
    if grant is not None:
        return grant

    org_id, org_slug = await resolve_cluster_org(host, db)
    raise HTTPException(
        status_code=403,
        detail={
            "error": "break_glass_required",
            "message": "Accessing this tenant resource requires an active break-glass grant.",
            "cluster_id": host,
            "target_organization_id": org_id,
            "target_organization_slug": org_slug,
            "request_url": "/api/v1/break-glass/grants",
        },
    )


class PinStoreUnavailable(Exception):
    """Vault could not say whether this cluster has a pin, which is not the same as having none."""


async def cluster_pin_state(cluster_id: str, *, strict: bool = False) -> Optional["PinState"]:
    """The cluster's pin with its trust kind and renewal lineage, or None when nothing is pinned.

    The same None-on-unreadable rule as `cluster_fingerprint` unless ``strict``, which raises
    `PinStoreUnavailable` instead for a caller that has to tell "no pin" from "could not read".
    """
    from app.services.lxd_certs.pinning import PinState

    try:
        from app.core.config import get_settings
        from app.core.vault_client import get_vault_client

        settings = get_settings()
        if not (settings.vault_enabled and settings.vault_token):
            if strict:
                raise PinStoreUnavailable("Vault is not configured")
            return None
        client = get_vault_client(settings.vault_addr, settings.vault_token)
        metadata = await asyncio.to_thread(
            client.vault_read, f"{settings.vault_clusters_path}/{cluster_id}/metadata"
        )
    except PinStoreUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read the certificate pin for %s: %s", cluster_id, exc)
        if strict:
            raise PinStoreUnavailable(str(exc)) from exc
        return None
    return PinState.from_metadata(metadata)


async def cluster_fingerprint(cluster_id: str) -> Optional[str]:
    """The cluster's pinned server-certificate fingerprint, or None when nothing is pinned.

    Recorded on a key grant so the grant ends with the registration it was issued against: a
    re-registration under the same `cluster_id` cannot reproduce the key pair.

    Returns None instead of raising when Vault is unreachable; a grant that records no fingerprint
    is not checked. Refusing the grant would make an unrelated Vault problem look like an
    authorisation failure.
    """
    state = await cluster_pin_state(cluster_id)
    return state.fingerprint if state else None


async def create_grant(
    db: AsyncSession,
    *,
    created_by: str,
    admin_email: Optional[str] = None,
    api_key_id: Optional[str] = None,
    cluster_id: str,
    reason: str,
    ticket_ref: Optional[str] = None,
    duration_minutes: Optional[int] = None,
    cluster_fingerprint: Optional[str] = None,
) -> BreakGlassGrant:
    """Create a time-boxed grant for a person or for an integration key.

    Reason is mandatory; duration is clamped to [1, max].

    important: `created_by` is separate from the grantee. For a human self-grant both are the same
    email. For a key grant the key is what the grant authorises and `created_by` is the person who
    answers for it; using `admin_email` for both would leave no accountable person on the grant.
    """
    settings = get_settings()
    if not reason or not reason.strip():
        raise HTTPException(status_code=400, detail="A justification (reason) is required.")
    if bool(admin_email) == bool(api_key_id):
        # Mirrors ck_bgg_one_principal, refused here so the caller gets a 400 naming the field
        # rather than an IntegrityError surfaced as a 500.
        raise HTTPException(
            status_code=400,
            detail="A grant names exactly one grantee: either admin_email or api_key_id.",
        )

    minutes = duration_minutes or settings.break_glass_default_minutes
    minutes = max(1, min(int(minutes), settings.break_glass_max_minutes))

    org_id, org_slug = await resolve_cluster_org(cluster_id, db)
    now = _now()
    grant = BreakGlassGrant(
        admin_email=admin_email,
        api_key_id=api_key_id,
        cluster_id=cluster_id,
        cluster_fingerprint=cluster_fingerprint,
        target_organization_id=org_id,
        target_org_slug=org_slug,
        reason=reason.strip(),
        ticket_ref=(ticket_ref.strip() if ticket_ref and ticket_ref.strip() else None),
        status="active",
        created_by=created_by,
        created_at=now,
        expires_at=now + timedelta(minutes=minutes),
    )
    db.add(grant)
    await db.commit()
    await db.refresh(grant)
    logger.info(
        "Break-glass grant %s created by %s for %s -> cluster %s (org %s) for %sm",
        grant.id, created_by, admin_email or f"key:{api_key_id}", cluster_id, org_id, minutes,
    )
    return grant


async def revoke_grant(db: AsyncSession, grant_id: int, revoked_by: str) -> BreakGlassGrant:
    """Revoke a grant. Idempotent. Effective immediately for the next access check."""
    grant = await db.get(BreakGlassGrant, grant_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="Break-glass grant not found.")
    if grant.revoked_at is None:
        grant.revoked_at = _now()
        grant.revoked_by = revoked_by
        grant.status = "revoked"
        await db.commit()
        await db.refresh(grant)
    return grant


async def list_grants(
    db: AsyncSession,
    *,
    cluster_filter: Optional[set[str]],
    mine_email: Optional[str] = None,
    organization_filter: Optional[set[int]] = None,
    limit: int = 200,
) -> list[BreakGlassGrant]:
    """
    List grants newest-first. ``cluster_filter`` None means no cluster restriction
    (admin sees all); a set restricts to grants on those clusters (tenant accountability
    view, fed the request's enforced cluster set). ``organization_filter`` keeps grants aimed
    at those organizations or at no organization. ``mine_email`` narrows to one admin.
    """
    stmt = select(BreakGlassGrant).order_by(BreakGlassGrant.created_at.desc()).limit(limit)
    if mine_email:
        stmt = stmt.where(BreakGlassGrant.admin_email == mine_email)
    if cluster_filter is not None:
        # empty set -> match nothing (a sentinel keeps the IN clause valid)
        stmt = stmt.where(BreakGlassGrant.cluster_id.in_(cluster_filter or {"__none__"}))
    if organization_filter is not None:
        stmt = stmt.where(or_(
            BreakGlassGrant.target_organization_id.is_(None),
            BreakGlassGrant.target_organization_id.in_(organization_filter or {-1}),
        ))
    return list((await db.execute(stmt)).scalars().all())


async def watch_grant_while_open(
    grant_id: int, websocket, poll_seconds: float = 20.0
) -> None:
    """Close ``websocket`` once the grant that authorized it stops being active.

    important: console and terminal sockets authorize once, at connect, and stay open as long as
    the tab does. Without this watch a short grant would buy an unbounded session and a revocation
    would not reach a running one. The row is re-read on a timer and the socket closed as soon as
    the grant stops being active.

    Runs as a background task; it must never raise into the session it is watching.
    """
    from app.core.database import async_session_maker

    try:
        while True:
            await asyncio.sleep(poll_seconds)
            try:
                async with async_session_maker() as db:
                    grant = await db.get(BreakGlassGrant, grant_id)
                    still_valid = grant is not None and effective_status(grant) == "active"
            except Exception as exc:  # noqa: BLE001 - a lookup failure must not kill the session
                logger.warning("Break-glass watchdog could not re-read grant %s: %s", grant_id, exc)
                continue
            if not still_valid:
                logger.info("Break-glass grant %s no longer active; closing session", grant_id)
                # 4428 instead of 1008: the page reads it as "break-glass ended" and does not
                # reconnect into a refusal (core/ws_close.py).
                try:
                    await websocket.close(code=ws_close.BREAK_GLASS, reason="break_glass_expired")
                except Exception:  # noqa: BLE001 - already gone
                    pass
                return
    except asyncio.CancelledError:
        raise
