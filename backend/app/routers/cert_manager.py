"""
Certificate Manager Router
Handles certificate lifecycle management, renewal, and monitoring.
"""

import logging
import hashlib
from typing import Optional
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends, status, BackgroundTasks
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

from app.models.schemas import (
    CertificateInfo,
    CertificateListResponse,
    CertificateRenewalRequest,
    CertificateRenewalResponse,
    CertificateVerifyRequest,
    CertificateVerifyResponse,
    CertificateWarning,
    SystemWarningsResponse,
    CertificateStatus,
)
from app.core.config import get_settings
from app.core.vault_client import get_vault_client, VaultClientError
from app.services.lxd_cert_manager import LXDCertificateManager
from app.core.dependencies import check_read_only_mode, require_partner
from app.core.type_utils import build_trust_add_command
from app.core.org_scope import org_scope_dependency, verify_cluster_access

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/certificates",
    tags=["Certificate Management"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)

# Warning thresholds in days
WARNING_THRESHOLDS = {
    "critical": 3,   # 3 days or less
    "high": 7,       # 7 days or less
    "warning": 14,   # 14 days or less
}


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


def parse_certificate_pem(cert_pem: str) -> dict:
    """
    Parse X.509 certificate PEM to extract metadata.
    Returns dict with issuer, subject, not_before, not_after, serial, fingerprint.
    """
    try:
        cert = x509.load_pem_x509_certificate(cert_pem.encode(), default_backend())
        
        # Extract issuer CN
        issuer_cn = None
        for attr in cert.issuer:
            if attr.oid == x509.oid.NameOID.COMMON_NAME:
                issuer_cn = attr.value
                break
        
        # Extract subject CN
        subject_cn = None
        for attr in cert.subject:
            if attr.oid == x509.oid.NameOID.COMMON_NAME:
                subject_cn = attr.value
                break
        
        # Calculate SHA256 fingerprint
        fingerprint = hashlib.sha256(cert.public_bytes(
            encoding=serialization.Encoding.DER
        )).hexdigest().upper()
        fingerprint = ':'.join(fingerprint[i:i+2] for i in range(0, len(fingerprint), 2))
        
        return {
            "issuer": issuer_cn,
            "subject": subject_cn,
            "not_before": cert.not_valid_before_utc if hasattr(cert, 'not_valid_before_utc') else cert.not_valid_before.replace(tzinfo=timezone.utc),
            "not_after": cert.not_valid_after_utc if hasattr(cert, 'not_valid_after_utc') else cert.not_valid_after.replace(tzinfo=timezone.utc),
            "serial_number": format(cert.serial_number, 'x').upper(),
            "fingerprint": fingerprint,
        }
    except Exception as e:
        logger.error(f"Failed to parse certificate: {e}")
        return {}


def get_certificate_status(expires_at: Optional[datetime], has_pending: bool = False) -> CertificateStatus:
    """Determine certificate status based on expiry date."""
    if has_pending:
        return CertificateStatus.PENDING_RENEWAL
    
    if not expires_at:
        return CertificateStatus.UNKNOWN
    
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    
    days_until_expiry = (expires_at - now).days
    
    if days_until_expiry < 0:
        return CertificateStatus.EXPIRED
    elif days_until_expiry <= WARNING_THRESHOLDS["warning"]:
        return CertificateStatus.EXPIRING
    else:
        return CertificateStatus.VALID


def get_warning_severity(days_until_expiry: int) -> str:
    """Get warning severity based on days until expiry."""
    if days_until_expiry <= 0:
        return "critical"
    elif days_until_expiry <= WARNING_THRESHOLDS["critical"]:
        return "critical"
    elif days_until_expiry <= WARNING_THRESHOLDS["high"]:
        return "high"
    elif days_until_expiry <= WARNING_THRESHOLDS["warning"]:
        return "warning"
    return "info"


