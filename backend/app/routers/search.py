"""Global cross-resource search API.

One endpoint that fans out a query across every searchable resource. Uses the
same router-level guards as the resource list endpoints (``require_tenant`` +
``org_scope_dependency``) so the request-scoped org/ACL context is set before
the adapters reuse the per-resource list paths.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.dependencies import AuthenticatedUser, require_tenant
from app.core.org_scope import org_scope_dependency
from app.models.search_schemas import SearchResponse
from app.services.lxd_service import LXDService, get_lxd_service
from app.services.search.service import global_search

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/search",
    tags=["Search"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)


@router.get(
    "",
    response_model=SearchResponse,
    summary="Global cross-resource search",
    description="Search instances, projects, networks, storage, images, profiles, tags, "
    "local identities and audit entries, ranked by relevance and scoped to the caller's RBAC.",
)
async def search(
    q: str = Query("", description="Search query"),
    types: Optional[str] = Query(None, description="Comma-separated resource types to restrict the search to"),
    limit: int = Query(5, ge=1, le=50, description="Max results returned per resource type"),
    user: AuthenticatedUser = Depends(require_tenant()),
    lxd_service: LXDService = Depends(get_lxd_service),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SearchResponse:
    type_set = {t.strip() for t in types.split(",") if t.strip()} if types else None
    return await global_search(q, type_set, limit, user, lxd_service, db, settings)
