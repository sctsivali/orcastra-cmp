"""
Certificate endpoints: status, renewal, verification and superseded trust cleanup.

The rotation logic lives in services/lxd_certs/rotation.py and the trust cleanup in
services/lxd_certs/trust_cleanup.py; this file maps HTTP onto them. Every single-cluster
mutation takes the cluster's certificate lock and answers 409 while another change on it
holds the lock.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.blocking import run_blocking
from app.core.config import get_settings
from app.core.database import async_session_maker, get_db
from app.core.dependencies import check_read_only_mode, require_partner
from app.core.auth import AuthenticatedUser, get_current_user
from app.core.org_scope import org_scope_dependency, verify_cluster_access
from app.core.whole_cluster_access import verify_whole_cluster_access, whole_clusters
from app.core.request_context import get_current_user_id
from app.core.vault_client import VaultClientError, get_vault_client
from app.models.cert_trust_schemas import (
    PendingCertificateResponse,
    TrustCleanupRequest,
    TrustCleanupResponse,
)
from app.models.schemas import (
    CertificateInfo,
    CertificateListResponse,
    CertificateRenewalRequest,
    CertificateRenewalResponse,
    CertificateStatus,
    CertificateVerifyRequest,
    CertificateVerifyResponse,
    CertificateWarning,
    SystemWarningsResponse,
)
from app.services.lxd_cert_manager import LXDCertificateManager
from app.services.lxd_certs import pending as pending_certs
from app.services.lxd_certs import rotation, trust_cleanup
from app.services.lxd_certs import trust_cleanup_store as store
from app.services.lxd_certs.inspect import (
    WARNING_THRESHOLDS,
    days_until,
    get_warning_severity,
    parse_certificate_pem,
    read_certificate_row,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/certificates",
    tags=["Certificate Management"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)

ROW_READ_TIMEOUT_S = 20.0
BUSY_DETAIL = "Another certificate change on this cluster is in progress. Try again in a moment."


def get_cert_manager() -> LXDCertificateManager:
    """Dependency to get LXD Certificate Manager instance."""
    settings = get_settings()

    if not settings.vault_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vault integration is not enabled"
        )

    if not settings.vault_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vault token is not configured"
        )

    try:
        vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
        return LXDCertificateManager(
            vault_client=vault_client,
            clusters_path=settings.vault_clusters_path
        )
    except Exception as e:
        logger.error(f"Failed to initialize cert manager: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Certificate manager initialization failed: {str(e)}"
        )


def _busy() -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=BUSY_DETAIL)


async def _read_rows(
    cert_manager: LXDCertificateManager, cluster_ids: List[str]
) -> List[Tuple[CertificateInfo, Optional[str]]]:
    """Each cluster's three Vault reads on a worker thread, all clusters at once."""

    async def one(cluster_id: str) -> Optional[Tuple[CertificateInfo, Optional[str]]]:
        try:
            return await run_blocking(
                read_certificate_row, cert_manager, cluster_id,
                timeout=ROW_READ_TIMEOUT_S, label="certificates.row", executor=rotation.EXECUTOR,
            )
        except Exception as exc:  # noqa: BLE001 - one slow cluster must not blank the page
            logger.warning("Certificate row for %s unavailable: %s", cluster_id, exc)
            return CertificateInfo(cluster_id=cluster_id, status=CertificateStatus.UNKNOWN), None

    results = await asyncio.gather(*(one(cid) for cid in cluster_ids))
    return [row for row in results if row is not None]


async def _with_superseded(
    db: AsyncSession, rows: List[Tuple[CertificateInfo, Optional[str]]]
) -> List[CertificateInfo]:
    grouped = await store.rows_for_clusters(db, [info.cluster_id for info, _ in rows])
    out = []
    for info, cluster_type in rows:
        info.superseded = trust_cleanup.superseded_view(grouped.get(info.cluster_id, []), cluster_type)
        out.append(info)
    return out


