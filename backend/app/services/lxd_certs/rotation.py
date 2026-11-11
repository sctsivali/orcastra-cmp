"""Renew, verify and cancel: replacing a cluster's client certificate without losing the old one.

important: every path here runs under the cluster's certificate lock and records each certificate
it lets go of BEFORE overwriting it in Vault (trust_cleanup_store.record_superseded). A failed
overwrite then leaves a record of a still-current certificate, which the sweep cancels, never a
trusted certificate nothing remembers. Renew lets go of the old active (and a replaced pending)
one, or of the new one when activation fails after trust was added; verify of the old active one;
cancel and the 24 hour pending cleanup of the pending one, which may already be trusted.

Regenerate (pending.py) also issues a pending certificate, so the active one keeps working until
Verify activates the trusted replacement; overwriting it at once would cut off any worker that
reconnects before trust is added. Vault and LXD calls block, so each goes through run_blocking
with this module's executor.
"""

import logging
import secrets
import string
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.blocking import run_blocking
from app.core.cert_fingerprint import lxd_fingerprint
from app.core.config import get_settings
from app.core.request_context import get_current_user_id
from app.core.type_utils import build_trust_add_command
from app.core.vault_client import VaultClientError
from app.models.schemas import (
    CertificateLifetime,
    CertificateRenewalRequest,
    CertificateRenewalResponse,
    CertificateVerifyResponse,
)
from app.services.lxd_certs import lifetime
from app.services.lxd_certs import trust_cleanup_store as store
from app.services.lxd_certs.inspect import parse_certificate_pem

logger = logging.getLogger(__name__)

EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cert-rotation")
VAULT_TIMEOUT_S = 25.0
TRUST_TIMEOUT_S = 60.0
PENDING_MAX_AGE_HOURS = 24


class CertificateNotFound(Exception):
    pass


class PendingMismatch(Exception):
    pass


@dataclass(frozen=True)
class RotationState:
    certs: Dict[str, Any]
    metadata: Dict[str, Any]
    pending: Optional[Dict[str, Any]]

    @property
    def current_pem(self) -> str:
        return self.certs.get("client.crt") or ""

    @property
    def current_fp(self) -> Optional[str]:
        return _fp(self.current_pem)

    @property
    def pending_fp(self) -> Optional[str]:
        return _fp((self.pending or {}).get("client.crt"))

    @property
    def endpoint(self) -> str:
        return self.metadata.get("endpoint") or ""


def _fp(pem: Optional[str]) -> Optional[str]:
    try:
        return lxd_fingerprint(pem) if pem else None
    except ValueError:
        return None


def _default_session_maker() -> Any:
    from app.core.database import async_session_maker

    return async_session_maker


async def _record(session_maker: Optional[Any], **record: Any) -> None:
    await store.record_superseded_now(session_maker or _default_session_maker(), **record)


_SUFFIX_ALPHABET = string.ascii_lowercase + string.digits


def trust_identity_name(cluster_id: str, now: Optional[datetime] = None) -> str:
    """The `--name` of a trust entry: `orca-<id>-YYYYMMDD-HHMMSSmmm-xxxx`.

    Registration, renew and regenerate share this shape because it is what an operator reads in
    `lxc config trust list` when auditing which entry is whose. The suffix only breaks ties inside
    one millisecond; nothing authenticates on it.
    """
    now = now or datetime.now(timezone.utc)
    suffix = "".join(secrets.choice(_SUFFIX_ALPHABET) for _ in range(4))
    return f"orca-{cluster_id}-{now:%Y%m%d}-{now:%H%M%S}{now.microsecond // 1000:03d}-{suffix}"


async def _blocking(fn: Any, *args: Any, timeout: float = VAULT_TIMEOUT_S, label: str) -> Any:
    return await run_blocking(fn, *args, timeout=timeout, label=label, executor=EXECUTOR)


def _path(cert_manager: Any, cluster_id: str, leaf: str) -> str:
    return f"{cert_manager.clusters_path}/{cluster_id}/{leaf}"


