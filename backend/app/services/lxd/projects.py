"""
Project management operations.
Handles create, update, delete, and list operations for LXD projects.
"""

import logging
from typing import List, Dict, Any, Optional
from pylxd.exceptions import LXDAPIException, NotFound

from .exceptions import LXDOperationError
from .connection import LXDConnectionManager

logger = logging.getLogger(__name__)


class ProjectManager:
    """
    Manages LXD project operations.
    """
    
    def __init__(self, connection_manager: LXDConnectionManager, cert_manager=None):
        self.connection_manager = connection_manager
        self.cert_manager = cert_manager
    
    def _get_client_with_reconnect(self, node_name: str):
        """Get client with automatic reconnection if stale."""
        client = self.connection_manager.get_client(node_name)
        if client is None and self.cert_manager:
            logger.info(f"Connection to {node_name} is stale, attempting reconnect...")
            try:
                # Pass connection_manager for proper pooling
                client = self.cert_manager.connect_with_vault_cert(
                    node_name,
                    connection_manager=self.connection_manager
                )
                logger.info(f"Reconnected to {node_name} successfully")
            except Exception as e:
                logger.warning(f"Reconnect failed for {node_name}: {e}")
                client = None
        return client
    
    async def list_projects(self, host: str) -> List[Dict[str, Any]]:
        """List all projects on a specific LXD host."""
        try:
            client = self._get_client_with_reconnect(host)
            
            # If client is None, raise specific error
            if client is None:
                raise LXDOperationError(f"No connection to host: {host}. Client returned None. Please ensure the cluster is properly registered and connected.")
            
            projects = client.projects.all()
            
            project_list = []
            for project in projects:
                # Parse tags from config
                tags_str = project.config.get("user.tags", "") if project.config else ""
                tags = [tag.strip() for tag in tags_str.split(",") if tag.strip()] if tags_str else []
                
                project_info = {
                    "name": project.name,
                    "description": project.description or "",
                    "config": project.config or {},
                    "used_by": project.used_by or [],
                    "host": host,
                    "tags": tags,
                }
                project_list.append(project_info)
            
            logger.info(f"Listed {len(project_list)} projects from {host}")
            return project_list
            
        except Exception as e:
            logger.error(f"Error listing projects from {host}: {e}")
            raise LXDOperationError(f"Failed to list projects: {str(e)}")
    
    async def create_project(
        self, 
        host: str, 
        name: str, 
        description: Optional[str] = None,
        config: Optional[Dict[str, str]] = None,
        max_cpu: Optional[int] = None,
        max_ram: Optional[str] = None,
        max_storage: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a new project on a specific LXD host."""
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDOperationError(f"No connection to host: {host}. Please ensure the cluster is properly registered and connected.")
            
            # prepare project config with default features
            project_config = config or {
                "features.images": "true",
                "features.profiles": "true",
                "features.storage.volumes": "true",
                "features.networks": "true",
            }
            
            # add resource limits if provided
            if max_cpu is not None:
                project_config["limits.cpu"] = str(max_cpu)
            if max_ram is not None:
                project_config["limits.memory"] = max_ram
            if max_storage is not None:
                project_config["limits.disk"] = max_storage
            
            # create project using API directly
            project_data = {
                "name": name,
                "description": description or "",
                "config": project_config
            }
            
            # use API endpoint directly
            client.api.projects.post(json=project_data)
            
            # get the created project
            project = client.projects.get(name)
            
            logger.info(f"Created project '{name}' on {host}")
            
            return {
                "name": project.name,
                "description": project.description or "",
                "config": project.config or {},
                "used_by": project.used_by or [],
                "host": host,
            }
            
        except LXDAPIException as e:
            logger.error(f"LXD API error creating project '{name}' on {host}: {e}")
            raise LXDOperationError(f"Failed to create project: {str(e)}")
        except Exception as e:
            logger.error(f"Error creating project '{name}' on {host}: {e}")
            raise LXDOperationError(f"Failed to create project: {str(e)}")
    
    async def update_project(
        self, 
        host: str, 
        project_name: str, 
        description: Optional[str] = None,
        max_cpu: Optional[int] = None,
        max_ram: Optional[str] = None,
        max_storage: Optional[str] = None
    ) -> Dict[str, Any]:
        """Update an existing project's description and resource limits."""
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDOperationError(f"No connection to host: {host}. Please ensure the cluster is properly registered and connected.")
            
            # get the current project
            project = client.projects.get(project_name)
            
            # update description if provided
            if description is not None:
                project.description = description
            
            # update resource limits if provided
            if max_cpu is not None:
                project.config["limits.cpu"] = str(max_cpu)
            if max_ram is not None:
                project.config["limits.memory"] = max_ram
            if max_storage is not None:
                project.config["limits.disk"] = max_storage
            
            # save the updated project
            project.save()
            
            logger.info(f"Updated project '{project_name}' on {host}")
            
            return {
                "name": project.name,
                "description": project.description or "",
                "config": project.config or {},
                "used_by": project.used_by or [],
                "host": host,
            }
            
        except NotFound:
            logger.error(f"Project '{project_name}' not found on {host}")
            raise LXDOperationError(f"Project '{project_name}' not found")
        except LXDAPIException as e:
            logger.error(f"LXD API error updating project '{project_name}' on {host}: {e}")
            raise LXDOperationError(f"Failed to update project: {str(e)}")
        except Exception as e:
            logger.error(f"Error updating project '{project_name}' on {host}: {e}")
            raise LXDOperationError(f"Failed to update project: {str(e)}")
    
    async def delete_project(self, host: str, name: str) -> None:
        """Delete a project from a specific LXD host."""
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDOperationError(f"No connection to host: {host}. Please ensure the cluster is properly registered and connected.")
            
            # get the project first
            project = client.projects.get(name)
            
            # delete the project
            project.delete()
            
            logger.info(f"Deleted project '{name}' from {host}")
            
        except NotFound:
            logger.error(f"Project '{name}' not found on {host}")
            raise LXDOperationError(f"Project '{name}' not found")
        except LXDAPIException as e:
            logger.error(f"LXD API error deleting project '{name}' from {host}: {e}")
            raise LXDOperationError(f"Failed to delete project: {str(e)}")
        except Exception as e:
            logger.error(f"Error deleting project '{name}' from {host}: {e}")
            raise LXDOperationError(f"Failed to delete project: {str(e)}")