@router.get(
    "",
    response_model=CertificateListResponse,
    summary="List all certificate statuses",
    description="Certificate status, LXD fingerprint and superseded trust entries for every cluster in scope."
)
async def list_certificates(
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        # important: a cluster certificate acts for every project on its cluster, so a non-admin
        # keeps only the clusters no other party holds (core/whole_cluster_access).
        cluster_ids = await whole_clusters(
            await asyncio.to_thread(cert_manager.list_clusters, use_cache=False), db, user
        )
        certificates = await _with_superseded(db, await _read_rows(cert_manager, cluster_ids))
    except Exception as e:
        logger.error(f"Failed to list certificates: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not list certificates: {str(e)}"
        )
    return CertificateListResponse(
        certificates=certificates,
        total=len(certificates),
        expiring_count=sum(1 for c in certificates if c.status == CertificateStatus.EXPIRING),
        expired_count=sum(1 for c in certificates if c.status == CertificateStatus.EXPIRED),
    )


@router.get(
    "/{cluster_id}",
    response_model=CertificateInfo,
    summary="Get certificate status for a cluster",
    description="Certificate details, LXD fingerprint and superseded trust entries for one cluster."
)
async def get_certificate(
    cluster_id: str,
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    verify_cluster_access(cluster_id)
    # important: a cluster's client certificate and its renewal act for every project on the
    # cluster, so this needs the whole cluster (core/whole_cluster_access).
    await verify_whole_cluster_access(cluster_id, db, user)
    rows = await _read_rows(cert_manager, [cluster_id])
    if not rows or rows[0][0].status == CertificateStatus.UNKNOWN and not rows[0][0].fingerprint:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No certificate found for cluster {cluster_id}"
        )
    return (await _with_superseded(db, rows))[0]


@router.get(
    "/{cluster_id}/pending",
    response_model=PendingCertificateResponse,
    summary="Resume a pending renewal",
    description="The pending certificate, its fingerprint and the trust command, so Verify can be resumed."
)
async def get_pending_certificate(
    cluster_id: str,
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    verify_cluster_access(cluster_id)
    # important: a cluster's client certificate and its renewal act for every project on the
    # cluster, so this needs the whole cluster (core/whole_cluster_access).
    await verify_whole_cluster_access(cluster_id, db, user)
    try:
        return await pending_certs.pending_view(cert_manager, cluster_id)
    except rotation.CertificateNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No pending certificate for cluster {cluster_id}"
        )


@router.post(
    "/renew",
    response_model=CertificateRenewalResponse,
    summary="Initiate certificate renewal",
    description="""
    Initiate certificate renewal for a cluster.

    **Flow:**
    1. If the current certificate is trusted, it adds the new one to LXD trust and it is active at once
    2. Otherwise the trust password, then the manual CLI command, and Verify activates it
    3. The replaced certificate's trust entry is removed after the grace period
    """
)
async def initiate_renewal(
    request: CertificateRenewalRequest,
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(get_current_user),
):
    verify_cluster_access(request.cluster_id)
    # important: a cluster's client certificate and its renewal act for every project on the
    # cluster, so this needs the whole cluster (core/whole_cluster_access).
    await verify_whole_cluster_access(request.cluster_id, db, user)
    try:
        return await rotation.renew(db, cert_manager, request)
    except store.CertificateOperationBusy:
        raise _busy()
    except rotation.CertificateNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No certificate found for cluster {request.cluster_id}"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to initiate renewal for {request.cluster_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not initiate renewal: {str(e)}"
        )


@router.post(
    "/verify",
    response_model=CertificateVerifyResponse,
    summary="Verify pending certificate renewal",
    description="""
    Verify that a pending certificate has been trusted by the LXD server.
    If successful, replaces the old certificate with the new one.
    """
)
async def verify_renewal(
    request: CertificateVerifyRequest,
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(get_current_user),
):
    verify_cluster_access(request.cluster_id)
    # important: a cluster's client certificate and its renewal act for every project on the
    # cluster, so this needs the whole cluster (core/whole_cluster_access).
    await verify_whole_cluster_access(request.cluster_id, db, user)
    try:
        return await rotation.verify(db, cert_manager, request.cluster_id, request.pending_cert_id)
    except store.CertificateOperationBusy:
        raise _busy()
    except rotation.PendingMismatch:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A newer renewal replaced this pending certificate. Reload and verify that one."
        )
    except rotation.CertificateNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No pending certificate found for cluster {request.cluster_id}"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to verify renewal for {request.cluster_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not verify renewal: {str(e)}"
        )


