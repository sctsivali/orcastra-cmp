"""
Cluster Registration Router
Handles LXD cluster registration, certificate management, and trust establishment via Vault.
"""

import logging
import asyncio
from typing import Optional
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
    derive_status,
    ClusterLocationUpdateRequest,
    ClusterLocationUpdateResponse,
    GeoLocation,
)
from app.core.config import get_settings
from app.core.vault_client import get_vault_client
from app.services.lxd_cert_manager import LXDCertificateManager, LXDCertManagerError
from app.services.lxd.exceptions import LXDLocalStorageError
from app.services.lxd.exceptions import LXDAuthorizationError
from app.core.dependencies import check_read_only_mode, require_admin, require_partner, require_tenant, AuthenticatedUser
from app.core.cache import (
    NODES_AVAILABILITY_CACHE_KEY,
    NODES_STATUS_CACHE_KEY,
    get_resource_list_generation,
)
from app.core.org_scope import org_scope_dependency, verify_cluster_access, get_scope_cache_key, apply_cluster_filter
from app.core.host_detail import cluster_for_tier, cluster_list_for_tier
from app.core.task_scope import caller_tier
from app.core.url_safety import normalize_endpoint
from app.core.type_utils import (
    detect_cluster_type_from_metadata,
    detect_clustered_from_metadata,
    detect_hypervisor_type_from_endpoint,
    resolve_cluster_type,
    resolve_clustered,
)
from app.services.activity_logger import get_activity_logger
from app.core.cert_fingerprint import lxd_fingerprint
from app.services.lxd_certs import rotation as cert_rotation
from app.services.lxd_certs import trust_cleanup
from app.services.lxd_certs import trust_cleanup_store as trust_store
from app.services.region_catalog import get_region_catalog
from app.core.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.shared_cluster_audit import (
    SharedClusterGuardError,
    assert_cluster_safe_to_share,
    serialize_cluster_share,
)
from app.services.migration_fence import (
    MigrationFenceConflict,
    assert_cluster_not_migration_fenced,
)

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
        
    except HTTPException:
        # important: let a deliberate status through. This try raises HTTPException
        # itself, and the broad handler below rewrote it, so an unreachable cluster
        # answered 500 or 404 instead of 503 and a tenant ACL denial answered 500
        # instead of 403.
        raise
    except Exception as e:
        logger.error(f"Failed to initialize certificate manager: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to initialize certificate manager: {str(e)}"
        )


