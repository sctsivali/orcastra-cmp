"""API cache prewarm run by the background collector's leader.

The collector keeps a few slow list endpoints warm so a request after a TTL expiry does not pay
for a fleet-wide fan-out. Moved out of metrics_collector unchanged; MetricsCollector mixes it in
and provides `_lxd_service`.
"""

import asyncio
import logging
from typing import List

logger = logging.getLogger(__name__)


class CachePrewarm:
    """Writes the cache entries the list endpoints read, with each endpoint's own TTL."""

    _lxd_service: object

    async def _prewarm_instances_cache(self):
        """
        Pre-warm critical API caches to ensure fast response times.
        This runs every 30 seconds to keep caches fresh before TTL expiry (120s).
        
        Performance impact (roughly estimated):
        - Cold cache: ~11 seconds for instances, varies for others
        - Warm cache: ~25-50 milliseconds
        """
        try:
            from app.core.cache import get_cache_manager
            # important: prewarm keys must be built the same way the endpoints
            # build theirs. Without get_scope_cache_key() they lacked the
            # ":scope=all" suffix, so nothing ever read them and every request
            # after a TTL expiry still paid the full cold cost.
            from app.core.cache import REGIONS_OVERVIEW_CACHE_KEY
            from app.core.org_scope import get_scope_cache_key
            
            cache = get_cache_manager()
            
            # important: the TTL belongs to the endpoint, not to this loop. A single
            # hardcoded 180 pinned the instances list six times longer than GET /instances
            # intends, so a prewarm both wrote a key the endpoint owns and changed how long
            # it lived. Each of these matches its handler's own cache.set.

            # note: no instances prewarm. It wrote "instances:list:host=None:...", a key GET
            # /instances never reads (its key is "instances:list:v2:...:project=..."), so every
            # worker paid a full recursion=2 fleet list each cycle for a value nobody used.

            # Pre-warm VMs cache (VM-only view) - key: vms:list:host=None:status=None
            await self._prewarm_single_cache(
                cache,
                get_scope_cache_key("vms:list:host=None:status=None:o=0:l=0"),
                self._fetch_vms,
                ttl=180,
            )

            # Pre-warm regions cache (slowest endpoint, 15-45s cold)
            await self._prewarm_single_cache(
                cache,
                get_scope_cache_key(REGIONS_OVERVIEW_CACHE_KEY),
                self._fetch_regions,
                ttl=120,
            )

            # Storage pool resources are warmed through their own handler.
            # important: the fetcher this replaced called pylxd directly from this coroutine,
            # serially per cluster, on the leader's event loop: a median 5.7 s and up to 13.9 s
            # in which that worker answered no request at all. The handler reads every cluster
            # in its thread pool, and it writes the entry itself behind its generation fence.
            await self._prewarm_storage_pool_resources()

            # Pre-warm cluster availability so a node picker never pays for a probe sweep. The
            # resolver owns its key: it answers from a fresh entry, serves a stale one while one
            # background read replaces it, and writes behind its own generation fence.
            # important: unlike the list fan-outs above, an all-offline result IS the answer
            # here and is cached. "Everything is offline" is precisely the fact a picker needs.
            await self._prewarm_availability()
            
        except Exception as e:
            logger.warning(f"Failed to pre-warm caches: {e}")
    
    async def _prewarm_single_cache(self, cache, cache_key: str, fetch_func, ttl: int):
        """Pre-warm a single cache entry if expired.

        important: a fetcher returns None to mean "nobody answered, do not remember this".
        The endpoints refuse to cache a fan-out no cluster answered, and this loop writes
        the same keys, so without that refusal here the collector reinstated the outage
        30s later through a side door and the endpoint's own guard accomplished nothing.
        """
        try:
            cached = cache.get("api_responses", cache_key)
            if cached:
                logger.debug(f"Cache already warm: {cache_key}")
                return

            logger.info(f"Pre-warming cache: {cache_key}")
            start = asyncio.get_event_loop().time()

            data = await fetch_func()
            if data:
                cache.set("api_responses", cache_key, data, ttl=ttl)
                elapsed = asyncio.get_event_loop().time() - start
                logger.info(f"Cache warmed in {elapsed:.2f}s: {cache_key}")
            else:
                logger.warning(f"Not warming {cache_key}: no cluster answered")

        except Exception as e:
            logger.debug(f"Pre-warm failed for {cache_key}: {e}")
    
    def _unreachable(self, connected_nodes: List[str]) -> List[str]:
        """Registered clusters that did not answer this sweep.

        important: a prewarm writes the payload the endpoint later serves, so any field the
        endpoint fills must be filled here too. Leaving this to the model default wrote an
        empty list into the cache, and every unforced read then reported a healthy fleet
        while fourteen clusters were down. The same shape mismatch took out GET /nodes.
        """
        return sorted(set(self._lxd_service.get_node_names()) - set(connected_nodes))

    async def _fetch_vms(self):
        """Fetch VMs data for cache warming."""
        from app.models.schemas import VMListResponse
        all_vms, connected_nodes = await self._lxd_service.list_all_vms()
        if not connected_nodes:
            # mirrors the guard in routers/vms.py
            return None
        # Filter to VMs only (type == virtual-machine)
        vms_only = [vm for vm in all_vms if vm.type == "virtual-machine"]
        response = VMListResponse(
            total=len(vms_only),
            vms=vms_only,
            nodes=connected_nodes,
            unreachable_nodes=self._unreachable(connected_nodes),
        )
        return response.model_dump()
    
    # note: there is deliberately no nodes prewarm. It used to write
    # {"nodes": [<names>], "count": N} to a key nobody read, so the shape mismatch
    # against NodesStatusResponse was invisible. Once the keys were corrected to
    # carry the scope suffix the two collided and GET /nodes returned 500 for the
    # rest of the TTL. Warming it properly means replicating the handler's Vault
    # cluster merge, which is not worth duplicating; get_node_names() never did
    # the expensive work anyway, so nothing is lost by leaving this cold.
    
    async def _fetch_regions(self):
        """Fetch regions overview for cache warming."""
        try:
            from app.routers.regions import get_regions_overview
            result = await get_regions_overview(lxd_service=self._lxd_service)
            # result is a dict or Pydantic model
            if hasattr(result, 'model_dump'):
                return result.model_dump()
            return result
        except Exception as e:
            logger.debug(f"Regions pre-warm fetch failed: {e}")
            return None
    
    async def _prewarm_availability(self):
        """Warm cluster availability through the resolver the endpoints share."""
        try:
            from app.services.lxd import status as lxd_status
            await lxd_status.resolve_availability(self._lxd_service)
        except Exception as e:
            logger.debug(f"Availability pre-warm failed: {e}")

    async def _prewarm_storage_pool_resources(self):
        """Warm GET /storage/pools/resources-all by calling the endpoint's own handler."""
        try:
            from app.routers.storage_pools import get_all_storage_pool_resources
            await get_all_storage_pool_resources(lxd_service=self._lxd_service, fresh=False)
        except Exception as e:
            logger.debug(f"Storage pool resources pre-warm failed: {e}")