@router.delete(
    "/{cluster_id}/pending",
    summary="Cancel pending certificate renewal",
    description="Delete the pending certificate. Its trust entry, if it was added, is removed later."
)
async def cancel_renewal(
    cluster_id: str,
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(get_current_user),
):
    verify_cluster_access(cluster_id)
    # important: a cluster's client certificate and its renewal act for every project on the
    # cluster, so this needs the whole cluster (core/whole_cluster_access).
    await verify_whole_cluster_access(cluster_id, db, user)
    try:
        cancelled = await rotation.cancel(db, cert_manager, cluster_id)
    except store.CertificateOperationBusy:
        raise _busy()
    except Exception as e:
        logger.error(f"Failed to cancel renewal for {cluster_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not cancel renewal: {str(e)}"
        )
    if cancelled:
        return {"success": True, "message": "Pending renewal cancelled"}
    return {"success": True, "message": "No pending renewal to cancel"}


@router.post(
    "/{cluster_id}/trust-cleanup",
    response_model=TrustCleanupResponse,
    summary="Remove superseded trust entries now",
    description=(
        "Remove the trust entries of certificates this dashboard replaced on the cluster, without "
        "waiting for the grace period. The current certificate must be trusted and unrestricted."
    ),
)
async def remove_superseded_trust(
    cluster_id: str,
    request: TrustCleanupRequest,
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(get_current_user),
):
    verify_cluster_access(cluster_id)
    # important: a cluster's client certificate and its renewal act for every project on the
    # cluster, so this needs the whole cluster (core/whole_cluster_access).
    await verify_whole_cluster_access(cluster_id, db, user)
    try:
        report = await trust_cleanup.sweep_cluster(
            async_session_maker, cluster_id, force=True, trigger="remove_now",
            actor=get_current_user_id(), fingerprint=request.fingerprint,
        )
    except store.CertificateOperationBusy:
        raise _busy()
    rows = await _read_rows(cert_manager, [cluster_id])
    cluster_type = rows[0][1] if rows else None
    grouped = await store.rows_for_clusters(db, [cluster_id])
    if report.skipped:
        message = f"Nothing removed: {report.skipped}."
    elif report.outcomes:
        message = report.probe or "Done."
    else:
        message = "Nothing to remove."
    return TrustCleanupResponse(
        cluster_id=cluster_id,
        message=message,
        outcomes=report.outcomes,
        superseded=trust_cleanup.superseded_view(grouped.get(cluster_id, []), cluster_type),
    )


