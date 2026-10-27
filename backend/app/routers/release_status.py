"""Whether the running version is out of date, for Administrators (services/release_check/).

GET answers from the last read of the release manifest; it never reaches the internet itself.
POST runs a read now, at most once a minute across every worker, and is audited.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.core.dependencies import require_admin
from app.services.activity_logger import get_activity_logger
from app.services.release_check import service
from app.services.release_check.store import get_store

router = APIRouter(
    prefix="/system/release-status",
    tags=["System"],
    dependencies=[Depends(require_admin())],
)


class ReleaseStatus(BaseModel):
    enabled: bool
    running_version: Optional[str] = None
    # critical | warning | info | none
    level: str
    # end_of_life, security_fix, deprecated, newer_release, unstamped, ahead_of_feed,
    # not_in_feed, no_feed, stale_feed, disabled
    reasons: List[str]
    running_status: Optional[str] = None
    latest_version: Optional[str] = None
    security_version: Optional[str] = None
    notes_url: Optional[str] = None
    upgrade_url: Optional[str] = None
    checked_at: Optional[str] = None
    last_success_at: Optional[str] = None
    source: Optional[str] = None
    error: Optional[str] = None
    stale: bool = False


@router.get("", response_model=ReleaseStatus)
async def get_release_status(response: Response):
    response.headers["Cache-Control"] = "no-store"
    state = await asyncio.to_thread(get_store().read)
    return ReleaseStatus(**service.describe(state))


@router.post("/check", response_model=ReleaseStatus)
async def check_release_status_now(response: Response):
    response.headers["Cache-Control"] = "no-store"
    if not service.get_settings().release_check_enabled:
        raise HTTPException(status_code=409, detail="The release check is turned off (RELEASE_CHECK_ENABLED=false).")
    wait = await asyncio.to_thread(get_store().take_cooldown, service.CHECK_NOW_COOLDOWN_SECONDS)
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail=f"The feed was read moments ago. Try again in {wait} seconds.",
            headers={"Retry-After": str(wait)},
        )
    state = await service.check()
    status = service.describe(state)
    failed = state.get("last_attempt_at") != state.get("last_success_at")
    get_activity_logger().log_activity(
        action="system.release_check.run",
        target_type="deployment",
        target_id="release_feed",
        result="failure" if failed else "success",
        error_message=state.get("error") if failed else None,
        details={"level": status["level"], "latest_version": status["latest_version"], "source": state.get("source")},
    )
    return ReleaseStatus(**status)
