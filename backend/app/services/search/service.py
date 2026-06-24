"""Global search orchestration.

Runs the role-permitted adapters concurrently, ranks their groups, and returns
one unified response. A failing source degrades gracefully (partial results +
``degraded=true``) instead of failing the whole search. The response is cached
under a scope+ACL-aware key so two tenants can never share each other's results.
"""

import asyncio
import logging
import time
from typing import Optional, Set

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser
from app.core.cache import get_cache_manager
from app.core.config import Settings
from app.core.org_scope import get_scope_cache_key
from app.core.rbac import Role, get_highest_role, resolve_roles
from app.models.search_schemas import SearchResponse
from app.services.search.adapters import ADAPTERS, SearchContext

logger = logging.getLogger(__name__)

# 2 chars min: a single char matches almost everything and wastes a fan-out.
# Matches the frontend's MIN_SEARCH_LEN so a direct API caller can't undercut it.
MIN_QUERY_LEN = 2
_CACHE_TTL = 20  # seconds; underlying list calls have their own 30-180s caches


async def global_search(
    query: str,
    types: Optional[Set[str]],
    per_type_limit: int,
    user: Optional[AuthenticatedUser],
    lxd_service,
    db: AsyncSession,
    settings: Settings,
) -> SearchResponse:
    q = (query or "").strip()
    if len(q) < MIN_QUERY_LEN:
        return SearchResponse(query=q, total=0, groups=[], degraded=False, took_ms=0)

    started = time.perf_counter()
    # dev bypass (AUTH_ENABLED=false) yields no user; treat as admin like the RBAC layer does.
    highest = get_highest_role(resolve_roles(user)) if user else Role.ADMIN

    cache = get_cache_manager()
    type_key = ",".join(sorted(types)) if types else "all"
    cache_key = get_scope_cache_key(f"search:q={q.lower()}:types={type_key}:l={per_type_limit}")
    cached = cache.get("api_responses", cache_key)
    if cached is not None:
        resp = SearchResponse(**cached)
        resp.took_ms = int((time.perf_counter() - started) * 1000)
        return resp

    ctx = SearchContext(
        query=q,
        per_type_limit=per_type_limit,
        lxd_service=lxd_service,
        db=db,
        user=user,
        highest_role=highest,
        settings=settings,
    )
    selected = [a for a in ADAPTERS if a.enabled(ctx) and (types is None or a.type in types)]

    results = await asyncio.gather(*(a.run(ctx) for a in selected), return_exceptions=True)

    groups = []
    degraded = False
    for adapter, res in zip(selected, results):
        if isinstance(res, Exception):
            logger.warning("search adapter '%s' failed: %s", adapter.type, res)
            degraded = True
            continue
        if res.total > 0:
            groups.append(res)

    # most relevant group first (ranked by its best item's score)
    groups.sort(key=lambda g: max((i.score for i in g.items), default=0), reverse=True)
    total = sum(g.total for g in groups)

    resp = SearchResponse(
        query=q,
        took_ms=int((time.perf_counter() - started) * 1000),
        degraded=degraded,
        total=total,
        groups=groups,
    )
    cache.set("api_responses", cache_key, resp.model_dump(), ttl=_CACHE_TTL)
    return resp