@router.get(
    "/warnings/all",
    response_model=SystemWarningsResponse,
    summary="Get all system warnings",
    description="Get certificate and cluster connectivity warnings for all clusters."
)
async def get_system_warnings(
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Get all certificate and node warnings."""
    try:
        settings = get_settings()
        # important: the same whole-cluster rule as the list (core/whole_cluster_access).
        cluster_ids = await whole_clusters(
            await asyncio.to_thread(cert_manager.list_clusters, use_cache=False), db, user
        )

        certificate_warnings = []
        node_warnings = []
        critical_count = 0
        warning_count = 0
        info_count = 0

        for cluster_id in cluster_ids:
            try:
                metadata_path = f"{settings.vault_clusters_path}/{cluster_id}/metadata"
                metadata = cert_manager.vault.vault_read(metadata_path)
                display_name = metadata.get("display_name", cluster_id)
                endpoint = metadata.get("endpoint", "")

                cert_path = f"{settings.vault_clusters_path}/{cluster_id}/certs"
                try:
                    cert_data = cert_manager.vault.vault_read(cert_path)
                    cert_pem = cert_data.get("client.crt", "")
                    cert_info = parse_certificate_pem(cert_pem) if cert_pem else {}

                    expires_at = cert_info.get("not_after")
                    days_until_expiry = days_until(expires_at)
                    if days_until_expiry is not None and days_until_expiry <= WARNING_THRESHOLDS["warning"]:
                        severity = get_warning_severity(days_until_expiry)

                        if days_until_expiry <= 0:
                            warning_type = "cert_expired"
                            message = f"Certificate expired {abs(days_until_expiry)} days ago"
                        else:
                            warning_type = "cert_expiring"
                            message = f"Certificate expires in {days_until_expiry} days"

                        certificate_warnings.append(CertificateWarning(
                            cluster_id=cluster_id,
                            display_name=display_name,
                            warning_type=warning_type,
                            severity=severity,
                            message=message,
                            days_until_expiry=days_until_expiry,
                            expires_at=expires_at,
                            endpoint=endpoint,
                        ))

                        if severity == "critical":
                            critical_count += 1
                        elif severity in ["high", "warning"]:
                            warning_count += 1
                        else:
                            info_count += 1

                except VaultClientError:
                    node_warnings.append(CertificateWarning(
                        cluster_id=cluster_id,
                        display_name=display_name,
                        warning_type="no_certificate",
                        severity="critical",
                        message="No certificate configured",
                        endpoint=endpoint,
                    ))
                    critical_count += 1

                connected, error_msg = cert_manager.verify_connection(cluster_id)
                if not connected:
                    node_warnings.append(CertificateWarning(
                        cluster_id=cluster_id,
                        display_name=display_name,
                        warning_type="node_offline",
                        severity="critical",
                        message=error_msg or "Cluster is not reachable",
                        endpoint=endpoint,
                    ))
                    critical_count += 1

            except Exception as e:
                logger.warning(f"Failed to check warnings for {cluster_id}: {e}")

        return SystemWarningsResponse(
            total=len(certificate_warnings) + len(node_warnings),
            critical=critical_count,
            warning=warning_count,
            info=info_count,
            certificate_warnings=certificate_warnings,
            node_warnings=node_warnings,
        )

    except Exception as e:
        logger.error(f"Failed to get system warnings: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not get warnings: {str(e)}"
        )


@router.post(
    "/cleanup-pending",
    summary="Cleanup stale pending certificates",
    description="Remove pending certificates older than 24 hours. Their trust entries are removed later."
)
async def cleanup_pending_certificates(
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    _: None = Depends(check_read_only_mode),
    db: AsyncSession = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Cleanup stale pending certificates."""
    try:
        # important: only the clusters this caller holds whole (core/whole_cluster_access).
        cluster_ids = await whole_clusters(
            await asyncio.to_thread(cert_manager.list_clusters, use_cache=False), db, user
        )
        now = datetime.now(timezone.utc)
        cleaned = 0

        for cluster_id in cluster_ids:
            try:
                state = await run_blocking(
                    rotation.read_rotation_state, cert_manager, cluster_id,
                    timeout=ROW_READ_TIMEOUT_S, label="certificates.cleanup.read",
                    executor=rotation.EXECUTOR,
                )
            except Exception:  # noqa: BLE001 - no certificate, or Vault did not answer
                continue
            if not state.pending or not pending_certs.pending_is_stale(state.pending, now):
                continue
            try:
                # One transaction per cluster, so each lock is released before the next.
                async with async_session_maker() as db:
                    if await rotation.cancel(db, cert_manager, cluster_id, reason="pending_expired"):
                        cleaned += 1
                        logger.info(f"Cleaned up stale pending cert for {cluster_id}")
            except store.CertificateOperationBusy:
                continue
            except Exception as e:
                logger.warning(f"Failed to clean pending cert for {cluster_id}: {e}")

        return {
            "success": True,
            "message": f"Cleaned up {cleaned} stale pending certificates",
            "cleaned_count": cleaned
        }

    except Exception as e:
        logger.error(f"Failed to cleanup pending certificates: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Cleanup failed: {str(e)}"
        )
