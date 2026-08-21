"""
Image management operations.
Handles listing and managing LXD images.
"""

import logging
from typing import List

from app.models.schemas import ImageInfo
from .exceptions import LXDConnectionError, LXDOperationError
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
                # important: returning [] here is what made a dead cluster look like a
                # cluster with no images. The router above cannot label what it cannot
                # see, and the single-host endpoint already turns this into a 503.
                logger.warning(f"Could not connect to {host} for images")
                raise LXDConnectionError(f"Could not connect to {host}")
            images = []
            
            for image in client.images.all():
                aliases = [a.get("name", "") for a in image.aliases] if image.aliases else []
                
                # note: pylxd declares auto_update optional, so the attribute is absent rather
                # than falsy when LXD omits it and a direct read raises AttributeError. Every
                # other field below is a readonly attribute that is always present. None of
                # these cost an extra request: pylxd syncs the whole object on first attribute
                # access, which the aliases and properties reads above have already done.
                images.append(ImageInfo(
                    fingerprint=image.fingerprint,
                    aliases=aliases,
                    description=image.properties.get("description", "") if image.properties else "",
                    architecture=image.architecture,
                    size=image.size,
                    upload_date=image.uploaded_at,
                    public=image.public,
                    type=image.type,
                    cached=bool(getattr(image, "cached", False)),
                    auto_update=bool(getattr(image, "auto_update", False)),
                    created_at=getattr(image, "created_at", None),
                    last_used_at=getattr(image, "last_used_at", None),
                    expires_at=getattr(image, "expires_at", None),
                    profiles=list(getattr(image, "profiles", None) or []),
                ))
            
            return images
            
        except LXDConnectionError:
            raise
        except Exception as e:
            logger.error(f"Error listing images from {host}: {e}")
            raise LXDOperationError(f"Failed to list images: {str(e)}")
