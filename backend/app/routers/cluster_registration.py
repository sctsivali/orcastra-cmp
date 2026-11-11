"""
Cluster registration endpoints: register, list, check trust, update location, regenerate or
re-pin certificates, and deregister. Vault holds each cluster's record and certificates.
"""

import logging
import asyncio
from typing import Any, Optional
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, HTTPException, Depends, Response, status
from datetime import datetime, timezone

from app.models.schemas import (
    CertificateLifetimeOptions,
    CertificateRegenerateRequest,
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
from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.vault_client import get_vault_client
from app.core import vault_probe
from app.core.registry_guard import require_known_registry, vault_unavailable_error
from app.services.lxd_cert_manager import LXDCertificateManager, LXDCertManagerError
from app.services.lxd.exceptions import LXDLocalStorageError
from app.services.lxd.exceptions import LXDAuthorizationError
from app.core.dependencies import check_read_only_mode, require_admin, require_partner, require_tenant, AuthenticatedUser
from app.core.cache import (
    NODES_STATUS_CACHE_KEY,
    get_resource_list_generation,
)
from app.core.org_scope import org_scope_dependency, verify_cluster_access, get_scope_cache_key, apply_cluster_filter
from app.core.host_detail import cluster_for_tier, cluster_list_for_tier
from app.core.task_scope import caller_tier
from app.core.url_safety import normalize_endpoint
from app.core.whole_cluster_access import refuse_unless_the_party_holds_it_whole, verify_whole_cluster_access
from app.core.type_utils import (
    detect_cluster_type_from_metadata,
    detect_clustered_from_metadata,
    detect_hypervisor_type_from_endpoint,
    get_cli_command_prefix,
    resolve_cluster_type,
    resolve_clustered,
)
from app.services import cluster_generations
from app.services.cluster_records import forget_cluster_records
from app.services.activity_logger import get_activity_logger
from app.services.registration_budget import spend_registration_budget
from app.services.lxd_certs import lifetime as cert_lifetime
from app.services.lxd_certs import pending as pending_certs
from app.services.lxd_certs import trust_cleanup
from app.services.lxd_certs.storage import stored_location, stored_location_fields
from app.core.error_sanitizer import GENERIC_SERVER_ERROR
from app.services.lxd_certs import trust_cleanup_store as trust_store
from app.services.region_catalog import get_region_catalog
from app.core.database import get_db
from app.core import swr
from app.core.swr import Built
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

        # important: the cached seal probe, not VaultClient.health_check(), which would retry on
        # every request and report a sealed Vault like a network fault. The probe answers once
        # per ten seconds per worker and the refusal carries the structured vault_unavailable.
        if vault_probe.seal_probe().state != "ok":
            raise vault_unavailable_error()
        
        cert_manager = LXDCertificateManager(
            vault_client=vault_client,
            clusters_path=settings.vault_clusters_path
        )
        
        return cert_manager
        
    except HTTPException:
        # important: let the 503 raised above through; the broad handler below would turn it
        # into a 500.
        raise
    except Exception as e:
        logger.error(f"Failed to initialize certificate manager: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not initialize certificate manager: {str(e)}"
        )


def _owns_a_binding(rows, org_ids, email: str) -> bool:
    """Whether one of a cluster's ownership rows (organization_id, registered_by[, source]) is the caller's.

    A binding provisioning wrote for a sale (no registrant, OrcaHub organization) is not
    ownership. Counting it would let the buyer register the seller's cluster again, rewriting its
    Vault record, and gain a binding of its own.
    """
    return any(
        (row[0] is not None and row[0] in org_ids and not (row[1] is None and len(row) > 2 and row[2] == "orcahub"))
        or (row[0] is None and row[1] == email)
        or row[1] == email
        for row in rows
    )


async def _continues_its_registration(db: AsyncSession, cluster_id: str, user: AuthenticatedUser) -> bool:
    """Whether this register call is the cluster's owner (or an Admin) registering it again.

    A Vault record alone does not say so. A partner can unbind a cluster without deregistering
    it, leaving the record and its registration with no owner; another organization registering
    that id must get a new registration, or every grant made for the earlier one would reach its
    cluster. No ownership row means a new trust identity.
    """
    from sqlalchemy import select as sa_select

    from app.core.org_scope import get_user_org_ids
    from app.core.rbac import Role, get_highest_role, resolve_roles
    from app.models.db_models import Organization, OrganizationCluster

    rows = (
        await db.execute(
            sa_select(OrganizationCluster.organization_id, OrganizationCluster.registered_by, Organization.source)
            .outerjoin(Organization, Organization.id == OrganizationCluster.organization_id)
            .where(OrganizationCluster.cluster_id == cluster_id)
        )
    ).all()
    if not rows:
        return False
    if get_highest_role(resolve_roles(user)) == Role.ADMIN:
        return True
    return _owns_a_binding(rows, await get_user_org_ids(user, db), user.email)


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
    from app.models.db_models import Organization, OrganizationCluster

    org_ids = await get_user_org_ids(user, db)
    prospective_org = org_ids[0] if org_ids else None

    rows = (
        await db.execute(
            sa_select(
                OrganizationCluster.organization_id,
                OrganizationCluster.registered_by,
                Organization.source,
            )
            .outerjoin(Organization, Organization.id == OrganizationCluster.organization_id)
            .where(OrganizationCluster.cluster_id == cluster_id)
        )
    ).all()
    if not rows:
        # important: an id still in the Vault registry with no binding is an orphan. Registering it
        # again would rewrite its endpoint and certificate for whoever still uses it, so only an
        # admin may claim one.
        if get_highest_role(resolve_roles(user)) != Role.ADMIN:
            from app.services.lxd import registry as cluster_registry

            registered = await cluster_registry.registered_cluster_ids(fresh=True)
            if registered is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="The cluster registry could not be read, so nothing was registered; retry shortly",
                )
            if cluster_id in registered:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        f"Cluster {cluster_id} is registered without an owning organization. "
                        "An administrator can register it again."
                    ),
                )
        return org_ids
    owns_existing_identity = _owns_a_binding(rows, org_ids, user.email)
    if (
        get_highest_role(resolve_roles(user)) != Role.ADMIN
        and not owns_existing_identity
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This cluster is already owned by another organization.",
        )
    # important: a registration rewrites the cluster's endpoint and certificate for every holder, and
    # owns_existing_identity counts any membership. A tenant row never owns a cluster, so registering
    # one that exists needs the whole cluster through the caller's party (core/whole_cluster_access).
    await refuse_unless_the_party_holds_it_whole(cluster_id, db, user)
    if prospective_org is not None and any(row[0] == prospective_org for row in rows):
        return org_ids
    if prospective_org is None and any(row[0] is None for row in rows):
        # The registration flow refuses a second unbound claimant, so no owner identity is
        # created and there is no share transition to audit.
        return org_ids

    await assert_cluster_safe_to_share(
        db,
        cluster_id,
        prospective_organization_id=prospective_org,
        prospective_registered_by=user.email,
    )
    return org_ids