def read_rotation_state(cert_manager: Any, cluster_id: str) -> RotationState:
    """Blocking. Raises CertificateNotFound when the cluster has no active certificate."""
    try:
        certs = cert_manager.vault.vault_read(_path(cert_manager, cluster_id, "certs")) or {}
    except VaultClientError as exc:
        raise CertificateNotFound(cluster_id) from exc
    if not certs.get("client.crt"):
        raise CertificateNotFound(cluster_id)
    metadata = cert_manager.vault.vault_read(_path(cert_manager, cluster_id, "metadata")) or {}
    try:
        pending = cert_manager.vault.vault_read(_path(cert_manager, cluster_id, "pending_certs")) or {}
    except VaultClientError:
        pending = {}
    return RotationState(certs, metadata, pending if pending.get("client.crt") else None)


def _issue_and_trust(
    cert_manager: Any,
    cluster_id: str,
    request: CertificateRenewalRequest,
    current_valid: bool,
    identity_name: str,
    lifetime_plan: lifetime.LifetimePlan,
) -> Dict[str, Any]:
    """Blocking. A new certificate from Vault PKI, added to trust when the dashboard can do it."""
    settings = get_settings()
    issued, outcome = lifetime.issue(
        cert_manager.vault, f"{cluster_id}.{settings.orcastra_domain}", lifetime_plan,
    )
    auto = False
    if current_valid:
        try:
            auto = bool(cert_manager.add_trust_with_existing_cert(
                cluster_id=cluster_id, new_cert_pem=issued["cert"], cert_name=identity_name
            ))
        except Exception as exc:  # noqa: BLE001 - falls through to the password, then manual
            logger.warning("Auto-renewal failed for %s: %s", cluster_id, exc)
    # A certificate can be valid and still untrusted (ex; its trust entry was replaced or dropped
    # on the LXD side), so the trust password is tried whenever the existing certificate could not
    # add trust, whether or not it has expired.
    if not auto and request.trust_password:
        try:
            auto = bool(cert_manager.add_trust_with_password(
                cluster_id=cluster_id,
                new_cert_pem=issued["cert"],
                cert_name=identity_name,
                trust_password=request.trust_password,
            ))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Renewal with password failed for %s: %s", cluster_id, exc)
    return {"issued": issued, "auto": auto, "lifetime": outcome}


def _activate(cert_manager: Any, cluster_id: str, material: Dict[str, str]) -> None:
    """Blocking. Make `material` the active certificate and drop any pending one."""
    from app.core.cache import get_cache_manager

    cert_manager.vault.vault_kv_put(_path(cert_manager, cluster_id, "certs"), material)
    try:
        cert_manager.vault.vault_delete(_path(cert_manager, cluster_id, "pending_certs"))
    except VaultClientError:
        pass
    cache = get_cache_manager()
    if cache:
        cache.invalidate_cluster_cache(cluster_id)


def _adopt(cluster_id: str, endpoint: str, cert: str, key: str) -> None:
    """Blocking. Switch the worker that served this request to the new certificate.

    Its trusted connect announces the cluster, and services/lxd/breaker_sync.py moves the other
    workers within seconds.

    important: the reachability caches are dropped here, after the connect, and not in
    _activate. A list rebuilt between the two would be cached again with the stale Pending
    Trust status.
    """
    from app.core.cache import get_cache_manager
    from app.services.lxd_service import get_lxd_service

    client = get_lxd_service().connection_manager.adopt_vault_certificate(cluster_id, endpoint, cert, key)
    if client is not None:
        get_cache_manager().invalidate_cluster_reachability(cluster_id)


def audit(action: str, cluster_id: str, result: str = "success", **details: Any) -> None:
    try:
        from app.services.activity_logger import get_activity_logger

        get_activity_logger().log_activity(
            action=action,
            target_type="cluster",
            target_id=cluster_id,
            target_host=cluster_id,
            result=result,
            details={key: value for key, value in details.items() if value is not None},
        )
    except Exception:  # noqa: BLE001 - a lost audit line must not fail the rotation
        logger.warning("Could not audit %s on %s", action, cluster_id, exc_info=True)


def _cli_type(state: RotationState) -> str:
    # An undetermined type is LXD-family, whose CLI is lxc; only Incus differs.
    return state.metadata.get("type") or "LXD"


