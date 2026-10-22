"""
Background Metrics Collector Service.
Collects monitoring data at regular intervals, independent of frontend requests.
This ensures historical data continuity even when no clients are connected.
"""

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
import threading

from app.core import vault_status
from app.core.blocking import BlockingTimeout, run_blocking
from app.services.collector_leadership import CollectorLease, DashboardRelay
from app.services.metrics_prewarm import CachePrewarm

logger = logging.getLogger(__name__)

# note: background work gets its own small pool. Using the loop's default executor would put
# a periodic timer task in the same place FastAPI runs sync route handlers.
_metrics_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="metrics_bg")

# Generous: this walks every registered cluster and may open TLS connections. The point of
# the cap is that a stuck cluster cannot wedge the collector forever, not to be tight.
ENSURE_CONNECTIONS_TIMEOUT = float(
    os.environ.get("ORCASTRA_ENSURE_CONNECTIONS_TIMEOUT", "60")
)


class MetricsCollector(CachePrewarm):
    """
    Background service that collects and stores metrics at regular intervals.
    Runs independently of frontend requests to ensure continuous data collection.

    One worker leads: it sweeps the fleet for the dashboard, then pauses for
    `collection_interval` seconds, so a cycle is the sweep plus the pause. Every other worker
    keeps its own cluster connections and relays the leader's dashboard to its own WebSocket
    clients without sweeping anything itself.
    """

    # important: the collector is started from the FastAPI lifespan, which runs once per worker.
    # With WEB_CONCURRENCY=4 that is four collectors; see collector_leadership for how one of
    # them leads and the others only relay. Over a day on the dev fleet, before that, the
    # dashboard was built three to four times per cycle and about 70% of history points were
    # copies.
    COLLECTION_LEASE_KEY = "orcastra:metrics_collector:cycle_lease"
    # important: short, and kept alive by a heartbeat rather than by the cycle. A lease long
    # enough to outlive a cycle also outlived a dead leader: on a deploy the old container's
    # workers keep collecting while uvicorn drains connections, Docker kills them before the
    # lifespan hands the lease back, and nothing swept until it lapsed, measured at two and a
    # half minutes. Renewed every 5 s, a 20 s lease holds through any sweep and a dead leader
    # is replaced within 20 s.
    LEASE_SECONDS = 20
    HEARTBEAT_SECONDS = 5.0
    # How often a follower looks for a new dashboard to relay while it waits out the interval.
    RELAY_POLL_SECONDS = 5.0

    def __init__(self, collection_interval: int = 60):
        """
        Initialize the metrics collector.
        """
        self.collection_interval = collection_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._lxd_service = None
        self._lock = threading.Lock()
        self._lease = CollectorLease(self.COLLECTION_LEASE_KEY, self.LEASE_SECONDS, _shared_redis)
        self._dashboards = DashboardRelay()
        self._leading = False
        self._prewarm_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        
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
                self._heartbeat_task = loop.create_task(self._heartbeat_loop())
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
        for task in (self._heartbeat_task, self._prewarm_task):
            if task:
                task.cancel()
        self._heartbeat_task = None
        self._prewarm_task = None
        self._lease.release()
        logger.info("Metrics collector stopped")
        
    async def start_async(self, lxd_service=None):
        """Start the collector asynchronously."""
        if self._running:
            return
            
        self._lxd_service = lxd_service
        self._running = True
        self._task = asyncio.create_task(self._collection_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(f"Metrics collector started async with {self.collection_interval}s interval")
        
    async def _collection_loop(self):
        """Main collection loop that runs in the background."""
        logger.info("Background metrics collection loop started")
        
        # Minimal delay just to ensure server is ready
        await asyncio.sleep(2)

        # note: the first cycle runs at once and warms the cache. A separate warm-up call used to
        # run here as well, which swept the whole fleet twice back to back on every start.
        while self._running:
            try:
                await self._collect_metrics()
            except Exception as e:
                logger.error(f"Error in metrics collection: {e}", exc_info=True)

            await self._relay_until_next_cycle()

        logger.info("Background metrics collection loop ended")

    async def _heartbeat_loop(self) -> None:
        """Keep the lease while leading, and take it over as soon as it lapses."""
        while self._running:
            self._leading = self._claim_collection_lease()
            await asyncio.sleep(self.HEARTBEAT_SECONDS)

    async def _relay_until_next_cycle(self) -> None:
        """Wait out the interval. A follower relays a new dashboard as soon as one lands, and
        one that has just taken over the lease starts its first sweep at once."""
        following = not self._leading
        deadline = time.monotonic() + self.collection_interval
        while self._running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(self.RELAY_POLL_SECONDS, remaining))
            if not self._leading:
                await self._dashboards.relay()
            elif following:
                return

    def _claim_collection_lease(self) -> bool:
        """True when this worker leads this cycle: it took the lease or renewed its own."""
        return self._lease.claim()

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
            self._leading = self._claim_collection_lease()

            # IMPORTANT: Perform connection health check first, in every worker: each one holds
            # its own cluster connections. This ensures clusters stay connected even if they
            # were disconnected due to timeout, network issues, or container restart.
            await self._ensure_cluster_connections()

            if not self._leading:
                # important: a follower never builds the dashboard, not even on a cache miss.
                # It relays what the leader stored; a miss only means nothing is relayed yet.
                await self._dashboards.relay()
                return

            # Import here to avoid circular dependency
            from app.routers.monitoring import get_monitoring_dashboard

            # The leader builds the dashboard through the endpoint's own code path, which also
            # stores the history point. fresh=True so every cycle is a new reading. Readers do
            # not wait for it: swr.serve answers them from the cache before taking any lock.
            result = await get_monitoring_dashboard(lxd_service=self._lxd_service, fresh=True)
            # A long sweep must not let the lease lapse under it.
            self._leading = self._claim_collection_lease()
            await self._dashboards.push(result)

            # PRE-WARM API CACHES on the leader only; followers sweeping these as well was the
            # same duplicated work. In the background, one run at a time: the dashboard sweep
            # takes about 30 s and the prewarm another 10 to 40, and run in line they stretched a
            # cycle to 80 to 100 s, past the page's 90 s stale mark.
            if self._leading and (self._prewarm_task is None or self._prewarm_task.done()):
                self._prewarm_task = asyncio.create_task(self._prewarm_instances_cache())

        except Exception as e:
            logger.warning(f"Background metrics collection: {e}")

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
            
            # Get all registered cluster IDs. A fresh read on purpose: this sweep runs in a
            # thread every 30 s and is the one caller that should pay the Vault LIST budget,
            # refreshing the list cache and the last-known memo for every request on the
            # loop, and recording an outage before a request can meet it.
            cluster_ids = cert_manager.list_clusters(use_cache=False)

            # important: the whitelist behind get_node_names() was synced at boot and in the
            # one worker that served a registration. Every other worker went on omitting a
            # cluster registered after boot, even once this sweep had connected it, so the
            # list pages named it unreachable from that worker until a restart. This sweep
            # runs in every worker outside any request context, so the Vault list it holds
            # is the unfiltered fleet. Guarded on the read having answered rather than on
            # non-empty: a lost LIST must not blank it, and a legitimately empty registry
            # (every cluster deregistered) must still clear it.
            if vault_status.registry_readable() is not False:
                self._lxd_service.connection_manager.sync_registered_clusters(cluster_ids)

            reconnected = 0
            
            for cluster_id in cluster_ids:
                try:
                    # get_client() validates and returns existing connection if valid
                    # or auto-reconnects using stored connection info
                    client = self._lxd_service.connection_manager.get_client(cluster_id)
                    
                    if client is None:
                        # No stored connection info, need to fetch from Vault
                        if not self._lxd_service.connection_manager.has_connection_info(cluster_id):
                            # important: the same door the read path uses, not a fourth copy of
                            # the connect block. ensure_connection consults the breaker, reads
                            # only <cluster>/metadata rather than get_cluster_info (which also
                            # pulls /secrets for a flag nobody reads, uncached), and records a
                            # refused certificate as unauthorized instead of losing it to a
                            # debug line. It returns False rather than raising, and False
                            # already covers the breaker refusing the attempt, which the old
                            # None check here existed to catch.
                            if self._lxd_service.ensure_connection(cluster_id):
                                reconnected += 1
                                logger.info(f"Background connected cluster: {cluster_id}")
                except Exception as e:
                    logger.debug(f"Health check failed for {cluster_id}: {e}")
            
            if reconnected > 0:
                logger.info(f"Background health check connected {reconnected} cluster(s)")
                
        except Exception as e:
            logger.debug(f"Cluster connection health check error: {e}")


def _shared_redis() -> Any:
    from app.core.cache import get_cache_manager

    return getattr(get_cache_manager(), "redis_client", None)


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