@router.get(
    "",
    response_model=CertificateListResponse,
    summary="List all certificate statuses",
    description="Get certificate status for all registered clusters including expiry information."
)
async def list_certificates(
    cert_manager: LXDCertificateManager = Depends(get_cert_manager)
):
    """List all certificates with their status."""
    try:
        settings = get_settings()
        cluster_ids = cert_manager.list_clusters(use_cache=False)
        
        certificates = []
        expiring_count = 0
        expired_count = 0
        
        for cluster_id in cluster_ids:
            try:
                # Get certificate data from Vault
                cert_path = f"{settings.vault_clusters_path}/{cluster_id}/certs"
                cert_data = cert_manager.vault.vault_read(cert_path)
                
                # Skip clusters with no valid certificate (ex; Pending Trust)
                cert_pem = cert_data.get("client.crt", "")
                if not cert_pem:
                    logger.debug(f"Skipping {cluster_id}: no certificate data (pending trust)")
                    continue
                
                # Get metadata
                metadata_path = f"{settings.vault_clusters_path}/{cluster_id}/metadata"
                metadata = cert_manager.vault.vault_read(metadata_path)
                
                # Check for pending renewal
                pending_path = f"{settings.vault_clusters_path}/{cluster_id}/pending_certs"
                has_pending = False
                try:
                    pending_data = cert_manager.vault.vault_read(pending_path)
                    has_pending = bool(pending_data.get("client.crt"))
                except:
                    pass
                
                # Parse certificate
                cert_pem = cert_data.get("client.crt", "")
                cert_info_parsed = parse_certificate_pem(cert_pem) if cert_pem else {}
                
                # Calculate days until expiry
                expires_at = cert_info_parsed.get("not_after")
                days_until_expiry = None
                if expires_at:
                    now = datetime.now(timezone.utc)
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                    days_until_expiry = (expires_at - now).days
                
                cert_status = get_certificate_status(expires_at, has_pending)
                
                if cert_status == CertificateStatus.EXPIRED:
                    expired_count += 1
                elif cert_status == CertificateStatus.EXPIRING:
                    expiring_count += 1
                
                certificates.append(CertificateInfo(
                    cluster_id=cluster_id,
                    common_name=cert_data.get("common_name") or cert_info_parsed.get("subject"),
                    endpoint=metadata.get("endpoint") if metadata else None,
                    issuer=cert_info_parsed.get("issuer"),
                    issued_at=cert_info_parsed.get("not_before"),
                    expires_at=expires_at,
                    days_until_expiry=days_until_expiry,
                    status=cert_status,
                    fingerprint=cert_info_parsed.get("fingerprint"),
                    has_pending_renewal=has_pending,
                    serial_number=cert_info_parsed.get("serial_number"),
                ))
                
            except VaultClientError as e:
                logger.warning(f"Failed to get certificate for {cluster_id}: {e}")
                certificates.append(CertificateInfo(
                    cluster_id=cluster_id,
                    status=CertificateStatus.UNKNOWN,
                ))
            except Exception as e:
                logger.error(f"Error processing certificate for {cluster_id}: {e}")
                certificates.append(CertificateInfo(
                    cluster_id=cluster_id,
                    status=CertificateStatus.UNKNOWN,
                ))
        
        return CertificateListResponse(
            certificates=certificates,
            total=len(certificates),
            expiring_count=expiring_count,
            expired_count=expired_count,
        )
        
    except Exception as e:
        logger.error(f"Failed to list certificates: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list certificates: {str(e)}"
        )


