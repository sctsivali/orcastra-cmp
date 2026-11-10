"""Removing the LXD trust entries of client certificates this dashboard replaced.

important: keep this order or the dashboard can lock itself out or delete an entry it does not own:
1. Only fingerprints recorded in `lxd_trust_cleanup` for this cluster are candidates, never names;
   anything still current or pending in Vault is cancelled instead.
2. The CURRENT Vault certificate is proven trusted and unrestricted on a fresh pinned session
   (trust_http), else the grace period is re-armed: right after a regenerate the cluster does not
   trust the new certificate yet, and removing the replaced entry would cut every worker off.
3. Each entry is looked up and its fingerprint compared exactly before the DELETE.

Every worker runs the loop; a per-cluster transaction lock (trust_cleanup_store.cluster_cert_lock)
picks who acts. No leader is elected: a Redis lease fails open and would let every worker sweep at
once during an outage. Workers pre-switch their pooled connection to the current certificate once
trusted, so none still presents the replaced one when its entry goes; one that misses the switch
heals on its next request (connection.py, _try_rotated_certificate).
"""

import asyncio
import logging
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.cert_fingerprint import lxd_fingerprint
from app.core.config import get_settings
from app.core.type_utils import build_trust_remove_command
from app.models.trust_cleanup_models import LxdTrustCleanup
from app.services.lxd_certs import trust_cleanup_store as store
from app.services.lxd_certs.trust_http import (
    IdentityProbe,
    RemovalOutcome,
    pinned_session,
    probe_identity,
    remove_fingerprint,
    server_identity_problem,
)

logger = logging.getLogger(__name__)

ACTION_TRUST_REMOVE = "cluster.certificate.trust_remove"

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lxd-trust")
READ_TIMEOUT_S = 20.0
REMOVE_TIMEOUT_S = 90.0
# A delete click waits on these, so they are tighter than the background sweep's.
DEREGISTER_READ_TIMEOUT_S = 10.0
DEREGISTER_REMOVE_TIMEOUT_S = 30.0
BACKOFF_BASE_S = 60
BACKOFF_CAP_S = 3600
FIRST_RUN_DELAY_S = 20.0
JITTER_S = 10.0

_task: Optional[asyncio.Task] = None


@dataclass(frozen=True)
class ClusterCertState:
    """What Vault holds for one cluster, read once per pass."""

    endpoint: str
    cluster_type: Optional[str]
    server_fingerprint: Optional[str]
    current_pem: str
    current_key: str = field(repr=False)
    current_fp: str
    pending_fp: Optional[str]


@dataclass
class SweepReport:
    cluster_id: str
    skipped: Optional[str] = None
    probe: Optional[str] = None
    outcomes: Dict[str, str] = field(default_factory=dict)


def _cert_manager() -> Any:
    from app.core.vault_client import get_vault_client
    from app.services.lxd_cert_manager import LXDCertificateManager

    settings = get_settings()
    if not (settings.vault_enabled and settings.vault_token):
        return None
    return LXDCertificateManager(
        vault_client=get_vault_client(settings.vault_addr, settings.vault_token),
        clusters_path=settings.vault_clusters_path,
    )


def read_cluster_state(cluster_id: str) -> Optional[ClusterCertState]:
    """Blocking. None when Vault cannot say what the current certificate is."""
    from app.core.vault_client import VaultClientError
    from app.services.lxd_certs.pinning import FINGERPRINT_FIELD

    manager = _cert_manager()
    if manager is None:
        return None
    base = f"{manager.clusters_path}/{cluster_id}"
    try:
        metadata = manager.vault.vault_read(f"{base}/metadata") or {}
        certs = manager.vault.vault_read(f"{base}/certs") or {}
    except VaultClientError as exc:
        logger.info("Trust cleanup cannot read %s from Vault: %s", cluster_id, exc)
        return None
    current_pem = certs.get("client.crt") or ""
    current_key = certs.get("client.key") or ""
    endpoint = metadata.get("endpoint") or ""
    if not (current_pem and current_key and endpoint):
        return None
    pending_fp = None
    try:
        pending = manager.vault.vault_read(f"{base}/pending_certs") or {}
        if pending.get("client.crt"):
            pending_fp = lxd_fingerprint(pending["client.crt"])
    except (VaultClientError, ValueError):
        pending_fp = None
    try:
        current_fp = lxd_fingerprint(current_pem)
    except ValueError:
        return None
    return ClusterCertState(
        endpoint=endpoint,
        cluster_type=metadata.get("type"),
        server_fingerprint=metadata.get(FINGERPRINT_FIELD),
        current_pem=current_pem,
        current_key=current_key,
        current_fp=current_fp,
        pending_fp=pending_fp,
    )


