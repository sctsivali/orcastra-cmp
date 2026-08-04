"""
API Router for VM operations.
Provides endpoints for listing, creating, and managing VMs across LXD nodes.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional
import logging

from app.models.schemas import (
    VMListResponse,
    VMInfo,
    VMCreateRequest,
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
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.core.org_scope import (
    org_scope_dependency,
    verify_cluster_access,
    get_scope_cache_key,
    check_tenant_permission,
    verify_tenant_permission,
)
from app.core.cache import get_cache_manager
from app.services.activity_logger import get_activity_logger
from app.services.tag_service import TagService
from app.routers.tags import get_tag_service_dep

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/vms",
    tags=["Virtual Machines"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)


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
):
    """List all VMs across all LXD nodes with optional filtering."""
    if host:
        verify_cluster_access(host)
    # Try cache first (30 second cache for VM list)
    cache = get_cache_manager()
    cache_key = get_scope_cache_key(f"vms:list:host={host}:status={status}:o={offset}:l={limit}")
    cached_response = cache.get("api_responses", cache_key)
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

        filtered_vms = [
            vm for vm in filtered_vms
            if check_tenant_permission(vm.host, vm.project or "default", vm.name, "view")
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
                    return metadata.get('type', 'lxd').lower()
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
        
        response = VMListResponse(
            total=len(filtered_vms),
            vms=filtered_vms[offset:offset + limit] if limit > 0 else filtered_vms,
            nodes=connected_nodes,
            lxd_count=lxd_count,
            microcloud_count=microcloud_count,
            incus_count=incus_count,
            offset=offset,
            limit=limit,
            has_more=(offset + limit < len(filtered_vms)) if limit > 0 else False,
        )
        
        # Cache for 120 seconds (TTL)
        cache.set("api_responses", cache_key, response.model_dump(), ttl=180)
        logger.debug(f"Cached VMs list for 120 seconds: {cache_key}")
        
        return response
        
    except LXDConnectionError as e:
        logger.error(f"LXD connection error: {e}")
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
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get detailed information about a specific VM."""
    verify_cluster_access(host)
    try:
        vm = await lxd_service.get_vm(vm_name, host)
        
        if not vm:
            raise HTTPException(
                status_code=404, 
                detail=f"VM '{vm_name}' not found on host '{host}'"
            )
        verify_tenant_permission(host, vm.project or "default", vm.name, "view")
        
        return vm
        
    except HTTPException:
        raise
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
        500: {"model": ErrorResponse, "description": "Server error"},
    }
)
async def create_vm(
    request: VMCreateRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Create a new VM on the specified host."""
    verify_cluster_access(request.host)
    try:
        # Validate host
        valid_nodes = lxd_service.get_node_names()
        if request.host not in valid_nodes:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid host. Valid hosts: {valid_nodes}"
            )
        
        vm = await lxd_service.create_vm(
            name=request.name,
            host=request.host,
            image=request.image,
            instance_type=request.instance_type,
            profiles=request.profiles,
            config=request.config,
            cloud_init=request.cloud_init,
            devices=request.devices,
            start_on_create=request.start_on_create,
        )
        
        # Log activity: VM creation success
        activity_logger.log_activity(
            action="vm.create",
            target_type="vm",
            target_id=request.name,
            target_host=request.host,
            result="success",
            details={
                "image": request.image,
                "instance_type": request.instance_type,
                "profiles": request.profiles,
                "start_on_create": request.start_on_create
            }
        )
        
        return VMCreateResponse(
            success=True,
            message=f"VM '{request.name}' created successfully on {request.host}",
            vm=vm,
        )
        
    except LXDOperationError as e:
        # Log activity: VM creation failure
        activity_logger.log_activity(
            action="vm.create",
            target_type="vm",
            target_id=request.name,
            target_host=request.host,
            result="failure",
            error_code="LXD_OPERATION_ERROR",
            error_message=str(e),
            details={"image": request.image, "instance_type": request.instance_type}
        )
        raise HTTPException(status_code=400, detail=str(e))
    except LXDConnectionError as e:
        # Log activity: VM creation failure
        activity_logger.log_activity(
            action="vm.create",
            target_type="vm",
            target_id=request.name,
            target_host=request.host,
            result="failure",
            error_code="LXD_CONNECTION_ERROR",
            error_message=str(e)
        )
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating VM: {e}")
        # Log activity: VM creation failure
        activity_logger.log_activity(
            action="vm.create",
            target_type="vm",
            target_id=request.name,
            target_host=request.host,
            result="failure",
            error_code="UNEXPECTED_ERROR",
            error_message=str(e)
        )
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
    verify_cluster_access(request.host)
    try:
        vm = await lxd_service.start_vm(
            name=vm_name,
            host=request.host,
            force=request.force,
            project=request.project,
        )
        get_cache_manager().invalidate_instance_cache()
        
        # Log activity: VM start success
        activity_logger.log_activity(
            action="vm.start",
            target_type="vm",
            target_id=vm_name,
            target_host=request.host,
            result="success"
        )
        
        return VMActionResponse(
            success=True,
            message=f"VM '{vm_name}' started successfully",
            vm_name=vm_name,
            action="start",
            new_status=vm.status,
        )
        
    except LXDOperationError as e:
        # Log activity: VM start failure
        activity_logger.log_activity(
            action="vm.start",
            target_type="vm",
            target_id=vm_name,
            target_host=request.host,
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
    verify_cluster_access(request.host)
    try:
        vm = await lxd_service.stop_vm(
            name=vm_name,
            host=request.host,
            force=request.force,
            timeout=request.timeout,
            project=request.project,
        )
        get_cache_manager().invalidate_instance_cache()
        
        # Log activity: VM stop success
        activity_logger.log_activity(
            action="vm.stop",
            target_type="vm",
            target_id=vm_name,
            target_host=request.host,
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
            result="failure",
            error_code="GRACEFUL_TIMEOUT",
            error_message=str(e),
        )
        raise HTTPException(status_code=504, detail=str(e))
    except LXDOperationError as e:
        # Log activity: VM stop failure
        activity_logger.log_activity(
            action="vm.stop",
            target_type="vm",
            target_id=vm_name,
            target_host=request.host,
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
    verify_cluster_access(request.host)
    try:
        vm = await lxd_service.restart_vm(
            name=vm_name,
            host=request.host,
            force=request.force,
            timeout=request.timeout,
            project=request.project,
        )
        get_cache_manager().invalidate_instance_cache()
        
        # Log activity: VM restart success
        activity_logger.log_activity(
            action="vm.restart",
            target_type="vm",
            target_id=vm_name,
            target_host=request.host,
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
            result="failure",
            error_code="GRACEFUL_TIMEOUT",
            error_message=str(e),
        )
        raise HTTPException(status_code=504, detail=str(e))
    except LXDOperationError as e:
        # Log activity: VM restart failure
        activity_logger.log_activity(
            action="vm.restart",
            target_type="vm",
            target_id=vm_name,
            target_host=request.host,
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
    verify_cluster_access(host)
    try:
        await lxd_service.delete_vm(
            name=vm_name,
            host=host,
            force=force,
            project=project,
        )
        get_cache_manager().invalidate_instance_cache()

        # Clean up the instance's tag rows so the tag store doesn't drift from the
        # deleted instance (best-effort — the instance is already gone).
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
            result="success",
            details={"force": force, "project": project}
        )
        
        return VMActionResponse(
            success=True,
            message=f"VM '{vm_name}' deleted successfully",
            vm_name=vm_name,
            action="delete",
        )
        
    except LXDOperationError as e:
        # Log activity: VM delete failure
        activity_logger.log_activity(
            action="vm.delete",
            target_type="vm",
            target_id=vm_name,
            target_host=host,
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
    verify_cluster_access(host)
    try:
        metrics = await lxd_service.get_instance_metrics(
            host=host,
            instance_name=instance_name,
            project=project,
        )
        
        return metrics
        
    except LXDOperationError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting metrics for {instance_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