async def _lifetime_plan(choice: Any, cert_manager: LXDCertificateManager) -> cert_lifetime.LifetimePlan:
    """The Vault request for ``choice``; a refused lifetime answers before anything is written."""
    try:
        return await asyncio.to_thread(cert_lifetime.plan, choice, cert_manager.vault)
    except cert_lifetime.LifetimeRefused as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get(
    "/certificate-lifetimes",
    response_model=CertificateLifetimeOptions,
    summary="Certificate lifetimes a cluster can be given",
    description=(
        "What the lifetime picker offers at registration, regenerate and renew, each with the date "
        "a certificate issued now would reach. The issued certificate's own date is in the write's answer."
    ),
)
async def certificate_lifetimes(
    response: Response,
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    _rbac: AuthenticatedUser = Depends(require_partner()),
) -> CertificateLifetimeOptions:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(cert_lifetime.options, cert_manager.vault)


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
    spend_registration_budget("register", _rbac)
    share_serialization = None
    try:
        settings = get_settings()
        cluster_id = request.cluster_id
        # The lifetime is checked before anything is written, so a refused one registers nothing.
        lifetime_plan = await _lifetime_plan(request.certificate_lifetime, cert_manager)

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

        # Which registration this is (services/cluster_generations.py), in this transaction and
        # under the share lock. An id the registry lists right now, registered again by its owner
        # or an Admin (ex; a new endpoint), keeps its registration and every API key grant made
        # for it. Anything else starts a new one, which no earlier grant reaches.
        # important: from Vault itself. A remembered registry could call a deregistered id live.
        read = await asyncio.to_thread(cert_manager.read_registry, False)
        if getattr(read, "source", None) != "vault":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The cluster registry could not be read. Try again in a moment.",
            )
        await cluster_generations.begin_registration(
            db,
            cluster_id,
            live=cluster_id in set(read.cluster_ids)
            and await _continues_its_registration(db, cluster_id, _rbac),
        )
        
        # Step 1: pre-auth type detection from the /1.0 endpoint. Without auth it may return
        # api_extensions, which can tell Incus from the LXD family, but it withholds
        # `environment`, so LXD vs MicroCloud stays undetermined (None) until the first trusted
        # read, and nothing is stored for it.
        cluster_type, raw_metadata = await detect_hypervisor_type_from_endpoint(request.endpoint)
        logger.info(
            f"Pre-auth type detection for {cluster_id}: "
            f"{cluster_type or 'undetermined, deferring to first trusted read'}"
        )

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
        
        # Step 2b: bind the cluster to the registrant's organization so it appears in org-scoped
        # queries. Runs before cache invalidation so the binding exists when caches repopulate.
        # important: use the same membership predicate as cluster visibility (get_user_org_ids),
        # so the binding cannot drift from the scope query.
        org_bind_warning: Optional[str] = None
        try:
            from sqlalchemy import select as sa_select
            from app.models.db_models import OrganizationCluster

            if org_ids:
                # Bind to the primary org, once.
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
                # A partner with no synced org yet still records ownership (org_id NULL), so the
                # registrant can see and grant API-key access to their own cluster;
                # reconcile_personal_clusters binds it to their org once it is synced.
                # important: filter on exactly the columns of uq_org_cluster_null, a partial
                # unique index on cluster_id where organization_id IS NULL. Filtering on
                # registered_by too would miss a row another user holds, and the INSERT would
                # then violate the index.
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
            cluster_generations.changed()
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
        
        # Invalidated after the org binding so scoped users see the cluster.
        from app.core.cache import get_cache_manager
        cache = get_cache_manager()
        if cache:
            cache.invalidate_resource_cache("clusters", "regions")
            cache.invalidate_capacity_cache()
            # The cluster list also reads the vault_list cache.
            vault_list_path = settings.vault_clusters_path.replace("/data/", "/metadata/")
            cache.delete("vault_list", vault_list_path)
            cache.delete("api_responses", get_scope_cache_key(NODES_STATUS_CACHE_KEY))
            cache.delete("api_responses", f"{NODES_STATUS_CACHE_KEY}:scope=all")
            cache.invalidate_availability_cache()
            # Invalidate regions overview (all scopes) so the inventory/monitoring
            # overview reflects the new cluster without a manual refresh
            cache.delete_pattern("api_responses", "regions:*")
            logger.info(f"Invalidated clusters list cache (scoped + vault_list + regions) after registering {cluster_id}")
        
        # Add the cluster to the connection manager's allowlist.
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
                    await pending_certs.record_current_superseded(
                        db, cert_manager, cluster_id, reason="register"
                    )
                    cert_data = await asyncio.to_thread(
                        cert_manager.generate_and_store_certificate,
                        cluster_id=cluster_id,
                        lifetime_plan=lifetime_plan,
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
            issued_lifetime = cert_data.get("lifetime")
            
            # Incus 6.0.7 and 7.6 send an untrusted caller no api_extensions, so before trust the
            # type is usually unknown for every engine and the message names both commands.
            register_message = (
                f"Certificate generated! Run the {get_cli_command_prefix(cluster_type)} command to add trust. "
                f"Detected hypervisor: {cluster_type}."
                if cluster_type
                else "Certificate generated! Run the trust command for your hypervisor (lxc on LXD or "
                "MicroCloud, incus on Incus). The type is confirmed once the certificate is trusted."
            )
            if issued_lifetime is not None and issued_lifetime.shortened:
                register_message = f"{register_message} {cert_lifetime.shortened_notice(issued_lifetime)}"
            if org_bind_warning:
                register_message = f"{register_message} {org_bind_warning}"

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
                    **(cert_lifetime.audit_fields(issued_lifetime) if issued_lifetime is not None else {}),
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
                cluster_type=cluster_type,
                **(cert_lifetime.response_fields(issued_lifetime) if issued_lifetime is not None else {}),
            )
                
        except LXDCertManagerError as e:
            logger.error(f"Certificate generation failed for {cluster_id}: {e}")

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
                # The Vault PKI text names mounts and roles; it is in the log and the audit row.
                message="Could not generate a certificate for this cluster. An administrator can find the cause in the backend log.",
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

        # use_breaker=False: an operator-initiated check probes for real rather than replay a
        # 60-second-old failure.
        auth_probe = cert_manager.probe_connection(cluster_id, use_breaker=False)
        connected, message = auth_probe.trusted, auth_probe.message
        
        if connected:
            try:
                client = cert_manager.connect_with_vault_cert(cluster_id, force=True)
                server_info = client.api.get().json()
                
                # Re-detect and update cluster type from authenticated live connection
                final_cluster_type = None
                final_clustered = None
                try:
                    metadata_info = server_info.get("metadata", {})
                    environment = metadata_info.get("environment", {})

                    # The first payload that carries `environment`, so the first reliable reading
                    # of both the hypervisor family and the clustering state.
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

                    # important: the only place either fact is written, and only after the host
                    # answered a trusted request; an untrusted probe cannot see `environment`.
                    # Both resolvers prefer a live client, so a stored reading answers only when
                    # there is nobody to ask. `clustered` is stored because the members picker
                    # must tell a host pending trust from an offline clustered host. The write
                    # condition includes it so a host with the right type still gets it written.
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
                        # Heal a stored endpoint with a trailing slash while the record is being
                        # written; the probe normalizes on read anyway.
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
                
                # Add the connection to the LXD service's connection manager so the cluster is
                # in the active list for project and instance queries.
                try:
                    from app.services.lxd_service import get_lxd_service
                    lxd_service = get_lxd_service()

                    settings = get_settings()
                    cert_data = cert_manager.vault.vault_read(
                        f"{settings.vault_clusters_path}/{cluster_id}/certs"
                    )
                    metadata = cert_manager.vault.vault_read(
                        f"{settings.vault_clusters_path}/{cluster_id}/metadata"
                    )

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
                
                from app.core.cache import get_cache_manager
                cache = get_cache_manager()
                if cache:
                    cache.invalidate_cluster_reachability(cluster_id)
                
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
                    # The exception text names the host's address and Vault paths; it is in the
                    # log line above.
                    message="Connected, but the server information could not be read.",
                )
        else:
            # A cluster that never answered is offline, not a trust problem, so the status
            # comes from reachability.
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
        # important: keep above the generic handler. probe_connection raises this when the
        # backend's own disk cannot hold the TLS material, which says nothing about the cluster
        # (derive_status would report OFFLINE). Re-raised so the app-level handler answers 503
        # BACKEND_TEMP_STORAGE_FULL.
        raise
    except Exception as e:
        # Logged here in full; the caller gets the fixed answer every server fault gets
        # (main.py adds the reference that ties the two together).
        logger.exception("Authentication check failed for %s: %s", request.cluster_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_SERVER_ERROR,
        )