def prove_and_remove(
    cluster_id: str, state: ClusterCertState, fingerprints: Sequence[str]
) -> Tuple[IdentityProbe, Dict[str, Tuple[RemovalOutcome, str]]]:
    """Blocking. Removes nothing unless the server is authenticated and the current certificate
    may manage trust."""
    problem = server_identity_problem(
        state.endpoint, state.server_fingerprint,
        pinning_enabled=bool(get_settings().lxd_pin_server_certificate),
    )
    if problem is not None:
        return problem, {}
    with pinned_session(cluster_id, state.current_pem, state.current_key, state.server_fingerprint) as session:
        probe = probe_identity(session, state.endpoint, state.current_fp)
        if not probe.may_remove:
            return probe, {}
        results = {fp: remove_fingerprint(session, state.endpoint, fp) for fp in fingerprints}
    return probe, results


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(BACKOFF_BASE_S * (2 ** max(attempts - 1, 0)), BACKOFF_CAP_S))


def _settle_unproven(row: LxdTrustCleanup, probe: IdentityProbe, now: datetime, grace: int) -> None:
    row.last_attempt_at = now
    row.last_error = probe.message
    if probe.blocked:
        row.state = "failed"
    elif not probe.reachable:
        row.attempts = (row.attempts or 0) + 1
        row.due_at = now + _backoff(row.attempts)
    elif not probe.trusted:
        # Not an attempt: removal is not yet safe. The grace restarts once the cluster trusts the
        # current certificate, which is what gives other workers time to switch to it.
        row.due_at = now + timedelta(seconds=grace)
    else:
        row.state = "failed"


def _settle(row: LxdTrustCleanup, outcome: RemovalOutcome, message: str, now: datetime) -> None:
    row.last_attempt_at = now
    if outcome in (RemovalOutcome.REMOVED, RemovalOutcome.ALREADY_GONE):
        row.state = "removed"
        row.removed_at = now
        row.already_gone = outcome is RemovalOutcome.ALREADY_GONE
        row.last_error = None
    elif outcome is RemovalOutcome.TRANSIENT:
        row.attempts = (row.attempts or 0) + 1
        row.due_at = now + _backoff(row.attempts)
        row.last_error = message
    else:
        row.state = "failed"
        row.last_error = message


def _audit(cluster_id: str, row: LxdTrustCleanup, trigger: str, actor: Optional[str]) -> None:
    if row.state not in ("removed", "failed"):
        return
    try:
        from app.services.activity_logger import get_activity_logger

        get_activity_logger().log_activity(
            action=ACTION_TRUST_REMOVE,
            target_type="cluster",
            target_id=cluster_id,
            target_host=cluster_id,
            result="success" if row.state == "removed" else "failure",
            error_message=row.last_error if row.state == "failed" else None,
            details={
                "fingerprint": row.fingerprint,
                "reason": row.reason,
                "trigger": trigger,
                "already_gone": bool(row.already_gone),
                "identity_name": row.identity_name,
            },
            actor_user_id=actor,
        )
    except Exception:  # noqa: BLE001 - a lost audit line must not undo a settled row
        logger.warning("Could not audit trust removal on %s", cluster_id, exc_info=True)


