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
    get_lxd_service,
)
from app.core.dependencies import check_read_only_mode
from app.core.cache import get_cache_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vms", tags=["Virtual Machines"])


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
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all VMs across all LXD nodes with optional filtering."""
    # Try cache first (30 second cache for VM list)
    cache = get_cache_manager()
    cache_key = f"vms:list:host={host}:status={status}"
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
        
        # Count LXD vs MicroCloud nodes
        lxd_count = 0
        microcloud_count = 0
        
        # Config nodes are LXD
        config_nodes = lxd_service.get_node_names()
        for node in connected_nodes:
            if node in config_nodes:
                lxd_count += 1
            else:
                # Check if it's MicroCloud from Vault
                try:
                    # Read metadata directly from Vault
                    metadata_path = f"secret/data/clusters/{node}/metadata"
                    metadata = lxd_service.cert_manager.vault.vault_read(metadata_path)
                    if metadata:
                        # Key in Vault is 'type' not 'cluster_type'
                        cluster_type = metadata.get('type', 'lxd').lower()
                        if cluster_type == 'microcloud':
                            microcloud_count += 1
                        else:
                            lxd_count += 1
                    else:
                        lxd_count += 1
                except:
                    lxd_count += 1  # Default to LXD if unknown
        
        response = VMListResponse(
            total=len(filtered_vms),
            vms=filtered_vms,
            nodes=connected_nodes,
            lxd_count=lxd_count,
            microcloud_count=microcloud_count,
        )
        
        # Cache for 120 seconds (enterprise TTL)
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
    try:
        vm = await lxd_service.get_vm(vm_name, host)
        
        if not vm:
            raise HTTPException(
                status_code=404, 
                detail=f"VM '{vm_name}' not found on host '{host}'"
            )
        
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
):
    """Create a new VM on the specified host."""
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
        
        return VMCreateResponse(
            success=True,
            message=f"VM '{request.name}' created successfully on {request.host}",
            vm=vm,
        )
        
    except LXDOperationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except LXDConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
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
    try:
        vm = await lxd_service.start_vm(
            name=vm_name,
            host=request.host,
            force=request.force,
        )
        
        return VMActionResponse(
            success=True,
            message=f"VM '{vm_name}' started successfully",
            vm_name=vm_name,
            action="start",
            new_status=vm.status,
        )
        
    except LXDOperationError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error starting VM {vm_name}: {e}")
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
    try:
        vm = await lxd_service.stop_vm(
            name=vm_name,
            host=request.host,
            force=request.force,
            timeout=request.timeout,
        )
        
        return VMActionResponse(
            success=True,
            message=f"VM '{vm_name}' stopped successfully",
            vm_name=vm_name,
            action="stop",
            new_status=vm.status,
        )
        
    except LXDOperationError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error stopping VM {vm_name}: {e}")
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
    try:
        vm = await lxd_service.restart_vm(
            name=vm_name,
            host=request.host,
            force=request.force,
            timeout=request.timeout,
        )
        
        return VMActionResponse(
            success=True,
            message=f"VM '{vm_name}' restarted successfully",
            vm_name=vm_name,
            action="restart",
            new_status=vm.status,
        )
        
    except LXDOperationError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error restarting VM {vm_name}: {e}")
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
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Delete a VM."""
    try:
        await lxd_service.delete_vm(
            name=vm_name,
            host=host,
            force=force,
        )
        
        return VMActionResponse(
            success=True,
            message=f"VM '{vm_name}' deleted successfully",
            vm_name=vm_name,
            action="delete",
        )
        
    except LXDOperationError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error deleting VM {vm_name}: {e}")
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
