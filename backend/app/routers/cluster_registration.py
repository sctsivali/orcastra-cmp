"""
Cluster Registration Router
Handles LXD cluster registration, certificate management, and trust establishment via Vault.
"""

import logging
from typing import Optional, List
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
)
from app.core.config import get_settings
from app.core.vault_client import get_vault_client, VaultClient, VaultClientError
from app.services.lxd_cert_manager import LXDCertificateManager, LXDCertManagerError
from app.core.dependencies import check_read_only_mode
from app.core.type_utils import detect_cluster_type_from_metadata, normalize_cluster_type

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/clusters", tags=["Cluster Registration"])


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
    _: None = Depends(check_read_only_mode)
):
    """Register a new LXD cluster."""
    try:
        settings = get_settings()
        cluster_id = request.cluster_id
        
        # Step 1: No pre-detection for unauthenticated clusters
        # Type will be properly detected after authentication via check-auth endpoint.
        # This prevents showing incorrect "LXD" or "MicroCloud" labels before trust is added.
        cluster_type = None  # Will be detected after authentication
        logger.info(f"Cluster {request.cluster_id} registered. Type will be detected after authentication/trust is added.")
        
        # Step 2: Save metadata with auto-detected type
        logger.info(f"Saving metadata for cluster {cluster_id}")
        cert_manager.save_cluster_metadata(
            cluster_id=cluster_id,
            display_name=request.display_name,
            endpoint=request.endpoint,
            zone=request.zone or "",
            cluster_type=cluster_type,
            merchant_id=request.merchant_id or ""
        )
        
        # Also save to node metadata store for quick access
        from app.core.node_metadata import get_node_metadata_store
        metadata_store = get_node_metadata_store()
        metadata_store.set_node_display_name(cluster_id, request.display_name)
        metadata_store.set_node_type(cluster_id, cluster_type)
        
        # Save empty secrets (for compatibility)
        cert_manager.save_cluster_secrets(cluster_id=cluster_id, trust_password=None)
        
        # Invalidate clusters list cache so new cluster appears immediately
        from app.core.cache import get_cache_manager
        cache = get_cache_manager()
        if cache:
            cache.delete("api_responses", "clusters:list:all")
            # CRITICAL: Also invalidate vault_list cache for cluster list
            vault_list_path = settings.vault_clusters_path.replace("/data/", "/metadata/")
            cache.delete("vault_list", vault_list_path)
            logger.info(f"Invalidated clusters list cache (api + vault_list) after registering {cluster_id}")
        
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
            
            return ClusterRegisterResponse(
                success=True,
                message="Certificate generated! Copy and paste it into your LXD Web UI to complete trust setup. Cluster type will be detected after authentication.",
                cluster_id=cluster_id,
                status=ClusterStatus.NOT_AUTHORIZED,
                certificate_generated=True,
                trust_added=False,
                certificate=certificate_pem
            )
                
        except LXDCertManagerError as e:
            logger.error(f"Certificate generation failed for {cluster_id}: {e}")
            return ClusterRegisterResponse(
                success=False,
                message=f"Failed to generate certificate: {str(e)}",
                cluster_id=cluster_id,
                status=ClusterStatus.ERROR,
                certificate_generated=False,
                trust_added=False
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
    cert_manager: LXDCertificateManager = Depends(get_cert_manager)
):
    """Check cluster authentication status."""
    try:
        cluster_id = request.cluster_id
        
        # Verify connection
        connected, message = cert_manager.verify_connection(cluster_id)
        
        if connected:
            # Get server info
            try:
                client = cert_manager.connect_with_vault_cert(cluster_id)
                server_info = client.api.get().json()
                
                # Re-detect and update cluster type from authenticated live connection
                try:
                    metadata_info = server_info.get("metadata", {})
                    environment = metadata_info.get("environment", {})
                    
                    # Use centralized detection logic (will use server_clustered=true for MicroCloud)
                    detected_type = detect_cluster_type_from_metadata(metadata_info)
                    
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
                    cache.delete("api_responses", "clusters:list:all")
                    # CRITICAL: Also invalidate vault_list cache for cluster list
                    vault_list_path = settings.vault_clusters_path.replace("/data/", "/metadata/")
                    cache.delete("vault_list", vault_list_path)
                    # Invalidate instances and projects cache patterns
                    cache.delete_pattern("api_responses", "instances:*")
                    cache.delete_pattern("api_responses", "projects:*")
                    cache.delete_pattern("api_responses", "vms:*")
                    cache.delete_pattern("api_responses", "nodes:*")
                    logger.info(f"Invalidated all relevant caches (api + vault_list) after authenticating {cluster_id}")
                
                return ClusterAuthCheckResponse(
                    cluster_id=cluster_id,
                    connected=True,
                    authenticated=True,
                    status=ClusterStatus.ONLINE,
                    message="Connection successful and authenticated",
                    server_info=server_info.get("metadata", {})
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
            last_checked=datetime.utcnow()
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
    cache_key = "clusters:list:all"
    
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
        
        # Cache the response for 120 seconds (enterprise TTL)
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
            error_message=error_msg if not connected else None
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
    _: None = Depends(check_read_only_mode)
):
    """Delete a cluster registration."""
    try:
        # Delete all cluster data from Vault
        vault = cert_manager.vault
        base_path = cert_manager.clusters_path
        
        logger.info(f"Starting deletion of cluster {cluster_id}")
        
        # Step 1: Delete data paths (soft delete in KV v2)
        for subpath in ["metadata", "secrets", "certs"]:
            try:
                vault.vault_delete(f"{base_path}/{cluster_id}/{subpath}")
                logger.info(f"Deleted data for {subpath} of {cluster_id}")
            except Exception as e:
                logger.warning(f"Failed to delete data {subpath} for {cluster_id}: {e}")
        
        # Step 2: Delete metadata for each subpath to remove from KV v2
        # KV v2 requires deleting BOTH data and metadata paths
        for subpath in ["metadata", "secrets", "certs"]:
            try:
                metadata_subpath = f"secret/metadata/clusters/{cluster_id}/{subpath}"
                vault.vault_delete_metadata(metadata_subpath)
                logger.info(f"Deleted metadata for {subpath} of {cluster_id}")
            except Exception as e:
                logger.warning(f"Failed to delete metadata {subpath} for {cluster_id}: {e}")
        
        # Step 3: Delete parent metadata folder to PERMANENTLY remove from LIST
        # This is CRITICAL - removes cluster_id from LIST results
        try:
            parent_metadata_path = f"secret/metadata/clusters/{cluster_id}"
            vault.vault_delete_metadata(parent_metadata_path)
            logger.info(f"Permanently removed {cluster_id} folder from Vault LIST")
        except Exception as e:
            logger.error(f"Failed to delete parent metadata path for {cluster_id}: {e}")
        
        # Step 4: Clear ALL cache for this cluster - critical for immediate UI update
        from app.core.cache import get_cache_manager
        try:
            cache = get_cache_manager()
            if cache:
                # CRITICAL: Invalidate clusters list cache first for immediate UI update
                cache.delete("api_responses", "clusters:list:all")
                
                # Use dedicated cache invalidation method for cluster-specific data
                cache.invalidate_cluster_cache(cluster_id)
                
                # Also invalidate the Vault list cache to remove from list immediately
                list_path = cert_manager.clusters_path.replace("/data/", "/metadata/")
                cache.delete("vault_list", list_path)
                
                # Clear nodes status cache to ensure UI updates immediately
                cache.delete("api_responses", "nodes:status:all")
                cache.delete("api_responses", "nodes:status")
                
                # Clear instances and VMs cache
                cache.delete("api_responses", "instances:list:host=None:status=None:type=None")
                cache.delete("api_responses", "vms:list:host=None:status=None")
                
                logger.info(f"Cleared all cache entries for cluster {cluster_id} including clusters list cache")
        except Exception as e:
            logger.warning(f"Failed to clear cache for {cluster_id}: {e}")
        
        # Step 5: Remove from LXD connection manager (complete removal, not just cleanup)
        try:
            from app.services.lxd_service import get_lxd_service
            lxd_service = get_lxd_service()
            lxd_service.connection_manager.remove_connection(cluster_id)
            logger.info(f"Completely removed {cluster_id} from LXD connection manager")
        except Exception as e:
            logger.warning(f"Failed to remove connection for {cluster_id}: {e}")
        
        # Step 6: Delete from node metadata store
        from app.core.node_metadata import get_node_metadata_store
        try:
            metadata_store = get_node_metadata_store()
            metadata_store.delete_node_metadata(cluster_id)
            logger.info(f"Deleted node metadata for {cluster_id}")
        except Exception as e:
            logger.warning(f"Failed to delete node metadata for {cluster_id}: {e}")
        
        logger.info(f"Successfully completed deletion of cluster {cluster_id}")
        
        return ClusterDeleteResponse(
            success=True,
            message=f"Cluster {cluster_id} deleted successfully",
            cluster_id=cluster_id
        )
        
    except Exception as e:
        logger.error(f"Failed to delete cluster {cluster_id}: {e}")
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
    _: None = Depends(check_read_only_mode)
):
    """Regenerate certificate for a cluster."""
    try:
        settings = get_settings()
        
        # Generate new certificate
        cert_data = cert_manager.generate_and_store_certificate(
            cluster_id=cluster_id,
            common_name=f"orcastra-{cluster_id}",
            ttl=settings.vault_cert_ttl
        )
        
        # Get the certificate to return in response
        # The generate_and_store_certificate returns dict with keys: cert, key, ca
        certificate_pem = cert_data.get("cert", "")
        
        return ClusterRegisterResponse(
            success=True,
            message="Certificate regenerated successfully. Please re-add trust to LXD server.",
            cluster_id=cluster_id,
            status=ClusterStatus.NOT_AUTHORIZED,
            certificate_generated=True,
            trust_added=False,
            certificate=certificate_pem
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
