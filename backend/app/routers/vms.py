"""
API Router for VM operations.
Provides endpoints for listing, creating, and managing VMs across LXD nodes.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional
import asyncio
import hashlib
import json
import logging
import secrets
from urllib.parse import urlparse

from app.models.schemas import (
    VMListResponse,
    VMInfo,
    VMCreateRequest,
    InstanceCreateRequest,
    VMCreateResponse,
    VMActionRequest,
    VMActionResponse,
    VMStatus,
    ErrorResponse,
    InstanceMetrics,
)
from app.services.lxd_service import (
    LXDService,
    LXDConnectionError,
    LXDOperationError,
    LXDGracefulTimeoutError,
    get_lxd_service,
)
from app.core.single_flight import single_flight
from app.core.blocking import BlockingPoolFull, BlockingTimeout
from app.core.error_sanitizer import sanitize_error
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.core.database import get_db
from app.core.org_scope import (
    can_view_instance,
    org_scope_dependency,
    verify_cluster_access,
    get_scope_cache_key,
    check_tenant_permission,
    verify_instance_view_access,
    verify_project_access,
    verify_tenant_permission,
)
from app.core.config import get_settings
from app.core.cache import get_cache_manager
from app.services.activity_logger import get_activity_logger
from app.services.tag_service import TagService
from app.routers.tags import get_tag_service_dep
from app.core.org_scope import project_is_visible
from app.services.instance_security_policy import (
    ClusterWritePolicy,
    InstanceSecurityPolicyViolation,
    resolve_cluster_write_policy,
    validate_instance_payload,
    validate_target_project_isolation,
)
from app.services.instance_job_identity import resolve_instance_job_organization
from app.services.instance_specifications import (
    SpecificationConflict,
    create_operation_job,
    get_operation_job,
)
from app.services.lxd.create_preflight import (
    CreatePreflightEvaluation,
    CreatePreflightUnavailable,
    evaluate_instance_create,
)
from app.services.lxd.instance import CreateSubmissionUnknown
from app.services.shared_cluster_audit import serialize_cluster_configuration
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/vms",
    tags=["Virtual Machines"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)


_LEGACY_CREATE_WAIT_SECONDS = 31 * 60
_LEGACY_CREATE_INITIAL_POLL_SECONDS = 0.25
_LEGACY_CREATE_MAX_POLL_SECONDS = 2.0


def _policy_refusal(exc: InstanceSecurityPolicyViolation) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.as_detail())


async def _cluster_write_policy(
    user: AuthenticatedUser,
    host: str,
    db: AsyncSession,
) -> ClusterWritePolicy:
    try:
        return await resolve_cluster_write_policy(user, host, db)
    except InstanceSecurityPolicyViolation as exc:
        raise _policy_refusal(exc) from exc
    except Exception as exc:
        logger.error("Could not verify write policy for cluster %s: %s", host, type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="Cluster ownership could not be verified. No change was sent.",
        ) from exc


def _legacy_create_request_hash(request: VMCreateRequest) -> str:
    """Bind the durable job to this request without persisting cloud-init or raw config."""
    encoded = json.dumps(
        request.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _legacy_create_requester(user: AuthenticatedUser) -> str:
    requester = str(getattr(user, "user_id", None) or "").strip()
    if not requester:
        requester = str(getattr(user, "email", None) or "").strip()
    if not requester:
        raise HTTPException(status_code=401, detail="Authenticated user identity is unavailable")
    return requester


def _validate_legacy_image_server(image_server: Optional[str]) -> None:
    """Keep the durable legacy path from turning LXD's remote pull into SSRF."""
    if not image_server:
        return
    from app.services.remote_image_catalog import REMOTE_IMAGE_SERVERS

    allowed_hosts = {urlparse(row["server"]).hostname for row in REMOTE_IMAGE_SERVERS}
    parsed = urlparse(image_server)
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid image_server. Allowed remote image servers: "
                + ", ".join(sorted(host for host in allowed_hosts if host))
            ),
        )


