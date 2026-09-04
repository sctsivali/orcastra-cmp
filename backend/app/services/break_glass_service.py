"""
Break-glass grant lifecycle + sensitive-access enforcement.

A platform admin keeps operational visibility of the fleet but cannot open a console
or terminal into a tenant's VM without an explicit, justified, time-boxed grant. This
module owns: creating/listing/revoking grants, the authoritative "is there a live grant"
lookup, and the gate that console/terminal session creation calls.

Single source of truth so the router and the enforcement gate share one liveness rule:
a grant is live iff ``revoked_at IS NULL AND expires_at > now()`` (status is advisory).
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser
from app.core.config import get_settings
from app.core.org_scope import (
    get_current_allowed_clusters,
    get_owned_clusters_ignoring_admin,
    resolve_cluster_org,
)
from app.models.db_models import BreakGlassGrant

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

    One query rather than two near-copies, because the liveness predicate is the load-bearing part
    and a second spelling of it is a second thing to get wrong. Authoritative liveness is
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
    db: AsyncSession, api_key_id: str, cluster_id: str, *, fingerprint: Optional[str] = None
) -> Optional[BreakGlassGrant]:
    """The live grant (if any) authorizing an INTEGRATION KEY to reach cluster_id.

    A key has no human behind it, so the grant is what supplies the reason, the expiry and the
    revocation that an unattended caller cannot otherwise be held to.

    important: when the caller can supply the cluster's current server-certificate fingerprint,
    a grant that recorded a different one is refused. `cluster_id` is a free-text Vault key with
    no foreign key, so a cluster deleted and re-registered under another owner matches an old
    grant by string alone. A grant issued before that column existed has no fingerprint and is
    not subjected to the check, because it never made the promise.
    """
    grant = await _active_grant_for(db, BreakGlassGrant.api_key_id, api_key_id, cluster_id)
    if grant is None:
        return None
    if fingerprint and grant.cluster_fingerprint and grant.cluster_fingerprint != fingerprint:
        logger.warning(
            "Refusing break-glass grant %s for key %s on %s: the cluster's certificate no longer "
            "matches the one the grant was issued against, so this is a different registration",
            grant.id, api_key_id, cluster_id,
        )
        return None
    return grant


async def _effective_owned(user: AuthenticatedUser, db: AsyncSession) -> set[str]:
    """
    Clusters the actor OWNS (role-agnostic). Reuses the request-scoped allowed set for
    partner/tenant (already resolved by org_scope_dependency / set_org_scope_for_ws); for
    admin (context var is None) computes the real owned set so an admin-who-is-also-a-partner
    is never locked out of their own org's clusters (the admin+partner trap).
    """
    allowed = get_current_allowed_clusters()
    if allowed is not None:
        return allowed
    return await get_owned_clusters_ignoring_admin(user, db)


async def inspect_access(
    user: AuthenticatedUser, host: str, db: AsyncSession
) -> dict:
    """
    Non-mutating pre-flight: does this actor need a break-glass grant to open
    console/terminal on ``host``, and do they already hold one? Lets the UI prompt for
    a grant before connecting instead of failing at the gate. No session is created.
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

    Returns None when the actor owns the cluster (in-scope self-service: unchanged).
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


async def create_grant(
    db: AsyncSession,
    *,
    admin_email: str,
    cluster_id: str,
    reason: str,
    ticket_ref: Optional[str] = None,
    duration_minutes: Optional[int] = None,
) -> BreakGlassGrant:
    """Create a time-boxed grant. Reason is mandatory; duration is clamped to [1, max]."""
    settings = get_settings()
    if not reason or not reason.strip():
        raise HTTPException(status_code=400, detail="A justification (reason) is required.")

    minutes = duration_minutes or settings.break_glass_default_minutes
    minutes = max(1, min(int(minutes), settings.break_glass_max_minutes))

    org_id, org_slug = await resolve_cluster_org(cluster_id, db)
    now = _now()
    grant = BreakGlassGrant(
        admin_email=admin_email,
        cluster_id=cluster_id,
        target_organization_id=org_id,
        target_org_slug=org_slug,
        reason=reason.strip(),
        ticket_ref=(ticket_ref.strip() if ticket_ref and ticket_ref.strip() else None),
        status="active",
        created_by=admin_email,
        created_at=now,
        expires_at=now + timedelta(minutes=minutes),
    )
    db.add(grant)
    await db.commit()
    await db.refresh(grant)
    logger.info(
        f"Break-glass grant {grant.id} created: {admin_email} -> cluster {cluster_id} "
        f"(org {org_id}) for {minutes}m"
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
    limit: int = 200,
) -> list[BreakGlassGrant]:
    """
    List grants newest-first. ``cluster_filter`` None means no cluster restriction
    (admin sees all); a set restricts to grants on those clusters (tenant accountability
    view, fed the caller's get_user_allowed_clusters). ``mine_email`` narrows to one admin.
    """
    stmt = select(BreakGlassGrant).order_by(BreakGlassGrant.created_at.desc()).limit(limit)
    if mine_email:
        stmt = stmt.where(BreakGlassGrant.admin_email == mine_email)
    if cluster_filter is not None:
        # empty set -> match nothing (a sentinel keeps the IN clause valid)
        stmt = stmt.where(BreakGlassGrant.cluster_id.in_(cluster_filter or {"__none__"}))
    return list((await db.execute(stmt)).scalars().all())


async def watch_grant_while_open(
    grant_id: int, websocket, poll_seconds: float = 20.0
) -> None:
    """Close ``websocket`` once the grant that authorized it stops being active.

    important: console and terminal sockets authorize once, at connect, and then stay open for
    as long as the operator keeps the tab. A one-minute grant therefore bought an unbounded
    session, and revoking a grant did nothing to a session already running, which is the case
    revocation exists for. This re-reads the row on a timer and closes the socket the moment it
    is no longer active.

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
                try:
                    await websocket.close(code=1008, reason="break_glass_expired")
                except Exception:  # noqa: BLE001 - already gone
                    pass
                return
    except asyncio.CancelledError:
        raise
