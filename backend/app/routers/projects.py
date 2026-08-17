"""
API Router for LXD Project operations.
Provides endpoints for listing and creating projects.
"""

from fastapi import APIRouter, HTTPException, Depends
from typing import List, Optional, Tuple
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

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
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.services.orcahub_webhook import schedule_orcahub_event
from app.core.org_scope import org_scope_dependency, verify_cluster_access, get_scope_cache_key, filter_projects_by_acl
from app.core.cache import get_cache_manager
from app.services.activity_logger import get_activity_logger

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/projects",
    tags=["Projects"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# Shared thread pool for blocking operations
_projects_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="projects_")


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
    # Try cache first (180 second cache)
    cache = get_cache_manager()
    cache_key = get_scope_cache_key("projects:list:all")
    cached_response = cache.get("api_responses", cache_key)
    if cached_response is not None:
        logger.debug("Returning cached projects list")
        return [ProjectListResponse(**item) for item in cached_response]
    
    try:
        node_names = lxd_service.get_node_names()
        logger.info(f"Fetching projects from {len(node_names)} nodes in parallel")
        
        # Helper to fetch projects for a single host
        async def fetch_host_projects(host: str) -> Optional[ProjectListResponse]:
            try:
                projects_data = await lxd_service.list_projects(host)
                projects = [ProjectInfo(**proj) for proj in projects_data]
                return ProjectListResponse(
                    host=host,
                    projects=projects,
                    total=len(projects),
                )
            except Exception as e:
                logger.warning(f"Could not list projects from {host}: {e}")
                return None
        
        # Fetch from all nodes in parallel
        results = await asyncio.gather(
            *[fetch_host_projects(host) for host in node_names],
            return_exceptions=True
        )
        
        # Filter valid results
        all_projects = []
        for result in results:
            if isinstance(result, ProjectListResponse):
                # apply tenant ACL filtering per host
                allowed_names = filter_projects_by_acl(result.host, [p.name for p in result.projects])
                filtered = [p for p in result.projects if p.name in allowed_names]
                all_projects.append(ProjectListResponse(
                    host=result.host,
                    projects=filtered,
                    total=len(filtered),
                ))
            elif isinstance(result, Exception):
                logger.warning(f"Project fetch error: {result}")
        
        # Cache for 30 seconds (short TTL - ACL-filtered data)
        cache_data = [proj.model_dump() for proj in all_projects]
        cache.set("api_responses", cache_key, cache_data, ttl=30)
        logger.debug("Cached projects list for 30 seconds")
        
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
    verify_cluster_access(host)
    try:
        projects_data = await lxd_service.list_projects(host)
        
        projects = [ProjectInfo(**proj) for proj in projects_data]
        
        # apply tenant ACL filtering
        allowed_names = filter_projects_by_acl(host, [p.name for p in projects])
        projects = [p for p in projects if p.name in allowed_names]
        
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
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Create a new project on a specific LXD host."""
    verify_cluster_access(request.host)
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
        
        # Invalidate project list caches so subsequent API calls return fresh data
        cache = get_cache_manager()
        cache.delete("api_responses", get_scope_cache_key("projects:list:all"))
        cache.delete("api_responses", get_scope_cache_key(f"projects:list:{request.host}"))
        cache.delete("api_responses", "projects:list:all:scope=all")
        cache.delete("api_responses", f"projects:list:{request.host}:scope=all")
        
        # Log activity for audit trail
        activity_logger.log_activity(
            action="project.create",
            target_type="project",
            target_id=request.name,
            target_host=request.host,
            result="success",
            details={
                "description": request.description,
                "max_cpu": request.max_cpu,
                "max_ram": request.max_ram,
                "max_storage": request.max_storage,
            }
        )
        
        schedule_orcahub_event("project.created", request.host, request.name, request.name, resource_type="PROJECT")
        # Readers hold a cached list; without this the change is invisible until the TTL.
        get_cache_manager().invalidate_resource_cache("projects")
        return ProjectCreateResponse(
            success=True,
            message=f"Project '{request.name}' created successfully on {request.host}",
            project=project,
        )
        
    except LXDConnectionError as e:
        logger.error(f"LXD connection error: {e}")
        activity_logger.log_activity(
            action="project.create",
            target_type="project",
            target_id=request.name,
            target_host=request.host,
            result="failure",
            error_code="LXDConnectionError",
            error_message=str(e),
        )
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        logger.error(f"LXD operation error: {e}")
        activity_logger.log_activity(
            action="project.create",
            target_type="project",
            target_id=request.name,
            target_host=request.host,
            result="failure",
            error_code="LXDOperationError",
            error_message=str(e),
        )
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
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Update a project's description and resource limits."""
    verify_cluster_access(host)
    try:
        # Get current project to ensure it exists
        projects = await lxd_service.list_projects(host)
        project = next((p for p in projects if p["name"] == project_name), None)
        
        if not project:
            raise HTTPException(
                status_code=404,
                detail=f"Project '{project_name}' not found on {host}"
            )
        
        # If resource limits are being updated (non-zero), validate against available resources
        # Note: 0 = clear limit (unlimited), None = no change
        has_real_limits = (
            (request.max_cpu is not None and request.max_cpu > 0) or
            (request.max_ram is not None and request.max_ram != "0") or
            (request.max_storage is not None and request.max_storage != "0")
        )
        if has_real_limits:
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
            
            # Validate new limits (skip validation for 0 = clear limit)
            if request.max_cpu and request.max_cpu > 0 and request.max_cpu > available_cpu:
                raise HTTPException(
                    status_code=400,
                    detail=f"Requested CPU ({request.max_cpu} cores) exceeds available ({available_cpu} cores)"
                )
            
            if request.max_ram and request.max_ram != "0":
                ram_bytes = _parse_size_to_bytes(request.max_ram)
                if ram_bytes > available_ram:
                    available_gb = available_ram / (1024**3)
                    raise HTTPException(
                        status_code=400,
                        detail=f"Requested RAM exceeds available ({available_gb:.1f} GiB)"
                    )
            
            if request.max_storage and request.max_storage != "0":
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
        
        # Invalidate project list caches so frontend gets fresh data
        cache = get_cache_manager()
        cache.delete("api_responses", get_scope_cache_key("projects:list:all"))
        cache.delete("api_responses", get_scope_cache_key(f"projects:list:{host}"))
        cache.delete("api_responses", "projects:list:all:scope=all")
        cache.delete("api_responses", f"projects:list:{host}:scope=all")
        logger.debug(f"Invalidated project cache after updating '{project_name}' on {host}")
        
        project = ProjectInfo(**project_data)
        
        # Readers hold a cached list; without this the change is invisible until the TTL.
        get_cache_manager().invalidate_resource_cache("projects")
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
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Delete a project from a specific LXD host."""
    verify_cluster_access(host)
    try:
        await lxd_service.delete_project(host, project_name)
        
        # Invalidate project list caches so frontend gets fresh data
        # Must use scoped cache keys to match the keys used by list endpoints
        cache = get_cache_manager()
        cache.delete("api_responses", get_scope_cache_key("projects:list:all"))
        cache.delete("api_responses", get_scope_cache_key(f"projects:list:{host}"))
        # Also clear unscoped keys for admin/background tasks
        cache.delete("api_responses", "projects:list:all:scope=all")
        cache.delete("api_responses", f"projects:list:{host}:scope=all")
        logger.debug(f"Invalidated project cache after deleting '{project_name}' from {host}")
        
        # Log activity for audit trail
        activity_logger.log_activity(
            action="project.delete",
            target_type="project",
            target_id=project_name,
            target_host=host,
            result="success",
        )
        
        schedule_orcahub_event("project.deleted", host, project_name, project_name, resource_type="PROJECT")
        # Readers hold a cached list; without this the change is invisible until the TTL.
        get_cache_manager().invalidate_resource_cache("projects")
        return {
            "success": True,
            "message": f"Project '{project_name}' deleted successfully from {host}"
        }
        
    except LXDConnectionError as e:
        logger.error(f"LXD connection error: {e}")
        activity_logger.log_activity(
            action="project.delete",
            target_type="project",
            target_id=project_name,
            target_host=host,
            result="failure",
            error_code="LXDConnectionError",
            error_message=str(e),
        )
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        logger.error(f"LXD operation error: {e}")
        activity_logger.log_activity(
            action="project.delete",
            target_type="project",
            target_id=project_name,
            target_host=host,
            result="failure",
            error_code="LXDOperationError",
            error_message=str(e),
        )
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error deleting project: {e}")
        raise HTTPException(status_code=500, detail=str(e))
