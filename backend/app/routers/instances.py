"""
API Router for Instance operations.
Provides endpoints for managing LXD instances (containers and VMs).
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional
from urllib.parse import urlparse
import logging

from app.models.schemas import (
    VMListResponse,
    VMInfo,
    InstanceCreateRequest,
    VMCreateResponse,
    VMActionRequest,
    VMActionResponse,
    VMStatus,
    InstanceMetrics,
)
from app.services.lxd_service import (
    LXDService,
    LXDConnectionError,
    LXDOperationError,
    LXDGracefulTimeoutError,
    get_lxd_service,
)
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.core.org_scope import org_scope_dependency, verify_cluster_access, get_scope_cache_key, check_tenant_permission, verify_tenant_permission
from app.core.config import get_settings
from app.services.orcahub_webhook import schedule_orcahub_event
from app.core.cache import get_cache_manager
from app.services.activity_logger import get_activity_logger
from app.services.tag_service import TagService
from app.routers.tags import get_tag_service_dep

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/instances",
    tags=["Instances"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)


def _validate_image_server(image_server: Optional[str]) -> None:
    """Guard against SSRF on instance creation.

    `image_server` is handed straight to the LXD daemon, which then fetches the
    image from that URL. Since `POST /instances` only requires Partner, an
    unvalidated value lets a Partner point the daemon at internal/metadata
    endpoints. Restrict it to the same public simplestreams catalogs the image
    picker uses (the `/images/remote` allowlist), over https only.
    """
    if not image_server:
        return
    from app.routers.images import REMOTE_IMAGE_SERVERS

    allowed_hosts = {
        urlparse(s["server"]).hostname for s in REMOTE_IMAGE_SERVERS
    }
    parsed = urlparse(image_server)
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid image_server. Allowed remote image servers: "
                + ", ".join(sorted(h for h in allowed_hosts if h))
            ),
        )


@router.get(
    "",
    response_model=VMListResponse,
    summary="List all instances",
    description="Get a list of all instances (containers and VMs) across all connected LXD nodes.",
)
async def list_instances(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    status: Optional[VMStatus] = Query(None, description="Filter by instance status"),
    instance_type: Optional[str] = Query(None, description="Filter by type: container or virtual-machine"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    limit: int = Query(0, ge=0, le=1000, description="Page size (0 = return all)"),
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """List all instances across all nodes with optional filters."""
    if host:
        verify_cluster_access(host)
    # Try cache first (scope+ACL aware, 30s TTL)
    cache = get_cache_manager()
    cache_key = get_scope_cache_key(f"instances:list:host={host}:status={status}:type={instance_type}:o={offset}:l={limit}")
    cached_response = None if fresh else cache.get("api_responses", cache_key)
    if cached_response is not None:
        logger.debug(f"Returning cached instances list for {cache_key}")
        return VMListResponse(**cached_response)
    
    try:
        all_vms, connected_nodes = await lxd_service.list_all_vms()
        
        # Filter by host if provided
        if host:
            all_vms = [vm for vm in all_vms if vm.host == host]
        
        # Filter by status if provided
        if status:
            all_vms = [vm for vm in all_vms if vm.status == status]
        
        # Filter by instance type if provided
        if instance_type:
            all_vms = [vm for vm in all_vms if vm.type == instance_type]
        
        # Apply tenant ACL filtering (project + instance level)
        all_vms = [
            vm for vm in all_vms
            if check_tenant_permission(
                vm.host,
                vm.project or "default",
                vm.name,
                "view",
            )
        ]
        
        response = VMListResponse(
            total=len(all_vms),
            vms=all_vms[offset:offset + limit] if limit > 0 else all_vms,
            nodes=connected_nodes,
            offset=offset,
            limit=limit,
            has_more=(offset + limit < len(all_vms)) if limit > 0 else False,
        )
        
        # important: do not remember an answer nobody gave. When every cluster is
        # unreachable the connection breaker short-circuits the fan-out and this
        # endpoint still returns 200 with an empty list, so caching it would pin
        # emptiness for the whole TTL and, on a forced refresh, overwrite good
        # data the operator could still see. No node answered means no result.
        if connected_nodes:
            cache.set("api_responses", cache_key, response.model_dump(), ttl=30)
            logger.debug(f"Cached instances list for 30 seconds: {cache_key}")
        else:
            logger.warning("Not caching instances list: no cluster answered")
        
        return response
    except LXDConnectionError as e:
        logger.error(f"LXD connection error: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Error listing instances: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{instance_name}",
    response_model=VMInfo,
    summary="Get instance details",
    description="Get detailed information about a specific instance.",
)
async def get_instance(
    instance_name: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific instance."""
    verify_cluster_access(host)
    try:
        vm = await lxd_service.get_vm(instance_name, host)
        if not vm:
            raise HTTPException(status_code=404, detail=f"Instance {instance_name} not found on {host}")
        # enforce tenant ACL on individual instance access
        verify_tenant_permission(host, vm.project or "default", vm.name, "view")
        return vm
    except HTTPException:
        # 404 (not found) / 403 (ACL) — don't let the generic handler mask these.
        raise
    except LXDConnectionError as e:
        logger.error(f"LXD connection error: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting instance {instance_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "",
    response_model=VMCreateResponse,
    summary="Create new instance",
    description="Create a new instance (container or VM).",
)
async def create_instance(
    request: InstanceCreateRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Create a new instance."""
    verify_cluster_access(request.host)
    _validate_image_server(request.image_server)
    try:
        result = await lxd_service.create_instance(
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
            source_type=request.source_type,
        )

        # Log activity: instance creation success
        activity_logger.log_activity(
            action="instance.create",
            target_type="instance",
            target_id=request.name,
            target_host=request.host,
            result="success",
            details={
                "image": request.image,
                "instance_type": request.instance_type,
                "profiles": request.profiles,
                "project": request.project,
                "start_on_create": request.start_on_create,
            }
        )
        
        schedule_orcahub_event("instance.created", request.host, None, request.name, "Running" if request.start_on_create else "Stopped")
        # Non-blocking: create_vm returns the operation id; the instance pulls/
        # creates in the background and appears via the polling instance list.
        # Readers hold a cached list; without this the change is invisible until the TTL.
        get_cache_manager().invalidate_instance_cache()
        return VMCreateResponse(
            success=True,
            message=f"Creating instance '{request.name}' on {request.host}",
            vm=None,
            operation_id=result,
        )
    except LXDOperationError as e:
        logger.error(f"LXD operation error: {e}")

        # Log activity: instance creation failure
        activity_logger.log_activity(
            action="instance.create",
            target_type="instance",
            target_id=request.name,
            target_host=request.host,
            result="failure",
            error_code="LXD_OPERATION_ERROR",
            error_message=str(e),
            details={"image": request.image, "instance_type": request.instance_type}
        )

        # Map to a meaningful status instead of a blanket 500.
        low = str(e).lower()
        if "already exists" in low:
            status = 409
        elif "not found" in low:
            status = 404
        elif "connect" in low or "not available" in low or "could not open project" in low:
            status = 503
        else:
            status = 400
        raise HTTPException(status_code=status, detail=str(e))
    except LXDConnectionError as e:
        # Cluster unreachable (e.g. during the duplicate-name exists() check or
        # the create POST) — 503, matching the other instance endpoints.
        logger.error(f"LXD connection error creating instance: {e}")
        activity_logger.log_activity(
            action="instance.create",
            target_type="instance",
            target_id=request.name,
            target_host=request.host,
            result="failure",
            error_code="LXD_CONNECTION_ERROR",
            error_message=str(e),
        )
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating instance: {e}")

        # Log activity: instance creation failure
        activity_logger.log_activity(
            action="instance.create",
            target_type="instance",
            target_id=request.name,
            target_host=request.host,
            result="failure",
            error_code="UNEXPECTED_ERROR",
            error_message=str(e)
        )
        
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/{instance_name}/start",
    response_model=VMActionResponse,
    summary="Start instance",
    description="Start a stopped instance.",
)
async def start_instance(
    instance_name: str,
    request: VMActionRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Start an instance."""
    verify_cluster_access(request.host)
    # important: enforce BEFORE the try - a 403 raised inside it would be swallowed and re-raised 500.
    if get_settings().tenant_perm_enforcement_enabled:
        verify_tenant_permission(request.host, request.project or "default", instance_name, "operate")
    try:
        vm = await lxd_service.start_vm(
            instance_name,
            request.host,
            force=False,
            project=request.project,
        )
        get_cache_manager().invalidate_instance_cache()
        
        # Log activity: instance start success
        activity_logger.log_activity(
            action="instance.start",
            target_type="instance",
            target_id=instance_name,
            target_host=request.host,
            result="success"
        )
        
        return VMActionResponse(
            success=True,
            message=f"Instance {instance_name} started successfully",
            vm_name=instance_name,
            action="start",
            new_status=vm.status
        )
    except Exception as e:
        logger.error(f"Error starting instance {instance_name}: {e}")
        
        # Log activity: instance start failure
        activity_logger.log_activity(
            action="instance.start",
            target_type="instance",
            target_id=instance_name,
            target_host=request.host,
            result="failure",
            error_message=str(e)
        )
        
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/{instance_name}/stop",
    response_model=VMActionResponse,
    summary="Stop instance",
    description="Stop a running instance.",
)
async def stop_instance(
    instance_name: str,
    request: VMActionRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Stop an instance."""
    verify_cluster_access(request.host)
    if get_settings().tenant_perm_enforcement_enabled:
        verify_tenant_permission(request.host, request.project or "default", instance_name, "operate")
    try:
        await lxd_service.stop_vm(
            instance_name, 
            request.host, 
            force=request.force,
            timeout=request.timeout,
            project=request.project,
        )
        get_cache_manager().invalidate_instance_cache()
        
        # Log activity: instance stop success
        activity_logger.log_activity(
            action="instance.stop",
            target_type="instance",
            target_id=instance_name,
            target_host=request.host,
            result="success",
            details={"force": request.force, "timeout": request.timeout}
        )
        
        return VMActionResponse(
            success=True,
            message=f"Instance {instance_name} stopped successfully",
            vm_name=instance_name,
            action="stop",
            new_status=VMStatus.STOPPED  # Stop always results in Stopped
        )
    except LXDGracefulTimeoutError as e:
        # The guest ignored the ACPI shutdown (commonly a VM with no OS, or one
        # sitting at a boot menu). Recoverable by retrying with force, so say so
        # rather than reporting a bare 500.
        logger.warning(f"Graceful stop timed out for {instance_name}: {e}")
        activity_logger.log_activity(
            action="instance.stop",
            target_type="instance",
            target_id=instance_name,
            target_host=request.host,
            result="failure",
            error_code="GRACEFUL_TIMEOUT",
            error_message=str(e),
        )
        raise HTTPException(status_code=504, detail=str(e))
    except Exception as e:
        logger.error(f"Error stopping instance {instance_name}: {e}")
        
        # Log activity: instance stop failure
        activity_logger.log_activity(
            action="instance.stop",
            target_type="instance",
            target_id=instance_name,
            target_host=request.host,
            result="failure",
            error_message=str(e)
        )
        
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/{instance_name}/restart",
    response_model=VMActionResponse,
    summary="Restart instance",
    description="Restart a running instance.",
)
async def restart_instance(
    instance_name: str,
    request: VMActionRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Restart an instance."""
    verify_cluster_access(request.host)
    if get_settings().tenant_perm_enforcement_enabled:
        verify_tenant_permission(request.host, request.project or "default", instance_name, "operate")
    try:
        await lxd_service.restart_vm(
            instance_name,
            request.host,
            force=request.force,
            timeout=request.timeout,
            project=request.project,
        )
        get_cache_manager().invalidate_instance_cache()
        
        # Log activity: instance restart success
        activity_logger.log_activity(
            action="instance.restart",
            target_type="instance",
            target_id=instance_name,
            target_host=request.host,
            result="success",
            details={"force": request.force, "timeout": request.timeout}
        )
        
        return VMActionResponse(
            success=True,
            message=f"Instance {instance_name} restarted successfully",
            vm_name=instance_name,
            action="restart",
            new_status=VMStatus.RUNNING  # Restart ends in Running
        )
    except LXDGracefulTimeoutError as e:
        # The guest ignored the ACPI shutdown (commonly a VM with no OS, or one
        # sitting at a boot menu). Recoverable by retrying with force, so say so
        # rather than reporting a bare 500.
        logger.warning(f"Graceful restart timed out for {instance_name}: {e}")
        activity_logger.log_activity(
            action="instance.restart",
            target_type="instance",
            target_id=instance_name,
            target_host=request.host,
            result="failure",
            error_code="GRACEFUL_TIMEOUT",
            error_message=str(e),
        )
        raise HTTPException(status_code=504, detail=str(e))
    except Exception as e:
        logger.error(f"Error restarting instance {instance_name}: {e}")
        
        # Log activity: instance restart failure
        activity_logger.log_activity(
            action="instance.restart",
            target_type="instance",
            target_id=instance_name,
            target_host=request.host,
            result="failure",
            error_message=str(e)
        )
        
        raise HTTPException(status_code=500, detail=str(e))


@router.delete(
    "/{instance_name}",
    response_model=VMActionResponse,
    summary="Delete instance",
    description="Delete an instance.",
)
async def delete_instance(
    instance_name: str,
    host: str = Query(..., description="Target host node"),
    project: Optional[str] = Query(None, description="LXD project name"),
    force: bool = Query(False, description="Force delete a running instance"),
    lxd_service: LXDService = Depends(get_lxd_service),
    tag_service: TagService = Depends(get_tag_service_dep),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Delete an instance."""
    verify_cluster_access(host)
    try:
        await lxd_service.delete_vm(instance_name, host, force=force, project=project)
        get_cache_manager().invalidate_instance_cache()

        # Clean up the instance's tag rows so the tag store doesn't drift from the
        # deleted container (best-effort — the instance is already gone).
        try:
            await tag_service.remove_resource_assignments(
                "instance", instance_name,
                resource_host=host, resource_project=project or "default",
            )
        except Exception as tag_err:
            logger.warning(
                f"Failed to clean up tags for deleted instance {instance_name}: {tag_err}"
            )

        # Log activity: instance deletion success
        activity_logger.log_activity(
            action="instance.delete",
            target_type="instance",
            target_id=instance_name,
            target_host=host,
            result="success",
            details={"force": force, "project": project},
        )

        schedule_orcahub_event("instance.deleted", host, project, instance_name)
        return VMActionResponse(
            success=True,
            message=f"Instance {instance_name} deleted successfully",
            vm_name=instance_name,
            action="delete",
        )
    except LXDConnectionError as e:
        logger.error(f"LXD connection error deleting {instance_name}: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        # Map daemon errors to meaningful codes instead of a blanket 500.
        msg = str(e)
        low = msg.lower()
        if "not found" in low:
            status = 404
        elif "running" in low or "stop it" in low:
            status = 409  # running instance, retry with force=true
        elif "connect" in low or "not available" in low:
            status = 503
        else:
            status = 400
        activity_logger.log_activity(
            action="instance.delete",
            target_type="instance",
            target_id=instance_name,
            target_host=host,
            result="failure",
            error_message=msg,
        )
        raise HTTPException(status_code=status, detail=msg)
    except Exception as e:
        logger.error(f"Error deleting instance {instance_name}: {e}")

        # Log activity: instance deletion failure
        activity_logger.log_activity(
            action="instance.delete",
            target_type="instance",
            target_id=instance_name,
            target_host=host,
            result="failure",
            error_message=str(e)
        )

        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{instance_name}/metrics",
    response_model=InstanceMetrics,
    summary="Get instance metrics",
    description="Get real-time metrics for an instance.",
)
async def get_instance_metrics(
    instance_name: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get metrics for a specific instance."""
    verify_cluster_access(host)
    try:
        metrics = await lxd_service.get_instance_metrics(host, instance_name)
        if not metrics:
            raise HTTPException(status_code=404, detail=f"Instance {instance_name} not found on {host}")
        return metrics
    except Exception as e:
        logger.error(f"Error getting metrics for {instance_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