async def _legacy_effective_payload(
    request: VMCreateRequest,
    lxd_service: LXDService,
) -> CreatePreflightEvaluation:
    """Resolve profiles in daemon order and apply local overrides, including ``type:none``."""
    try:
        evaluation = await evaluate_instance_create(
            lxd_service,
            InstanceCreateRequest.model_validate(request.model_dump(mode="python")),
        )
    except (BlockingPoolFull, BlockingTimeout) as exc:
        logger.warning("Legacy create profile resolution exhausted capacity: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Effective profile configuration is busy or timed out. Try again shortly.",
        ) from exc
    except (CreatePreflightUnavailable, LXDConnectionError) as exc:
        safe_error = sanitize_error(str(exc), limit=500)
        logger.warning("Legacy create profile resolution unavailable: %s", safe_error)
        raise HTTPException(
            status_code=503,
            detail=(safe_error or "Effective profile configuration could not be verified."),
        ) from exc
    if evaluation.response.blockers:
        codes = {issue.code for issue in evaluation.response.blockers}
        raise HTTPException(
            status_code=409 if "INSTANCE_ALREADY_EXISTS" in codes else 400,
            detail={
                "code": "CREATE_PREFLIGHT_FAILED",
                "message": "The instance cannot be created until the blockers are resolved.",
                "blockers": [
                    issue.model_dump(mode="json")
                    for issue in evaluation.response.blockers
                ],
                "plan_revision": evaluation.response.plan_revision,
            },
        )
    # NETWORKLESS remains a warning on the synchronous compatibility endpoint. Its callers do
    # not have an acknowledgement field, and isolated instances were already a supported use.
    return evaluation


