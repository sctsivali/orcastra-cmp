"""
Background Metrics Collector Service.
Collects monitoring data at regular intervals, independent of frontend requests.
This ensures historical data continuity even when no clients are connected.
"""

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
import threading

from app.core.blocking import BlockingTimeout, run_blocking

logger = logging.getLogger(__name__)

# note: background work gets its own small pool. Using the loop's default executor would put
# a periodic timer task in the same place FastAPI runs sync route handlers.
_metrics_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="metrics_bg")

# Generous: this walks every registered cluster and may open TLS connections. The point of
# the cap is that a stuck cluster cannot wedge the collector forever, not to be tight.
ENSURE_CONNECTIONS_TIMEOUT = float(
    os.environ.get("ORCASTRA_ENSURE_CONNECTIONS_TIMEOUT", "60")
)


class MetricsCollector:
    """
    Background service that collects and stores metrics at regular intervals.
    Runs independently of frontend requests to ensure continuous data collection.
    
    TIMING (roughly estimated):
    - Cache TTL: 180 seconds
    - Collection interval: 60 seconds  
    - Pre-warm takes ~25-45 seconds (instances + VMs + monitoring)
    - Total cycle: ~85-105 seconds (well within 180s TTL)
    """
    
    # Cache TTL for API responses (180s = 3 minutes)
    CACHE_TTL = 180
    
    def __init__(self, collection_interval: int = 60):
        """
        Initialize the metrics collector.
        """
        self.collection_interval = collection_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._lxd_service = None
        self._lock = threading.Lock()
        
    def start(self, lxd_service=None):
        """Start the background collector."""
        if self._running:
            logger.info("Metrics collector already running")
            return
            
        self._lxd_service = lxd_service
        self._running = True
        
        # Create task in the current event loop
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                self._task = loop.create_task(self._collection_loop())
            else:
                # If no event loop is running, we'll start it when one is available
                logger.info("Event loop not running, collector will start when available")
        except RuntimeError:
            logger.info("No event loop, collector will start on first request")
            
        logger.info(f"Metrics collector started with {self.collection_interval}s interval")
        
    def stop(self):
        """Stop the background collector."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Metrics collector stopped")
        
    async def start_async(self, lxd_service=None):
        """Start the collector asynchronously."""
        if self._running:
            return
            
        self._lxd_service = lxd_service
        self._running = True
        self._task = asyncio.create_task(self._collection_loop())
        logger.info(f"Metrics collector started async with {self.collection_interval}s interval")
        
    async def _collection_loop(self):
        """Main collection loop that runs in the background."""
        logger.info("Background metrics collection loop started")
        
        # Minimal delay just to ensure server is ready
        await asyncio.sleep(2)
        
        # Immediately collect metrics on startup to warm up cache
        logger.info("Performing initial metrics collection to warm up cache...")
        try:
            await self._collect_metrics()
            logger.info("Initial metrics collection completed - cache is warm")
        except Exception as e:
            logger.warning(f"Initial metrics collection failed: {e}")
        
        while self._running:
            try:
                await self._collect_metrics()
            except Exception as e:
                logger.error(f"Error in metrics collection: {e}", exc_info=True)
            
            # Wait for next collection interval
            await asyncio.sleep(self.collection_interval)
            
        logger.info("Background metrics collection loop ended")
        
    async def _collect_metrics(self):
        """
        Collect metrics by calling the dashboard endpoint logic.
        This ensures consistent data collection through a single code path.
        Also performs connection health checks to keep clusters connected.
        """
        if not self._lxd_service:
            # Try to get the service if not set
            try:
                from app.services.lxd_service import get_lxd_service
                self._lxd_service = get_lxd_service()
            except Exception as e:
                logger.warning(f"Could not get LXD service: {e}")
                return
        
        try:
            # IMPORTANT: Perform connection health check first
            # This ensures clusters stay connected even if they were disconnected
            # due to timeout, network issues, or container restart
            await self._ensure_cluster_connections()
            
            # PRE-WARM INSTANCES CACHE - Critical for performance
            # This keeps the instances endpoint fast (32ms vs 41s)
            await self._prewarm_instances_cache()
            
            # Import here to avoid circular dependency
            from app.routers.monitoring import get_monitoring_dashboard
            
            # Call the dashboard endpoint - it handles all metrics collection and history storage
            # This ensures a single code path for metrics collection, avoiding race conditions
            # fresh=True bypasses the 35s dashboard cache so the collector always
            # performs a full collection and stores a history point every cycle
            result = await get_monitoring_dashboard(lxd_service=self._lxd_service, fresh=True)
            logger.debug("Background collector triggered dashboard metrics collection")
            
            # Push to WebSocket subscribers (if any connected)
            try:
                from app.services.ws_broadcast import get_monitoring_broadcaster
                broadcaster = get_monitoring_broadcaster()
                if broadcaster.client_count > 0:
                    # result is a Pydantic model or dict
                    data = result.model_dump() if hasattr(result, 'model_dump') else result
                    await broadcaster.broadcast(data)
                    logger.debug(f"Pushed monitoring data to {broadcaster.client_count} WS clients")
            except Exception as ws_err:
                logger.debug(f"WS broadcast skipped: {ws_err}")
            
        except Exception as e:
            logger.warning(f"Background metrics collection: {e}")
    
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
            from app.core.org_scope import get_scope_cache_key
            
            cache = get_cache_manager()
            
            # Pre-warm instances cache (most critical for dashboard)
            await self._prewarm_single_cache(
                cache,
                get_scope_cache_key("instances:list:host=None:status=None:type=None:o=0:l=0"),
                self._fetch_instances
            )
            
            # Pre-warm VMs cache (VM-only view) - key: vms:list:host=None:status=None
            await self._prewarm_single_cache(
                cache,
                get_scope_cache_key("vms:list:host=None:status=None:o=0:l=0"),
                self._fetch_vms
            )
            
            # Pre-warm nodes cache
            await self._prewarm_single_cache(
                cache,
                get_scope_cache_key("nodes:status:all"),
                self._fetch_nodes
            )
            
            # Pre-warm regions cache (slowest endpoint, 15-45s cold)
            await self._prewarm_single_cache(
                cache,
                get_scope_cache_key("regions:overview"),
                self._fetch_regions
            )
            
            # Pre-warm storage pool resources cache
            await self._prewarm_single_cache(
                cache,
                get_scope_cache_key("storage_pool_resources:all"),
                self._fetch_storage_pool_resources
            )
            
        except Exception as e:
            logger.warning(f"Failed to pre-warm caches: {e}")
    
    async def _prewarm_single_cache(self, cache, cache_key: str, fetch_func):
        """Pre-warm a single cache entry if expired."""
        try:
            cached = cache.get("api_responses", cache_key)
            if cached:
                logger.debug(f"Cache already warm: {cache_key}")
                return
            
            logger.info(f"Pre-warming cache: {cache_key}")
            start = asyncio.get_event_loop().time()
            
            data = await fetch_func()
            if data:
                cache.set("api_responses", cache_key, data, ttl=180)
                elapsed = asyncio.get_event_loop().time() - start
                logger.info(f"Cache warmed in {elapsed:.2f}s: {cache_key}")
                
        except Exception as e:
            logger.debug(f"Pre-warm failed for {cache_key}: {e}")
    
    async def _fetch_instances(self):
        """Fetch instances data for cache warming."""
        from app.models.schemas import VMListResponse
        all_vms, connected_nodes = await self._lxd_service.list_all_vms()
        response = VMListResponse(
            total=len(all_vms),
            vms=all_vms,
            nodes=connected_nodes
        )
        return response.model_dump()
    
    async def _fetch_vms(self):
        """Fetch VMs data for cache warming."""
        from app.models.schemas import VMListResponse
        all_vms, connected_nodes = await self._lxd_service.list_all_vms()
        # Filter to VMs only (type == virtual-machine)
        vms_only = [vm for vm in all_vms if vm.type == "virtual-machine"]
        response = VMListResponse(
            total=len(vms_only),
            vms=vms_only,
            nodes=connected_nodes
        )
        return response.model_dump()
    
    async def _fetch_nodes(self):
        """Fetch nodes status for cache warming."""
        nodes = self._lxd_service.connection_manager.get_node_names()
        # Return basic structure - the actual nodes endpoint will fill in details
        return {"nodes": nodes, "count": len(nodes)}
    
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
    
    async def _fetch_storage_pool_resources(self):
        """Fetch storage pool resources for cache warming."""
        try:
            from app.routers.storage_pools import _serialize_pool_resources
            all_resources = {}
            nodes = self._lxd_service.connection_manager.get_node_names()
            for node_name in nodes:
                try:
                    client = self._lxd_service.get_client_with_reconnect(node_name)
                    if client is None:
                        continue
                    pools = client.storage_pools.all()
                    for pool in pools:
                        try:
                            resources = pool.resources.get()
                            all_resources[f"{node_name}-{pool.name}"] = _serialize_pool_resources(resources)
                        except Exception:
                            pass
                except Exception:
                    pass
            return {"resources": all_resources}
        except Exception as e:
            logger.debug(f"Storage pool resources pre-warm failed: {e}")
            return None
    
    async def _ensure_cluster_connections(self):
        """Run the connection sweep off the event loop.

        important: the body below is entirely blocking (Vault reads, TLS handshakes, LXD
        probes) and it runs on a timer. Inline it stalled the loop on every cycle. Measured
        with py-spy on dev, 42 of 127 event-loop samples were parked in
        connect_with_vault_cert reached from here, which is what drove /health to 11s and to
        outright timeouts. Background work must never be the reason a request stalls.
        """
        try:
            await run_blocking(
                self._ensure_cluster_connections_blocking,
                timeout=ENSURE_CONNECTIONS_TIMEOUT,
                label="metrics.ensure_cluster_connections",
                executor=_metrics_executor,
            )
        except BlockingTimeout:
            logger.warning(
                f"Cluster connection sweep exceeded {ENSURE_CONNECTIONS_TIMEOUT}s, "
                "skipping this cycle"
            )
        except Exception as e:
            logger.debug(f"Cluster connection health check error: {e}")

    def _ensure_cluster_connections_blocking(self):
        """
        Ensure all registered clusters are connected.
        This is crucial for maintaining cluster status as "Online" rather than "Pending Trust".
        
        ARCHITECTURE (2-tier - Security Best Practice):
        - Memory: Connection info stored during runtime
        - Vault: Source of truth for certificates (NO Redis)
        
        RECONNECTION LOGIC:
        - Only reconnect clusters that are NOT already connected
        - NO TTL refresh (connections persist while valid)
        - Use get_client() which validates and auto-reconnects if needed
        """
        try:
            from app.core.config import get_settings
            from app.core.vault_client import get_vault_client
            from app.services.lxd_cert_manager import LXDCertificateManager
            
            settings = get_settings()
            
            if not settings.vault_enabled or not settings.vault_token:
                return
            
            vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
            cert_manager = LXDCertificateManager(
                vault_client=vault_client,
                clusters_path=settings.vault_clusters_path
            )
            
            # Get all registered cluster IDs
            cluster_ids = cert_manager.list_clusters()
            
            reconnected = 0
            
            for cluster_id in cluster_ids:
                try:
                    # get_client() validates and returns existing connection if valid
                    # or auto-reconnects using stored connection info
                    client = self._lxd_service.connection_manager.get_client(cluster_id)
                    
                    if client is None:
                        # No stored connection info, need to fetch from Vault
                        if not self._lxd_service.connection_manager.has_connection_info(cluster_id):
                            try:
                                cert_data = cert_manager.get_cluster_certificate(cluster_id)
                                cluster_info = cert_manager.get_cluster_info(cluster_id)
                                
                                healed = self._lxd_service.connection_manager.connect_with_vault_cert(
                                    cluster_id=cluster_id,
                                    endpoint=cluster_info["metadata"]["endpoint"],
                                    cert_content=cert_data["client.crt"],
                                    key_content=cert_data["client.key"],
                                    verify=False
                                )
                                # note: the breaker can refuse this and return None. Counting that
                                # as a heal made the sweep report recoveries that never happened.
                                if healed is not None:
                                    reconnected += 1
                                    logger.info(f"Background connected cluster: {cluster_id}")
                            except Exception as conn_err:
                                # Expected for clusters without valid trust
                                logger.debug(f"Could not connect {cluster_id}: {conn_err}")
                except Exception as e:
                    logger.debug(f"Health check failed for {cluster_id}: {e}")
            
            if reconnected > 0:
                logger.info(f"Background health check connected {reconnected} cluster(s)")
                
        except Exception as e:
            logger.debug(f"Cluster connection health check error: {e}")


# Singleton instance
_metrics_collector: Optional[MetricsCollector] = None


def get_metrics_collector() -> MetricsCollector:
    """Get or create the metrics collector singleton."""
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = MetricsCollector(collection_interval=30)
    return _metrics_collector


async def start_background_collector(lxd_service=None):
    """Start the background metrics collector."""
    collector = get_metrics_collector()
    await collector.start_async(lxd_service)
    return collector


def stop_background_collector():
    """Stop the background metrics collector."""
    global _metrics_collector
    if _metrics_collector:
        _metrics_collector.stop()