async def renew(
    db: AsyncSession,
    cert_manager: Any,
    request: CertificateRenewalRequest,
    *,
    session_maker: Optional[Any] = None,
) -> CertificateRenewalResponse:
    cluster_id = request.cluster_id
    grace = int(get_settings().lxd_trust_cleanup_grace_seconds)
    actor = get_current_user_id()
    async with store.cluster_cert_lock(db, cluster_id):
        state = await _blocking(read_rotation_state, cert_manager, cluster_id, label="cert.renew.read")
        expires = parse_certificate_pem(state.current_pem).get("not_after")
        current_valid = bool(expires and expires > datetime.now(timezone.utc))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        identity_name = trust_identity_name(cluster_id)
        pending_cert_id = f"pending-{stamp}-{secrets.token_hex(4)}"

        # Checked before anything is recorded: a refused lifetime leaves the cluster as it was. Six
        # months is what a renewal meant before the choice existed.
        lifetime_plan = await _blocking(
            lifetime.plan,
            lifetime.resolve(request.ttl, state.certs.get("lifetime"), CertificateLifetime.SIX_MONTHS),
            cert_manager.vault,
            label="cert.renew.lifetime",
        )

        if state.pending:
            await _record(
                session_maker, cluster_id=cluster_id, fingerprint=state.pending_fp or "", reason="pending_replaced",
                grace_seconds=grace, identity_name=state.pending.get("identity_name"), requested_by=actor,
            )
        outcome = await _blocking(
            _issue_and_trust, cert_manager, cluster_id, request, current_valid, identity_name, lifetime_plan,
            timeout=TRUST_TIMEOUT_S, label="cert.renew.issue",
        )
        issued, auto, issued_lifetime = outcome["issued"], outcome["auto"], outcome["lifetime"]
        new_fp = _fp(issued["cert"])
        domain = get_settings().orcastra_domain
        material = {
            "client.crt": issued["cert"],
            "client.key": issued["key"],
            "ca.pem": issued.get("ca", ""),
            "common_name": f"{cluster_id}.{domain}",
            "lifetime": issued_lifetime.lifetime.value,
        }

        if auto:
            await _record(
                session_maker, cluster_id=cluster_id, fingerprint=state.current_fp or "", reason="renew",
                grace_seconds=grace, requested_by=actor,
            )
            try:
                await _blocking(_activate, cert_manager, cluster_id, material, label="cert.renew.activate")
            except Exception:
                # Trust was added for the new certificate but it never became active.
                await _record(
                    session_maker, cluster_id=cluster_id, fingerprint=new_fp or "", reason="renew_aborted",
                    grace_seconds=grace, identity_name=identity_name, requested_by=actor,
                )
                await db.commit()
                raise
            await db.commit()
            try:
                await _blocking(
                    _adopt, cluster_id, state.endpoint, issued["cert"], issued["key"],
                    timeout=TRUST_TIMEOUT_S, label="cert.renew.adopt",
                )
            except Exception as exc:  # noqa: BLE001 - this worker heals on its next request
                logger.warning("Could not switch this worker to the renewed certificate: %s", exc)
            audit("cluster.certificate.renew", cluster_id, mode="automatic",
                   fingerprint=new_fp, previous_fingerprint=state.current_fp, identity_name=identity_name,
                   **lifetime.audit_fields(issued_lifetime))
            audit("cluster.certificate.activate", cluster_id,
                   fingerprint=new_fp, previous_fingerprint=state.current_fp)
            return CertificateRenewalResponse(
                success=True,
                message="Certificate renewed and active. The old trust entry is removed after the grace period.",
                cluster_id=cluster_id,
                auto_renewed=True,
                requires_manual_trust=False,
                pending_cert_id=pending_cert_id,
                fingerprint=new_fp,
                previous_fingerprint=state.current_fp,
                **lifetime.response_fields(issued_lifetime),
            )

        await _blocking(
            cert_manager.vault.vault_kv_put,
            _path(cert_manager, cluster_id, "pending_certs"),
            {
                **material,
                "pending_cert_id": pending_cert_id,
                "identity_name": identity_name,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "auto_renewed": False,
            },
            label="cert.renew.pending",
        )
        await db.commit()
    audit("cluster.certificate.renew", cluster_id, mode="manual",
           fingerprint=new_fp, previous_fingerprint=state.current_fp, identity_name=identity_name,
           **lifetime.audit_fields(issued_lifetime))
    return CertificateRenewalResponse(
        success=True,
        message="New certificate generated. Add trust manually and verify.",
        cluster_id=cluster_id,
        auto_renewed=False,
        requires_manual_trust=True,
        certificate=issued["cert"],
        cli_command=build_trust_add_command(_cli_type(state), issued["cert"], identity_name),
        pending_cert_id=pending_cert_id,
        fingerprint=new_fp,
        previous_fingerprint=state.current_fp,
        **lifetime.response_fields(issued_lifetime),
    )


