"""
Image management operations.
Handles listing and managing LXD images.
"""

import asyncio
import logging
from typing import Any, Dict, List, NamedTuple, Optional

from app.models.schemas import ImageInfo
from .exceptions import LXDConnectionError, LXDOperationError
from .connection import LXDConnectionManager
from .project_scope import (
    DEFAULT_PROJECT,
    _metadata,
    list_across_projects,
    list_in_project,
    project_params,
    resolve_owner,
)
from .utils import wait_for_lxd_operation
from app.core.lxd_path import safe_segment

logger = logging.getLogger(__name__)

# Same shape as the project-scope reads: a daemon that does not answer a single-image read in
# this long is treated as unreachable rather than waited on.
IMAGE_READ_TIMEOUT = (3, 25)
# Under the router's own deadline for a delete, so the daemon's answer is what the caller sees.
IMAGE_DELETE_WAIT_SECONDS = 50

# LXD's zero time on an image field that was never set.
_ZERO_TIME = "0001-01-01T00:00:00Z"


class ImageStoreListing(NamedTuple):
    images: List[ImageInfo]
    """The store that was read when one project was asked for; None for an all-projects read."""
    project: Optional[str]
    complete: bool


def _status_of(exc: Exception) -> Optional[int]:
    return getattr(getattr(exc, "response", None), "status_code", None)


def _timestamp(value: Any) -> Optional[str]:
    if not value or value == _ZERO_TIME:
        return None
    return str(value)


def _info_from_row(row: Dict[str, Any], owner: Optional[str] = None) -> ImageInfo:
    """One LXD image record (the JSON the daemon answers) to ImageInfo.

    Shared by the list and single-image reads. `owner` is the store the record was read
    from; an all-projects listing stamps `project` on every row itself.
    """
    aliases = [
        a.get("name", "") for a in (row.get("aliases") or []) if isinstance(a, dict)
    ]
    properties = row.get("properties") or {}
    return ImageInfo(
        fingerprint=str(row.get("fingerprint") or ""),
        aliases=aliases,
        description=str(properties.get("description") or ""),
        architecture=str(row.get("architecture") or ""),
        size=int(row.get("size") or 0),
        upload_date=_timestamp(row.get("uploaded_at")),
        public=bool(row.get("public", False)),
        type=str(row.get("type") or ImageInfo.model_fields["type"].default),
        cached=bool(row.get("cached", False)),
        auto_update=bool(row.get("auto_update", False)),
        created_at=_timestamp(row.get("created_at")),
        last_used_at=_timestamp(row.get("last_used_at")),
        expires_at=_timestamp(row.get("expires_at")),
        profiles=list(row.get("profiles") or []),
        project=str(row.get("project") or owner or DEFAULT_PROJECT),
    )


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
    
    @staticmethod
    def _image_or_none(client: Any, fingerprint: str, owner: str) -> Optional[Dict[str, Any]]:
        """One image's record from the store `owner` holds, or None when it has no such
        fingerprint.

        important: blocking. Callers on the event loop must reach this through run_blocking.
        """
        try:
            response = client.api.images[safe_segment(fingerprint)].get(
                params=project_params(owner), timeout=IMAGE_READ_TIMEOUT
            )
        except Exception as e:
            # A miss and a daemon fault arrive as the same exception class, so the status
            # and the message are what separate them. A miss must not read as an outage,
            # and an outage must not read as "already gone".
            if _status_of(e) == 404 or "not found" in str(e).lower():
                return None
            raise
        metadata = _metadata(response)
        return metadata if isinstance(metadata, dict) else None

    def _client(self, host: str) -> Any:
        client = self._get_client_with_reconnect(host)
        if client is None:
            # important: returning nothing here is what made a dead cluster look like a
            # cluster with no images. The router cannot label what it cannot see, and the
            # single-host endpoint already turns this into a 503.
            logger.warning(f"Could not connect to {host} for images")
            raise LXDConnectionError(f"Could not connect to {host}")
        return client

    def image_store_of(self, host: str, project: Optional[str]) -> str:
        """The store `project` reads on `host`: its own when it isolates images, else default."""
        return resolve_owner(self._client(host), "images", project)

    def get_image(
        self, host: str, fingerprint: str, project: Optional[str] = None
    ) -> Optional[ImageInfo]:
        """Read a single image's metadata from the store `project` reads. None when it does
        not exist there."""
        try:
            client = self._client(host)
            owner = resolve_owner(client, "images", project)
            row = self._image_or_none(client, fingerprint, owner)
            if row is None:
                return None
            return _info_from_row(row, owner)
        except LXDConnectionError:
            raise
        except Exception as e:
            logger.error(f"Error reading image {fingerprint} from {host}: {e}")
            raise LXDOperationError(f"Failed to read image: {str(e)}")

    def delete_image(self, host: str, fingerprint: str, project: Optional[str] = None) -> bool:
        """Delete an image from the store `project` reads. False when it was already gone.

        important: blocking, and waited on so a failure surfaces here rather than as a
        silently rejected background operation. The caller maps the daemon's error text onto
        a status; this layer does not decide policy.
        """
        client = self._client(host)
        owner = resolve_owner(client, "images", project)
        if self._image_or_none(client, fingerprint, owner) is None:
            return False
        response = client.api.images[safe_segment(fingerprint)].delete(
            params=project_params(owner), timeout=(3, IMAGE_DELETE_WAIT_SECONDS + 10)
        )
        wait_for_lxd_operation(
            client, response, timeout=IMAGE_DELETE_WAIT_SECONDS, project=owner
        )
        return True

    def _list_images_sync(self, host: str, project: Optional[str]) -> ImageStoreListing:
        client = self._client(host)
        if project:
            # The store the project really reads: its own when it isolates images, else
            # default. Resolved here so the browser never has to know the feature flag.
            owner: Optional[str] = resolve_owner(client, "images", project)
            rows, complete = list_in_project(client, "images", owner), True
        else:
            owner = None
            rows, complete = list_across_projects(client, "images")
        return ImageStoreListing(
            images=[_info_from_row(row, owner) for row in rows],
            project=owner,
            complete=complete,
        )

    async def list_images(self, host: str, project: Optional[str] = None) -> ImageStoreListing:
        """The images a project can use on a host, or every store on the host when no project
        is named. Each row names the store that holds it."""
        try:
            return await asyncio.to_thread(self._list_images_sync, host, project)
        except LXDConnectionError:
            raise
        except Exception as e:
            logger.error(f"Error listing images from {host}: {e}")
            raise LXDOperationError(f"Failed to list images: {str(e)}")
