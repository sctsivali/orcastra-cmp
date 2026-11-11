"""Pending certificates: issuing one for Regenerate, reading one back for Verify, and expiry.

Renew, verify and cancel, and the helpers both modules use, live in rotation.py.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.request_context import get_current_user_id
from app.core.type_utils import build_trust_add_command
from app.models.cert_trust_schemas import PendingCertificateResponse
from app.models.schemas import CertificateLifetime
from app.services.lxd_certs import lifetime
from app.services.lxd_certs import trust_cleanup_store as store
from app.services.lxd_certs.lifetime import IssuedLifetime
from app.services.lxd_certs.rotation import (
    PENDING_MAX_AGE_HOURS,
    TRUST_TIMEOUT_S,
    CertificateNotFound,
    RotationState,
    _activate,
    _blocking,
    _cli_type,
    _fp,
    _path,
    _record,
    audit,
    read_rotation_state,
    trust_identity_name,
)


def pending_is_stale(pending: Dict[str, Any], now: datetime) -> bool:
    created = pending.get("created_at")
    if not created:
        return False
    try:
        created_at = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return (now - created_at).total_seconds() > PENDING_MAX_AGE_HOURS * 3600


async def pending_view(cert_manager: Any, cluster_id: str) -> PendingCertificateResponse:
    state = await _blocking(read_rotation_state, cert_manager, cluster_id, label="cert.pending.read")
    pending = state.pending
    if not pending:
        raise CertificateNotFound(cluster_id)
    identity_name = pending.get("identity_name") or f"orca-{cluster_id}"
    created = pending.get("created_at")
    try:
        created_at = datetime.fromisoformat(str(created).replace("Z", "+00:00")) if created else None
    except ValueError:
        created_at = None
    return PendingCertificateResponse(
        cluster_id=cluster_id,
        pending_cert_id=pending.get("pending_cert_id"),
        fingerprint=state.pending_fp,
        certificate=pending["client.crt"],
        cli_command=build_trust_add_command(_cli_type(state), pending["client.crt"], identity_name),
        identity_name=identity_name,
        created_at=created_at,
    )


async def record_current_superseded(
    db: AsyncSession,
    cert_manager: Any,
    cluster_id: str,
    *,
    reason: str,
    session_maker: Optional[Any] = None,
) -> Optional[str]:
    """Record the active certificate before something overwrites it outside renew (regenerate).

    Called under cluster_cert_lock. Returns the fingerprint recorded, or None when there was none.
    """
    try:
        state = await _blocking(read_rotation_state, cert_manager, cluster_id, label=f"cert.{reason}.read")
    except CertificateNotFound:
        return None
    previous = state.current_fp
    if previous:
        await _record(
            session_maker, cluster_id=cluster_id, fingerprint=previous, reason=reason,
            grace_seconds=int(get_settings().lxd_trust_cleanup_grace_seconds),
            requested_by=get_current_user_id(),
        )
    return previous


@dataclass(frozen=True)
class PendingIssue:
    """What regenerate handed back: a certificate waiting for trust, and how to give it."""

    pending_cert_id: str
    certificate: str
    cli_command: str
    identity_name: str
    fingerprint: Optional[str]
    previous_fingerprint: Optional[str]
    # True only when the cluster had no certificate at all, so there was nothing to protect.
    activated: bool
    lifetime: Optional[IssuedLifetime] = None


async def regenerate(
    db: AsyncSession,
    cert_manager: Any,
    cluster_id: str,
    *,
    cluster_type: Optional[str],
    session_maker: Optional[Any] = None,
    lifetime_choice: Optional[CertificateLifetime] = None,
) -> PendingIssue:
    """Issue a replacement certificate as PENDING; Verify activates it once the cluster trusts it.

    No attempt is made to add trust with the current certificate: Regenerate is the recovery for a
    cluster that refuses it, and renew already covers the case where it is still trusted. A pending
    renewal this replaces is recorded, since its trust may already have been added.
    """
    grace = int(get_settings().lxd_trust_cleanup_grace_seconds)
    actor = get_current_user_id()
    settings = get_settings()
    async with store.cluster_cert_lock(db, cluster_id):
        try:
            state: Optional[RotationState] = await _blocking(
                read_rotation_state, cert_manager, cluster_id, label="cert.regenerate.read"
            )
        except CertificateNotFound:
            state = None
        # Checked before anything is recorded: a refused lifetime leaves the cluster as it was.
        lifetime_plan = await _blocking(
            lifetime.plan,
            lifetime.resolve(lifetime_choice, state.certs.get("lifetime") if state is not None else None),
            cert_manager.vault,
            label="cert.regenerate.lifetime",
        )
        if state is not None and state.pending:
            await _record(
                session_maker, cluster_id=cluster_id, fingerprint=state.pending_fp or "",
                reason="pending_replaced", grace_seconds=grace,
                identity_name=state.pending.get("identity_name"), requested_by=actor,
            )
        common_name = f"{cluster_id}.{settings.orcastra_domain}"
        issued, issued_lifetime = await _blocking(
            partial(lifetime.issue, cert_manager.vault, common_name, lifetime_plan),
            timeout=TRUST_TIMEOUT_S, label="cert.regenerate.issue",
        )
        material = {
            "client.crt": issued["cert"],
            "client.key": issued["key"],
            "ca.pem": issued.get("ca", ""),
            "common_name": common_name,
            "lifetime": issued_lifetime.lifetime.value,
        }
        identity_name = trust_identity_name(cluster_id)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        pending_cert_id = f"pending-{stamp}-{secrets.token_hex(4)}"
        if state is None:
            # Nothing is active, so nothing can be cut off: the certificate is active at once,
            # exactly as registration stores the first one.
            await _blocking(_activate, cert_manager, cluster_id, material, label="cert.regenerate.activate")
        else:
            await _blocking(
                cert_manager.vault.vault_kv_put,
                _path(cert_manager, cluster_id, "pending_certs"),
                {
                    **material,
                    "pending_cert_id": pending_cert_id,
                    "identity_name": identity_name,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "auto_renewed": False,
                    "origin": "regenerate",
                },
                label="cert.regenerate.pending",
            )
        await db.commit()
    new_fp = _fp(issued["cert"])
    previous_fp = state.current_fp if state is not None else None
    audit("cluster.certificate.regenerate", cluster_id,
          mode="pending" if state is not None else "initial",
          fingerprint=new_fp, previous_fingerprint=previous_fp, identity_name=identity_name,
          **lifetime.audit_fields(issued_lifetime))
    cli_type = cluster_type or (_cli_type(state) if state is not None else "LXD")
    return PendingIssue(
        pending_cert_id=pending_cert_id,
        certificate=issued["cert"],
        cli_command=build_trust_add_command(cli_type, issued["cert"], identity_name),
        identity_name=identity_name,
        fingerprint=new_fp,
        previous_fingerprint=previous_fp,
        activated=state is None,
        lifetime=issued_lifetime,
    )
