"""
API Router for LXD Warnings.
Provides endpoints for viewing LXD warnings.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional, List, Dict, Any
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
)
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.core.org_scope import org_scope_dependency, verify_cluster_access, check_tenant_permission, apply_cluster_filter
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/warnings",
    tags=["Warnings"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# Thread pool for parallel operations
_executor = ThreadPoolExecutor(max_workers=5)


class WarningInfo(BaseModel):
    """Warning information."""
    uuid: str = Field(..., description="Warning UUID")
    location: str = Field("", description="Warning location (cluster member)")
    source_node: str = Field("", description="Source node that reported this warning")
    project: str = Field("", description="Project name")
    type: str = Field("", description="Warning type")
    count: int = Field(0, description="Number of occurrences")
    first_seen_at: str = Field("", description="First occurrence timestamp")
    last_seen_at: str = Field("", description="Last occurrence timestamp")
    last_message: str = Field("", description="Last warning message")
    severity: str = Field("", description="Warning severity")
    status: str = Field("", description="Warning status")


class WarningListResponse(BaseModel):
    """Response for listing warnings."""
    total: int = Field(..., description="Total number of warnings")
    warnings: List[WarningInfo] = Field(..., description="List of warnings")
    nodes: List[str] = Field(..., description="List of nodes")


def _fetch_warnings_for_node(lxd_service: LXDService, node_name: str) -> List[WarningInfo]:
    """Fetch warnings for a single node - runs in thread pool."""
    warnings = []
    try:
        client = lxd_service.connection_manager.get_client(node_name)
        # Use recursion=1 to get full details in one call
        response = client.api.warnings.get(params={'recursion': '1'}, timeout=(5, 30))
        if response.status_code == 200:
            warnings_data = response.json().get('metadata', [])
            for warning_detail in warnings_data:
                try:
                    # LXD returns "none" for standalone nodes - replace with actual node name
                    raw_location = warning_detail.get('location', '')
                    location = raw_location if raw_location and raw_location.lower() != "none" else node_name
                    
                    # LXD returns "default" for global warnings - keep it as is (it's the default project)
                    project = warning_detail.get('project', '') or 'default'
                    
                    warnings.append(WarningInfo(
                        uuid=warning_detail.get('uuid', ''),
                        location=location,
                        source_node=node_name,
                        project=project,
                        type=warning_detail.get('type', ''),
                        count=warning_detail.get('count', 0),
                        first_seen_at=warning_detail.get('first_seen_at', ''),
                        last_seen_at=warning_detail.get('last_seen_at', ''),
                        last_message=warning_detail.get('last_message', ''),
                        severity=warning_detail.get('severity', ''),
                        status=warning_detail.get('status', '')
                    ))
                except Exception as e:
                    logger.error(f"Error parsing warning data on {node_name}: {e}")
        elif response.status_code == 404:
            logger.debug(f"Warnings endpoint not available on {node_name} (404 - older LXD version?)")
        else:
            logger.warning(f"Got status {response.status_code} for warnings on {node_name}")
    except Exception as e:
        logger.error(f"Error listing warnings on {node_name}: {e}", exc_info=True)
    return warnings


@router.get(
    "",
    response_model=WarningListResponse,
    summary="List all warnings",
    description="Get a list of all warnings from LXD nodes.",
)
async def list_warnings(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all warnings."""
    try:
        if host:
            verify_cluster_access(host)
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        
        # Fetch warnings from all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_executor, _fetch_warnings_for_node, lxd_service, node_name)
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Flatten results
        all_warnings = []
        for result in results:
            if isinstance(result, list):
                all_warnings.extend(result)
            elif isinstance(result, Exception):
                logger.error(f"Error fetching warnings: {result}")

        all_warnings = [
            warning for warning in all_warnings
            if check_tenant_permission(warning.source_node, warning.project or "default", None, "view")
        ]
        
        return WarningListResponse(
            total=len(all_warnings),
            warnings=all_warnings,
            nodes=lxd_service.connection_manager.get_node_names()
        )
    except Exception as e:
        logger.error(f"Error listing warnings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{warning_uuid}",
    response_model=WarningInfo,
    summary="Get warning details",
    description="Get detailed information about a specific warning.",
)
async def get_warning(
    warning_uuid: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific warning."""
    try:
        verify_cluster_access(host)
        client = lxd_service.connection_manager.get_client(host)
        warning_response = client.api.warnings[warning_uuid].get(timeout=(5, 30))
        
        if warning_response.status_code == 200:
            warning_data = warning_response.json().get('metadata', {})
            # LXD returns "none" for standalone nodes - replace with actual node name
            raw_location = warning_data.get('location', '')
            location = raw_location if raw_location and raw_location.lower() != "none" else host
            project = warning_data.get('project', '') or 'default'
            if not check_tenant_permission(host, project, None, "view"):
                raise HTTPException(status_code=403, detail="Access denied to this warning")
            
            return WarningInfo(
                uuid=warning_data.get('uuid', ''),
                location=location,
                source_node=host,
                project=project,
                type=warning_data.get('type', ''),
                count=warning_data.get('count', 0),
                first_seen_at=warning_data.get('first_seen_at', ''),
                last_seen_at=warning_data.get('last_seen_at', ''),
                last_message=warning_data.get('last_message', ''),
                severity=warning_data.get('severity', ''),
                status=warning_data.get('status', '')
            )
        else:
            raise HTTPException(status_code=404, detail=f"Warning {warning_uuid} not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting warning {warning_uuid}: {e}")
        raise HTTPException(status_code=404, detail=f"Warning {warning_uuid} not found")


@router.delete(
    "/{warning_uuid}",
    response_model=dict,
    summary="Delete warning",
    description="Delete/dismiss a warning.",
)
async def delete_warning(
    warning_uuid: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Delete a warning."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        response = client.api.warnings[warning_uuid].delete(timeout=(5, 30))
        
        if response.status_code in [200, 202]:
            return {
                "success": True,
                "message": f"Warning {warning_uuid} deleted successfully"
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to delete warning")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting warning {warning_uuid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/system/all",
    response_model=dict,
    summary="Get all system warnings including certificates",
    description="Get combined warnings from LXD nodes and certificate/connectivity issues.",
)
async def get_all_system_warnings(
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get all system warnings including certificate and node issues."""
    try:
        # Get LXD warnings
        nodes_to_query = lxd_service.connection_manager.get_node_names()
        
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_executor, _fetch_warnings_for_node, lxd_service, node_name)
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        lxd_warnings = []
        for result in results:
            if isinstance(result, list):
                lxd_warnings.extend(result)

        lxd_warnings = [
            warning for warning in lxd_warnings
            if check_tenant_permission(warning.source_node, warning.project or "default", None, "view")
        ]
        
        # Get certificate and node warnings from cert_manager
        cert_warnings = []
        node_warnings = []
        
        try:
            from app.core.config import get_settings
            from app.core.vault_client import get_vault_client
            from app.services.lxd_cert_manager import LXDCertificateManager
            from app.routers.cert_manager import (
                parse_certificate_pem,
                get_warning_severity,
                WARNING_THRESHOLDS
            )
            from datetime import datetime, timezone
            
            settings = get_settings()
            if settings.vault_enabled and settings.vault_token:
                vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
                cert_manager = LXDCertificateManager(
                    vault_client=vault_client,
                    clusters_path=settings.vault_clusters_path
                )
                
                cluster_ids = apply_cluster_filter(cert_manager.list_clusters(use_cache=False))
                
                for cluster_id in cluster_ids:
                    try:
                        # Get metadata
                        metadata = cert_manager.vault.vault_read(
                            f"{settings.vault_clusters_path}/{cluster_id}/metadata"
                        )
                        display_name = metadata.get("display_name", cluster_id)
                        endpoint = metadata.get("endpoint", "")
                        
                        # Check certificate expiry
                        try:
                            cert_data = cert_manager.vault.vault_read(
                                f"{settings.vault_clusters_path}/{cluster_id}/certs"
                            )
                            cert_pem = cert_data.get("client.crt", "")
                            if cert_pem:
                                cert_info = parse_certificate_pem(cert_pem)
                                expires_at = cert_info.get("not_after")
                                
                                if expires_at:
                                    now = datetime.now(timezone.utc)
                                    if expires_at.tzinfo is None:
                                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                                    days_until_expiry = (expires_at - now).days
                                    
                                    if days_until_expiry <= WARNING_THRESHOLDS["warning"]:
                                        severity = get_warning_severity(days_until_expiry)
                                        if days_until_expiry <= 0:
                                            message = f"Certificate expired {abs(days_until_expiry)} days ago"
                                            warning_type = "cert_expired"
                                        else:
                                            message = f"Certificate expires in {days_until_expiry} days"
                                            warning_type = "cert_expiring"
                                        
                                        cert_warnings.append({
                                            "cluster_id": cluster_id,
                                            "display_name": display_name,
                                            "warning_type": warning_type,
                                            "severity": severity,
                                            "message": message,
                                            "days_until_expiry": days_until_expiry,
                                            "expires_at": expires_at.isoformat(),
                                            "endpoint": endpoint,
                                        })
                        except Exception:
                            pass
                        
                        # Check node connectivity
                        connected, error_msg = cert_manager.verify_connection(cluster_id)
                        if not connected:
                            from datetime import datetime
                            node_warnings.append({
                                "cluster_id": cluster_id,
                                "display_name": display_name,
                                "warning_type": "node_offline",
                                "severity": "critical",
                                "message": error_msg or "Node is not reachable",
                                "endpoint": endpoint,
                                "checked_at": datetime.utcnow().isoformat(),
                            })
                            
                    except Exception as e:
                        logger.debug(f"Error checking {cluster_id}: {e}")
                        
        except Exception as e:
            logger.warning(f"Could not fetch certificate warnings: {e}")
        
        # Calculate totals
        critical = sum(1 for w in cert_warnings if w.get("severity") == "critical")
        critical += sum(1 for w in node_warnings if w.get("severity") == "critical")
        critical += sum(1 for w in lxd_warnings if w.severity.lower() in ["critical", "error", "high"])
        
        warning = sum(1 for w in cert_warnings if w.get("severity") in ["high", "warning"])
        warning += sum(1 for w in node_warnings if w.get("severity") in ["high", "warning"])
        warning += sum(1 for w in lxd_warnings if w.severity.lower() in ["warning", "medium"])
        
        info = sum(1 for w in cert_warnings if w.get("severity") == "info")
        info += sum(1 for w in node_warnings if w.get("severity") == "info")
        info += sum(1 for w in lxd_warnings if w.severity.lower() in ["info", "low"])
        
        return {
            "total": len(lxd_warnings) + len(cert_warnings) + len(node_warnings),
            "critical": critical,
            "warning": warning,
            "info": info,
            "lxd_warnings": [w.model_dump() for w in lxd_warnings],
            "certificate_warnings": cert_warnings,
            "node_warnings": node_warnings,
            "nodes": nodes_to_query,
        }
        
    except Exception as e:
        logger.error(f"Error getting system warnings: {e}")
        raise HTTPException(status_code=500, detail=str(e))