async def _guard_registration_binding(
    db: AsyncSession,
    cluster_id: str,
    user: AuthenticatedUser,
) -> list[int]:
    """Audit a prospective additional owner before any Vault mutation.

    The caller holds the cluster configuration lock until the later binding commit. Keeping the
    write after Vault metadata means a failed remote preparation cannot silently add an
    organization binding. A later database failure is reported as a partial registration.
    """

    from sqlalchemy import select as sa_select

    from app.core.org_scope import get_user_org_ids
    from app.core.rbac import Role, get_highest_role, resolve_roles
    from app.models.db_models import OrganizationCluster

    org_ids = await get_user_org_ids(user, db)
    prospective_org = org_ids[0] if org_ids else None

    rows = (
        await db.execute(
            sa_select(
                OrganizationCluster.organization_id,
                OrganizationCluster.registered_by,
            ).where(OrganizationCluster.cluster_id == cluster_id)
        )
    ).all()
    if not rows:
        return org_ids
    owns_existing_identity = any(
        (row[0] is not None and row[0] in org_ids)
        or (row[0] is None and row[1] == user.email)
        or row[1] == user.email
        for row in rows
    )
    if (
        get_highest_role(resolve_roles(user)) != Role.ADMIN
        and not owns_existing_identity
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This cluster is already owned by another organization.",
        )
    if prospective_org is not None and any(row[0] == prospective_org for row in rows):
        return org_ids
    if prospective_org is None and any(row[0] is None for row in rows):
        # The registration flow deliberately refuses a second unbound claimant below. It
        # will not create an owner identity, so there is no share transition to audit.
        return org_ids

    await assert_cluster_safe_to_share(
        db,
        cluster_id,
        prospective_organization_id=prospective_org,
        prospective_registered_by=user.email,
    )
    return org_ids


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
    share_serialization = None
    try:
        settings = get_settings()
        cluster_id = request.cluster_id

        try:
            pending_serialization = serialize_cluster_share(db, cluster_id)
            await pending_serialization.__aenter__()
            share_serialization = pending_serialization
            org_ids = await _guard_registration_binding(
                db,
                cluster_id,
                _rbac,
            )
        except SharedClusterGuardError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.as_detail(),
            ) from exc
        
        # Step 1: Pre-auth type detection from /1.0 API endpoint
        # The /1.0 endpoint returns api_extensions even without auth, which is enough to tell
        # Incus from the LXD family. It withholds `environment`, so LXD vs MicroCloud stays
        # undetermined (None) until the first trusted read, and nothing is stored for it.
        cluster_type, raw_metadata = await detect_hypervisor_type_from_endpoint(request.endpoint)
        logger.info(
            f"Pre-auth type detection for {cluster_id}: "
            f"{cluster_type or 'undetermined, deferring to first trusted read'}"
        )
        
        # Prepare location data for storage
        location_data = None
        if request.location:
            location_data = get_region_catalog().canonical_location(request.location.model_dump(exclude_none=True))
        
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
        
        # Step 2b: Auto-bind cluster to the registering user's organization so it
        # appears in org-scoped queries (partner/tenant see their own clusters).
        # important: use the SAME membership predicate as cluster visibility
        # (get_user_org_ids) so binding can never drift from the scope query - a
        # role-filtered binding vs a role-agnostic scope was what orphaned clusters
        # registered by tenant-role members of OrcaHub-synced orgs.
        # NOTE: must run BEFORE cache invalidation so the binding exists when caches repopulate.
        org_bind_warning: Optional[str] = None
        try:
            from sqlalchemy import select as sa_select
            from app.models.db_models import OrganizationCluster

            if org_ids:
                # bind to the primary org - avoid duplicates
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
                    logger.info(
                        f"Auto-bound cluster {cluster_id} to org {primary_org_id} "
                        f"for user {_rbac.email}"
                    )
            else:
                # partner with no synced org yet: still record ownership (org_id NULL)
                # so the registrant can see and grant API-key access to their own cluster.
                # reconcile_personal_clusters binds it to their org once it is synced.
                # important: match the constraint, not more than it. uq_org_cluster_null is a
                # partial unique index on cluster_id alone where organization_id IS NULL, so
                # filtering on registered_by too made this miss a row another user already
                # holds. The INSERT then violated the index, the broad handler below swallowed
                # it, and registration returned success while recording no ownership at all.
                existing = await db.execute(
                    sa_select(OrganizationCluster.registered_by).where(
                        OrganizationCluster.organization_id.is_(None),
                        OrganizationCluster.cluster_id == cluster_id,
                    )
                )
                holder = existing.scalar_one_or_none()
                if holder is not None and holder != _rbac.email:
                    logger.warning(
                        f"Cluster {cluster_id} already has unbound ownership recorded for "
                        f"{holder}; not recording a second claim for {_rbac.email}"
                    )
                elif holder is None:
                    db.add(OrganizationCluster(
                        organization_id=None,
                        cluster_id=cluster_id,
                        registered_by=_rbac.email,
                    ))
                    logger.info(
                        f"Recorded unbound ownership of cluster {cluster_id} "
                        f"for user {_rbac.email} (no organization yet)"
                    )
                org_bind_warning = (
                    "Cluster registered and visible to you. Your account is not linked "
                    "to an organization yet - complete the OrcaHub integration to assign "
                    "it to your organization."
                )
            # Commit even when the binding already existed. PostgreSQL transaction advisory
            # locks are released only at transaction end, and configuration writes must not
            # remain blocked during certificate generation after ownership is settled.
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.error("Failed to persist cluster ownership for %s", cluster_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Cluster metadata was saved, but its ownership binding could not be "
                    "persisted. Retry registration before using this cluster."
                ),
            ) from exc

        if share_serialization is not None:
            closing_serialization = share_serialization
            share_serialization = None
            await closing_serialization.__aexit__(None, None, None)
        
        # Invalidate clusters list cache AFTER org binding so scoped users see the cluster
        from app.core.cache import get_cache_manager
        cache = get_cache_manager()
        if cache:
            cache.invalidate_resource_cache("clusters", "regions")
            cache.invalidate_capacity_cache()
            # CRITICAL: Also invalidate vault_list cache for cluster list
            vault_list_path = settings.vault_clusters_path.replace("/data/", "/metadata/")
            cache.delete("vault_list", vault_list_path)
            # Invalidate nodes status cache
            cache.delete("api_responses", get_scope_cache_key(NODES_STATUS_CACHE_KEY))
            cache.delete("api_responses", f"{NODES_STATUS_CACHE_KEY}:scope=all")
            # trailing * so every org-scope suffix goes with it
            cache.delete_pattern("api_responses", f"{NODES_AVAILABILITY_CACHE_KEY}*")
            # Invalidate regions overview (all scopes) so the inventory/monitoring
            # overview reflects the new cluster without a manual refresh
            cache.delete_pattern("api_responses", "regions:*")
            logger.info(f"Invalidated clusters list cache (scoped + vault_list + regions) after registering {cluster_id}")
        
        # Update connection_manager registered clusters whitelist
        from app.services.lxd_service import get_lxd_service
        get_lxd_service().connection_manager.register_cluster(cluster_id)
        
        # Step 3: Always auto-generate certificate
        certificate_pem = None
        try:
            logger.info(f"Generating certificate for cluster {cluster_id}")
            # The owning organization may re-register a cluster, which replaces a certificate the
            # cluster can still trust: recorded first and under the lock, as regenerate does.
            try:
                async with trust_store.cluster_cert_lock(db, cluster_id):
                    await cert_rotation.record_current_superseded(
                        db, cert_manager, cluster_id, reason="register"
                    )
                    cert_data = await asyncio.to_thread(
                        cert_manager.generate_and_store_certificate,
                        cluster_id=cluster_id,
                        ttl=settings.vault_cert_ttl,
                    )
                    await db.commit()
            except trust_store.CertificateOperationBusy:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Another certificate change on this cluster is in progress. Try again in a moment.",
                )
            logger.info(f"Certificate generated and stored for {cluster_id}")
            
            # Get certificate PEM for user to copy-paste
            certificate_pem = cert_data.get("cert")
            
            # Build CLI-friendly message based on detected type
            cli_tool = "incus" if cluster_type == "Incus" else "lxc"
            register_message = f"Certificate generated! Run the {cli_tool} command to add trust. Detected hypervisor: {cluster_type}."
            if org_bind_warning:
                register_message = f"{register_message} {org_bind_warning}"

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
                message=register_message,
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
        
    except HTTPException:
        raise
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
    finally:
        if share_serialization is not None:
            # A pre-binding failure must end the transaction to release PostgreSQL's advisory
            # lock. No ownership row has been committed on this path.
            closing_serialization = share_serialization
            share_serialization = None
            try:
                await db.rollback()
            finally:
                await closing_serialization.__aexit__(None, None, None)


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
    # important: the body names an arbitrary cluster, and the handler then connects to it with
    # the Vault-stored certificate and writes the result back to that cluster's metadata. Without
    # this a partner could probe and mutate clusters belonging to other organisations.
    verify_cluster_access(request.cluster_id)
    try:
        cluster_id = request.cluster_id
        
        # Verify connection
        # use_breaker=False: this is an operator-initiated check, so it must probe for real
        # rather than replay a 60-second-old failure.
        auth_probe = cert_manager.probe_connection(cluster_id, use_breaker=False)
        connected, message = auth_probe.trusted, auth_probe.message
        
        if connected:
            # Get server info
            try:
                client = cert_manager.connect_with_vault_cert(cluster_id, force=True)
                server_info = client.api.get().json()
                
                # Re-detect and update cluster type from authenticated live connection
                final_cluster_type = None
                final_clustered = None
                try:
                    metadata_info = server_info.get("metadata", {})
                    environment = metadata_info.get("environment", {})

                    # This is the first payload that carries `environment`, so it is the first
                    # honest reading of both the hypervisor family and the clustering state.
                    detected_type = detect_cluster_type_from_metadata(metadata_info)
                    final_cluster_type = detected_type
                    final_clustered = detect_clustered_from_metadata(metadata_info)

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
                    current_type = vault_metadata.get("type")
                    current_clustered = vault_metadata.get("clustered")
                    current_endpoint = vault_metadata.get("endpoint", "")
                    clean_endpoint = normalize_endpoint(current_endpoint)

                    # important: this is the ONLY place either fact is written, and it runs only
                    # after the host has answered a trusted request, so nothing here can be a
                    # pre-auth guess. That distinction is what makes storing them safe: the
                    # defect that left a MicroCloud labelled LXD was a value written at
                    # REGISTRATION from an untrusted probe, which cannot see `environment`, and
                    # which nothing ever re-derived. Both resolvers prefer a live client, so a
                    # stored reading can only ever answer when there is nobody to ask.
                    #
                    # `clustered` is stored as well as `type` because the members picker has to
                    # tell a host pending trust apart from a clustered host that is merely
                    # offline, and both answer None with no fallback.
                    #
                    # The comparison includes `clustered` on purpose: gating the write on the
                    # type alone would mean a host whose type is already correct never got its
                    # clustering state written at all.
                    if (
                        current_type != detected_type
                        or current_clustered != final_clustered
                        or current_endpoint != clean_endpoint
                    ):
                        logger.info(
                            f"Updating cluster metadata for {cluster_id}: "
                            f"type {current_type} -> {detected_type}, "
                            f"clustered {current_clustered} -> {final_clustered}"
                        )
                        vault_metadata["type"] = detected_type
                        vault_metadata["clustered"] = final_clustered
                        # Heal a stored endpoint that carries a trailing slash while we are
                        # already writing this record; the probe normalizes on read anyway.
                        vault_metadata["endpoint"] = clean_endpoint
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
                        verify=False,
                        force=True,
                    )
                    logger.info(f"Added authenticated cluster {cluster_id} to LXD service connection manager")
                except Exception as conn_err:
                    logger.warning(f"Failed to add connection to LXD service: {conn_err}")
                
                # Invalidate clusters list cache so status updates appear immediately
                # Also invalidate instances and projects caches so new cluster data loads
                from app.core.cache import get_cache_manager
                cache = get_cache_manager()
                if cache:
                    cache.invalidate_resource_cache("clusters", "projects", "regions")
                    # CRITICAL: Also invalidate vault_list cache for cluster list
                    vault_list_path = settings.vault_clusters_path.replace("/data/", "/metadata/")
                    cache.delete("vault_list", vault_list_path)
                    # Invalidate instances and projects cache patterns
                    cache.invalidate_instance_cache()
                    cache.delete_pattern("api_responses", "nodes:*")
                    # Regions overview too so the inventory/monitoring overview updates immediately
                    cache.delete_pattern("api_responses", "regions:*")
                    logger.info(f"Invalidated all relevant caches (scoped + vault_list + regions) after authenticating {cluster_id}")
                
                return ClusterAuthCheckResponse(
                    cluster_id=cluster_id,
                    connected=True,
                    authenticated=True,
                    status=ClusterStatus.ONLINE,
                    message="Connection successful and authenticated",
                    server_info=server_info.get("metadata", {}),
                    cluster_type=final_cluster_type,
                    clustered=final_clustered,
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
            # A cluster that never answered is offline, not a trust problem. Reporting
            # NOT_AUTHORIZED here sent operators to rotate a certificate on a dead host.
            unreachable_status, _reason_code, _detail = derive_status(
                connected=False, reachable=auth_probe.reachable, error=message
            )
            return ClusterAuthCheckResponse(
                cluster_id=cluster_id,
                connected=bool(auth_probe.reachable),
                authenticated=False,
                status=unreachable_status,
                message=message
            )
            
    except LXDLocalStorageError:
        # important: above LXDCertManagerError and the generic handler. probe_connection
        # raises this when the backend's own disk could not hold the TLS material, so there
        # is no honest cluster status to return: derive_status would default it to OFFLINE,
        # which is how a full tmpfs came to label healthy hypervisors down. Re-raised so the
        # app-level handler answers 503 BACKEND_TEMP_STORAGE_FULL and names the real fault.
        raise
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
        
        _settings = get_settings()
        lxd_service = get_lxd_service()
        
        # Try to get client (this will auto-reconnect if connection info is stored)
        existing_client = lxd_service.connection_manager.get_client(cluster_id)
        
        connected = False
        # important: reachable is tracked separately from connected because they answer
        # different questions. This used to be `ONLINE if connected else NOT_AUTHORIZED`,
        # which reported every unreachable cluster as "Pending Trust" and left an operator
        # chasing a certificate problem on a host that was simply powered off.
        reachable = None
        error_msg = None
        
        live_client = existing_client
        if existing_client:
            # We have a valid connection
            connected = True
            reachable = True
            logger.debug(f"Cluster {cluster_id} already connected in pool")
        else:
            # No valid connection, try to reconnect using Vault certs
            try:
                cert_data = cert_manager.get_cluster_certificate(cluster_id)
                info = cert_manager.get_cluster_info(cluster_id)
                
                # Keep the client this returns: reading its cached host_info below is how the
                # type is resolved, and a second get_client would pay for another revalidation.
                live_client = lxd_service.connection_manager.connect_with_vault_cert(
                    cluster_id=cluster_id,
                    endpoint=info["metadata"]["endpoint"],
                    cert_content=cert_data["client.crt"],
                    key_content=cert_data["client.key"],
                    verify=False,
                    force=True,
                )
                connected = True
                reachable = True
                logger.info(f"Reconnected cluster {cluster_id} to connection pool from Vault certs")
            except LXDAuthorizationError as auth_err:
                # The host answered and refused the certificate. That IS Pending Trust.
                reachable, error_msg = True, str(auth_err)
                logger.debug(f"{cluster_id} answered but refused our certificate: {auth_err}")
            except Exception as reconnect_err:
                logger.debug(f"Could not reconnect {cluster_id}: {reconnect_err}")
                # Fall back to a probe, which reports reachability and trust separately
                probe = cert_manager.probe_connection(cluster_id)
                connected, reachable, error_msg = probe.trusted, probe.reachable, probe.message
        
        # Get cluster info (may already be cached from above)
        with ThreadPoolExecutor(max_workers=1) as executor:
            info = await loop.run_in_executor(executor, cert_manager.get_cluster_info, cluster_id)
        
        current_status, _reason_code, derived_message = derive_status(
            connected=connected, reachable=reachable, error=error_msg
        )
        
        # Prefer what the host says now over what was stored. client.host_info is pylxd's
        # cached /1.0 body, so resolving from a live client costs no extra round trip.
        #
        # important: `clustered` is safe to read from storage unconditionally. This release is
        # the first thing that ever writes it, and the only writer is the check-auth path, after
        # the host has answered a trusted request, so a stored value is always evidence. That is
        # what lets the members picker tell a host pending trust apart from a clustered host
        # that is merely offline.
        #
        # `type` cannot be read from storage unconditionally, because a record written before
        # registration stopped guessing can still hold a "LXD" default nobody ever read off a
        # host. A stored `clustered` is the marker that separates the two: check-auth writes
        # both facts in the same statement, so its presence proves this record came from a
        # trusted read rather than from the old pre-auth probe.
        stored_clustered = info["metadata"].get("clustered")
        clustered = resolve_clustered(stored_clustered, live_client)
        cluster_type = None
        if connected or isinstance(stored_clustered, bool):
            cluster_type = resolve_cluster_type(info["metadata"].get("type"), live_client)

        return ClusterInfo(
            cluster_id=cluster_id,
            display_name=info["metadata"].get("display_name", cluster_id),
            endpoint=info["metadata"]["endpoint"],
            zone=info["metadata"].get("zone"),
            cluster_type=cluster_type,
            clustered=clustered,
            merchant_id=info["metadata"].get("merchant_id"),
            status=current_status,
            has_certificate=info["certificate"]["has_certificate"],
            certificate_common_name=info["certificate"].get("common_name"),
            last_checked=datetime.utcnow(),
            error_message=None if connected else (error_msg or derived_message or None),
            # important: `or None`. A cluster registered without a location stores {} for it,
            # and an empty dict is not a valid GeoLocation - all nine fields are required - so
            # building ClusterInfo from it raised nine validation errors. That surfaced as a
            # 500 on GET /clusters/{id} and, on the list, as a row degraded to
            # endpoint="unknown" by the handler's own fallback. The UI always sends a region,
            # so only API callers hit it, which is why it went unseen.
            location=info["metadata"].get("location") or None
        )
        
    except LXDLocalStorageError:
        # important: above LXDCertManagerError and the generic handler. probe_connection
        # raises this when the backend's own disk could not hold the TLS material, so there
        # is no honest cluster status to return: derive_status would default it to OFFLINE,
        # which is how a full tmpfs came to label healthy hypervisors down. Re-raised so the
        # app-level handler answers 503 BACKEND_TEMP_STORAGE_FULL and names the real fault.
        raise
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
            return cluster_list_for_tier(ClusterListResponse(**cached_response), caller_tier())
    list_generation = get_resource_list_generation(cache, "clusters") if cache else None
    
    try:
        # When fresh=True, bypass Vault list cache to get latest data
        # This ensures deleted clusters don't appear in the list
        # important: apply org-scope filter so partners/tenants only see their own
        # org's clusters (admin = unfiltered). Matches overview/nodes behaviour and
        # closes the cross-tenant visibility hole. Cache key is already scope-aware.
        cluster_ids = apply_cluster_filter(
            await asyncio.to_thread(cert_manager.list_clusters, use_cache=not fresh)
        )
        
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
        if cache and get_resource_list_generation(cache, "clusters") == list_generation:
            cache.set("api_responses", cache_key, response.model_dump(), ttl=180)
        elif cache:
            logger.info("Not caching clusters list: cluster metadata changed during the read")
        
        return cluster_list_for_tier(response, caller_tier())
        
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
    fresh: bool = False,
):
    """Get unique facility suggestions from all registered clusters."""
    from app.core.cache import get_cache_manager
    
    cache = get_cache_manager()
    cache_key = get_scope_cache_key("clusters:facilities")
    
    if cache:
        cached = None if fresh else cache.get("api_responses", cache_key)
        if cached is not None:
            return cached
    list_generation = get_resource_list_generation(cache, "clusters") if cache else None
    
    facilities = []
    seen = set()
    registry_answered = False
    
    try:
        cluster_ids = apply_cluster_filter(
            await asyncio.to_thread(cert_manager.list_clusters, use_cache=not fresh)
        )
        registry_answered = True
        
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
    
    # Cache for 5 minutes only when the scoped registry answered and no cluster mutation raced
    # this read. Otherwise an outage or pre-mutation result would become the next suggestion set.
    if (
        cache
        and registry_answered
        and get_resource_list_generation(cache, "clusters") == list_generation
    ):
        cache.set("api_responses", cache_key, facilities, ttl=300)
    elif cache and registry_answered:
        logger.info("Not caching facility suggestions: cluster metadata changed during the read")
    
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
        
        # Check current connection status. The probe reports reachability and trust
        # separately, so a powered-off cluster no longer reads as a certificate problem.
        probe = cert_manager.probe_connection(cluster_id)
        connected, error_msg = probe.trusted, probe.message
        current_status, _reason_code, _message = derive_status(
            connected=connected, reachable=probe.reachable, error=error_msg
        )

        # Same rule as the list endpoint, including why the two facts are gated differently.
        live_client = None
        stored_clustered = info["metadata"].get("clustered")
        cluster_type = None
        if connected:
            from app.services.lxd_service import get_lxd_service
            # Off the loop: get_client revalidates the pooled connection with a live request.
            # Passed by reference, not called here, for the same reason. A None result is fine -
            # it only means the resolvers fall back to storage, and this endpoint must still
            # answer for a cluster that is down. Skipped entirely when not connected, because
            # the revalidation is the expensive part and it cannot succeed.
            loop = asyncio.get_event_loop()
            with ThreadPoolExecutor(max_workers=1) as executor:
                live_client = await loop.run_in_executor(
                    executor, get_lxd_service().connection_manager.get_client, cluster_id
                )
        if connected or isinstance(stored_clustered, bool):
            cluster_type = resolve_cluster_type(info["metadata"].get("type"), live_client)
        clustered = resolve_clustered(stored_clustered, live_client)

        return cluster_for_tier(ClusterInfo(
            cluster_id=cluster_id,
            display_name=info["metadata"].get("display_name", cluster_id),
            endpoint=info["metadata"]["endpoint"],
            zone=info["metadata"].get("zone"),
            cluster_type=cluster_type,
            clustered=clustered,
            merchant_id=info["metadata"].get("merchant_id"),
            status=current_status,
            has_certificate=info["certificate"]["has_certificate"],
            certificate_common_name=info["certificate"].get("common_name"),
            last_checked=datetime.utcnow(),
            error_message=error_msg if not connected else None,
            # important: `or None`. A cluster registered without a location stores {} for it,
            # and an empty dict is not a valid GeoLocation - all nine fields are required - so
            # building ClusterInfo from it raised nine validation errors. That surfaced as a
            # 500 on GET /clusters/{id} and, on the list, as a row degraded to
            # endpoint="unknown" by the handler's own fallback. The UI always sends a region,
            # so only API callers hit it, which is why it went unseen.
            location=info["metadata"].get("location") or None
        ), caller_tier())
        
    except LXDLocalStorageError:
        # important: above LXDCertManagerError and the generic handler. probe_connection
        # raises this when the backend's own disk could not hold the TLS material, so there
        # is no honest cluster status to return: derive_status would default it to OFFLINE,
        # which is how a full tmpfs came to label healthy hypervisors down. Re-raised so the
        # app-level handler answers 503 BACKEND_TEMP_STORAGE_FULL and names the real fault.
        raise
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
    
    The dashboard first removes its own trust entries from the cluster (superseded ones, the
    pending certificate, then the current one). Whatever it cannot remove is returned in
    `manual_trust_commands`, and the deletion proceeds either way.
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
        await assert_cluster_not_migration_fenced(cluster_id=cluster_id, db=db)
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    # Step 0: revoke our own access while the key still exists. Before any Vault delete, because
    # removing a trust entry needs the current certificate. Best effort: it never blocks the delete.
    manual_trust_commands: list = []
    trust_cleanup_complete = False
    try:
        async with trust_store.cluster_cert_lock(db, cluster_id):
            revoked = await trust_cleanup.remove_trust_for_deregistration(db, cluster_id)
            manual_trust_commands, trust_cleanup_complete = revoked.commands, revoked.complete
            await trust_store.delete_for_cluster(db, cluster_id)
            await db.commit()
    except trust_store.CertificateOperationBusy:
        raise HTTPException(
            status_code=409,
            detail="Another certificate change on this cluster is in progress. Try again in a moment.",
        )
    except Exception as exc:
        logger.warning(f"Could not remove trust entries for {cluster_id} before deletion: {exc}")
        try:
            await db.rollback()
        except Exception:
            pass
    try:
        # Delete all cluster data from Vault
        vault = cert_manager.vault
        base_path = cert_manager.clusters_path
        loop = asyncio.get_event_loop()

        # important: bound cleanup so a slow/degraded Vault or a locked DB cannot make
        # delete hang (users were double-clicking a button that never responded).
        VAULT_DELETE_TIMEOUT = 20.0
        DB_DELETE_TIMEOUT = 10.0

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
        
        # Run a parallel batch of vault deletes, hard-bounded by VAULT_DELETE_TIMEOUT.
        # Manual executor + shutdown(wait=False) so the timeout actually caps wall-clock
        # (a `with ThreadPoolExecutor` block would re-block on its wait=True shutdown).
        async def _run_vault_batch(delete_fn, ops, max_workers: int, label: str) -> None:
            executor = ThreadPoolExecutor(max_workers=max_workers)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*[
                        loop.run_in_executor(executor, delete_fn, path, name)
                        for path, name in ops
                    ]),
                    timeout=VAULT_DELETE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"Vault {label} deletion timed out for {cluster_id} after "
                    f"{VAULT_DELETE_TIMEOUT}s; continuing with cache/connection cleanup"
                )
            finally:
                executor.shutdown(wait=False)

        # Step 1: Delete data paths in parallel (soft delete in KV v2)
        subpaths = ["metadata", "secrets", "certs", "pending_certs"]
        await _run_vault_batch(
            _vault_delete_safe,
            [(f"{base_path}/{cluster_id}/{sp}", sp) for sp in subpaths],
            max_workers=4,
            label="data",
        )

        # Step 2+3: Delete metadata for each subpath + parent folder in parallel
        # KV v2 requires deleting BOTH data and metadata paths
        metadata_ops = [
            (f"secret/metadata/clusters/{cluster_id}/{sp}", sp) for sp in subpaths
        ]
        # Include parent metadata folder deletion (CRITICAL - removes from LIST)
        metadata_ops.append((f"secret/metadata/clusters/{cluster_id}", "parent_folder"))
        await _run_vault_batch(
            _vault_delete_metadata_safe, metadata_ops, max_workers=5, label="metadata",
        )
        
        # Step 4: Delete OrganizationCluster DB binding so cluster disappears from org scope.
        # Bounded by DB_DELETE_TIMEOUT so a lock/slow DB can't hang the whole request.
        try:
            from app.models.db_models import OrganizationCluster
            from sqlalchemy import delete as sql_delete

            async def _delete_binding() -> int:
                result = await db.execute(
                    sql_delete(OrganizationCluster).where(OrganizationCluster.cluster_id == cluster_id)
                )
                # Again here, in case step 0 rolled back: a row for a cluster with no Vault entry
                # would wait on a certificate that will never be read.
                await trust_store.delete_for_cluster(db, cluster_id)
                await db.commit()
                return result.rowcount

            deleted_rows = await asyncio.wait_for(_delete_binding(), timeout=DB_DELETE_TIMEOUT)
            if deleted_rows > 0:
                logger.info(f"Deleted {deleted_rows} OrganizationCluster binding(s) for {cluster_id}")
            else:
                logger.info(f"No OrganizationCluster binding found for {cluster_id}")
        except asyncio.TimeoutError:
            logger.warning(
                f"DB binding deletion timed out for {cluster_id} after {DB_DELETE_TIMEOUT}s"
            )
            try:
                await db.rollback()
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"Failed to delete OrganizationCluster binding for {cluster_id}: {e}")
        
        # Step 5: Clear ALL cache for this cluster - critical for immediate UI update
        from app.core.cache import get_cache_manager
        try:
            cache = get_cache_manager()
            if cache:
                # Use dedicated cache invalidation method for cluster-specific data
                cache.invalidate_cluster_cache(cluster_id)

                # Clear every scoped list shape, including versioned instance keys, and leave
                # the instance-list generation token that prevents an in-flight fleet read
                # from writing the deleted cluster back after this request completes.
                cache.invalidate_instance_cache()
                cache.invalidate_resource_cache("clusters", "projects", "regions")
                
                # Also invalidate the Vault list cache to remove from list immediately
                list_path = cert_manager.clusters_path.replace("/data/", "/metadata/")
                cache.delete("vault_list", list_path)
                
                # Clear nodes status cache to ensure UI updates immediately
                cache.delete("api_responses", get_scope_cache_key(NODES_STATUS_CACHE_KEY))
                cache.delete("api_responses", f"{NODES_STATUS_CACHE_KEY}:scope=all")
                cache.delete_pattern("api_responses", f"{NODES_AVAILABILITY_CACHE_KEY}*")
                cache.delete("api_responses", get_scope_cache_key("nodes:status"))
                cache.delete("api_responses", "nodes:status:scope=all")
                
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
            cluster_id=cluster_id,
            manual_trust_commands=manual_trust_commands,
            trust_cleanup_complete=trust_cleanup_complete,
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
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Regenerate certificate for a cluster."""
    # verify caller may access this cluster (org-scope), matching get/delete/update
    verify_cluster_access(cluster_id)
    try:
        await assert_cluster_not_migration_fenced(cluster_id=cluster_id)
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
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
        
        # Re-detect type from endpoint for accuracy (in case it changed or wasn't set).
        # important: only overwrite when the probe actually determined something. It is
        # unauthenticated, so it returns None for the whole LXD family, and taking that
        # unconditionally would blank a type an authenticated read had already established.
        cluster_type = stored_type
        if endpoint:
            detected_type, _ = await detect_hypervisor_type_from_endpoint(endpoint)
            if detected_type:
                cluster_type = detected_type
            logger.info(
                f"Regenerate cert: detected type for {cluster_id}: "
                f"{detected_type or 'undetermined, keeping ' + str(stored_type)}"
            )
        
        # Generate new certificate (let default FQDN logic apply: cluster_id.domain). The one it
        # overwrites is recorded first, under the cluster's certificate lock, so its trust entry
        # is removed once the cluster trusts the new one.
        try:
            async with trust_store.cluster_cert_lock(db, cluster_id):
                previous_fp = await cert_rotation.record_current_superseded(
                    db, cert_manager, cluster_id, reason="regenerate"
                )
                cert_data = await asyncio.to_thread(
                    cert_manager.generate_and_store_certificate,
                    cluster_id=cluster_id,
                    ttl=settings.vault_cert_ttl,
                )
                await db.commit()
        except trust_store.CertificateOperationBusy:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Another certificate change on this cluster is in progress. Try again in a moment.",
            )
        
        # Get the certificate to return in response
        certificate_pem = cert_data.get("cert", "")
        
        # Build CLI-friendly message based on detected type
        cli_tool = "incus" if cluster_type == "Incus" else "lxc"
        
        # The rotated certificate is what the Vault cache holds. Leaving it means the next
        # connect uses the superseded material and the cluster reports not_authorized.
        from app.core.cache import get_cache_manager
        get_cache_manager().invalidate_cluster_cache(cluster_id)
        try:
            new_fp = lxd_fingerprint(certificate_pem) if certificate_pem else None
        except ValueError:
            new_fp = None
        cert_rotation.audit(
            "cluster.certificate.regenerate", cluster_id,
            fingerprint=new_fp, previous_fingerprint=previous_fp,
        )
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
    except HTTPException:
        # important: let a deliberate status through. This try raises HTTPException
        # itself, and the broad handler below rewrote it, so an unreachable cluster
        # answered 500 or 404 instead of 503 and a tenant ACL denial answered 500
        # instead of 403.
        raise
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
        
        # note: copied before the update below, so the audit event can say what it replaced.
        previous = existing_metadata.get("location")
        previous_location = dict(previous) if isinstance(previous, dict) else {}
        # Update location in metadata. A catalogue region is stored as the catalogue has it,
        # whatever name or coordinates the request carried.
        location_data = get_region_catalog().canonical_location(request.location.model_dump(exclude_none=True))
        existing_metadata["location"] = location_data
        
        # IMPORTANT: Sync zone with location.city for backwards compatibility
        # The zone field is displayed in the clusters table
        new_zone = location_data["city"]
        
        # Save updated metadata with synced zone
        cert_manager.save_cluster_metadata(
            cluster_id=cluster_id,
            endpoint=existing_metadata.get("endpoint", ""),
            display_name=existing_metadata.get("display_name", cluster_id),
            zone=new_zone,  # Update zone to match location.city
            # Preserve whatever is stored, including "not determined yet". Defaulting to
            # "LXD" here would re-introduce the guess this change removes.
            cluster_type=existing_metadata.get("type"),
            merchant_id=existing_metadata.get("merchant_id"),
            location=location_data
        )
        
        # Invalidate all relevant caches
        from app.core.cache import get_cache_manager
        cache = get_cache_manager()
        if cache:
            # CRITICAL: Invalidate clusters list cache (scoped + admin)
            cache.invalidate_resource_cache("clusters", "regions")
            # Also invalidate individual cluster cache if exists
            cache.invalidate_cluster_cache(cluster_id)
        
        # Log activity
        activity_logger.log_activity(
            action="cluster.location.update",
            target_type="cluster",
            target_id=cluster_id,
            details={
                "location": location_data,
                "region_name": location_data["region_name"],
                "city": location_data["city"],
                "zone_updated_to": new_zone,
            },
            before_state=previous_location,
            after_state=location_data,
        )
        
        logger.info(f"Updated location for cluster {cluster_id}: {location_data['region_name']}, {location_data['city']} (zone set to: {new_zone})")
        
        return ClusterLocationUpdateResponse(
            success=True,
            message=f"Location updated successfully for cluster '{cluster_id}'",
            cluster_id=cluster_id,
            location=GeoLocation(**location_data)
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update location for {cluster_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update location: {str(e)}"
        )


@router.post(
    "/{cluster_id}/repin",
    summary="Re-pin a cluster's server certificate",
    description="Record the certificate the cluster is presenting now, replacing the pinned one.",
)
async def repin_cluster_certificate(
    cluster_id: str,
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_admin()),
):
    """Accept a rotated server certificate deliberately.

    important: pinning is only safe to enable if there is a way to accept a legitimate rotation.
    Without one, the first time an operator rotates a hypervisor certificate the cluster goes
    dark and the fix is to turn pinning off, which is how a control ends up permanently disabled.

    Admin only, and it takes no fingerprint from the caller: it records what the endpoint is
    presenting right now. A caller-supplied fingerprint would let someone pin a cluster to a
    certificate they hold rather than to the one it actually serves.
    """
    verify_cluster_access(cluster_id)
    try:
        await assert_cluster_not_migration_fenced(cluster_id=cluster_id)
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc

    settings = get_settings()
    if not (settings.vault_enabled and settings.vault_token):
        raise HTTPException(status_code=503, detail="Vault is not configured")

    from app.core.vault_client import get_vault_client
    from app.services.lxd_certs.pinning import (
        FINGERPRINT_FIELD,
        PINNED_AT_FIELD,
        fetch_server_fingerprint,
    )

    vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
    metadata = vault_client.vault_read(f"{settings.vault_clusters_path}/{cluster_id}/metadata")
    if not metadata:
        raise HTTPException(status_code=404, detail="Cluster not found")

    endpoint_url = metadata.get("endpoint")
    if not endpoint_url:
        raise HTTPException(status_code=409, detail="Cluster has no endpoint recorded")

    fingerprint = await asyncio.to_thread(fetch_server_fingerprint, endpoint_url)
    if not fingerprint:
        raise HTTPException(
            status_code=503,
            detail="Could not read the certificate the cluster is presenting",
        )

    previous = metadata.get(FINGERPRINT_FIELD)
    from datetime import datetime, timezone
    updated = dict(metadata)
    updated[FINGERPRINT_FIELD] = fingerprint
    updated[PINNED_AT_FIELD] = datetime.now(timezone.utc).isoformat()
    vault_client.vault_kv_put(f"{settings.vault_clusters_path}/{cluster_id}/metadata", updated)

    # The cluster metadata this just rewrote is what the availability and region reads render,
    # and a pooled client built before the re-pin still holds the old pin, so drop both.
    from app.core.cache import get_cache_manager
    from app.services.lxd_service import get_lxd_service

    cache = get_cache_manager()
    cache.delete_pattern("api_responses", f"{NODES_AVAILABILITY_CACHE_KEY}*")
    cache.delete_pattern("api_responses", "regions:*")
    # A client pooled before the re-pin was built against the old certificate, so drop it and
    # let the next call reconnect and re-check against the pin this just wrote.
    get_lxd_service().connection_manager.remove_connection(cluster_id)

    logger.warning(
        "Re-pinned %s from %s… to %s… by %s",
        cluster_id,
        (previous or "<unpinned>")[:16],
        fingerprint[:16],
        _rbac.email,
    )
    return {
        "cluster_id": cluster_id,
        "previous_fingerprint": (previous[:16] + "...") if previous else None,
        "fingerprint": fingerprint[:16] + "...",
        "repinned_by": _rbac.email,
    }
