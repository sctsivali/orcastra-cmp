"""API cache prewarm run by the background collector's leader.

The collector keeps a few slow list endpoints warm so a request after a TTL expiry does not pay
for a fleet-wide fan-out. MetricsCollector mixes this in and provides `_lxd_service`.
"""

import asyncio
import logging
from typing import Any, Callable, List

logger = logging.getLogger(__name__)


class CachePrewarm:
    """Writes the cache entries the list endpoints read, with each endpoint's own TTL."""

    _lxd_service: object

    async def _prewarm_instances_cache(self):
        """Pre-warm the slow list caches once per collector cycle, before their TTLs expire.

        Covers the VM list, regions, storage pool resources and cluster availability.
        """
        try:
            from app.core.cache import get_cache_manager
            # important: prewarm keys must be built the same way the endpoints
            # build theirs. Without get_scope_cache_key() a key lacks the
            # ":scope=all" suffix and no endpoint ever reads it.
            from app.core.org_scope import get_scope_cache_key
            
            cache = get_cache_manager()
            
            # important: the TTL belongs to the endpoint. Each TTL here matches its handler's
            # own cache.set, so a prewarm never changes how long the endpoint's key lives.

            # No instances prewarm. GET /instances keys its list as
            # "instances:list:v3:host=...:project=...:status=...", and a list written under any
            # other key goes unread while costing a full recursion=2 fan-out each cycle.

            # VM-only list view, as GET /vms caches it.
            await self._prewarm_single_cache(
                cache,
                get_scope_cache_key("vms:list:host=None:status=None:o=0:l=0"),
                self._fetch_vms,
                ttl=180,
                generation=cache.instance_list_generation,
            )

            # Pre-warm regions through its own builder: it is served stale-while-revalidate, and
            # a plain body written here would read as fresh with no age (cache_envelope.unwrap).
            await self._prewarm_regions()

            # Storage pool resources are warmed through their own handler, which reads every
            # cluster in its thread pool and writes the entry itself behind its generation fence.
            # important: never call pylxd from this coroutine. Serial per-cluster reads on the
            # leader's event loop leave that worker answering no request for seconds.
            await self._prewarm_storage_pool_resources()

            # Pre-warm cluster availability so a node picker never pays for a probe sweep. The
            # resolver owns its key: it answers from a fresh entry, serves a stale one while one
            # background read replaces it, and writes behind its own generation fence.
            # Unlike the list fan-outs above, an all-offline result is the answer here and is
            # cached: "everything is offline" is the fact a picker needs.
            await self._prewarm_availability()
            
        except Exception as e:
            logger.warning(f"Failed to pre-warm caches: {e}")
    
    async def _prewarm_single_cache(
        self,
        cache,
        cache_key: str,
        fetch_func,
        ttl: int,
        generation: Callable[[], Any],
    ):
        """Pre-warm a single cache entry if expired.

        important: a fetcher returns None to mean "nobody answered, do not remember this".
        The endpoints refuse to cache a fan-out no cluster answered, and this loop writes
        the same keys, so it must refuse too or it would reinstate the outage 30 s later.

        important: the write sits behind the endpoint's own generation fence. A key is most
        likely missing right after a mutation cleared it, which is when this read runs; a read
        that began before an invalidation would otherwise write the pre-mutation list back for
        its TTL.
        """
        try:
            cached = cache.get("api_responses", cache_key)
            if cached:
                logger.debug(f"Cache already warm: {cache_key}")
                return

            logger.info(f"Pre-warming cache: {cache_key}")
            start = asyncio.get_event_loop().time()

            before = generation()
            data = await fetch_func()
            if data and generation() != before:
                logger.info(f"Not warming {cache_key}: an invalidation landed during the read")
            elif data:
                cache.set("api_responses", cache_key, data, ttl=ttl)
                if generation() != before:
                    cache.delete("api_responses", cache_key)
                elapsed = asyncio.get_event_loop().time() - start
                logger.info(f"Cache warmed in {elapsed:.2f}s: {cache_key}")
            else:
                logger.warning(f"Not warming {cache_key}: no cluster answered")

        except Exception as e:
            logger.debug(f"Pre-warm failed for {cache_key}: {e}")
    
    def _unreachable(self, connected_nodes: List[str]) -> List[str]:
        """Registered clusters that did not answer this sweep.

        important: a prewarm writes the payload the endpoint later serves, so any field the
        endpoint fills must be filled here too. Left to the model default, the cached list is
        empty and every unforced read reports a healthy fleet.
        """
        return sorted(set(self._lxd_service.get_node_names()) - set(connected_nodes))

    async def _fetch_vms(self):
        """Fetch VMs data for cache warming."""
        from app.models.schemas import VMListResponse
        from app.services.lxd.fanout import complete_reasons
        all_vms, connected_nodes = await self._lxd_service.list_all_vms()
        if not connected_nodes:
            # mirrors the guard in routers/vms.py
            return None
        vms_only = [vm for vm in all_vms if vm.type == "virtual-machine"]
        unreachable_nodes = self._unreachable(connected_nodes)
        response = VMListResponse(
            total=len(vms_only),
            vms=vms_only,
            nodes=connected_nodes,
            unreachable_nodes=unreachable_nodes,
            node_errors=complete_reasons(unreachable_nodes, {}),
        )
        return response.model_dump()
    
    # No nodes prewarm. Warming GET /nodes means replicating the handler's Vault
    # cluster merge to build a NodesStatusResponse, and any other shape under the
    # same key makes GET /nodes answer 500 for the rest of the TTL.
    # get_node_names() is cheap, so nothing is lost by leaving it cold.
    
    async def _prewarm_regions(self):
        """Warm the regions overview through its builder, which stores the entry itself.

        important: use the builder, never the route. The route projects for a caller, and this
        shared entry has none. fresh=False: a warm entry is a hit, a stale one starts its own
        background read, and only a missing one is built here.
        """
        try:
            from app.routers.regions import build_regions_overview
            await build_regions_overview(lxd_service=self._lxd_service, fresh=False)
        except Exception as e:
            logger.debug(f"Regions pre-warm failed: {e}")
    
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