async def sweep_cluster(
    session_maker: Callable[[], Any],
    cluster_id: str,
    *,
    force: bool = False,
    trigger: str = "sweep",
    actor: Optional[str] = None,
    fingerprint: Optional[str] = None,
) -> SweepReport:
    """One pass over one cluster. `force` is "Remove now": no grace, failed rows retried.

    Raises store.CertificateOperationBusy only when `force`; the timed sweep just skips.
    """
    settings = get_settings()
    grace = int(settings.lxd_trust_cleanup_grace_seconds)
    now = store.utcnow()
    report = SweepReport(cluster_id)
    settled: List[LxdTrustCleanup] = []
    async with session_maker() as db:
        try:
            async with store.cluster_cert_lock(db, cluster_id):
                rows = await store.open_rows(
                    db, cluster_id, due_by=None if force else now, include_failed=force
                )
                if fingerprint:
                    rows = [row for row in rows if row.fingerprint == fingerprint]
                if not rows:
                    await db.rollback()
                    report.skipped = "nothing due"
                    return report
                try:
                    state = await run_blocking(
                        read_cluster_state, cluster_id,
                        timeout=READ_TIMEOUT_S, label="trust_cleanup.read", executor=_executor,
                    )
                except (BlockingTimeout, BlockingPoolFull):
                    state = None
                if state is None:
                    await db.rollback()
                    report.skipped = "Vault did not answer for this cluster"
                    return report
                protected = {state.current_fp, state.pending_fp}
                live = []
                for row in rows:
                    if row.fingerprint in protected:
                        row.state = "cancelled"
                        row.last_error = "still the cluster's current or pending certificate"
                        row.last_attempt_at = now
                    else:
                        live.append(row)
                if live:
                    try:
                        probe, results = await run_blocking(
                            prove_and_remove, cluster_id, state, [row.fingerprint for row in live],
                            timeout=REMOVE_TIMEOUT_S, label="trust_cleanup.remove", executor=_executor,
                        )
                    except (BlockingTimeout, BlockingPoolFull) as exc:
                        probe = IdentityProbe(False, False, None, f"timed out: {type(exc).__name__}")
                        results = {}
                    report.probe = probe.message
                    for row in live:
                        if row.fingerprint in results:
                            outcome, message = results[row.fingerprint]
                            _settle(row, outcome, message, now)
                            report.outcomes[row.fingerprint] = outcome.value
                        else:
                            _settle_unproven(row, probe, now, grace)
                            report.outcomes[row.fingerprint] = row.state
                        settled.append(row)
                await db.commit()
        except store.CertificateOperationBusy:
            if force:
                raise
            report.skipped = "another certificate change holds the lock"
            return report
    for row in settled:
        _audit(cluster_id, row, trigger, actor or row.requested_by)
    return report


def preswitch_worker(cluster_id: str) -> bool:
    """Blocking. Move this worker's pooled connection to the current certificate, if proven.

    important: never a blind connect. connect_with_vault_cert on a refused certificate drops the
    cached credentials and trips the breaker, which after a regenerate would take a working
    cluster offline for a minute in this worker. The staging probe proves trust first.
    """
    from app.services.lxd_service import get_lxd_service

    manager = get_lxd_service().connection_manager
    held = manager.active_certificate_fingerprint(cluster_id)
    if not held:
        return False
    state = read_cluster_state(cluster_id)
    if state is None or state.current_fp == held:
        return False
    with pinned_session(cluster_id, state.current_pem, state.current_key, state.server_fingerprint) as session:
        if not probe_identity(session, state.endpoint, state.current_fp).trusted:
            return False
    adopted = manager.adopt_vault_certificate(
        cluster_id, state.endpoint, state.current_pem, state.current_key
    )
    if adopted is not None:
        logger.info("Switched this worker's connection to %s onto the current certificate", cluster_id)
    return adopted is not None


async def sweep_once(session_maker: Callable[[], Any]) -> None:
    settings = get_settings()
    if not settings.lxd_trust_cleanup_enabled:
        return
    now = store.utcnow()
    async with session_maker() as db:
        pending = await store.clusters_with_pending(db)
        due = await store.clusters_with_pending(db, due_by=now)
        await db.rollback()
    for cluster_id in pending:
        try:
            await run_blocking(
                preswitch_worker, cluster_id,
                timeout=READ_TIMEOUT_S + 15, label="trust_cleanup.preswitch", executor=_executor,
            )
        except Exception as exc:  # noqa: BLE001 - the request path heals a worker this misses
            logger.debug("Pre-switch of %s skipped: %s", cluster_id, exc)
    for cluster_id in due:
        try:
            report = await sweep_cluster(session_maker, cluster_id)
            if report.outcomes:
                logger.info("Trust cleanup on %s: %s", cluster_id, report.outcomes)
        except Exception:  # noqa: BLE001 - one cluster must not stop the pass
            logger.warning("Trust cleanup on %s failed", cluster_id, exc_info=True)


async def _loop(session_maker: Callable[[], Any], interval: float) -> None:
    await asyncio.sleep(FIRST_RUN_DELAY_S + random.uniform(0, JITTER_S))
    while True:
        try:
            await sweep_once(session_maker)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop outlives a bad pass
            logger.warning("Trust cleanup pass failed", exc_info=True)
        await asyncio.sleep(interval + random.uniform(0, JITTER_S))


def start_trust_cleanup_loop(session_maker: Callable[[], Any], *, interval: float) -> None:
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop(session_maker, interval), name="lxd-trust-cleanup")


