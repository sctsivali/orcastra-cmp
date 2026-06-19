"""
Cluster Registration Router
Handles LXD cluster registration, certificate management, and trust establishment via Vault.
"""

import logging
import asyncio
from typing import Optional, List
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, HTTPException, Depends, status
from datetime import datetime

from app.models.schemas import (
    ClusterRegisterRequest,
    ClusterRegisterResponse,
    ClusterAuthCheckRequest,
    ClusterAuthCheckResponse,
    ClusterInfo,
    ClusterListResponse,
    ClusterDeleteResponse,
    ClusterStatus,
    ClusterLocationUpdateRequest,
    ClusterLocationUpdateResponse,
    GeoLocation,
)
from app.core.config import get_settings
from app.core.vault_client import get_vault_client, VaultClient, VaultClientError
from app.services.lxd_cert_manager import LXDCertificateManager, LXDCertManagerError
from app.core.dependencies import check_read_only_mode, require_admin, require_partner, require_tenant, AuthenticatedUser
from app.core.org_scope import org_scope_dependency, verify_cluster_access, get_scope_cache_key
from app.core.type_utils import detect_cluster_type_from_metadata, normalize_cluster_type, detect_hypervisor_type_from_endpoint
from app.services.activity_logger import get_activity_logger
from app.core.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/clusters",
    tags=["Cluster Registration"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)


def get_cert_manager() -> LXDCertificateManager:
    """
    Dependency to get LXD Certificate Manager instance.
    """
    settings = get_settings()
    
    if not settings.vault_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vault integration is not enabled. Set VAULT_ENABLED=true in environment."
        )
    
    if not settings.vault_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Vault token is not configured. Set VAULT_TOKEN in environment."
        )
    
    try:
        vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
        
        # Health check
        if not vault_client.health_check():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Vault server is not accessible or unhealthy"
            )
        
        cert_manager = LXDCertificateManager(
            vault_client=vault_client,
            clusters_path=settings.vault_clusters_path
        )
        
        return cert_manager
        
    except Exception as e:
        logger.error(f"Failed to initialize certificate manager: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to initialize certificate manager: {str(e)}"
        )