@router.get(
    "/{cluster_id}",
    response_model=CertificateInfo,
    summary="Get certificate status for a cluster",
    description="Get detailed certificate information for a specific cluster."
)
async def get_certificate(
    cluster_id: str,
    cert_manager: LXDCertificateManager = Depends(get_cert_manager)
):
    """Get certificate info for a specific cluster."""
    verify_cluster_access(cluster_id)
    try:
        settings = get_settings()
        
        # Get certificate data
        cert_path = f"{settings.vault_clusters_path}/{cluster_id}/certs"
        try:
            cert_data = cert_manager.vault.vault_read(cert_path)
        except VaultClientError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No certificate found for cluster {cluster_id}"
            )
        
        # Check for pending renewal
        pending_path = f"{settings.vault_clusters_path}/{cluster_id}/pending_certs"
        has_pending = False
        try:
            pending_data = cert_manager.vault.vault_read(pending_path)
            has_pending = bool(pending_data.get("client.crt"))
        except:
            pass
        
        # Parse certificate
        cert_pem = cert_data.get("client.crt", "")
        cert_info_parsed = parse_certificate_pem(cert_pem) if cert_pem else {}
        
        expires_at = cert_info_parsed.get("not_after")
        days_until_expiry = None
        if expires_at:
            now = datetime.now(timezone.utc)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            days_until_expiry = (expires_at - now).days
        
        return CertificateInfo(
            cluster_id=cluster_id,
            common_name=cert_data.get("common_name") or cert_info_parsed.get("subject"),
            issuer=cert_info_parsed.get("issuer"),
            issued_at=cert_info_parsed.get("not_before"),
            expires_at=expires_at,
            days_until_expiry=days_until_expiry,
            status=get_certificate_status(expires_at, has_pending),
            fingerprint=cert_info_parsed.get("fingerprint"),
            has_pending_renewal=has_pending,
            serial_number=cert_info_parsed.get("serial_number"),
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get certificate for {cluster_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get certificate: {str(e)}"
        )


@router.post(
    "/renew",
    response_model=CertificateRenewalResponse,
    summary="Initiate certificate renewal",
    description="""
    Initiate certificate renewal for a cluster.
    
    **Flow:**
    1. If current certificate is valid, use it to auto-add new certificate to LXD trust
    2. If current certificate is expired, requires trust_password or manual CLI trust
    3. New certificate is stored in pending_certs until verified
    """
)
async def initiate_renewal(
    request: CertificateRenewalRequest,
    background_tasks: BackgroundTasks,
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    _: None = Depends(check_read_only_mode)
):
    """Initiate certificate renewal."""
    verify_cluster_access(request.cluster_id)
    try:
        settings = get_settings()
        cluster_id = request.cluster_id
        
        # Get current certificate status
        cert_path = f"{settings.vault_clusters_path}/{cluster_id}/certs"
        try:
            current_cert_data = cert_manager.vault.vault_read(cert_path)
        except VaultClientError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No certificate found for cluster {cluster_id}"
            )
        
        # Parse current cert to check if expired
        current_cert_pem = current_cert_data.get("client.crt", "")
        current_cert_info = parse_certificate_pem(current_cert_pem) if current_cert_pem else {}
        current_expires = current_cert_info.get("not_after")
        
        is_current_valid = False
        if current_expires:
            now = datetime.now(timezone.utc)
            if current_expires.tzinfo is None:
                current_expires = current_expires.replace(tzinfo=timezone.utc)
            is_current_valid = current_expires > now
        
        # Get cluster metadata
        metadata_path = f"{settings.vault_clusters_path}/{cluster_id}/metadata"
        metadata = cert_manager.vault.vault_read(metadata_path)
        _endpoint = metadata.get("endpoint", "")
        
        # Generate new certificate
        logger.info(f"Generating new certificate for {cluster_id} with TTL {request.ttl.value}")
        domain = settings.orcastra_domain
        new_cert_data = cert_manager.vault.generate_lxd_certificate(
            common_name=f"{cluster_id}.{domain}",
            ttl=request.ttl.value
        )
        
        # Generate unique identity name for LXD trust
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        identity_name = f"orca-{cluster_id}-{timestamp}"
        
        auto_renewed = False
        requires_manual = False
        certificate_for_manual = None
        cli_command = None
        
        if is_current_valid:
            # Current cert is valid - try to auto-add new cert to trust
            try:
                logger.info(f"Attempting auto-renewal for {cluster_id} using existing cert")
                success = cert_manager.add_trust_with_existing_cert(
                    cluster_id=cluster_id,
                    new_cert_pem=new_cert_data["cert"],
                    cert_name=identity_name
                )
                if success:
                    auto_renewed = True
                    logger.info(f"Auto-renewal successful for {cluster_id}")
            except Exception as e:
                logger.warning(f"Auto-renewal failed for {cluster_id}: {e}")

            # important: a certificate can be valid and still untrusted, which is the state every
            # cluster lands in after its trust entry is replaced or dropped on the LXD side. The
            # branch above authenticates with the very certificate the cluster is rejecting, so it
            # can never succeed there, and the trust password used to be unreachable because it
            # was only consulted when the certificate had EXPIRED. That left the dashboard
            # structurally unable to repair the one fault it is most likely to meet.
            if not auto_renewed and request.trust_password:
                try:
                    logger.info(f"Existing-cert renewal did not take for {cluster_id}; falling back to the trust password")
                    success = cert_manager.add_trust_with_password(
                        cluster_id=cluster_id,
                        new_cert_pem=new_cert_data["cert"],
                        cert_name=identity_name,
                        trust_password=request.trust_password
                    )
                    if success:
                        auto_renewed = True
                        logger.info(f"Renewal with password successful for {cluster_id}")
                except Exception as e:
                    logger.warning(f"Renewal with password failed for {cluster_id}: {e}")

            if not auto_renewed:
                requires_manual = True
        else:
            # Current cert is expired
            if request.trust_password:
                # Try using trust password
                try:
                    logger.info(f"Attempting renewal with trust password for {cluster_id}")
                    success = cert_manager.add_trust_with_password(
                        cluster_id=cluster_id,
                        new_cert_pem=new_cert_data["cert"],
                        cert_name=identity_name,
                        trust_password=request.trust_password
                    )
                    if success:
                        auto_renewed = True
                        logger.info(f"Renewal with password successful for {cluster_id}")
                except Exception as e:
                    logger.warning(f"Renewal with password failed for {cluster_id}: {e}")
                    requires_manual = True
            else:
                requires_manual = True
        
        if requires_manual:
            # Provide CLI command for manual trust - dynamic based on cluster type
            certificate_for_manual = new_cert_data["cert"]
            # An undetermined type is LXD-family, whose CLI is lxc; only Incus differs.
            cluster_type = metadata.get("type") or "LXD"
            cli_command = build_trust_add_command(cluster_type, new_cert_data["cert"], identity_name)
        
        # Store pending certificate in Vault
        pending_path = f"{settings.vault_clusters_path}/{cluster_id}/pending_certs"
        pending_cert_id = f"pending-{timestamp}"
        cert_manager.vault.vault_kv_put(pending_path, {
            "client.crt": new_cert_data["cert"],
            "client.key": new_cert_data["key"],
            "ca.pem": new_cert_data["ca"],
            "common_name": f"{cluster_id}.{domain}",
            "pending_cert_id": pending_cert_id,
            "identity_name": identity_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "auto_renewed": auto_renewed,
        })
        
        if auto_renewed:
            # Auto-renewed successfully - activate immediately
            # Store new cert as active
            cert_manager.vault.vault_kv_put(cert_path, {
                "client.crt": new_cert_data["cert"],
                "client.key": new_cert_data["key"],
                "ca.pem": new_cert_data["ca"],
                "common_name": f"{cluster_id}.{domain}",
            })
            
            # Clean up pending
            try:
                cert_manager.vault.vault_delete(pending_path)
            except:
                pass
            
            # Invalidate caches
            from app.core.cache import get_cache_manager
            cache = get_cache_manager()
            if cache:
                cache.invalidate_cluster_cache(cluster_id)
            
            return CertificateRenewalResponse(
                success=True,
                message="Certificate renewed successfully. New certificate is now active.",
                cluster_id=cluster_id,
                auto_renewed=True,
                requires_manual_trust=False,
                pending_cert_id=pending_cert_id,
            )
        
        return CertificateRenewalResponse(
            success=True,
            message="New certificate generated. Please add trust manually and verify.",
            cluster_id=cluster_id,
            auto_renewed=False,
            requires_manual_trust=True,
            certificate=certificate_for_manual,
            cli_command=cli_command,
            pending_cert_id=pending_cert_id,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to initiate renewal for {request.cluster_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to initiate renewal: {str(e)}"
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
    _: None = Depends(check_read_only_mode)
):
    """Verify pending certificate and activate if trusted."""
    verify_cluster_access(request.cluster_id)
    try:
        settings = get_settings()
        cluster_id = request.cluster_id
        
        # Get pending certificate
        pending_path = f"{settings.vault_clusters_path}/{cluster_id}/pending_certs"
        try:
            pending_data = cert_manager.vault.vault_read(pending_path)
        except VaultClientError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No pending certificate found for cluster {cluster_id}"
            )
        
        if not pending_data.get("client.crt"):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No pending certificate to verify"
            )
        
        # Get cluster metadata
        metadata_path = f"{settings.vault_clusters_path}/{cluster_id}/metadata"
        metadata = cert_manager.vault.vault_read(metadata_path)
        endpoint = metadata.get("endpoint", "")
        
        # Test connection with pending certificate
        verified = cert_manager.verify_connection_with_cert(
            endpoint=endpoint,
            cert_pem=pending_data["client.crt"],
            key_pem=pending_data["client.key"]
        )
        
        if verified:
            # Replace active certificate with pending
            cert_path = f"{settings.vault_clusters_path}/{cluster_id}/certs"
            cert_manager.vault.vault_kv_put(cert_path, {
                "client.crt": pending_data["client.crt"],
                "client.key": pending_data["client.key"],
                "ca.pem": pending_data.get("ca.pem", ""),
                "common_name": pending_data.get("common_name", ""),
            })
            
            # Delete pending certificate
            cert_manager.vault.vault_delete(pending_path)
            
            # Invalidate caches and reconnect
            from app.core.cache import get_cache_manager
            cache = get_cache_manager()
            if cache:
                cache.invalidate_cluster_cache(cluster_id)
            
            # Update connection manager
            try:
                from app.services.lxd_service import get_lxd_service
                lxd_service = get_lxd_service()
                lxd_service.connection_manager.remove_connection(cluster_id)
                lxd_service.connection_manager.connect_with_vault_cert(
                    cluster_id=cluster_id,
                    endpoint=endpoint,
                    cert_content=pending_data["client.crt"],
                    key_content=pending_data["client.key"],
                    verify=False,
                    force=True,
                )
            except Exception as e:
                logger.warning(f"Failed to update connection manager: {e}")
            
            return CertificateVerifyResponse(
                success=True,
                message="Certificate verified and activated successfully.",
                cluster_id=cluster_id,
                verified=True,
                activated=True,
            )
        
        return CertificateVerifyResponse(
            success=False,
            message="Certificate not yet trusted by hypervisor server. Please add trust using the CLI command.",
            cluster_id=cluster_id,
            verified=False,
            activated=False,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to verify renewal for {request.cluster_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to verify renewal: {str(e)}"
        )


@router.delete(
    "/{cluster_id}/pending",
    summary="Cancel pending certificate renewal",
    description="Delete pending certificate and cancel renewal process."
)
async def cancel_renewal(
    cluster_id: str,
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    _: None = Depends(check_read_only_mode)
):
    """Cancel pending certificate renewal."""
    verify_cluster_access(cluster_id)
    try:
        settings = get_settings()
        pending_path = f"{settings.vault_clusters_path}/{cluster_id}/pending_certs"
        
        try:
            cert_manager.vault.vault_delete(pending_path)
            return {"success": True, "message": "Pending renewal cancelled"}
        except VaultClientError:
            return {"success": True, "message": "No pending renewal to cancel"}
        
    except Exception as e:
        logger.error(f"Failed to cancel renewal for {cluster_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to cancel renewal: {str(e)}"
        )


@router.get(
    "/warnings/all",
    response_model=SystemWarningsResponse,
    summary="Get all system warnings",
    description="Get certificate and node connectivity warnings for all clusters."
)
async def get_system_warnings(
    cert_manager: LXDCertificateManager = Depends(get_cert_manager)
):
    """Get all certificate and node warnings."""
    try:
        settings = get_settings()
        cluster_ids = cert_manager.list_clusters(use_cache=False)
        
        certificate_warnings = []
        node_warnings = []
        critical_count = 0
        warning_count = 0
        info_count = 0
        
        for cluster_id in cluster_ids:
            try:
                # Get metadata
                metadata_path = f"{settings.vault_clusters_path}/{cluster_id}/metadata"
                metadata = cert_manager.vault.vault_read(metadata_path)
                display_name = metadata.get("display_name", cluster_id)
                endpoint = metadata.get("endpoint", "")
                
                # Get certificate and check expiry
                cert_path = f"{settings.vault_clusters_path}/{cluster_id}/certs"
                try:
                    cert_data = cert_manager.vault.vault_read(cert_path)
                    cert_pem = cert_data.get("client.crt", "")
                    cert_info = parse_certificate_pem(cert_pem) if cert_pem else {}
                    
                    expires_at = cert_info.get("not_after")
                    if expires_at:
                        now = datetime.now(timezone.utc)
                        if expires_at.tzinfo is None:
                            expires_at = expires_at.replace(tzinfo=timezone.utc)
                        days_until_expiry = (expires_at - now).days
                        
                        if days_until_expiry <= WARNING_THRESHOLDS["warning"]:
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
                    # No certificate - add warning
                    node_warnings.append(CertificateWarning(
                        cluster_id=cluster_id,
                        display_name=display_name,
                        warning_type="no_certificate",
                        severity="critical",
                        message="No certificate configured",
                        endpoint=endpoint,
                    ))
                    critical_count += 1
                
                # Check node connectivity
                connected, error_msg = cert_manager.verify_connection(cluster_id)
                if not connected:
                    node_warnings.append(CertificateWarning(
                        cluster_id=cluster_id,
                        display_name=display_name,
                        warning_type="node_offline",
                        severity="critical",
                        message=error_msg or "Node is not reachable",
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
            detail=f"Failed to get warnings: {str(e)}"
        )


@router.post(
    "/cleanup-pending",
    summary="Cleanup stale pending certificates",
    description="Remove pending certificates older than 24 hours."
)
async def cleanup_pending_certificates(
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    _: None = Depends(check_read_only_mode)
):
    """Cleanup stale pending certificates."""
    try:
        settings = get_settings()
        cluster_ids = cert_manager.list_clusters(use_cache=False)
        cleaned = 0
        
        for cluster_id in cluster_ids:
            try:
                pending_path = f"{settings.vault_clusters_path}/{cluster_id}/pending_certs"
                pending_data = cert_manager.vault.vault_read(pending_path)
                
                created_at_str = pending_data.get("created_at")
                if created_at_str:
                    created_at = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
                    now = datetime.now(timezone.utc)
                    hours_old = (now - created_at).total_seconds() / 3600
                    
                    if hours_old > 24:
                        cert_manager.vault.vault_delete(pending_path)
                        cleaned += 1
                        logger.info(f"Cleaned up stale pending cert for {cluster_id}")
                        
            except VaultClientError:
                pass  # No pending cert
            except Exception as e:
                logger.warning(f"Failed to check pending cert for {cluster_id}: {e}")
        
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
