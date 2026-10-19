"""Host security advisories: which cluster members run a kernel with a known host-side flaw.

Partner and Admin only. A Tenant has no business with host facts, and the org scope still
limits a Partner to the clusters bound to its organization.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.dependencies import require_partner
from app.core.org_scope import org_scope_dependency, verify_cluster_access
from app.core.single_flight import single_flight
from app.models.kernel_advisory_schemas import KernelAdvisoryResponse
from app.services.kernel_advisory_report import build_kernel_report
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
        raise HTTPException(status_code=500, detail="Could not read host kernels") from exc
