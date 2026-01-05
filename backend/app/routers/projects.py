"""
API Router for LXD Project operations.
Provides endpoints for listing and creating projects.
"""

from fastapi import APIRouter, HTTPException, Depends
from typing import List
import logging

from app.models.schemas import (
    ProjectInfo,
    ProjectListResponse,
    ProjectCreateRequest,
    ProjectCreateResponse,
    ProjectUpdateRequest,
    ProjectUpdateResponse,
    ErrorResponse,
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

router = APIRouter(prefix="/projects", tags=["Projects"])


def _parse_size_to_bytes(size_str: str) -> int:
    """Parse size string like '4GiB' or '512MiB' to bytes."""
    size_str = size_str.strip()
    
    # extract number and unit
    import re
    match = re.match(r'^(\d+(?:\.\d+)?)\s*(GiB|MiB|GB|MB)$', size_str, re.IGNORECASE)
    if not match:
        raise ValueError(f"Invalid size format: {size_str}")
    
    value = float(match.group(1))
    unit = match.group(2).upper()
    
    # convert to bytes
    if unit in ('GIB', 'GB'):
        return int(value * 1024 * 1024 * 1024)
    elif unit in ('MIB', 'MB'):
        return int(value * 1024 * 1024)
    else:
        raise ValueError(f"Unsupported unit: {unit}")


@router.get(
    "",
    response_model=List[ProjectListResponse],
    summary="List all projects from all nodes",
    description="Get a list of all projects across all connected LXD nodes.",
    responses={
        200: {"description": "List of projects retrieved successfully"},
        500: {"model": ErrorResponse, "description": "Server error"},
    }
)
async def list_all_projects(
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all projects across all LXD nodes."""
    # Try cache first (30 second cache)
    cache = get_cache_manager()
    cache_key = "projects:list:all"
    cached_response = cache.get("api_responses", cache_key)
    if cached_response is not None:
        logger.debug("Returning cached projects list")
        # Reconstruct from cached dict
        return [ProjectListResponse(**item) for item in cached_response]
    
    try:
        all_projects = []
        node_names = lxd_service.get_node_names()
        logger.info(f"Fetching projects from {len(node_names)} nodes: {node_names}")
        
        for host in node_names:
            try:
                projects_data = await lxd_service.list_projects(host)
                projects = [ProjectInfo(**proj) for proj in projects_data]
                
                all_projects.append(ProjectListResponse(
                    host=host,
                    projects=projects,
                    total=len(projects),
                ))
            except Exception as e:
                logger.warning(f"Could not list projects from {host}: {e}")
                continue
        
        # Cache for 120 seconds (enterprise TTL)
        cache_data = [proj.model_dump() for proj in all_projects]
        cache.set("api_responses", cache_key, cache_data, ttl=180)
        logger.debug("Cached projects list for 120 seconds")
        
        return all_projects
        
    except Exception as e:
        logger.error(f"Error listing all projects: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{host}",
    response_model=ProjectListResponse,
    summary="List all projects",
    description="Get a list of all projects on a specific LXD node.",
    responses={
        200: {"description": "List of projects retrieved successfully"},
        503: {"model": ErrorResponse, "description": "LXD connection error"},
        500: {"model": ErrorResponse, "description": "Server error"},
    }
)
async def list_projects(
    host: str,
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all projects on a specific LXD host."""
    try:
        projects_data = await lxd_service.list_projects(host)
        
        projects = [ProjectInfo(**proj) for proj in projects_data]
        
        return ProjectListResponse(
            host=host,
            projects=projects,
            total=len(projects),
        )
        
    except LXDConnectionError as e:
        logger.error(f"LXD connection error: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        logger.error(f"LXD operation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Error listing projects: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "",
    response_model=ProjectCreateResponse,
    summary="Create a new project",
    description="Create a new project on a specific LXD node.",
    responses={
        201: {"description": "Project created successfully"},
        400: {"model": ErrorResponse, "description": "Invalid request"},
        503: {"model": ErrorResponse, "description": "LXD connection error"},
        500: {"model": ErrorResponse, "description": "Server error"},
    },
    status_code=201,
)
async def create_project(
    request: ProjectCreateRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Create a new project on a specific LXD host."""
    try:
        # validate resource limits against available resources
        try:
            node_resources = lxd_service.get_node_resources(request.host)
            
            # validate CPU
            if request.max_cpu > node_resources["cpu_available"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"Max CPU ({request.max_cpu}) exceeds available CPU ({node_resources['cpu_available']})"
                )
            
            # validate RAM
            ram_bytes = _parse_size_to_bytes(request.max_ram)
            if ram_bytes > node_resources["memory_available"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"Max RAM ({request.max_ram}) exceeds available RAM ({node_resources['memory_available'] / 1024 / 1024 / 1024:.1f} GiB)"
                )
            
            # validate Storage
            storage_bytes = _parse_size_to_bytes(request.max_storage)
            if storage_bytes > node_resources["storage_available"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"Max Storage ({request.max_storage}) exceeds available storage ({node_resources['storage_available'] / 1024 / 1024 / 1024:.1f} GiB)"
                )
        except LXDOperationError:
            # if we can't get resources, just log and continue
            logger.warning(f"Could not validate resources for {request.host}, proceeding anyway")
        
        project_data = await lxd_service.create_project(
            host=request.host,
            name=request.name,
            description=request.description,
            config=request.config,
            max_cpu=request.max_cpu,
            max_ram=request.max_ram,
            max_storage=request.max_storage,
        )
        
        project = ProjectInfo(**project_data)
        
        return ProjectCreateResponse(
            success=True,
            message=f"Project '{request.name}' created successfully on {request.host}",
            project=project,
        )
        
    except LXDConnectionError as e:
        logger.error(f"LXD connection error: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        logger.error(f"LXD operation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating project: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch(
    "/{host}/{project_name}",
    response_model=ProjectUpdateResponse,
    summary="Update a project",
    description="Update project description and resource limits.",
    responses={
        200: {"model": ProjectUpdateResponse, "description": "Project updated successfully"},
        400: {"model": ErrorResponse, "description": "Invalid request or insufficient resources"},
        503: {"model": ErrorResponse, "description": "LXD connection error"},
        500: {"model": ErrorResponse, "description": "Server error"},
    }
)
async def update_project(
    host: str,
    project_name: str,
    request: ProjectUpdateRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Update a project's description and resource limits."""
    try:
        # Get current project to ensure it exists
        projects = await lxd_service.list_projects(host)
        project = next((p for p in projects if p["name"] == project_name), None)
        
        if not project:
            raise HTTPException(
                status_code=404,
                detail=f"Project '{project_name}' not found on {host}"
            )
        
        # If resource limits are being updated, validate against available resources
        # Note: we need to add back the current project's allocation to get true available
        if request.max_cpu or request.max_ram or request.max_storage:
            available = lxd_service.get_node_resources(host)
            
            # Add back current project's limits to available pool
            current_cpu = int(project["config"].get("limits.cpu", 0)) if project["config"].get("limits.cpu") else 0
            current_ram_str = project["config"].get("limits.memory")
            current_storage_str = project["config"].get("limits.disk")
            
            available_cpu = available["cpu_available"] + current_cpu
            available_ram = available["memory_available"]
            available_storage = available["storage_available"]
            
            if current_ram_str:
                try:
                    available_ram += _parse_size_to_bytes(current_ram_str)
                except:
                    pass
            
            if current_storage_str:
                try:
                    available_storage += _parse_size_to_bytes(current_storage_str)
                except:
                    pass
            
            # Validate new limits
            if request.max_cpu and request.max_cpu > available_cpu:
                raise HTTPException(
                    status_code=400,
                    detail=f"Requested CPU ({request.max_cpu} cores) exceeds available ({available_cpu} cores)"
                )
            
            if request.max_ram:
                ram_bytes = _parse_size_to_bytes(request.max_ram)
                if ram_bytes > available_ram:
                    available_gb = available_ram / (1024**3)
                    raise HTTPException(
                        status_code=400,
                        detail=f"Requested RAM exceeds available ({available_gb:.1f} GiB)"
                    )
            
            if request.max_storage:
                storage_bytes = _parse_size_to_bytes(request.max_storage)
                if storage_bytes > available_storage:
                    available_gb = available_storage / (1024**3)
                    raise HTTPException(
                        status_code=400,
                        detail=f"Requested storage exceeds available ({available_gb:.1f} GiB)"
                    )
        
        # Update project
        project_data = await lxd_service.update_project(
            host=host,
            project_name=project_name,
            description=request.description,
            max_cpu=request.max_cpu,
            max_ram=request.max_ram,
            max_storage=request.max_storage,
        )
        
        project = ProjectInfo(**project_data)
        
        return ProjectUpdateResponse(
            success=True,
            message=f"Project '{project_name}' updated successfully on {host}",
            project=project,
        )
        
    except HTTPException:
        raise
    except LXDConnectionError as e:
        logger.error(f"LXD connection error: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        logger.error(f"LXD operation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error updating project: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete(
    "/{host}/{project_name}",
    summary="Delete a project",
    description="Delete a project from a specific LXD node.",
    responses={
        200: {"description": "Project deleted successfully"},
        400: {"model": ErrorResponse, "description": "Invalid request"},
        503: {"model": ErrorResponse, "description": "LXD connection error"},
        500: {"model": ErrorResponse, "description": "Server error"},
    }
)
async def delete_project(
    host: str,
    project_name: str,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Delete a project from a specific LXD host."""
    try:
        await lxd_service.delete_project(host, project_name)
        
        return {
            "success": True,
            "message": f"Project '{project_name}' deleted successfully from {host}"
        }
        
    except LXDConnectionError as e:
        logger.error(f"LXD connection error: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        logger.error(f"LXD operation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error deleting project: {e}")
        raise HTTPException(status_code=500, detail=str(e))