def stop_trust_cleanup_loop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None


def manual_command(cluster_type: Optional[str], fingerprint: str) -> str:
    return build_trust_remove_command(cluster_type, fingerprint)


@dataclass(frozen=True)
class DeregistrationTrust:
    """What deregistration managed to revoke. `complete` is False whenever the dashboard could not
    prove its own current entry gone, including when it could not even say what that entry was."""

    commands: List[str]
    complete: bool


async def remove_trust_for_deregistration(db: Any, cluster_id: str) -> DeregistrationTrust:
    """Best effort, before the Vault material is deleted: superseded entries, the pending
    certificate, then the dashboard's own entry last. Returns the commands an operator still needs.

    Called under cluster_cert_lock by delete_cluster. Never raises: deregistration proceeds either way.
    """
    rows = await store.open_rows(db, cluster_id, include_failed=True)
    try:
        state = await run_blocking(
            read_cluster_state, cluster_id,
            timeout=DEREGISTER_READ_TIMEOUT_S, label="trust_cleanup.deregister.read", executor=_executor,
        )
    except Exception:  # noqa: BLE001
        state = None
    tracked = [row.fingerprint for row in rows]
    if state is None:
        # The current certificate is unknown, so the one entry that matters most cannot be named.
        return DeregistrationTrust([manual_command(None, fp) for fp in tracked], complete=False)
    ordered = [fp for fp in tracked if fp not in (state.current_fp, state.pending_fp)]
    if state.pending_fp and state.pending_fp != state.current_fp:
        ordered.append(state.pending_fp)
    ordered.append(state.current_fp)
    try:
        probe, results = await run_blocking(
            prove_and_remove, cluster_id, state, ordered,
            timeout=DEREGISTER_REMOVE_TIMEOUT_S, label="trust_cleanup.deregister.remove", executor=_executor,
        )
    except Exception as exc:  # noqa: BLE001
        probe, results = IdentityProbe(False, False, None, f"{type(exc).__name__}"), {}
    commands = []
    for fp in ordered:
        outcome, message = results.get(fp, (RemovalOutcome.FAILED, probe.message))
        if fp == state.current_fp and probe.reachable and not probe.trusted and not probe.blocked:
            # The cluster answered and does not trust the current certificate, so it has no entry
            # for it: handing the operator a command for it would send them after nothing.
            outcome, message = RemovalOutcome.ALREADY_GONE, "the cluster does not trust it"
        removed = outcome in (RemovalOutcome.REMOVED, RemovalOutcome.ALREADY_GONE)
        if not removed:
            commands.append(manual_command(state.cluster_type, fp))
        _audit_deregistration(cluster_id, fp, outcome, message, own=fp == state.current_fp)
    return DeregistrationTrust(commands, complete=not commands)


def _audit_deregistration(
    cluster_id: str, fingerprint: str, outcome: RemovalOutcome, message: str, *, own: bool
) -> None:
    try:
        from app.services.activity_logger import get_activity_logger

        ok = outcome in (RemovalOutcome.REMOVED, RemovalOutcome.ALREADY_GONE)
        get_activity_logger().log_activity(
            action=ACTION_TRUST_REMOVE,
            target_type="cluster",
            target_id=cluster_id,
            target_host=cluster_id,
            result="success" if ok else "failure",
            error_message=None if ok else message,
            details={
                "fingerprint": fingerprint,
                "trigger": "deregister",
                "outcome": outcome.value,
                "own_certificate": own,
            },
        )
    except Exception:  # noqa: BLE001
        logger.warning("Could not audit trust removal on %s", cluster_id, exc_info=True)


def superseded_view(rows: Sequence[LxdTrustCleanup], cluster_type: Optional[str]) -> List[Any]:
    """Rows as the API shows them; an entry still on the cluster carries its manual command."""
    from app.models.cert_trust_schemas import SupersededTrust

    return [
        SupersededTrust(
            fingerprint=row.fingerprint,
            reason=row.reason,
            state=row.state,
            identity_name=row.identity_name,
            superseded_at=row.superseded_at,
            due_at=row.due_at if row.state == "pending" else None,
            removed_at=row.removed_at,
            already_gone=bool(row.already_gone),
            attempts=int(row.attempts or 0),
            last_error=row.last_error,
            manual_command=(
                manual_command(cluster_type, row.fingerprint)
                if row.state in ("pending", "failed")
                else None
            ),
        )
        for row in rows
    ]
