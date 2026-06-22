"""Shared mapping of LXD lifecycle exceptions to clean HTTP responses.

Keeps raw driver/urllib3 error strings (ex; "HTTPSConnectionPool(...): Read
timed out") out of client responses - those are logged server-side only. Used by
both the /vms and /instances action endpoints so error behaviour stays uniform.
"""

import logging

from fastapi import HTTPException

from app.services.lxd_service import (
    LXDConnectionError,
    LXDOperationError,
    LXDTimeoutError,
)
from app.services.activity_logger import get_activity_logger

logger = logging.getLogger(__name__)
_activity_logger = get_activity_logger()


def lxd_http_error(exc: Exception, *, what: str, host: str | None = None) -> HTTPException:
    """Map an LXD service exception to a clean HTTPException (no audit logging).

    Use for read paths (list/get/metrics). ``what`` is a short noun phrase reused in
    the generic 500 message (ex; "list VMs", "get VM 'vm-x'"). Raw driver/urllib3
    strings are never placed in the client-facing detail.
    """
    node = f" node '{host}'" if host else ""
    # LXDTimeoutError subclasses LXDOperationError, so check it first.
    if isinstance(exc, LXDTimeoutError):
        logger.warning(f"{what}: hypervisor timed out: {exc}")
        return HTTPException(
            status_code=504,
            detail=f"The hypervisor{node} did not respond in time. Please retry.",
        )
    if isinstance(exc, LXDConnectionError):
        logger.warning(f"{what}: connection failed: {exc}")
        return HTTPException(
            status_code=503,
            detail=f"Cannot reach the hypervisor{node}. Check that the node is online.",
        )
    if isinstance(exc, LXDOperationError):
        message = str(exc)
        if "not found" in message.lower():
            return HTTPException(status_code=404, detail=message)
        return HTTPException(status_code=400, detail=message)
    # Unknown/unexpected: log the raw error, return a generic message.
    logger.error(f"Unexpected error while trying to {what}: {exc}")
    return HTTPException(status_code=500, detail=f"Failed to {what} due to an internal error.")


def lxd_action_http_error(
    exc: Exception,
    action: str,
    target_id: str,
    host: str,
    target_type: str = "instance",
) -> HTTPException:
    """Log a lifecycle-action failure to the audit trail, then map it to a clean HTTPException.

    ``action`` is the audit action (ex; "vm.stop", "instance.restart"); its last
    segment is reused in the generic fallback message.
    """
    _activity_logger.log_activity(
        action=action,
        target_type=target_type,
        target_id=target_id,
        target_host=host,
        result="failure",
        error_message=str(exc),
    )
    verb = action.split(".")[-1]
    return lxd_http_error(exc, what=f"{verb} '{target_id}'", host=host)
