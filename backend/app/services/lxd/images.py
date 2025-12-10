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
    
    def __init__(self, connection_manager: LXDConnectionManager):
        self.connection_manager = connection_manager
    
    async def list_images(self, host: str) -> List[ImageInfo]:
        """List available images on a host."""
        try:
            client = self.connection_manager.get_client(host)
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