async def _wait_for_legacy_create(
    job_id: str,
    request: VMCreateRequest,
    lxd_service: LXDService,
) -> VMInfo:
    """Preserve the synchronous legacy response while the durable worker owns the outcome."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _LEGACY_CREATE_WAIT_SECONDS
    poll_seconds = _LEGACY_CREATE_INITIAL_POLL_SECONDS
    while True:
        row = await get_operation_job(job_id)
        if row is None:
            raise LXDOperationError("The durable create result is unavailable.")
        if row.status == "success":
            vm = await lxd_service.get_vm(request.name, request.host, request.project)
            if vm is None:
                raise LXDOperationError(
                    "The create succeeded, but the instance readback is unavailable."
                )
            return vm
        if row.status in {"failure", "partial"}:
            raise LXDOperationError(
                sanitize_error(
                    row.error
                    or (
                        "The instance was created only partially."
                        if row.status == "partial"
                        else "The daemon refused the instance create."
                    ),
                    limit=1000,
                )
            )
        if row.status == "unknown":
            timeout = BlockingTimeout("legacy create outcome", _LEGACY_CREATE_WAIT_SECONDS)
            raise LXDOperationError(
                "The create outcome is unknown; it may still complete, so check the "
                "instance list before retrying."
            ) from timeout

        remaining = deadline - loop.time()
        if remaining <= 0:
            timeout = BlockingTimeout("legacy create outcome", _LEGACY_CREATE_WAIT_SECONDS)
            raise LXDOperationError(
                "VM creation timed out; it may still complete, so check the instance list "
                "before retrying."
            ) from timeout
        await asyncio.sleep(min(poll_seconds, remaining))
        poll_seconds = min(poll_seconds * 2, _LEGACY_CREATE_MAX_POLL_SECONDS)


async def _create_vm_under_policy(
    request: VMCreateRequest,
    lxd_service: LXDService,
    user: AuthenticatedUser,
    db: AsyncSession,
) -> VMInfo:
    """Durably admit and submit a legacy create, then await its verified result."""
    async with serialize_cluster_configuration(db, request.host):
        policy = await _cluster_write_policy(user, request.host, db)
        evaluation = await _legacy_effective_payload(request, lxd_service)
        validate_target_project_isolation(policy, evaluation.target_project_config)
        validate_instance_payload(
            policy,
            config=evaluation.effective_config,
            devices=evaluation.effective_devices,
        )

        requester = _legacy_create_requester(user)
        target_organization = await resolve_instance_job_organization(
            db,
            cluster_id=request.host,
            project=request.project,
        )
        try:
            job, _created = await create_operation_job(
                kind="create",
                host=request.host,
                project=request.project,
                instance_name=request.name,
                requester=requester,
                target_organization_id=target_organization.organization_id,
                target_organization_slug=target_organization.organization_slug,
                target_organization_resolution=target_organization.resolution,
                idempotency_key=f"legacy-vms:{secrets.token_hex(24)}",
                payload_hash=_legacy_create_request_hash(request),
                requested={
                    "instance_type": request.instance_type,
                    "start_on_create": bool(request.start_on_create),
                    "profiles": list(request.profiles or ["default"]),
                    "legacy_synchronous": True,
                },
                instance_type=request.instance_type,
            )
        except SpecificationConflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        job_id = job.job_id

        activity_logger.log_activity(
            action="instance.create",
            target_type="instance",
            target_id=request.name,
            target_host=request.host,
            target_project=request.project,
            result="accepted",
            details={
                "instance_type": request.instance_type,
                "profiles": request.profiles or ["default"],
                "project": request.project,
                "start_on_create": request.start_on_create,
                "job_id": job_id,
                "legacy_api": True,
            },
            actor_user_id=requester,
            target_organization_id=target_organization.organization_id,
            target_organization_slug=target_organization.organization_slug,
            target_organization_resolution=target_organization.resolution,
        )

        try:
            await lxd_service.create_instance(
                name=request.name,
                host=request.host,
                image=request.image,
                instance_type=request.instance_type,
                profiles=request.profiles,
                config=request.config,
                cloud_init=request.cloud_init,
                devices=request.devices,
                start_on_create=request.start_on_create,
                project=request.project,
                image_server=request.image_server,
                image_protocol=request.image_protocol,
                description=request.description,
                source_type="image",
                job_id=job_id,
            )
        except CreateSubmissionUnknown:
            # The job is already ``unknown`` and remains a pre-share admission blocker until
            # exact operation/readback reconciliation reaches a terminal verdict.
            pass
        except (LXDConnectionError, LXDOperationError) as exc:
            await lxd_service.fail_create_job(
                job_id,
                error=sanitize_error(str(exc), limit=1000),
                error_code=(
                    "LXD_CONNECTION_ERROR"
                    if isinstance(exc, LXDConnectionError)
                    else "LXD_OPERATION_ERROR"
                ),
            )
            raise
        except Exception as exc:
            await lxd_service.fail_create_job(
                job_id,
                error=sanitize_error(str(exc), limit=1000),
                error_code="UNEXPECTED_ERROR",
            )
            raise

        # PostgreSQL's ownership lock is transaction-scoped. End the read-only admission
        # transaction after daemon submission so the synchronous compatibility wait does not
        # hold one database connection and block unrelated ownership work for up to 31 minutes.
        await db.commit()

    # Do not hold the ownership transaction for a remote image pull. The durable active row is
    # now the admission guard, and its background watcher survives client cancellation.
    return await _wait_for_legacy_create(job_id, request, lxd_service)


@router.get(
    "",
    response_model=VMListResponse,
    summary="List all VMs",
    description="Get a list of all virtual machines across all connected LXD nodes.",
    responses={
        200: {"description": "List of VMs retrieved successfully"},
        500: {"model": ErrorResponse, "description": "Server error"},
    }
)
@single_flight("vms:list")
async def list_vms(
    host: Optional[str] = Query(
        None, 
        description="Filter by specific host node"
    ),
    status: Optional[VMStatus] = Query(
        None, 
        description="Filter by VM status"
    ),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    limit: int = Query(0, ge=0, le=1000, description="Page size (0 = return all)"),
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """List all VMs across all LXD nodes with optional filtering."""
    if host:
        verify_cluster_access(host)
    # Try cache first (30 second cache for VM list)
    cache = get_cache_manager()
    cache_key = get_scope_cache_key(f"vms:list:host={host}:status={status}:o={offset}:l={limit}")
    cached_response = None if fresh else cache.get("api_responses", cache_key)
    if cached_response is not None:
        logger.debug(f"Returning cached VMs list for {cache_key}")
        # Reconstruct from cached dict
        return VMListResponse(**cached_response)
    
    try:
        vms, connected_nodes = await lxd_service.list_all_vms()
        
        # Apply filters
        filtered_vms = vms
        
        if host:
            filtered_vms = [vm for vm in filtered_vms if vm.host == host]
        
        if status:
            filtered_vms = [vm for vm in filtered_vms if vm.status == status]

        # important: check_tenant_permission alone does not cover a Partner. The tenant ACL is
        # always None for Admin and Partner, so it passes everything for them, and a Partner is
        # scoped by cluster rather than by project. This list spans every project, so without
        # the project bound it named the other organization's instances on a shared cluster.
        # This is the same gate the /instances list applies; this route is a second, parallel
        # instance list and was missed when that one was fixed.
        filtered_vms = [
            vm for vm in filtered_vms
            if project_is_visible(vm.host, vm.project or "default")
            and check_tenant_permission(vm.host, vm.project or "default", vm.name, "view")
        ]
        
        # Count LXD vs MicroCloud vs Incus nodes
        lxd_count = 0
        microcloud_count = 0
        incus_count = 0
        
        # Config nodes are LXD - classify non-config nodes via Vault in parallel
        config_nodes = lxd_service.get_node_names()
        non_config_nodes = [n for n in connected_nodes if n not in config_nodes]
        lxd_count = len(connected_nodes) - len(non_config_nodes)
        
        def _classify_node(node_name: str) -> str:
            """Read node type from Vault metadata (runs in thread pool)."""
            try:
                metadata_path = f"secret/data/clusters/{node_name}/metadata"
                metadata = lxd_service.cert_manager.vault.vault_read(metadata_path)
                if metadata:
                    # `or`, not a get default: the key exists with a null value for a
                    # cluster whose type is not determined yet, and None has no .lower().
                    return (metadata.get('type') or 'lxd').lower()
            except Exception:
                pass
            return 'lxd'

        if non_config_nodes:
            import asyncio
            from concurrent.futures import ThreadPoolExecutor
            loop = asyncio.get_event_loop()
            _classify_executor = ThreadPoolExecutor(max_workers=min(len(non_config_nodes), 10))
            classify_tasks = [loop.run_in_executor(_classify_executor, _classify_node, n) for n in non_config_nodes]
            classify_results = await asyncio.gather(*classify_tasks, return_exceptions=True)
            _classify_executor.shutdown(wait=False)
            for result in classify_results:
                if isinstance(result, str):
                    if result == 'microcloud':
                        microcloud_count += 1
                    elif result == 'incus':
                        incus_count += 1
                    else:
                        lxd_count += 1
                else:
                    lxd_count += 1
        
        # important: connected_nodes is what answered, not what exists. Both it and
        # get_node_names() pass through apply_cluster_filter, so the difference is the
        # registered clusters this operator can see that stayed silent. Without it the
        # list just gets shorter and nothing says why.
        unreachable_nodes = (
            [] if host else sorted(set(lxd_service.get_node_names()) - set(connected_nodes))
        )
        response = VMListResponse(
            total=len(filtered_vms),
            vms=filtered_vms[offset:offset + limit] if limit > 0 else filtered_vms,
            nodes=connected_nodes,
            unreachable_nodes=unreachable_nodes,
            lxd_count=lxd_count,
            microcloud_count=microcloud_count,
            incus_count=incus_count,
            offset=offset,
            limit=limit,
            has_more=(offset + limit < len(filtered_vms)) if limit > 0 else False,
        )
        
        # important: see instances.py. A fan-out where no cluster answered still
        # returns 200 with an empty list, and caching that pins emptiness for the
        # full TTL. Worse on a forced refresh, which would overwrite data the
        # operator could still see with the result of an outage.
        if connected_nodes:
            cache.set("api_responses", cache_key, response.model_dump(), ttl=180)
            logger.debug(f"Cached VMs list for 180 seconds: {cache_key}")
        else:
            logger.warning("Not caching VMs list: no cluster answered")
        
        return response
        
    except LXDConnectionError as e:
        logger.warning(f"LXD connection error: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Error listing VMs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{vm_name}",
    response_model=VMInfo,
    summary="Get VM details",
    description="Get detailed information about a specific VM.",
    responses={
        200: {"description": "VM details retrieved successfully"},
        404: {"model": ErrorResponse, "description": "VM not found"},
        500: {"model": ErrorResponse, "description": "Server error"},
    }
)
async def get_vm(
    vm_name: str,
    host: str = Query(..., description="LXD host node where the VM resides"),
    project: Optional[str] = Query(
        None,
        description=(
            "LXD project the VM belongs to. A name is unique per project, not per host, so "
            "omitting this searches only projects visible to the caller."
        ),
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get one visible VM, searching only authorized projects when omitted."""
    scoped_project: Optional[str] = None
    project_filter = None
    if project is None:
        verify_cluster_access(host)
        project_filter = lambda candidate: can_view_instance(  # noqa: E731
            host, candidate, vm_name
        )
    else:
        scoped_project = verify_instance_view_access(host, project, vm_name)
    try:
        if project_filter is None:
            vm = await lxd_service.get_vm(vm_name, host, scoped_project)
        else:
            vm = await lxd_service.get_vm(
                vm_name,
                host,
                scoped_project,
                project_filter=project_filter,
            )
        
        if not vm:
            raise HTTPException(
                status_code=404, 
                detail=f"VM '{vm_name}' not found on host '{host}'"
            )
        verify_instance_view_access(host, vm.project or "default", vm.name)
        
        return vm
        
    except HTTPException:
        raise
    except LXDConnectionError as e:
        # A cluster that did not answer is not a client error and is not
        # a missing resource. Without this clause the retyped exception
        # falls to `except Exception` below and answers 500.
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting VM {vm_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "",
    response_model=VMCreateResponse,
    status_code=201,
    summary="Create a new VM",
    description="Create a new virtual machine on the specified LXD host.",
    responses={
        201: {"description": "VM created successfully"},
        400: {"model": ErrorResponse, "description": "Invalid request"},
        503: {"model": ErrorResponse, "description": "Create capacity temporarily unavailable"},
        500: {"model": ErrorResponse, "description": "Server error"},
    }
)
async def create_vm(
    request: VMCreateRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Create a new VM on the specified host."""
    project = verify_project_access(request.host, request.project)
    request = request.model_copy(update={"project": project})
    _validate_legacy_image_server(request.image_server)
    try:
        # Validate host
        valid_nodes = lxd_service.get_node_names()
        if request.host not in valid_nodes:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid host. Valid hosts: {valid_nodes}"
            )

        vm = await _create_vm_under_policy(
            request,
            lxd_service,
            _rbac,
            db,
        )

        # Readers hold a cached list; without this the change is invisible until the TTL.
        get_cache_manager().invalidate_instance_cache()
        return VMCreateResponse(
            success=True,
            message=f"VM '{request.name}' created successfully on {request.host}",
            vm=vm,
        )
        
    except LXDOperationError as e:
        temporarily_unavailable = isinstance(
            e.__cause__, (BlockingTimeout, BlockingPoolFull)
        )
        raise HTTPException(
            status_code=503 if temporarily_unavailable else 400,
            detail=str(e),
        )
    except LXDConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except HTTPException:
        # important: let a deliberate status through. This try raises HTTPException
        # itself, and the broad handler below rewrote it, so an unreachable cluster
        # answered 500 or 404 instead of 503 and a tenant ACL denial answered 500
        # instead of 403.
        raise
    except InstanceSecurityPolicyViolation as exc:
        raise _policy_refusal(exc) from exc
    except Exception as e:
        logger.error(f"Error creating VM: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/{vm_name}/start",
    response_model=VMActionResponse,
    summary="Start a VM",
    description="Start a stopped virtual machine.",
    responses={
        200: {"description": "VM started successfully"},
        400: {"model": ErrorResponse, "description": "Operation failed"},
        404: {"model": ErrorResponse, "description": "VM not found"},
    }
)
async def start_vm(
    vm_name: str,
    request: VMActionRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Start a VM."""
    # important: ONE project value is authorized and acted on. This checked the ACL against
    # `request.project or "default"` and then handed `request.project` to the service, which
    # searches EVERY project when it is None and returns the first hit. A caller holding
    # `operate` on `default` was authorized there while the action ran against whichever project
    # LXD listed first, which on a shared cluster is another tenant's instance.
    #
    # note: this router is the one the console actually drives. The identical routes under
    # /instances carry the same fix, and fixing only those would have left the live path open.
    project = verify_instance_view_access(request.host, request.project, vm_name)
    # important: enforce BEFORE the try - a 403 raised inside it would be swallowed and re-raised 500.
    if get_settings().tenant_perm_enforcement_enabled:
        verify_tenant_permission(request.host, project, vm_name, "operate")
    try:
        vm = await lxd_service.start_vm(
            name=vm_name,
            host=request.host,
            force=request.force,
            project=project,
        )
        get_cache_manager().invalidate_instance_cache()
        
        # Log activity: VM start success
        activity_logger.log_activity(
            action="vm.start",
            target_type="vm",
            target_id=vm_name,
            target_host=request.host,
            target_project=project,
            result="success"
        )
        
        return VMActionResponse(
            success=True,
            message=f"VM '{vm_name}' started successfully",
            vm_name=vm_name,
            action="start",
            new_status=vm.status,
        )
        
    except LXDConnectionError as e:
        # A cluster that did not answer is not a client error and is not
        # a missing resource. Without this clause the retyped exception
        # falls to `except Exception` below and answers 500.
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        # Log activity: VM start failure
        activity_logger.log_activity(
            action="vm.start",
            target_type="vm",
            target_id=vm_name,
            target_host=request.host,
            target_project=project,
            result="failure",
            error_message=str(e)
        )
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error starting VM {vm_name}: {e}")
        # Log activity: VM start failure
        activity_logger.log_activity(
            action="vm.start",
            target_type="vm",
            target_id=vm_name,
            target_host=request.host,
            target_project=project,
            result="failure",
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/{vm_name}/stop",
    response_model=VMActionResponse,
    summary="Stop a VM",
    description="Stop a running virtual machine.",
    responses={
        200: {"description": "VM stopped successfully"},
        400: {"model": ErrorResponse, "description": "Operation failed"},
        404: {"model": ErrorResponse, "description": "VM not found"},
    }
)
async def stop_vm(
    vm_name: str,
    request: VMActionRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Stop a VM."""
    # important: ONE project value is authorized and acted on. This checked the ACL against
    # `request.project or "default"` and then handed `request.project` to the service, which
    # searches EVERY project when it is None and returns the first hit. A caller holding
    # `operate` on `default` was authorized there while the action ran against whichever project
    # LXD listed first, which on a shared cluster is another tenant's instance.
    #
    # note: this router is the one the console actually drives. The identical routes under
    # /instances carry the same fix, and fixing only those would have left the live path open.
    project = verify_instance_view_access(request.host, request.project, vm_name)
    if get_settings().tenant_perm_enforcement_enabled:
        verify_tenant_permission(request.host, project, vm_name, "operate")
    try:
        vm = await lxd_service.stop_vm(
            name=vm_name,
            host=request.host,
            force=request.force,
            timeout=request.timeout,
            project=project,
        )
        get_cache_manager().invalidate_instance_cache()
        
        # Log activity: VM stop success
        activity_logger.log_activity(
            action="vm.stop",
            target_type="vm",
            target_id=vm_name,
            target_host=request.host,
            target_project=project,
            result="success",
            details={"force": request.force, "timeout": request.timeout}
        )
        
        return VMActionResponse(
            success=True,
            message=f"VM '{vm_name}' stopped successfully",
            vm_name=vm_name,
            action="stop",
            new_status=vm.status,
        )

    except LXDGracefulTimeoutError as e:
        # Must precede LXDOperationError (its parent). The guest ignored the ACPI
        # shutdown (commonly a VM with no OS, or one at a boot menu); recoverable
        # by retrying with force, so return 504 + a GRACEFUL_TIMEOUT audit signal
        # instead of a bare 400 -- matching /instances/{name}/stop.
        logger.warning(f"Graceful stop timed out for VM {vm_name}: {e}")
        activity_logger.log_activity(
            action="vm.stop",
            target_type="vm",
            target_id=vm_name,
            target_host=request.host,
            target_project=project,
            result="failure",
            error_code="GRACEFUL_TIMEOUT",
            error_message=str(e),
        )
        raise HTTPException(status_code=504, detail=str(e))
    except LXDConnectionError as e:
        # A cluster that did not answer is not a client error and is not
        # a missing resource. Without this clause the retyped exception
        # falls to `except Exception` below and answers 500.
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        # Log activity: VM stop failure
        activity_logger.log_activity(
            action="vm.stop",
            target_type="vm",
            target_id=vm_name,
            target_host=request.host,
            target_project=project,
            result="failure",
            error_message=str(e)
        )
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error stopping VM {vm_name}: {e}")
        # Log activity: VM stop failure
        activity_logger.log_activity(
            action="vm.stop",
            target_type="vm",
            target_id=vm_name,
            target_host=request.host,
            target_project=project,
            result="failure",
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/{vm_name}/restart",
    response_model=VMActionResponse,
    summary="Restart a VM",
    description="Restart a virtual machine.",
    responses={
        200: {"description": "VM restarted successfully"},
        400: {"model": ErrorResponse, "description": "Operation failed"},
        404: {"model": ErrorResponse, "description": "VM not found"},
    }
)
async def restart_vm(
    vm_name: str,
    request: VMActionRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Restart a VM."""
    # important: ONE project value is authorized and acted on. This checked the ACL against
    # `request.project or "default"` and then handed `request.project` to the service, which
    # searches EVERY project when it is None and returns the first hit. A caller holding
    # `operate` on `default` was authorized there while the action ran against whichever project
    # LXD listed first, which on a shared cluster is another tenant's instance.
    #
    # note: this router is the one the console actually drives. The identical routes under
    # /instances carry the same fix, and fixing only those would have left the live path open.
    project = verify_instance_view_access(request.host, request.project, vm_name)
    if get_settings().tenant_perm_enforcement_enabled:
        verify_tenant_permission(request.host, project, vm_name, "operate")
    try:
        vm = await lxd_service.restart_vm(
            name=vm_name,
            host=request.host,
            force=request.force,
            timeout=request.timeout,
            project=project,
        )
        get_cache_manager().invalidate_instance_cache()
        
        # Log activity: VM restart success
        activity_logger.log_activity(
            action="vm.restart",
            target_type="vm",
            target_id=vm_name,
            target_host=request.host,
            target_project=project,
            result="success",
            details={"force": request.force, "timeout": request.timeout}
        )
        
        return VMActionResponse(
            success=True,
            message=f"VM '{vm_name}' restarted successfully",
            vm_name=vm_name,
            action="restart",
            new_status=vm.status,
        )

    except LXDGracefulTimeoutError as e:
        # Must precede LXDOperationError (its parent). Guest ignored the ACPI
        # shutdown during the restart; recoverable with force. 504 + GRACEFUL_TIMEOUT
        # audit signal, matching /instances/{name}/restart.
        logger.warning(f"Graceful restart timed out for VM {vm_name}: {e}")
        activity_logger.log_activity(
            action="vm.restart",
            target_type="vm",
            target_id=vm_name,
            target_host=request.host,
            target_project=project,
            result="failure",
            error_code="GRACEFUL_TIMEOUT",
            error_message=str(e),
        )
        raise HTTPException(status_code=504, detail=str(e))
    except LXDConnectionError as e:
        # A cluster that did not answer is not a client error and is not
        # a missing resource. Without this clause the retyped exception
        # falls to `except Exception` below and answers 500.
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        # Log activity: VM restart failure
        activity_logger.log_activity(
            action="vm.restart",
            target_type="vm",
            target_id=vm_name,
            target_host=request.host,
            target_project=project,
            result="failure",
            error_message=str(e)
        )
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error restarting VM {vm_name}: {e}")
        # Log activity: VM restart failure
        activity_logger.log_activity(
            action="vm.restart",
            target_type="vm",
            target_id=vm_name,
            target_host=request.host,
            target_project=project,
            result="failure",
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.delete(
    "/{vm_name}",
    response_model=VMActionResponse,
    summary="Delete a VM",
    description="Delete a virtual machine. Use force=true to delete running VMs.",
    responses={
        200: {"description": "VM deleted successfully"},
        400: {"model": ErrorResponse, "description": "Operation failed"},
        404: {"model": ErrorResponse, "description": "VM not found"},
    }
)
async def delete_vm(
    vm_name: str,
    host: str = Query(..., description="LXD host node"),
    force: bool = Query(False, description="Force delete running VM"),
    project: Optional[str] = Query(None, description="LXD project name"),
    lxd_service: LXDService = Depends(get_lxd_service),
    tag_service: TagService = Depends(get_tag_service_dep),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Delete a VM."""
    project = verify_project_access(host, project)
    try:
        await lxd_service.delete_vm(
            name=vm_name,
            host=host,
            force=force,
            project=project,
        )
        get_cache_manager().invalidate_instance_cache()

        # Clean up the instance's tag rows so the tag store doesn't drift from the
        # deleted instance. Best-effort, because the instance is already gone.
        try:
            await tag_service.remove_resource_assignments(
                "instance", vm_name,
                resource_host=host, resource_project=project or "default",
            )
        except Exception as tag_err:
            logger.warning(
                f"Failed to clean up tags for deleted instance {vm_name}: {tag_err}"
            )

        # Log activity: VM delete success
        activity_logger.log_activity(
            action="vm.delete",
            target_type="vm",
            target_id=vm_name,
            target_host=host,
            target_project=project or "default",
            result="success",
            details={"force": force, "project": project}
        )
        
        return VMActionResponse(
            success=True,
            message=f"VM '{vm_name}' deleted successfully",
            vm_name=vm_name,
            action="delete",
        )
        
    except LXDConnectionError as e:
        # A cluster that did not answer is not a client error and is not
        # a missing resource. Without this clause the retyped exception
        # falls to `except Exception` below and answers 500.
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        # Log activity: VM delete failure
        activity_logger.log_activity(
            action="vm.delete",
            target_type="vm",
            target_id=vm_name,
            target_host=host,
            target_project=project or "default",
            result="failure",
            error_message=str(e)
        )
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error deleting VM {vm_name}: {e}")
        # Log activity: VM delete failure
        activity_logger.log_activity(
            action="vm.delete",
            target_type="vm",
            target_id=vm_name,
            target_host=host,
            target_project=project or "default",
            result="failure",
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{host}/{instance_name}/metrics",
    response_model=InstanceMetrics,
    summary="Get instance metrics",
    description="Get real-time metrics for a specific instance including CPU, memory, disk, and network usage.",
    responses={
        200: {"description": "Instance metrics retrieved successfully"},
        404: {"model": ErrorResponse, "description": "Instance not found"},
        500: {"model": ErrorResponse, "description": "Server error"},
    }
)
async def get_instance_metrics(
    host: str,
    instance_name: str,
    project: str = Query("default", description="LXD project name"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get real-time metrics for an instance."""
    scoped_project = verify_instance_view_access(host, project, instance_name)
    try:
        metrics = await lxd_service.get_instance_metrics(
            host=host,
            instance_name=instance_name,
            project=scoped_project,
        )
        
        return metrics
        
    except LXDConnectionError as e:
        # A cluster that did not answer is not a client error and is not
        # a missing resource. Without this clause the retyped exception
        # falls to `except Exception` below and answers 500.
        raise HTTPException(status_code=503, detail=str(e))
    except (BlockingTimeout, BlockingPoolFull) as e:
        logger.warning("Instance metrics temporarily unavailable: %s", e)
        raise HTTPException(status_code=503, detail="Instance metrics are temporarily unavailable")
    except LXDOperationError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting metrics for {instance_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