@router.post(
    "/register",
    response_model=ClusterRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register new LXD cluster",
    description="""
    Register a new LXD cluster using Vault for certificate management.
    
    **Workflow:**
    1. Save cluster metadata to Vault
    2. Save trust password (if provided) to Vault
    3. Generate LXD client certificate via Vault PKI (if enabled)
    4. Register certificate with LXD trust store
    5. Verify connection
    6. Optionally remove trust password after successful registration
    
    **Initial Status:** After registration, cluster status is "not_authorized" until 
    the certificate is trusted by LXD. Use the "Check Authentication" endpoint to verify.
    """
)
async def register_cluster(
    request: ClusterRegisterRequest,
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Register a new LXD cluster."""
    try:
        settings = get_settings()
        cluster_id = request.cluster_id
        
        # Step 1: Pre-auth type detection from /1.0 API endpoint
        # The /1.0 endpoint returns metadata (including api_extensions) even without auth
        # This allows us to detect Incus vs LXD before trust is established
        cluster_type, raw_metadata = await detect_hypervisor_type_from_endpoint(request.endpoint)
        logger.info(f"Pre-auth type detection for {cluster_id}: {cluster_type}")
        
        # Prepare location data for storage
        location_data = None
        if request.location:
            location_data = request.location.model_dump()
        
        # Step 2: Save metadata with auto-detected type and location
        logger.info(f"Saving metadata for cluster {cluster_id}")
        cert_manager.save_cluster_metadata(
            cluster_id=cluster_id,
            display_name=request.display_name,
            endpoint=request.endpoint,
            zone=request.zone or "",
            cluster_type=cluster_type,
            merchant_id=request.merchant_id or "",
            location=location_data
        )
        
        # Also save to node metadata store for quick access
        from app.core.node_metadata import get_node_metadata_store
        metadata_store = get_node_metadata_store()
        metadata_store.set_node_display_name(cluster_id, request.display_name)
        metadata_store.set_node_type(cluster_id, cluster_type)
        
        # Save empty secrets (for compatibility)
        cert_manager.save_cluster_secrets(cluster_id=cluster_id, trust_password=None)
        
        # Step 2b: Auto-bind cluster to the registering user's organization
        # so it appears in org-scoped queries (partner/tenant see their clusters)
        # NOTE: This must happen BEFORE cache invalidation so the org binding
        # exists when the cache is repopulated
        try:
            from sqlalchemy import select as sa_select, or_ as sa_or
            from app.models.db_models import Organization, OrganizationMember, OrganizationCluster

            # find orgs the user created or belongs to as partner
            org_result = await db.execute(
                sa_select(Organization.id).where(
                    Organization.is_active == True,
                    sa_or(
                        Organization.created_by == _rbac.email,
                        Organization.id.in_(
                            sa_select(OrganizationMember.organization_id)
                            .where(
                                OrganizationMember.user_email == _rbac.email,
                                OrganizationMember.role.in_(["partner", "admin"]),
                            )
                        ),
                    ),
                )
            )
            org_ids = list(org_result.scalars().all())

            if org_ids:
                # bind to the first (primary) org - avoid duplicates
                primary_org_id = org_ids[0]
                existing = await db.execute(
                    sa_select(OrganizationCluster.id).where(
                        OrganizationCluster.organization_id == primary_org_id,
                        OrganizationCluster.cluster_id == cluster_id,
                    )
                )
                if not existing.scalar_one_or_none():
                    db.add(OrganizationCluster(
                        organization_id=primary_org_id,
                        cluster_id=cluster_id,
                        registered_by=_rbac.email,
                    ))
                    await db.commit()
                    logger.info(
                        f"Auto-bound cluster {cluster_id} to org {primary_org_id} "
                        f"for user {_rbac.email}"
                    )
            else:
                logger.warning(
                    f"User {_rbac.email} has no organization - "
                    f"cluster {cluster_id} not bound to any org"
                )
        except Exception as e:
            logger.error(f"Failed to auto-bind cluster {cluster_id} to org: {e}")
            # don't fail the registration - cluster is in Vault, just not bound
        
        # Invalidate clusters list cache AFTER org binding so scoped users see the cluster
        from app.core.cache import get_cache_manager
        cache = get_cache_manager()
        if cache:
            cache.delete("api_responses", get_scope_cache_key("clusters:list:all"))
            cache.delete("api_responses", "clusters:list:all:scope=all")
            # CRITICAL: Also invalidate vault_list cache for cluster list
            vault_list_path = settings.vault_clusters_path.replace("/data/", "/metadata/")
            cache.delete("vault_list", vault_list_path)
            # Invalidate nodes status cache
            cache.delete("api_responses", get_scope_cache_key("nodes:status:all"))
            cache.delete("api_responses", "nodes:status:all:scope=all")
            logger.info(f"Invalidated clusters list cache (scoped + vault_list) after registering {cluster_id}")
        
        # Update connection_manager registered clusters whitelist
        from app.services.lxd_service import get_lxd_service
        get_lxd_service().connection_manager.register_cluster(cluster_id)
        
        # Step 3: Always auto-generate certificate
        certificate_pem = None
        try:
            logger.info(f"Generating certificate for cluster {cluster_id}")
            cert_data = cert_manager.generate_and_store_certificate(
                cluster_id=cluster_id,
                ttl=settings.vault_cert_ttl
            )
            logger.info(f"Certificate generated and stored for {cluster_id}")
            
            # Get certificate PEM for user to copy-paste
            certificate_pem = cert_data.get("cert")
            
            # Build CLI-friendly message based on detected type
            cli_tool = "incus" if cluster_type == "Incus" else "lxc"
            
            # Log activity: cluster registration success
            activity_logger.log_activity(
                action="cluster.register",
                target_type="cluster",
                target_id=cluster_id,
                target_host=request.endpoint,
                result="success",
                details={
                    "display_name": request.display_name,
                    "endpoint": request.endpoint,
                    "zone": request.zone,
                    "cluster_type": cluster_type,
                    "certificate_generated": True,
                }
            )
            
            return ClusterRegisterResponse(
                success=True,
                message=f"Certificate generated! Run the {cli_tool} command to add trust. Detected hypervisor: {cluster_type}.",
                cluster_id=cluster_id,
                status=ClusterStatus.NOT_AUTHORIZED,
                certificate_generated=True,
                trust_added=False,
                certificate=certificate_pem,
                cluster_type=cluster_type
            )
                
        except LXDCertManagerError as e:
            logger.error(f"Certificate generation failed for {cluster_id}: {e}")
            
            # Log activity: cluster registration failure
            activity_logger.log_activity(
                action="cluster.register",
                target_type="cluster",
                target_id=cluster_id,
                target_host=request.endpoint,
                result="failure",
                error_code="CERT_GENERATION_FAILED",
                error_message=str(e),
                details={
                    "display_name": request.display_name,
                    "endpoint": request.endpoint,
                }
            )
            
            return ClusterRegisterResponse(
                success=False,
                message=f"Failed to generate certificate: {str(e)}",
                cluster_id=cluster_id,
                status=ClusterStatus.ERROR,
                certificate_generated=False,
                trust_added=False,
                cluster_type=cluster_type
            )
        
    except LXDCertManagerError as e:
        logger.error(f"Cluster registration failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Unexpected error during cluster registration: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error: {str(e)}"
        )


@router.post(
    "/check-auth",
    response_model=ClusterAuthCheckResponse,
    summary="Check cluster authentication",
    description="""
    Verify that the cluster is accessible and the certificate is trusted.
    
    This endpoint attempts to connect to the LXD server using the certificate
    stored in Vault and retrieve server information.
    
    **Use this after:**
    - Initial cluster registration
    - Adding trust via LXD CLI
    - Certificate renewal
    """
)
async def check_cluster_authentication(
    request: ClusterAuthCheckRequest,
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Check cluster authentication status."""
    try:
        cluster_id = request.cluster_id
        
        # Verify connection
        connected, message = cert_manager.verify_connection(cluster_id, use_breaker=False)
        
        if connected:
            # Get server info
            try:
                client = cert_manager.connect_with_vault_cert(cluster_id)
                server_info = client.api.get().json()
                
                # Re-detect and update cluster type from authenticated live connection
                final_cluster_type = None
                try:
                    metadata_info = server_info.get("metadata", {})
                    environment = metadata_info.get("environment", {})
                    
                    # Use centralized detection logic (will use server_clustered=true for MicroCloud)
                    detected_type = detect_cluster_type_from_metadata(metadata_info)
                    final_cluster_type = detected_type
                    
                    logger.info(
                        f"Authenticated detection for {cluster_id}: {detected_type}\n"
                        f"  - server_clustered: {environment.get('server_clustered', False)}\n"
                        f"  - server: {environment.get('server', 'N/A')}\n"
                        f"  - server_name: {environment.get('server_name', 'N/A')}"
                    )
                    
                    # Update metadata in Vault and node_metadata store
                    settings = get_settings()
                    vault_metadata = cert_manager.vault.vault_read(
                        f"{settings.vault_clusters_path}/{cluster_id}/metadata"
                    )
                    current_type = vault_metadata.get("type", "LXD")
                    
                    if current_type != detected_type:
                        logger.info(f"Updating cluster type for {cluster_id}: {current_type} -> {detected_type}")
                        vault_metadata["type"] = detected_type
                        cert_manager.vault.vault_kv_put(
                            f"{settings.vault_clusters_path}/{cluster_id}/metadata",
                            vault_metadata
                        )
                        
                        # Also update node_metadata store
                        from app.core.node_metadata import get_node_metadata_store
                        metadata_store = get_node_metadata_store()
                        metadata_store.set_node_type(cluster_id, detected_type)
                    else:
                        logger.info(f"Cluster type for {cluster_id} unchanged: {detected_type}")
                except Exception as type_err:
                    logger.warning(f"Failed to re-detect cluster type for {cluster_id}: {type_err}")
                
                # IMPORTANT: Add the connection to LXD service connection manager
                # so it appears in active nodes list for projects/instances queries
                try:
                    from app.services.lxd_service import get_lxd_service
                    lxd_service = get_lxd_service()
                    
                    # Get certificate from Vault
                    settings = get_settings()
                    cert_data = cert_manager.vault.vault_read(
                        f"{settings.vault_clusters_path}/{cluster_id}/certs"
                    )
                    metadata = cert_manager.vault.vault_read(
                        f"{settings.vault_clusters_path}/{cluster_id}/metadata"
                    )
                    
                    # Add connection to LXD service
                    lxd_service.connection_manager.connect_with_vault_cert(
                        cluster_id=cluster_id,
                        endpoint=metadata["endpoint"],
                        cert_content=cert_data["client.crt"],
                        key_content=cert_data["client.key"],
                        verify=False
                    )
                    logger.info(f"Added authenticated cluster {cluster_id} to LXD service connection manager")
                except Exception as conn_err:
                    logger.warning(f"Failed to add connection to LXD service: {conn_err}")
                
                # Invalidate clusters list cache so status updates appear immediately
                # Also invalidate instances and projects caches so new cluster data loads
                from app.core.cache import get_cache_manager
                cache = get_cache_manager()
                if cache:
                    cache.delete("api_responses", get_scope_cache_key("clusters:list:all"))
                    cache.delete("api_responses", "clusters:list:all:scope=all")
                    # CRITICAL: Also invalidate vault_list cache for cluster list
                    vault_list_path = settings.vault_clusters_path.replace("/data/", "/metadata/")
                    cache.delete("vault_list", vault_list_path)
                    # Invalidate instances and projects cache patterns
                    cache.delete_pattern("api_responses", "instances:*")
                    cache.delete_pattern("api_responses", "projects:*")
                    cache.delete_pattern("api_responses", "vms:*")
                    cache.delete_pattern("api_responses", "nodes:*")
                    logger.info(f"Invalidated all relevant caches (scoped + vault_list) after authenticating {cluster_id}")
                
                return ClusterAuthCheckResponse(
                    cluster_id=cluster_id,
                    connected=True,
                    authenticated=True,
                    status=ClusterStatus.ONLINE,
                    message="Connection successful and authenticated",
                    server_info=server_info.get("metadata", {}),
                    cluster_type=final_cluster_type
                )
            except Exception as e:
                logger.error(f"Failed to get server info for {cluster_id}: {e}")
                return ClusterAuthCheckResponse(
                    cluster_id=cluster_id,
                    connected=True,
                    authenticated=False,
                    status=ClusterStatus.ERROR,
                    message=f"Connected but failed to retrieve server info: {str(e)}"
                )
        else:
            return ClusterAuthCheckResponse(
                cluster_id=cluster_id,
                connected=False,
                authenticated=False,
                status=ClusterStatus.NOT_AUTHORIZED,
                message=message
            )
            
    except LXDCertManagerError as e:
        logger.error(f"Authentication check failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Unexpected error during authentication check: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error: {str(e)}"
        )


async def _get_cluster_info_async(cluster_id: str, cert_manager: LXDCertificateManager) -> Optional[ClusterInfo]:
    """Helper function to get cluster info asynchronously for parallel processing.
    
    This function also ensures connected clusters are added to the connection manager
    so they can be used for subsequent API calls (instances, networks, etc.).
    
    Auto-reconnection priority:
    1. Try to use existing connection from connection manager
    2. If not connected, try to reconnect using stored connection info
    3. If still not connected, verify connection using cert_manager
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    
    try:
        loop = asyncio.get_event_loop()
        
        # First, check if we already have a valid connection in the pool
        from app.services.lxd_service import get_lxd_service
        from app.core.config import get_settings
        
        settings = get_settings()
        lxd_service = get_lxd_service()
        
        # Try to get client (this will auto-reconnect if connection info is stored)
        existing_client = lxd_service.connection_manager.get_client(cluster_id)
        
        connected = False
        error_msg = None
        
        if existing_client:
            # We have a valid connection
            connected = True
            logger.debug(f"Cluster {cluster_id} already connected in pool")
        else:
            # No valid connection, try to reconnect using Vault certs
            try:
                cert_data = cert_manager.get_cluster_certificate(cluster_id)
                info = cert_manager.get_cluster_info(cluster_id)
                
                lxd_service.connection_manager.connect_with_vault_cert(
                    cluster_id=cluster_id,
                    endpoint=info["metadata"]["endpoint"],
                    cert_content=cert_data["client.crt"],
                    key_content=cert_data["client.key"],
                    verify=False
                )
                connected = True
                logger.info(f"Reconnected cluster {cluster_id} to connection pool from Vault certs")
            except Exception as reconnect_err:
                logger.debug(f"Could not reconnect {cluster_id}: {reconnect_err}")
                # Fall back to verify_connection for status check
                connected, error_msg = cert_manager.verify_connection(cluster_id)
        
        # Get cluster info (may already be cached from above)
        with ThreadPoolExecutor(max_workers=1) as executor:
            info = await loop.run_in_executor(executor, cert_manager.get_cluster_info, cluster_id)
        
        current_status = ClusterStatus.ONLINE if connected else ClusterStatus.NOT_AUTHORIZED
        
        # Only show cluster type if connected/authenticated
        cluster_type = None
        if connected:
            cluster_type = info["metadata"].get("type")
            if cluster_type:
                cluster_type = normalize_cluster_type(cluster_type)
        
        return ClusterInfo(
            cluster_id=cluster_id,
            display_name=info["metadata"].get("display_name", cluster_id),
            endpoint=info["metadata"]["endpoint"],
            zone=info["metadata"].get("zone"),
            cluster_type=cluster_type,
            merchant_id=info["metadata"].get("merchant_id"),
            status=current_status,
            has_certificate=info["certificate"]["has_certificate"],
            certificate_common_name=info["certificate"].get("common_name"),
            last_checked=datetime.utcnow(),
            location=info["metadata"].get("location")
        )
        
    except Exception as e:
        logger.warning(f"Failed to get info for cluster {cluster_id}: {e}")
        return ClusterInfo(
            cluster_id=cluster_id,
            display_name=cluster_id,
            endpoint="unknown",
            cluster_type=None,
            status=ClusterStatus.ERROR,
            has_certificate=False,
            error_message=str(e)
        )


@router.get(
    "",
    response_model=ClusterListResponse,
    summary="List all registered clusters",
    description="Get a list of all clusters registered in Vault with their current status."
)
async def list_clusters(
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    fresh: bool = False
):
    """List all registered clusters. Set fresh=true to bypass cache."""
    import asyncio
    from app.core.cache import get_cache_manager
    
    cache = get_cache_manager()
    cache_key = get_scope_cache_key("clusters:list:all")
    
    # Try cache first (unless fresh=true)
    if not fresh and cache:
        cached_response = cache.get("api_responses", cache_key)
        if cached_response is not None:
            logger.debug("Returning cached clusters list")
            return ClusterListResponse(**cached_response)
    
    try:
        # When fresh=True, bypass Vault list cache to get latest data
        # This ensures deleted clusters don't appear in the list
        cluster_ids = cert_manager.list_clusters(use_cache=not fresh)
        
        # Fetch all cluster info in parallel for better performance
        tasks = [_get_cluster_info_async(cluster_id, cert_manager) for cluster_id in cluster_ids]
        clusters = await asyncio.gather(*tasks)
        
        # Filter out None values (shouldn't happen but just in case)
        clusters = [c for c in clusters if c is not None]
        
        response = ClusterListResponse(
            clusters=clusters,
            total=len(clusters)
        )
        
        # Cache the response for 120 seconds
        if cache:
            cache.set("api_responses", cache_key, response.model_dump(), ttl=180)
        
        return response
        
    except Exception as e:
        logger.error(f"Failed to list clusters: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list clusters: {str(e)}"
        )


@router.get(
    "/facilities",
    summary="Get facility suggestions",
    description="Get unique facility names and providers from registered clusters for autocomplete."
)
async def get_facility_suggestions(
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
):
    """Get unique facility suggestions from all registered clusters."""
    from app.core.cache import get_cache_manager
    
    cache = get_cache_manager()
    cache_key = get_scope_cache_key("clusters:facilities")
    
    if cache:
        cached = cache.get("api_responses", cache_key)
        if cached is not None:
            return cached
    
    facilities = []
    seen = set()
    
    try:
        cluster_ids = cert_manager.list_clusters(use_cache=True)
        
        for cluster_id in cluster_ids:
            try:
                info = cert_manager.get_cluster_info(cluster_id)
                location = info.get("metadata", {}).get("location", {})
                name = location.get("facility_name")
                provider = location.get("facility_provider")
                
                if name:
                    key = f"{name}|{provider or ''}"
                    if key not in seen:
                        seen.add(key)
                        facilities.append({
                            "facility_name": name,
                            "facility_provider": provider,
                        })
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"Error fetching facility suggestions: {e}")
    
    # Cache for 5 minutes
    if cache:
        cache.set("api_responses", cache_key, facilities, ttl=300)
    
    return facilities


@router.get(
    "/{cluster_id}",
    response_model=ClusterInfo,
    summary="Get cluster information",
    description="Get detailed information about a specific cluster."
)
async def get_cluster(
    cluster_id: str,
    cert_manager: LXDCertificateManager = Depends(get_cert_manager)
):
    """Get information about a specific cluster."""
    # verify caller has access to this cluster
    verify_cluster_access(cluster_id)
    try:
        info = cert_manager.get_cluster_info(cluster_id)
        
        # Check current connection status
        connected, error_msg = cert_manager.verify_connection(cluster_id)
        current_status = ClusterStatus.ONLINE if connected else ClusterStatus.NOT_AUTHORIZED
        
        return ClusterInfo(
            cluster_id=cluster_id,
            display_name=info["metadata"].get("display_name", cluster_id),
            endpoint=info["metadata"]["endpoint"],
            zone=info["metadata"].get("zone"),
            cluster_type=info["metadata"].get("type", "LXD"),
            merchant_id=info["metadata"].get("merchant_id"),
            status=current_status,
            has_certificate=info["certificate"]["has_certificate"],
            certificate_common_name=info["certificate"].get("common_name"),
            last_checked=datetime.utcnow(),
            error_message=error_msg if not connected else None,
            location=info["metadata"].get("location")
        )
        
    except LXDCertManagerError as e:
        logger.error(f"Failed to get cluster {cluster_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cluster not found: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Unexpected error getting cluster {cluster_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error: {str(e)}"
        )


@router.delete(
    "/{cluster_id}",
    response_model=ClusterDeleteResponse,
    summary="Delete cluster registration",
    description="""
    Delete a cluster registration from Vault.
    
    **Warning:** This will remove all cluster data including:
    - Metadata
    - Certificates
    - Secrets
    
    This does NOT remove the certificate from the LXD server's trust store.
    """
)
async def delete_cluster(
    cluster_id: str,
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Delete a cluster registration."""
    verify_cluster_access(cluster_id)
    try:
        # Delete all cluster data from Vault
        vault = cert_manager.vault
        base_path = cert_manager.clusters_path
        loop = asyncio.get_event_loop()
        
        logger.info(f"Starting deletion of cluster {cluster_id}")
        
        # Helper to run blocking vault ops in thread pool
        def _vault_delete_safe(path: str, op_name: str) -> None:
            try:
                vault.vault_delete(path)
                logger.info(f"Deleted data for {op_name} of {cluster_id}")
            except Exception as e:
                logger.warning(f"Failed to delete data {op_name} for {cluster_id}: {e}")
        
        def _vault_delete_metadata_safe(path: str, op_name: str) -> None:
            try:
                vault.vault_delete_metadata(path)
                logger.info(f"Deleted metadata for {op_name} of {cluster_id}")
            except Exception as e:
                logger.warning(f"Failed to delete metadata {op_name} for {cluster_id}: {e}")
        
        # Step 1: Delete data paths in parallel (soft delete in KV v2)
        subpaths = ["metadata", "secrets", "certs", "pending_certs"]
        with ThreadPoolExecutor(max_workers=4) as executor:
            await asyncio.gather(*[
                loop.run_in_executor(executor, _vault_delete_safe, f"{base_path}/{cluster_id}/{sp}", sp)
                for sp in subpaths
            ])
        
        # Step 2+3: Delete metadata for each subpath + parent folder in parallel
        # KV v2 requires deleting BOTH data and metadata paths
        metadata_ops = [
            (f"secret/metadata/clusters/{cluster_id}/{sp}", sp) for sp in subpaths
        ]
        # Include parent metadata folder deletion (CRITICAL - removes from LIST)
        metadata_ops.append((f"secret/metadata/clusters/{cluster_id}", "parent_folder"))
        
        with ThreadPoolExecutor(max_workers=5) as executor:
            await asyncio.gather(*[
                loop.run_in_executor(executor, _vault_delete_metadata_safe, path, name)
                for path, name in metadata_ops
            ])
        
        # Step 4: Delete OrganizationCluster DB binding so cluster disappears from org scope
        try:
            from app.models.db_models import OrganizationCluster
            from sqlalchemy import delete as sql_delete
            result = await db.execute(
                sql_delete(OrganizationCluster).where(OrganizationCluster.cluster_id == cluster_id)
            )
            await db.commit()
            deleted_rows = result.rowcount
            if deleted_rows > 0:
                logger.info(f"Deleted {deleted_rows} OrganizationCluster binding(s) for {cluster_id}")
            else:
                logger.info(f"No OrganizationCluster binding found for {cluster_id}")
        except Exception as e:
            logger.warning(f"Failed to delete OrganizationCluster binding for {cluster_id}: {e}")
        
        # Step 5: Clear ALL cache for this cluster - critical for immediate UI update
        from app.core.cache import get_cache_manager
        try:
            cache = get_cache_manager()
            if cache:
                # CRITICAL: Invalidate clusters list cache (scoped + admin)
                cache.delete("api_responses", get_scope_cache_key("clusters:list:all"))
                cache.delete("api_responses", "clusters:list:all:scope=all")
                
                # Use dedicated cache invalidation method for cluster-specific data
                cache.invalidate_cluster_cache(cluster_id)
                
                # Also invalidate the Vault list cache to remove from list immediately
                list_path = cert_manager.clusters_path.replace("/data/", "/metadata/")
                cache.delete("vault_list", list_path)
                
                # Clear nodes status cache to ensure UI updates immediately
                cache.delete("api_responses", get_scope_cache_key("nodes:status:all"))
                cache.delete("api_responses", "nodes:status:all:scope=all")
                cache.delete("api_responses", get_scope_cache_key("nodes:status"))
                cache.delete("api_responses", "nodes:status:scope=all")
                
                # Clear instances, VMs, and projects cache
                cache.delete("api_responses", get_scope_cache_key("instances:list:host=None:status=None:type=None"))
                cache.delete("api_responses", "instances:list:host=None:status=None:type=None:scope=all")
                cache.delete("api_responses", get_scope_cache_key("vms:list:host=None:status=None"))
                cache.delete("api_responses", "vms:list:host=None:status=None:scope=all")
                cache.delete("api_responses", get_scope_cache_key("projects:list:all"))
                cache.delete("api_responses", "projects:list:all:scope=all")
                
                logger.info(f"Cleared all cache entries for cluster {cluster_id} including clusters/projects list cache")
        except Exception as e:
            logger.warning(f"Failed to clear cache for {cluster_id}: {e}")
        
        # Step 6: Remove from LXD connection manager (complete removal, not just cleanup)
        try:
            from app.services.lxd_service import get_lxd_service
            lxd_service = get_lxd_service()
            lxd_service.connection_manager.remove_connection(cluster_id)
            logger.info(f"Completely removed {cluster_id} from LXD connection manager")
        except Exception as e:
            logger.warning(f"Failed to remove connection for {cluster_id}: {e}")
        
        # Step 7: Delete from node metadata store
        from app.core.node_metadata import get_node_metadata_store
        try:
            metadata_store = get_node_metadata_store()
            metadata_store.delete_node_metadata(cluster_id)
            logger.info(f"Deleted node metadata for {cluster_id}")
        except Exception as e:
            logger.warning(f"Failed to delete node metadata for {cluster_id}: {e}")
        
        logger.info(f"Successfully completed deletion of cluster {cluster_id}")
        
        # Log activity: cluster deletion success
        activity_logger.log_activity(
            action="cluster.delete",
            target_type="cluster",
            target_id=cluster_id,
            result="success",
            details={"deletion_type": "full", "subpaths_deleted": ["metadata", "secrets", "certs", "pending_certs"]}
        )
        
        return ClusterDeleteResponse(
            success=True,
            message=f"Cluster {cluster_id} deleted successfully",
            cluster_id=cluster_id
        )
        
    except Exception as e:
        logger.error(f"Failed to delete cluster {cluster_id}: {e}")
        
        # Log activity: cluster deletion failure
        activity_logger.log_activity(
            action="cluster.delete",
            target_type="cluster",
            target_id=cluster_id,
            result="failure",
            error_code="DELETE_FAILED",
            error_message=str(e)
        )
        
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete cluster: {str(e)}"
        )


