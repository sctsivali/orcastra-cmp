"""Host security advisories: which cluster members run a kernel with a known host-side flaw.

Partner and Admin only. A Tenant has no business with host facts, and the org scope still
limits a Partner to the clusters bound to its organization.

GETs answer from cached kernel facts and the last stored reads of Ubuntu's records; none of them
reaches the internet. POST .../check reads Ubuntu now: Administrators only, at most once a minute
across every worker, and audited.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.core.cache import get_cache_manager
from app.core.dependencies import require_admin, require_partner
from app.core.org_scope import org_scope_dependency, verify_cluster_access
from app.core.single_flight import single_flight
from app.models.kernel_advisory_schemas import KernelAdvisoryResponse, KernelCveFeedStatus
from app.services.activity_logger import get_activity_logger
from app.services.kernel_advisory_report import build_kernel_report, feed_status, read_feed_status
from app.services.kernel_cve_feed import service as cve_feed
from app.services.lxd_service import LXDService, get_lxd_service

logger = logging.getLogger(__name__)

# Wall clock this request waits per cluster. A cluster still reading past it keeps going in its
# thread and fills the cache for the next request.
KERNEL_ADVISORY_WALL_SECONDS = 30.0

router = APIRouter(
    prefix="/security",
    tags=["Security"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)


@router.get(
    "/kernel-advisories",
    response_model=KernelAdvisoryResponse,
    summary="Host kernel advisories",
    description=(
        "Each cluster member's running kernel judged against known host kernel advisories "
        "(patched, exposed or unverified), with the command that settles an unverified one."
    ),
)
@single_flight("security:kernel_advisories")
async def list_kernel_advisories(
    host: Optional[str] = Query(None, description="Limit to one registered cluster"),
    fresh: bool = False,
    lxd_service: LXDService = Depends(get_lxd_service),
) -> KernelAdvisoryResponse:
    if host:
        verify_cluster_access(host)
    try:
        return await build_kernel_report(
            lxd_service, host=host, fresh=fresh, wall=KERNEL_ADVISORY_WALL_SECONDS
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Could not build kernel advisories: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not read the cluster members' kernels") from exc


@router.get(
    "/kernel-advisories/ubuntu-status",
    response_model=KernelCveFeedStatus,
    summary="When Ubuntu's kernel CVE records were last read",
    description="The stored reads of Ubuntu's record of each tracked kernel CVE. Never reaches the internet.",
)
async def get_kernel_cve_ubuntu_status(response: Response) -> KernelCveFeedStatus:
    response.headers["Cache-Control"] = "no-store"
    return await read_feed_status()


@router.post(
    "/kernel-advisories/check",
    response_model=KernelCveFeedStatus,
    summary="Read Ubuntu's kernel CVE records now",
    dependencies=[Depends(require_admin())],
)
async def check_kernel_cves_now(response: Response) -> KernelCveFeedStatus:
    response.headers["Cache-Control"] = "no-store"
    if not cve_feed.get_settings().kernel_cve_feed_enabled:
        raise HTTPException(
            status_code=409, detail="The Ubuntu check is turned off (KERNEL_CVE_FEED_ENABLED=false)."
        )
    wait = await asyncio.to_thread(cve_feed.get_store().take_cooldown, cve_feed.CHECK_NOW_COOLDOWN_SECONDS)
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail=f"Ubuntu was checked moments ago. Try again in {wait} seconds.",
            headers={"Retry-After": str(wait)},
        )
    states = await cve_feed.check()
    errors = {cve_id: state.get("error") for cve_id, state in states.items() if state.get("error")}
    try:
        # The warnings feed caches its kernel rows; the next read must judge with the new records.
        await asyncio.to_thread(get_cache_manager().invalidate_resource_cache, "warnings")
    except Exception as exc:  # noqa: BLE001 - the feed's own TTL still catches up
        logger.warning("Warnings cache not cleared after the Ubuntu check: %s", type(exc).__name__)
    status = feed_status(
        {cve_id: cve_feed.view(cve_id, state, True) for cve_id, state in states.items()}
    )
    get_activity_logger().log_activity(
        action="system.kernel_cve_check.run",
        target_type="deployment",
        target_id="kernel_cve_feed",
        result="failure" if errors else "success",
        error_message="; ".join(f"{cve_id}: {error}" for cve_id, error in errors.items()) or None,
        details={
            "checked": sorted(states),
            "mirror": status.mirror,
            "priorities": {check.id: check.ubuntu_priority for check in status.advisories},
        },
    )
    return status
