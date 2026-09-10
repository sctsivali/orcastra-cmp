"""
Image management operations.
Handles listing and managing LXD images.
"""

import logging
from typing import List, Optional

from app.models.schemas import ImageInfo
from .exceptions import LXDConnectionError, LXDOperationError
from .connection import LXDConnectionManager
from app.core.lxd_path import safe_segment

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
    
    def _image_or_none(self, host: str, fingerprint: str):
        """Fetch one pylxd image, or None when the host has no such fingerprint.

        important: blocking. Callers on the event loop must reach this through run_blocking.
        """
        client = self._get_client_with_reconnect(host)
        if client is None:
            raise LXDConnectionError(f"Could not connect to {host}")
        try:
            return client.images.get(safe_segment(fingerprint))
        except Exception as e:
            # pylxd raises NotFound for a missing fingerprint and the same class hierarchy for
            # a daemon fault, so the message is the only thing that separates them. A miss must
            # not read as an outage, and an outage must not read as "already gone".
            if "not found" in str(e).lower():
                return None
            raise

    def get_image(self, host: str, fingerprint: str) -> Optional[ImageInfo]:
        """Read a single image's metadata. None when it does not exist on that host."""
        try:
            image = self._image_or_none(host, fingerprint)
            if image is None:
                return None
            return self._to_info(image)
        except LXDConnectionError:
            raise
        except Exception as e:
            logger.error(f"Error reading image {fingerprint} from {host}: {e}")
            raise LXDOperationError(f"Failed to read image: {str(e)}")

    def delete_image(self, host: str, fingerprint: str) -> bool:
        """Delete an image from a host's image store. False when it was already gone.

        important: blocking, and `wait=True` so a failure surfaces here rather than as a
        silently rejected background operation. The caller maps the daemon's error text onto
        a status; this layer does not decide policy.
        """
        image = self._image_or_none(host, fingerprint)
        if image is None:
            return False
        image.delete(wait=True)
        return True

    @staticmethod
    def _to_info(image) -> ImageInfo:
        """One pylxd image to ImageInfo. Shared by the list and single-image reads."""
        aliases = [a.get("name", "") for a in image.aliases] if image.aliases else []
        # note: pylxd declares auto_update optional, so the attribute is absent rather
        # than falsy when LXD omits it and a direct read raises AttributeError. Every
        # other field below is a readonly attribute that is always present. None of
        # these cost an extra request: pylxd syncs the whole object on first attribute
        # access, which the aliases and properties reads have already done.
        return ImageInfo(
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
        )

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
            return [self._to_info(image) for image in client.images.all()]

        except LXDConnectionError:
            raise
        except Exception as e:
            logger.error(f"Error listing images from {host}: {e}")
            raise LXDOperationError(f"Failed to list images: {str(e)}")
