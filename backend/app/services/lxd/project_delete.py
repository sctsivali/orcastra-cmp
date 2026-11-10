"""The LXD calls behind a project delete, run on the project executor (projects.py).

important: the budgets aim at the browser's 45 s request window (lib/api.ts fetchAPI): 5 s for
the cluster lock, 3 + 15 s for the DELETE, 10 s for an operation if LXD answers 202, then 3 + 5 s
for the exact readback that settles a timed-out one. Getting a client can add its own validation
read, and a reconnect more, so a slow cluster can still outlast the window; the browser then reads
its own 504 as an unknown outcome and follows the project list, the same as for the route's 503.
The DELETE gets more than the pooled client's 8 s read because LXD can take well over that just
after a project's last instance is removed.
"""

import logging
from typing import Any, Callable, Optional

from pylxd.exceptions import LXDAPIException, NotFound
from requests import exceptions as requests_exceptions

from app.core.lxd_errors import definitive_lxd_refusal
from app.core.lxd_segment import lxd_segment
from app.services.lxd.utils import wait_for_lxd_operation

from .exceptions import (
    LXDConnectionError,
    LXDOperationError,
    LXDOperationUnknownError,
    LXDProjectNotFoundError,
)

logger = logging.getLogger(__name__)

DELETE_TIMEOUT = (3, 15)
DELETE_OPERATION_SECONDS = 10
PROBE_TIMEOUT = (3, 5)

ClientFor = Callable[[str], Optional[Any]]


NOT_EMPTY_REFUSAL = "Only empty projects can be removed"


def project_exists_sync(client_for: ClientFor, host: str, name: str) -> bool:
    """Exact, short read of one project: only a 200 or a 404 is an answer."""
    lxd_segment(name, what="Project name")  # a `.` or `..` is the caller's 400, before any client
    client = client_for(host)
    if client is None:
        raise LXDConnectionError(f"Could not connect to {host}")
    try:
        client.api.projects[lxd_segment(name, what="Project name")].get(timeout=PROBE_TIMEOUT)
    except NotFound:
        return False
    except Exception as exc:  # noqa: BLE001 - anything else leaves the outcome open
        raise LXDOperationUnknownError("delete", name) from exc
    return True


def _send_delete(client: Any, name: str) -> None:
    response = client.api.projects[lxd_segment(name, what="Project name")].delete(timeout=DELETE_TIMEOUT)
    wait_for_lxd_operation(client, response, timeout=DELETE_OPERATION_SECONDS)


def _drop_cached_images(client: Any, name: str) -> bool:
    """Remove the project's daemon-cached images when they are all that keeps it non-empty.

    Returns False, removing nothing, when anything else is left: an instance, a volume, a profile
    other than default, or an image someone uploaded or named (`cached` false). Only a project
    with its own image store is touched; one sharing default's store has none of its own.
    """
    project = client.api.projects[lxd_segment(name, what="Project name")].get(timeout=PROBE_TIMEOUT).json().get("metadata") or {}
    if str((project.get("config") or {}).get("features.images", "")).lower() != "true":
        return False
    images = client.api.images.get(params={"recursion": "1", "project": name}, timeout=PROBE_TIMEOUT).json().get("metadata") or []
    cached = {img.get("fingerprint") for img in images if isinstance(img, dict) and img.get("cached") is True}
    default_profile = f"/1.0/profiles/default?project={name}"
    for entry in project.get("used_by") or []:
        if entry == default_profile:
            continue
        path = entry.split("?", 1)[0]
        if not (path.startswith("/1.0/images/") and path.rsplit("/", 1)[-1] in cached):
            return False
    if not cached:
        return False
    for fingerprint in sorted(cached):
        response = client.api.images[lxd_segment(fingerprint, what="Image fingerprint")].delete(
            params={"project": name}, timeout=DELETE_TIMEOUT
        )
        wait_for_lxd_operation(client, response, timeout=DELETE_OPERATION_SECONDS)
        logger.info(f"Removed cached image {fingerprint[:12]} from project '{name}' before deleting it")
    return True


def delete_project_sync(client_for: ClientFor, host: str, name: str) -> None:
    """Send the DELETE; after it was sent, any failure but LXD's own refusal is unknown."""
    # Before the try, so a `.` or `..` is the caller's 400 rather than an outcome to settle.
    lxd_segment(name, what="Project name")
    phase = "connecting"
    try:
        client = client_for(host)
        if client is None:
            raise LXDConnectionError(f"Could not connect to {host}")
        phase = "submitting"
        try:
            _send_delete(client, name)
        except NotFound:
            raise
        except LXDAPIException as refusal:
            # Incus counts the images it cached for an instance's create as use and refuses
            # the project; LXD 5.21 skips and removes them. Same outcome on both: when they are all
            # that is left, they go and the delete is sent once more.
            if NOT_EMPTY_REFUSAL not in str(refusal) or not _drop_cached_images(client, name):
                raise
            _send_delete(client, name)
        logger.info(f"Deleted project '{name}' from {host}")
    except LXDConnectionError:
        raise
    except NotFound:
        raise LXDProjectNotFoundError(name) from None
    except LXDAPIException as e:
        # important: LXD 5.21 sends the non-empty refusal as a plain error rather than a 400, so the
        # status alone reads as ambiguous and the refusal would be reported as "may still finish".
        if definitive_lxd_refusal(e, phase) or NOT_EMPTY_REFUSAL in str(e):
            # The message is LXD's own sentence rather than transport text.
            raise LXDOperationError(f"Could not delete project: {e}") from e
        raise LXDOperationUnknownError("delete", name) from e
    except requests_exceptions.RequestException as e:
        if phase == "connecting":
            raise LXDConnectionError(f"Could not connect to {host}") from e
        raise LXDOperationUnknownError("delete", name) from e
    except Exception as e:  # noqa: BLE001 - after submit, the readback decides
        logger.error(f"Error deleting project '{name}' from {host}: {type(e).__name__}")
        if phase == "connecting":
            raise LXDOperationError("Could not delete project") from e
        raise LXDOperationUnknownError("delete", name) from e