@router.post(
    "/{cluster_id}/regenerate-cert",
    response_model=ClusterRegisterResponse,
    summary="Regenerate cluster certificate",
    description="""
    Regenerate the certificate for an existing cluster.
    
    This is useful when:
    - Certificate is expiring
    - Certificate was compromised
    - Need to rotate certificates
    
    **Note:** You will need to re-add trust to the LXD server after regeneration.
    """
)
async def regenerate_certificate(
    cluster_id: str,
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Regenerate certificate for a cluster."""
    try:
        settings = get_settings()
        
        # Get cluster info to retrieve endpoint and detect type
        info = cert_manager.get_cluster_info(cluster_id)
        if not info:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Cluster {cluster_id} not found"
            )
        
        endpoint = info.get("metadata", {}).get("endpoint", "")
        stored_type = info.get("metadata", {}).get("type")
        
        # Re-detect type from endpoint for accuracy (in case it changed or wasn't set)
        cluster_type = stored_type
        if endpoint:
            detected_type, _ = await detect_hypervisor_type_from_endpoint(endpoint)
            cluster_type = detected_type
            logger.info(f"Regenerate cert: detected type for {cluster_id}: {detected_type}")
        
        # Generate new certificate (let default FQDN logic apply: cluster_id.domain)
        cert_data = cert_manager.generate_and_store_certificate(
            cluster_id=cluster_id,
            ttl=settings.vault_cert_ttl
        )
        
        # Get the certificate to return in response
        certificate_pem = cert_data.get("cert", "")
        
        # Build CLI-friendly message based on detected type
        cli_tool = "incus" if cluster_type == "Incus" else "lxc"
        
        return ClusterRegisterResponse(
            success=True,
            message=f"Certificate regenerated! Run the {cli_tool} command to re-add trust. Detected: {cluster_type}.",
            cluster_id=cluster_id,
            status=ClusterStatus.NOT_AUTHORIZED,
            certificate_generated=True,
            trust_added=False,
            certificate=certificate_pem,
            cluster_type=cluster_type
        )
        
    except LXDCertManagerError as e:
        logger.error(f"Failed to regenerate certificate for {cluster_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Unexpected error regenerating certificate: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error: {str(e)}"
        )


@router.patch(
    "/{cluster_id}/location",
    response_model=ClusterLocationUpdateResponse,
    summary="Update cluster location",
    description="""
    Update the geographic location of an existing cluster.
    
    This is useful for:
    - Setting location on clusters registered before location feature was available
    - Correcting location data
    - Moving clusters to new data centers
    
    The location data is used in the Regions feature to display nodes on the map.
    """
)
async def update_cluster_location(
    cluster_id: str,
    request: ClusterLocationUpdateRequest,
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Update the location of an existing cluster."""
    verify_cluster_access(cluster_id)
    logger.info(f"Updating location for cluster {cluster_id}")
    
    try:
        # Get existing cluster info (may not exist for legacy clusters)
        try:
            cluster_info = cert_manager.get_cluster_info(cluster_id)
            existing_metadata = cluster_info.get("metadata", {}) if cluster_info else {}
        except LXDCertManagerError:
            # Metadata doesn't exist yet - initialize empty metadata
            logger.warning(f"No existing metadata for cluster {cluster_id}, creating fresh")
            existing_metadata = {"endpoint": "", "display_name": cluster_id, "type": "LXD"}
        
        # Update location in metadata
        location_data = request.location.model_dump()
        existing_metadata["location"] = location_data
        
        # IMPORTANT: Sync zone with location.city for backwards compatibility
        # The zone field is displayed in the clusters table
        new_zone = request.location.city
        
        # Save updated metadata with synced zone
        cert_manager.save_cluster_metadata(
            cluster_id=cluster_id,
            endpoint=existing_metadata.get("endpoint", ""),
            display_name=existing_metadata.get("display_name", cluster_id),
            zone=new_zone,  # Update zone to match location.city
            cluster_type=existing_metadata.get("type", "LXD"),
            merchant_id=existing_metadata.get("merchant_id"),
            location=location_data
        )
        
        # Invalidate all relevant caches
        from app.core.cache import get_cache_manager
        cache = get_cache_manager()
        if cache:
            # CRITICAL: Invalidate clusters list cache (scoped + admin)
            cache.delete("api_responses", get_scope_cache_key("clusters:list:all"))
            cache.delete("api_responses", "clusters:list:all:scope=all")
            cache.delete("api_responses", get_scope_cache_key("regions:overview"))
            cache.delete("api_responses", "regions:overview:scope=all")
            # Also invalidate individual cluster cache if exists
            cache.invalidate_cluster_cache(cluster_id)
        
        # Log activity
        activity_logger.log_activity(
            action="cluster.location.update",
            target_type="cluster",
            target_id=cluster_id,
            details={
                "location": location_data,
                "region_name": request.location.region_name,
                "city": request.location.city,
                "zone_updated_to": new_zone,
            }
        )
        
        logger.info(f"Updated location for cluster {cluster_id}: {request.location.region_name}, {request.location.city} (zone set to: {new_zone})")
        
        return ClusterLocationUpdateResponse(
            success=True,
            message=f"Location updated successfully for cluster '{cluster_id}'",
            cluster_id=cluster_id,
            location=request.location
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update location for {cluster_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update location: {str(e)}"
        )