async def verify(
    db: AsyncSession,
    cert_manager: Any,
    cluster_id: str,
    pending_cert_id: Optional[str],
    *,
    session_maker: Optional[Any] = None,
) -> CertificateVerifyResponse:
    grace = int(get_settings().lxd_trust_cleanup_grace_seconds)
    async with store.cluster_cert_lock(db, cluster_id):
        state = await _blocking(read_rotation_state, cert_manager, cluster_id, label="cert.verify.read")
        pending = state.pending
        if not pending:
            raise CertificateNotFound(cluster_id)
        stored_id = pending.get("pending_cert_id")
        if pending_cert_id and stored_id and pending_cert_id != stored_id:
            raise PendingMismatch(cluster_id)
        verified = await _blocking(
            cert_manager.verify_connection_with_cert,
            state.endpoint, pending["client.crt"], pending["client.key"],
            timeout=TRUST_TIMEOUT_S, label="cert.verify.probe",
        )
        if not verified:
            await db.rollback()
            return CertificateVerifyResponse(
                success=False,
                message="Certificate not yet trusted by hypervisor server. Add trust using the CLI command.",
                cluster_id=cluster_id,
                verified=False,
                activated=False,
                fingerprint=state.pending_fp,
                previous_fingerprint=state.current_fp,
            )
        await _record(
            session_maker, cluster_id=cluster_id, fingerprint=state.current_fp or "",
            reason="regenerate" if pending.get("origin") == "regenerate" else "verify",
            grace_seconds=grace, requested_by=get_current_user_id(),
        )
        material = {
            "client.crt": pending["client.crt"],
            "client.key": pending["client.key"],
            "ca.pem": pending.get("ca.pem", ""),
            "common_name": pending.get("common_name", ""),
            **({"lifetime": pending["lifetime"]} if pending.get("lifetime") else {}),
        }
        await _blocking(_activate, cert_manager, cluster_id, material, label="cert.verify.activate")
        await db.commit()
    try:
        await _blocking(
            _adopt, cluster_id, state.endpoint, pending["client.crt"], pending["client.key"],
            timeout=TRUST_TIMEOUT_S, label="cert.verify.adopt",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not switch this worker to the verified certificate: %s", exc)
    audit("cluster.certificate.activate", cluster_id,
           fingerprint=state.pending_fp, previous_fingerprint=state.current_fp)
    return CertificateVerifyResponse(
        success=True,
        message="Certificate verified and activated. The old trust entry is removed after the grace period.",
        cluster_id=cluster_id,
        verified=True,
        activated=True,
        fingerprint=state.pending_fp,
        previous_fingerprint=state.current_fp,
    )


async def cancel(
    db: AsyncSession,
    cert_manager: Any,
    cluster_id: str,
    *,
    reason: str = "cancel",
    session_maker: Optional[Any] = None,
) -> bool:
    """Drop the pending certificate, remembering it: its trust may already have been added.

    Returns False when there was nothing pending.
    """
    grace = int(get_settings().lxd_trust_cleanup_grace_seconds)
    async with store.cluster_cert_lock(db, cluster_id):
        try:
            state = await _blocking(read_rotation_state, cert_manager, cluster_id, label="cert.cancel.read")
        except CertificateNotFound:
            state = None
        pending = state.pending if state else None
        if not pending:
            await db.rollback()
            return False
        await _record(
            session_maker, cluster_id=cluster_id, fingerprint=state.pending_fp or "", reason=reason,
            grace_seconds=grace, identity_name=pending.get("identity_name"),
            requested_by=get_current_user_id(),
        )
        await _blocking(
            cert_manager.vault.vault_delete, _path(cert_manager, cluster_id, "pending_certs"),
            label="cert.cancel.delete",
        )
        await db.commit()
    audit("cluster.certificate.cancel", cluster_id, fingerprint=state.pending_fp, reason=reason)
    return True
