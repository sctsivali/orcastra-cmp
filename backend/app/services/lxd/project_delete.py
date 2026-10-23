"""The LXD calls behind a project delete, run on the project executor (projects.py).

important: a delete answers inside the browser's 45 s request window (lib/api.ts fetchAPI):
5 s for the cluster lock, at most 3 + 15 s for the DELETE and 10 s for an operation if LXD
answers 202, then 3 + 5 s for the exact readback that settles a timed-out one. LXD took well over
the pooled client's 8 s just after a project's last instance went (measured live, 2026-10-23),
and with that budget the route answered 400 with the transport text, cluster address included.
"""

import logging
from typing import Any, Callable, Optional

from pylxd.exceptions import LXDAPIException, NotFound
from requests import exceptions as requests_exceptions

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


def definitive_lxd_refusal(exc: LXDAPIException, phase: str) -> bool:
    """Whether LXD supplied a terminal verdict rather than a transport/wait ambiguity."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if phase == "submitting" and isinstance(status_code, int) and 400 <= status_code < 500:
        return True
    try:
        metadata = response.json().get("metadata")
    except Exception:
        return False
    return isinstance(metadata, dict) and metadata.get("status") in {"Failure", "Cancelled"}


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


def delete_project_sync(client_for: ClientFor, host: str, name: str) -> None:
    """Send the DELETE; after it was sent, any failure but LXD's own refusal is unknown."""
    # note: before the try: a `.` or `..` is the caller's 400, not an outcome to settle.
    lxd_segment(name, what="Project name")
    phase = "connecting"
    try:
        client = client_for(host)
        if client is None:
            raise LXDConnectionError(f"Could not connect to {host}")
        phase = "submitting"
        response = client.api.projects[lxd_segment(name, what="Project name")].delete(timeout=DELETE_TIMEOUT)
        phase = "waiting"
        wait_for_lxd_operation(client, response, timeout=DELETE_OPERATION_SECONDS)
        logger.info(f"Deleted project '{name}' from {host}")
    except LXDConnectionError:
        raise
    except NotFound:
        raise LXDProjectNotFoundError(name) from None
    except LXDAPIException as e:
        if definitive_lxd_refusal(e, phase):
            # LXD's own sentence ("Only empty projects can be removed"), not transport text.
            raise LXDOperationError(f"Failed to delete project: {e}") from e
        raise LXDOperationUnknownError("delete", name) from e
    except requests_exceptions.RequestException as e:
        if phase == "connecting":
            raise LXDConnectionError(f"Could not connect to {host}") from e
        raise LXDOperationUnknownError("delete", name) from e
    except Exception as e:  # noqa: BLE001 - after submit, the readback decides
        logger.error(f"Error deleting project '{name}' from {host}: {type(e).__name__}")
        if phase == "connecting":
            raise LXDOperationError("Failed to delete project") from e
        raise LXDOperationUnknownError("delete", name) from e