def _cluster_info_blocking(
    cluster_id: str, cert_manager: LXDCertificateManager, *, force: bool = False
) -> Optional[ClusterInfo]:
    """One cluster list row. Blocking: run it through ``_get_cluster_info_async``.

    Also adds connected clusters to the connection manager for later calls. Order: reuse the
    pooled connection; if this worker last saw the cluster fail, report that verdict (unless
    ``force``); otherwise reconnect with the Vault certificate, then fall back to a probe.
    """
    try:
        from app.services.lxd_service import get_lxd_service
        from app.services.lxd import health as lxd_health

        lxd_service = get_lxd_service()
        manager = lxd_service.connection_manager
        verdict = None if force else lxd_health.last_failure(cluster_id)

        # get_client reconnects from stored connection info, but not for a cluster this worker
        # saw fail and holds no client for: that reconnect is a dial of its own once the trip
        # lapses (about 8 s against a dead peer), and it is the healer's job to pay it.
        if verdict is not None and manager.get_cached_client(cluster_id) is None:
            existing_client = None
        else:
            existing_client = manager.get_client(cluster_id)
        
        connected = False
        # important: reachable is tracked apart from connected. An unreachable cluster is
        # offline, not pending trust.
        reachable = None
        error_msg = None
        
        live_client = existing_client
        if existing_client:
            connected = True
            reachable = True
            logger.debug(f"Cluster {cluster_id} already connected in pool")
        elif verdict is not None:
            # important: this worker already knows the cluster is down, tripped or between two
            # retries. Dialing anyway costs a forced connect per dead cluster on every cold list
            # read. The background healer keeps retrying and clears the verdict on its first
            # success; Refresh sends fresh=true, which still forces a real recheck.
            # Only the kind is used. The reason is raw exception text, so it stays in the logs
            # and derive_status words the row, as the availability rows do (services/lxd/status.py).
            reachable = verdict[1] == lxd_health.KIND_UNAUTHORIZED
        else:
            # No pooled connection: reconnect with the Vault certificate.
            try:
                cert_data = cert_manager.get_cluster_certificate(cluster_id)
                info = cert_manager.get_cluster_info(cluster_id)
                
                # Keep the client this returns: reading its cached host_info below is how the
                # type is resolved, and a second get_client would pay for another revalidation.
                live_client = manager.connect_with_vault_cert(
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
                # The host answered and refused the certificate: pending trust.
                reachable, error_msg = True, str(auth_err)
                logger.debug(f"{cluster_id} answered but refused our certificate: {auth_err}")
            except Exception as reconnect_err:
                logger.debug(f"Could not reconnect {cluster_id}: {reconnect_err}")
                # Fall back to a probe, which reports reachability and trust separately
                probe = cert_manager.probe_connection(cluster_id)
                connected, reachable, error_msg = probe.trusted, probe.reachable, probe.message
        
        # Get cluster info (may already be cached from above)
        info = cert_manager.get_cluster_info(cluster_id)
        
        current_status, _reason_code, derived_message = derive_status(
            connected=connected, reachable=reachable, error=error_msg
        )
        
        # Prefer what the host says now over what was stored. client.host_info is pylxd's
        # cached /1.0 body, so resolving from a live client costs no extra round trip.
        # important: a stored `clustered` is always evidence: only check-auth writes it, after a
        # trusted request. A stored `type` may be an "LXD" default no host reported, so it is
        # read only when connected or when a stored `clustered` shows check-auth wrote the record
        # (it writes both together).
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
            # important: never the raw stored value. A malformed location ({}, null, a partial
            # one, a string or a list) would fail ClusterInfo and degrade the whole row;
            # stored_location reads it as no location.
            location=stored_location(cluster_id, info["metadata"]),
        )

    except LXDLocalStorageError:
        # important: keep above the generic handler. probe_connection raises this when the
        # backend's own disk cannot hold the TLS material, which says nothing about the cluster
        # (derive_status would report OFFLINE). Re-raised so the app-level handler answers 503
        # BACKEND_TEMP_STORAGE_FULL.
        raise
    except Exception as e:
        # The text stays in the log. It is Vault's, pydantic's or a driver's and can carry paths
        # and stored values, and the row reaches every caller of the list.
        logger.warning(f"Failed to get info for cluster {cluster_id}: {e}")
        return ClusterInfo(
            cluster_id=cluster_id,
            display_name=cluster_id,
            endpoint="unknown",
            cluster_type=None,
            status=ClusterStatus.ERROR,
            has_certificate=False,
            error_message=_ROW_UNREADABLE,
        )


