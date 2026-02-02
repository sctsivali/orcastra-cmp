"""
Tag management endpoints for nodes, projects, and instances.
"""

from fastapi import APIRouter, HTTPException, status, Depends
from pydantic import BaseModel, Field
from typing import List, Optional
from app.services.lxd_service import LXDService, get_lxd_service, LXDConnectionError
from app.models.schemas import ErrorResponse
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tags", tags=["tags"])


class TagUpdateRequest(BaseModel):
    """Request to update tags for a resource."""
    tags: List[str] = Field(..., description="List of tags to set")
    
    class Config:
        json_schema_extra = {
            "example": {
                "tags": ["production", "database", "critical"]
            }
        }


class TagUpdateResponse(BaseModel):
    """Response after updating tags."""
    success: bool
    message: str
    tags: List[str]


@router.put("/instances/{host}/{instance_name}")
async def update_instance_tags(
    host: str,
    instance_name: str,
    request: TagUpdateRequest,
    project: str = "default",
    lxd_service: LXDService = Depends(get_lxd_service)
) -> TagUpdateResponse:
    """
    Update tags for an instance (VM or container).
    
    Tags are stored in instance metadata as 'user.tags' config key.
    """
    try:
        client = lxd_service.connection_manager.get_client(host)
        
        # Get instance from specific project using API
        response = client.api.instances[instance_name].get(params={'project': project})
        instance_data = response.json()['metadata']
        
        # Update tags in config
        tags_str = ",".join(request.tags) if request.tags else ""
        if 'config' not in instance_data:
            instance_data['config'] = {}
        instance_data['config']['user.tags'] = tags_str
        
        # Save updated config
        client.api.instances[instance_name].put(
            json=instance_data,
            params={'project': project}
        )
        
        logger.info(f"Updated tags for instance {instance_name} on {host} in project {project}: {request.tags}")
        
        return TagUpdateResponse(
            success=True,
            message=f"Tags updated for instance {instance_name}",
            tags=request.tags
        )
    except LXDConnectionError as e:
        logger.error(f"LXD connection error for {host}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to connect to LXD host {host}: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Failed to update tags for instance {instance_name} on {host}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update tags: {str(e)}"
        )


@router.put("/projects/{host}/{project_name}")
async def update_project_tags(
    host: str,
    project_name: str,
    request: TagUpdateRequest,
    lxd_service: LXDService = Depends(get_lxd_service)
) -> TagUpdateResponse:
    """
    Update tags for a project.
    
    Tags are stored in project config as 'user.tags' key.
    """
    try:
        client = lxd_service.connection_manager.get_client(host)
        project = client.projects.get(project_name)
        
        # Store tags as comma-separated string in config
        tags_str = ",".join(request.tags) if request.tags else ""
        project.config["user.tags"] = tags_str
        project.save()
        
        logger.info(f"Updated tags for project {project_name} on {host}: {request.tags}")
        
        return TagUpdateResponse(
            success=True,
            message=f"Tags updated for project {project_name}",
            tags=request.tags
        )
    except LXDConnectionError as e:
        logger.error(f"LXD connection error for {host}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to connect to LXD host {host}: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Failed to update tags for project {project_name} on {host}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update tags: {str(e)}"
        )


@router.put("/nodes/{node_name}")
async def update_node_tags(
    node_name: str,
    request: TagUpdateRequest,
    lxd_service: LXDService = Depends(get_lxd_service)
) -> TagUpdateResponse:
    """
    Update tags for a node.
    
    Tags are stored in server config as 'user.tags' key.
    Note: This requires admin privileges on the LXD server.
    """
    try:
        client = lxd_service.connection_manager.get_client(node_name)
        
        # Update tags in server config using direct config update
        tags_str = ",".join(request.tags) if request.tags else ""
        logger.info(f"Updating tags for node {node_name} with tags string: '{tags_str}'")
        
        # Use the server's config property to update
        response = client.api.get()
        server_data = response.json()["metadata"]
        logger.info(f"Current server config for {node_name}: {server_data.get('config', {})}")
        
        # Update config
        if "config" not in server_data:
            server_data["config"] = {}
        
        server_data["config"]["user.tags"] = tags_str
        logger.info(f"New config with tags: {server_data['config']}")
        
        # Put the updated config
        put_response = client.api.put(json={"config": server_data["config"]})
        logger.info(f"PUT response status: {put_response.status_code}")
        
        # Verify the update
        verify_response = client.api.get()
        verify_data = verify_response.json()["metadata"]
        saved_tags = verify_data.get("config", {}).get("user.tags", "")
        logger.info(f"Verified saved tags for {node_name}: '{saved_tags}'")
        
        return TagUpdateResponse(
            success=True,
            message=f"Tags updated for node {node_name}",
            tags=request.tags
        )
    except LXDConnectionError as e:
        logger.error(f"LXD connection error for {node_name}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to connect to LXD node {node_name}: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Failed to update tags for node {node_name}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update tags: {str(e)}"
        )


@router.get("/instances/{host}/{instance_name}")
async def get_instance_tags(
    host: str,
    instance_name: str,
    project: str = "default",
    lxd_service: LXDService = Depends(get_lxd_service)
) -> TagUpdateResponse:
    """Get tags for an instance."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        instance = client.instances.get(instance_name)
        
        # Parse tags from config
        tags_str = instance.config.get("user.tags", "")
        tags = [tag.strip() for tag in tags_str.split(",") if tag.strip()]
        
        return TagUpdateResponse(
            success=True,
            message=f"Retrieved tags for instance {instance_name}",
            tags=tags
        )
    except LXDConnectionError as e:
        logger.error(f"LXD connection error for {host}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to connect to LXD host {host}: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Failed to get tags for instance {instance_name} on {host}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to get tags: {str(e)}"
        )
