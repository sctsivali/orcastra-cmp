"""
API Router for Instance operations.
Provides endpoints for managing LXD instances (containers and VMs).
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional, List
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/instances", tags=["Instances"])


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
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all instances across all nodes with optional filters."""
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
        
        return VMListResponse(
            total=len(all_vms),
            vms=all_vms,
            nodes=connected_nodes
        )
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
    try:
        vm = await lxd_service.get_vm(instance_name, host)
        if not vm:
            raise HTTPException(status_code=404, detail=f"Instance {instance_name} not found on {host}")
        return vm
    except LXDConnectionError as e:
        logger.error(f"LXD connection error: {e}")
        raise HTTPException(status_code=503, detail=str(e))
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
    request: VMCreateRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Create a new instance."""
    try:
        result = await lxd_service.create_vm(
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
        return result
    except LXDOperationError as e:
        logger.error(f"LXD operation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating instance: {e}")
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
    try:
        result = await lxd_service.start_vm(instance_name, request.host, force=False)
        return result
    except Exception as e:
        logger.error(f"Error starting instance {instance_name}: {e}")
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
    try:
        result = await lxd_service.stop_vm(
            instance_name, 
            request.host, 
            force=request.force,
            timeout=request.timeout
        )
        return result
    except Exception as e:
        logger.error(f"Error stopping instance {instance_name}: {e}")
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
    try:
        result = await lxd_service.restart_vm(
            instance_name,
            request.host,
            force=request.force,
            timeout=request.timeout
        )
        return result
    except Exception as e:
        logger.error(f"Error restarting instance {instance_name}: {e}")
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
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Delete an instance."""
    try:
        result = await lxd_service.delete_vm(instance_name, host)
        return result
    except Exception as e:
        logger.error(f"Error deleting instance {instance_name}: {e}")
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
    try:
        metrics = await lxd_service.get_instance_metrics(host, instance_name)
        if not metrics:
            raise HTTPException(status_code=404, detail=f"Instance {instance_name} not found on {host}")
        return metrics
    except Exception as e:
        logger.error(f"Error getting metrics for {instance_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