# Sized for a fleet's worth of rows at once. A row's own sockets carry (3, 8)s budgets, so the
# wall cap below only bounds how long the list waits for a row, never a leaked thread.
_cluster_list_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="cluster-list")
_CLUSTER_ROW_TIMEOUT_SECONDS = 30.0
# The wall also counts time queued behind other lists' rows, so the queue is bounded too: past
# this many rows in flight a row is refused at once rather than waiting out its wall.
_CLUSTER_ROW_MAX_IN_FLIGHT = 64
_ROW_UNFINISHED = "The status check did not finish in time. Refresh to try again."
# How long a cluster list read stays fresh (see the build below).
_LIST_FRESH_SECONDS = 30
_ROW_UNREADABLE = "This cluster's record could not be read. The server log has the detail."


async def _get_cluster_info_async(
    cluster_id: str, cert_manager: LXDCertificateManager, *, force: bool = False
) -> Optional[ClusterInfo]:
    """Build one list row on a worker thread, so the rows are probed side by side.

    The row build is blocking (pooled-client revalidation, Vault reads, a TLS connect). Awaited
    inline, the rows would run one after another on the loop thread and stall the worker.
    """
    try:
        return await run_blocking(
            _cluster_info_blocking,
            cluster_id,
            cert_manager,
            force=force,
            timeout=_CLUSTER_ROW_TIMEOUT_SECONDS,
            label=f"clusters.list[{cluster_id}]",
            executor=_cluster_list_executor,
            max_in_flight=_CLUSTER_ROW_MAX_IN_FLIGHT,
        )
    except (BlockingTimeout, BlockingPoolFull) as exc:
        logger.warning(f"Cluster {cluster_id} status read did not finish: {exc}")
        return ClusterInfo(
            cluster_id=cluster_id,
            display_name=cluster_id,
            endpoint="unknown",
            cluster_type=None,
            status=ClusterStatus.ERROR,
            has_certificate=False,
            error_message=_ROW_UNFINISHED,
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

    async def build() -> Built:
        # fresh=true bypasses the Vault list cache, so a deleted cluster does not linger.
        # important: the org-scope filter limits partners and tenants to their own org's
        # clusters (Admin unfiltered), as overview and nodes do. The cache key is scope-aware.
        cluster_ids = apply_cluster_filter(
            await asyncio.to_thread(cert_manager.list_clusters, use_cache=not fresh)
        )
        # An unreadable registry with nothing known is not "no clusters registered".
        require_known_registry(cluster_ids)

        # Rows are built in parallel. `force=fresh` is captured, so a background refresh (always
        # fresh=False) never forces a probe past the reachability breaker.
        tasks = [
            _get_cluster_info_async(cluster_id, cert_manager, force=fresh)
            for cluster_id in cluster_ids
        ]
        clusters = await asyncio.gather(*tasks)

        # Drop None rows defensively.
        clusters = [c for c in clusters if c is not None]

        response = ClusterListResponse(
            clusters=clusters,
            total=len(clusters),
            collected_at=datetime.now(timezone.utc),
        )

        # important: a row that did not finish says nothing about its cluster. Cached, it would
        # show every reader of this scope an error row for the whole TTL.
        finished = not any(c.error_message == _ROW_UNFINISHED for c in clusters)
        if not finished:
            logger.info("Not caching clusters list: a row did not finish in time")
        # Fresh for 30 s. A recovery clears this list at once but a failure does not, so a longer
        # TTL would show a downed cluster online until the entry aged out. Past 30 s a read is
        # served stale and refreshed in the background, so an open page shows the change on its
        # next look.
        return Built(value=response, body=response.model_dump(), fresh_for=_LIST_FRESH_SECONDS if finished else None)

    try:
        # e1: the stale-while-revalidate envelope, with collected_at in the body.
        served = await swr.serve(
            cache=cache,
            base_key="clusters:list:all:e1",
            fresh=fresh,
            build=build,
            generation=lambda: get_resource_list_generation(cache, "clusters"),
            label="clusters list",
        )
        return cluster_list_for_tier(served.as_model(ClusterListResponse), caller_tier())

    except HTTPException:
        raise
    except Exception as e:
        # The detail is logged here; the caller gets the fixed answer every server fault gets
        # (main.py adds the reference that ties the two together).
        logger.exception("Failed to list clusters: %s", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_SERVER_ERROR,
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
                location = stored_location_fields(cluster_id, info.get("metadata"))
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
    verify_cluster_access(cluster_id)
    try:
        info = cert_manager.get_cluster_info(cluster_id)

        # The probe reports reachability and trust separately, so a powered-off cluster does
        # not read as a certificate problem.
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
            # A None result means the resolvers fall back to storage. Skipped when not
            # connected, since the revalidation is the expensive part and cannot succeed.
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
            # important: never the raw stored value. Anything short of a complete location would
            # fail this read with a 500 (stored_location says why).
            location=stored_location(cluster_id, info["metadata"]),
        ), caller_tier())
        
    except LXDLocalStorageError:
        # important: keep above LXDCertManagerError and the generic handler. probe_connection
        # raises this when the backend's own disk cannot hold the TLS material, which says
        # nothing about the cluster (derive_status would report OFFLINE). Re-raised so the
        # app-level handler answers 503 BACKEND_TEMP_STORAGE_FULL.
        raise
    except LXDCertManagerError as e:
        # A 4xx passes main.py's handler untouched, and the exception text is Vault's.
        logger.error(f"Failed to get cluster {cluster_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cluster '{cluster_id}' not found",
        )
    except Exception as e:
        # important: the text is pydantic's, Vault's or a driver's and can carry stored values and
        # Vault paths. It is logged here; the caller gets the fixed answer every server fault
        # gets (main.py adds the reference that ties the two together).
        logger.exception("Unexpected error getting cluster %s: %s", cluster_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=GENERIC_SERVER_ERROR,
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
    # important: deleting a registration ends it for every organization that holds the cluster,
    # strips every key grant and removes the dashboard's trust. Reaching the cluster through a
    # purchase or a member row is not holding it (core/whole_cluster_access).
    await verify_whole_cluster_access(cluster_id, db, _rbac)
    try:
        await assert_cluster_not_migration_fenced(cluster_id=cluster_id, db=db)
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    # important: the registration ends first, in its own transaction that commits before anything
    # is deleted (services/cluster_generations.py). The Vault deletes below are best effort; if it
    # ended among them, a re-registration could find this registration still active and inherit
    # every grant made for it.
    try:
        async with serialize_cluster_share(db, cluster_id):
            ended = await cluster_generations.end_registration(db, cluster_id)
            await db.commit()
    except Exception as exc:
        logger.error("Could not end the registration of %s; nothing was deleted: %s", cluster_id, exc)
        try:
            await db.rollback()
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The cluster's registration could not be ended, so nothing was deleted. Try again.",
        ) from exc
    cluster_generations.changed()
    # Step 0: remove the dashboard's own trust while the key still exists. Before any Vault delete,
    # because removing a trust entry needs the current certificate. Best effort: never blocks.
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

        # important: bound the cleanup so a slow Vault or a locked DB cannot make the delete
        # hang.
        VAULT_DELETE_TIMEOUT = 20.0
        DB_DELETE_TIMEOUT = 10.0
        KEY_GRANT_CLEANUP_TIMEOUT = 20.0

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
        
        # Run a parallel batch of Vault deletes, bounded by VAULT_DELETE_TIMEOUT. A manual
        # executor with shutdown(wait=False), so the timeout caps wall-clock time (a
        # `with ThreadPoolExecutor` block would block again on its wait=True shutdown).
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
        # The parent metadata folder too: deleting it removes the cluster from the Vault LIST.
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
                if await cluster_generations.active_registration(db, cluster_id) is not None:
                    # important: the id was registered again while this delete ran (its
                    # registration ended first, above). The binding, tags and schedules now
                    # belong to that registration.
                    logger.warning("Left %s's binding and records: it was registered again meanwhile", cluster_id)
                    return 0
                result = await db.execute(
                    sql_delete(OrganizationCluster).where(OrganizationCluster.cluster_id == cluster_id)
                )
                # Again here, in case step 0 rolled back: a row for a cluster with no Vault entry
                # would wait on a certificate that will never be read.
                await trust_store.delete_for_cluster(db, cluster_id)
                forgotten = await forget_cluster_records(db, cluster_id)
                await db.commit()
                if any(forgotten.values()):
                    logger.info("Forgot the records of deregistered %s: %s", cluster_id, forgotten)
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
        
        # Step 5: clear every cache entry for this cluster so the UI updates at once.
        from app.core.cache import get_cache_manager
        try:
            cache = get_cache_manager()
            if cache:
                cache.invalidate_cluster_cache(cluster_id)

                # Clear every scoped list shape, including versioned instance keys, and leave
                # the instance-list generation token that prevents an in-flight fleet read
                # from writing the deleted cluster back after this request completes.
                cache.invalidate_instance_cache()
                # Images and warnings too: both list rows per cluster, for 180 s and 60 s.
                cache.invalidate_resource_cache("clusters", "projects", "regions", "images", "warnings")
                
                # Also invalidate the Vault list cache to remove from list immediately
                list_path = cert_manager.clusters_path.replace("/data/", "/metadata/")
                cache.delete("vault_list", list_path)
                
                # Clear nodes status cache to ensure UI updates immediately
                cache.delete("api_responses", get_scope_cache_key(NODES_STATUS_CACHE_KEY))
                cache.delete("api_responses", f"{NODES_STATUS_CACHE_KEY}:scope=all")
                cache.invalidate_availability_cache()
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

        # Step 8: drop the grants made for the registration that just ended. They reach nothing,
        # but a key still reports them as held. Only grants for `ended` go, so a grant for a quick
        # re-registration stays.
        # important: shielded and never cancelled. A key rewrite cut off between its Vault write
        # and its lock release could land after a concurrent revoke and bring the key back; on a
        # slow Vault the route stops waiting and the cleanup carries on.
        keys_changed: Optional[int] = None
        try:
            from app.services.key_grant_hygiene import strip_cluster_from_key_grants
            from app.services.lxd import registry as cluster_registry
            from app.services.project_deletion import spawn

            cleanup = None
            if get_settings().key_grant_generations:
                cleanup = spawn(strip_cluster_from_key_grants(cluster_id, ended))
            else:
                # important: with generations switched off a grant matches by id alone, so every
                # grant naming the id goes, but only once the registry confirms the id is gone;
                # otherwise a failed Vault delete would strip live grants.
                still_registered = await cluster_registry.registered_cluster_ids(fresh=True)
                if still_registered is not None and cluster_id not in still_registered:
                    cleanup = spawn(strip_cluster_from_key_grants(cluster_id, None))
                else:
                    logger.warning("Not touching API key grants for %s: the registry does not confirm it is gone", cluster_id)
            if cleanup is not None:
                keys_changed = await asyncio.wait_for(
                    asyncio.shield(cleanup), timeout=KEY_GRANT_CLEANUP_TIMEOUT
                )
        except asyncio.TimeoutError:
            logger.warning(
                "API key grant cleanup for %s is still running after %ss; it finishes in the background",
                cluster_id, KEY_GRANT_CLEANUP_TIMEOUT,
            )
        except Exception as e:
            logger.warning(f"Failed to drop {cluster_id} from API key grants: {e}")

        logger.info(f"Successfully completed deletion of cluster {cluster_id}")

        activity_logger.log_activity(
            action="cluster.delete",
            target_type="cluster",
            target_id=cluster_id,
            result="success",
            details={
                "deletion_type": "full",
                "subpaths_deleted": ["metadata", "secrets", "certs", "pending_certs"],
                "api_key_grants_changed": keys_changed,
            },
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
            detail=f"Could not remove cluster: {str(e)}"
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
    
    **Note:** the new certificate is issued as PENDING and the current one stays active. Add it
    to the server's trust with the returned command, then activate it with
    `POST /certificates/verify` (pending_cert_id). A cluster with no certificate at all gets the
    new one active at once.
    """
)
async def regenerate_certificate(
    cluster_id: str,
    cert_manager: LXDCertificateManager = Depends(get_cert_manager),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    body: Optional[CertificateRegenerateRequest] = None,
):
    """Regenerate certificate for a cluster."""
    verify_cluster_access(cluster_id)
    # important: the certificate acts for every project on the cluster, and a new one replaces the
    # owner's pending renewal, so this is held to the whole cluster like /certificates/renew.
    await verify_whole_cluster_access(cluster_id, db, _rbac)
    spend_registration_budget("certificate", _rbac)
    try:
        await assert_cluster_not_migration_fenced(cluster_id=cluster_id)
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    try:
        info = cert_manager.get_cluster_info(cluster_id)
        if not info:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Cluster {cluster_id} not found"
            )
        
        endpoint = info.get("metadata", {}).get("endpoint", "")
        stored_type = info.get("metadata", {}).get("type")
        
        # Re-detect the type from the endpoint.
        # important: overwrite only when the probe determined something. It is unauthenticated
        # and returns None for the whole LXD family, which would blank a type an authenticated
        # read had established.
        cluster_type = stored_type
        if endpoint:
            detected_type, _ = await detect_hypervisor_type_from_endpoint(endpoint)
            if detected_type:
                cluster_type = detected_type
            logger.info(
                f"Regenerate cert: detected type for {cluster_id}: "
                f"{detected_type or 'undetermined, keeping ' + str(stored_type)}"
            )
        
        # A pending certificate, not a replacement: the active one keeps working until the cluster
        # trusts the new one and Verify activates it (services/lxd_certs/pending.py). Overwriting
        # it at once would cut off every worker that reconnects before trust is added.
        try:
            issued = await pending_certs.regenerate(
                db, cert_manager, cluster_id, cluster_type=cluster_type,
                lifetime_choice=body.certificate_lifetime if body is not None else None,
            )
        except trust_store.CertificateOperationBusy:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Another certificate change on this cluster is in progress. Try again in a moment.",
            )
        except cert_lifetime.LifetimeRefused as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        if issued.activated:
            # The cluster had no certificate at all, so the new one is active and the Vault cache
            # must not keep serving the absence.
            from app.core.cache import get_cache_manager
            get_cache_manager().invalidate_cluster_cache(cluster_id)
        cli_tool = get_cli_command_prefix(cluster_type)
        message = (
            f"Certificate issued and active. Run the {cli_tool} command to add trust. Detected: {cluster_type}."
            if issued.activated
            else f"New certificate issued as pending; the current one stays active. Run the {cli_tool} "
                 f"command on the host, then Verify to activate it. Detected: {cluster_type}."
        )
        if issued.lifetime is not None and issued.lifetime.shortened:
            message = f"{message} {cert_lifetime.shortened_notice(issued.lifetime)}"
        # `status` describes the new certificate, which the cluster does not trust yet; the
        # cluster's own status is unchanged until Verify.
        return ClusterRegisterResponse(
            success=True,
            message=message,
            cluster_id=cluster_id,
            status=ClusterStatus.NOT_AUTHORIZED,
            certificate_generated=True,
            trust_added=False,
            certificate=issued.certificate,
            cluster_type=cluster_type,
            pending_cert_id=None if issued.activated else issued.pending_cert_id,
            cli_command=issued.cli_command,
            identity_name=issued.identity_name,
            fingerprint=issued.fingerprint,
            previous_fingerprint=issued.previous_fingerprint,
            **(cert_lifetime.response_fields(issued.lifetime) if issued.lifetime is not None else {}),
        )

    except LXDCertManagerError as e:
        logger.error(f"Failed to regenerate certificate for {cluster_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )
    except HTTPException:
        # important: let the 404 and 409 raised above through; the broad handler below would
        # turn them into a 500.
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
    db: AsyncSession = Depends(get_db),
):
    """Update the location of an existing cluster."""
    verify_cluster_access(cluster_id)
    # important: the location is the cluster's, shown to every holder and to admins.
    await verify_whole_cluster_access(cluster_id, db, _rbac)
    logger.info(f"Updating location for cluster {cluster_id}")
    
    try:
        # An older cluster may have no metadata yet.
        try:
            cluster_info = cert_manager.get_cluster_info(cluster_id)
            existing_metadata = cluster_info.get("metadata", {}) if cluster_info else {}
        except LXDCertManagerError:
            logger.warning(f"No existing metadata for cluster {cluster_id}, creating fresh")
            existing_metadata = {"endpoint": "", "display_name": cluster_id, "type": "LXD"}
        
        # Copied before the update below, so the audit event can say what it replaced.
        previous = existing_metadata.get("location")
        previous_location = dict(previous) if isinstance(previous, dict) else {}
        # Update location in metadata. A catalogue region is stored as the catalogue has it,
        # whatever name or coordinates the request carried.
        location_data = get_region_catalog().canonical_location(request.location.model_dump(exclude_none=True))
        existing_metadata["location"] = location_data
        
        # Keep zone in step with location.city: the clusters table shows zone.
        new_zone = location_data["city"]

        cert_manager.save_cluster_metadata(
            cluster_id=cluster_id,
            endpoint=existing_metadata.get("endpoint", ""),
            display_name=existing_metadata.get("display_name", cluster_id),
            zone=new_zone,
            # Preserve whatever is stored, including "not determined yet"; defaulting to "LXD"
            # would store a guess.
            cluster_type=existing_metadata.get("type"),
            merchant_id=existing_metadata.get("merchant_id"),
            location=location_data
        )
        
        from app.core.cache import get_cache_manager
        cache = get_cache_manager()
        if cache:
            cache.invalidate_resource_cache("clusters", "regions")
            cache.invalidate_cluster_cache(cluster_id)

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
            detail=f"Could not update location: {str(e)}"
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
    """Accept a rotated server certificate.

    Pinning needs a way to accept a legitimate rotation; without one, rotating a hypervisor
    certificate takes the cluster dark and the only fix is turning pinning off.

    Admin only, and it takes no fingerprint from the caller: it records what the endpoint
    presents now. A caller-supplied fingerprint would let someone pin a cluster to a certificate
    they hold.
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
        TRUST_FIELD,
        fetch_server_fingerprint,
        repin_fields,
    )

    from app.core.vault_client import VaultCASConflict

    vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
    path = f"{settings.vault_clusters_path}/{cluster_id}/metadata"
    # Uncached and versioned: the write below must land on exactly what was read.
    metadata, version = await asyncio.to_thread(vault_client.vault_read_versioned, path)
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
    # A re-pin is a new trust decision: classified like a first sighting, and the renewal lineage
    # of the previous pin is dropped so nothing approved against it still counts as this cluster.
    fields = await asyncio.to_thread(repin_fields, endpoint_url, fingerprint)
    for attempt in range(2):
        try:
            # Check-and-set, so a renewal decided from the pre-re-pin record cannot land on top.
            await asyncio.to_thread(
                vault_client.vault_kv_put, path, {**metadata, **fields}, cas=version
            )
            break
        except VaultCASConflict:
            if attempt:
                raise HTTPException(
                    status_code=409,
                    detail="The cluster's pin changed while it was being re-pinned. Try again.",
                )
            metadata, version = await asyncio.to_thread(vault_client.vault_read_versioned, path)
            previous = metadata.get(FINGERPRINT_FIELD)

    # The availability and region reads render the metadata this just rewrote.
    from app.core.cache import get_cache_manager
    from app.services.lxd_service import get_lxd_service

    cache = get_cache_manager()
    cache.invalidate_availability_cache()
    cache.delete_pattern("api_responses", "regions:*")
    # A client pooled before the re-pin still trusts the previous certificate, so drop it and
    # let the next call reconnect and check against the pin this just wrote.
    get_lxd_service().connection_manager.remove_connection(cluster_id)

    logger.warning(
        "Re-pinned %s from %s… to %s… (%s) by %s",
        cluster_id,
        (previous or "<unpinned>")[:16],
        fingerprint[:16],
        fields[TRUST_FIELD],
        _rbac.email,
    )
    try:
        from app.services.activity_logger import get_activity_logger

        get_activity_logger().log_activity(
            action="cluster.server_certificate.repin",
            target_type="cluster",
            target_id=cluster_id,
            target_host=endpoint_url,
            result="success",
            details={
                "previous_fingerprint": (previous or "")[:16] or None,
                "fingerprint": fingerprint[:16],
                "trust": fields[TRUST_FIELD],
            },
        )
    except Exception:
        logger.debug("Could not audit the re-pin of %s", cluster_id, exc_info=True)
    return {
        "cluster_id": cluster_id,
        "previous_fingerprint": (previous[:16] + "...") if previous else None,
        "fingerprint": fingerprint[:16] + "...",
        "repinned_by": _rbac.email,
    }
