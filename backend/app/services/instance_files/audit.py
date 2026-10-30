"""The record of what a person did to a guest's files. Paths and sizes, never contents."""

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional, Tuple

from fastapi import HTTPException

from app.core.auth import AuthenticatedUser
from app.services.activity_logger import get_activity_logger

logger = logging.getLogger(__name__)

activity_logger = get_activity_logger()

MAX_AUDITED_PATHS = 256
_BROWSE_WINDOW_SECONDS = 900.0
_MAX_BROWSE_KEYS = 10_000
_browsed: Dict[Tuple[str, str, str, str], float] = {}


async def _org_of(host: str) -> Tuple[Optional[int], Optional[str]]:
    from app.core.database import async_session_maker
    from app.core.org_scope import resolve_cluster_org

    try:
        async with async_session_maker() as db:
            return await resolve_cluster_org(host, db)
    except Exception as exc:  # noqa: BLE001 - the action happened whether or not the org resolves
        logger.warning("Could not resolve the organization of %s for an audit record: %s", host, type(exc).__name__)
        return None, None


def _bounded(details: Dict[str, Any]) -> Dict[str, Any]:
    paths = details.get("paths")
    if isinstance(paths, list) and len(paths) > MAX_AUDITED_PATHS:
        return {**details, "paths": paths[:MAX_AUDITED_PATHS], "paths_total": len(paths)}
    return details


async def write(
    action: str,
    *,
    host: str,
    project: Optional[str],
    instance: str,
    result: str,
    details: Dict[str, Any],
    error_code: Optional[str] = None,
    started: Optional[float] = None,
) -> None:
    org_id, org_slug = await _org_of(host)
    activity_logger.log_activity(
        action=action,
        target_type="instance",
        target_id=instance,
        target_host=host,
        target_project=project,
        result=result,
        error_code=error_code,
        details=_bounded(details),
        duration_ms=(time.monotonic() - started) * 1000 if started is not None else None,
        target_organization_id=org_id,
        target_organization_slug=org_slug,
    )


@asynccontextmanager
async def audited(
    action: str,
    *,
    host: str,
    project: Optional[str],
    instance: str,
    details: Dict[str, Any],
) -> AsyncIterator[Dict[str, Any]]:
    """One record for one request, written whatever happens, refusals included.

    The body adds to the dictionary it receives, so sizes and counts known only at the end land in
    the same record.
    """
    started = time.monotonic()
    record = dict(details)
    code: Optional[str] = None
    result = "failure"
    try:
        yield record
        result = "success"
    except HTTPException as exc:
        detail = exc.detail
        code = str(detail.get("error")) if isinstance(detail, dict) else str(exc.status_code)
        raise
    except BaseException as exc:
        code = type(exc).__name__
        raise
    finally:
        await write(
            action, host=host, project=project, instance=instance, result=result,
            details=record, error_code=code, started=started,
        )


def should_record_browse(user: Optional[AuthenticatedUser], host: str, project: str, instance: str) -> bool:
    """Once per person, instance and quarter hour: the fact of browsing, not every folder opened."""
    key = (user.user_id if user else "anonymous", host, project, instance)
    now = time.monotonic()
    if len(_browsed) >= _MAX_BROWSE_KEYS:
        _browsed.clear()
    last = _browsed.get(key)
    if last is not None and now - last < _BROWSE_WINDOW_SECONDS:
        return False
    _browsed[key] = now
    return True


async def record_browse(
    user: Optional[AuthenticatedUser], host: str, project: str, instance: str, grant_id: Optional[int], path: str
) -> None:
    # important: under a break-glass grant every folder is recorded and ranks high. An Administrator
    # inside a tenant's guest is the case this log exists for, and a dedupe must not soften it.
    if grant_id is not None:
        await write(
            "file.browse.granted", host=host, project=project, instance=instance, result="success",
            details={"path": path, "break_glass_grant_id": grant_id},
        )
    elif should_record_browse(user, host, project, instance):
        await write(
            "file.browse", host=host, project=project, instance=instance, result="success",
            details={"path": path},
        )
