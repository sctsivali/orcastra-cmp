"""
Image management operations.
Handles listing and managing LXD images.
"""

import logging
from typing import List

from app.models.schemas import ImageInfo
from .exceptions import LXDOperationError
from .connection import LXDConnectionManager

logger = logging.getLogger(__name__)


class ImageManager:
    """
    Manages LXD image operations.
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
    
    async def list_images(self, host: str) -> List[ImageInfo]:
        """List available images on a host."""
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                logger.warning(f"Could not connect to {host} for images")
                return []
            images = []
            
            for image in client.images.all():
                aliases = [a.get("name", "") for a in image.aliases] if image.aliases else []
                
                images.append(ImageInfo(
                    fingerprint=image.fingerprint,
                    aliases=aliases,
                    description=image.properties.get("description", "") if image.properties else "",
                    architecture=image.architecture,
                    size=image.size,
                    upload_date=image.uploaded_at,
                    public=image.public,
                    type=image.type,
                ))
            
            return images
            
        except Exception as e:
            logger.error(f"Error listing images from {host}: {e}")
            raise LXDOperationError(f"Failed to list images: {str(e)}")
