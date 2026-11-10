"""Background metrics collector.

Collects monitoring data on a timer, independent of frontend requests, so history has no gaps
while no client is connected.
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
from app.services.lxd import breaker_sync, fanout
from app.services.metrics_prewarm import CachePrewarm

logger = logging.getLogger(__name__)

# Background work gets its own small pool. The loop's default executor would put a periodic
# timer task in the same place FastAPI runs sync route handlers.
_metrics_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="metrics_bg")

# Generous, because this walks every registered cluster and may open TLS connections. The cap
# only stops a stuck cluster from wedging the collector forever.
ENSURE_CONNECTIONS_TIMEOUT = float(
    os.environ.get("ORCASTRA_ENSURE_CONNECTIONS_TIMEOUT", "60")
)


class MetricsCollector(CachePrewarm):
    """Collects and stores metrics on a timer, independent of frontend requests.

    One worker leads: it sweeps the fleet for the dashboard, then pauses for
    `collection_interval` seconds, so a cycle is the sweep plus the pause. Every other worker
    keeps its own cluster connections and relays the leader's dashboard to its own WebSocket
    clients without sweeping anything itself.
    """

    # important: the collector is started from the FastAPI lifespan, which runs once per worker,
    # so WEB_CONCURRENCY=4 means four collectors. collector_leadership makes one of them lead and
    # the others only relay; without it the dashboard is built once per worker per cycle and most
    # history points are copies.
    COLLECTION_LEASE_KEY = "orcastra:metrics_collector:cycle_lease"
    # important: short, and kept alive by a heartbeat rather than by the cycle. A lease long
    # enough to outlive a cycle also outlives a dead leader: on a deploy the outgoing container's
    # workers keep collecting while uvicorn drains connections, Docker kills them before the
    # lifespan hands the lease back, and nothing sweeps until the lease lapses. Renewed every
    # 5 s, a 20 s lease holds through any sweep and a dead leader is replaced within 20 s.
    LEASE_SECONDS = 20
    HEARTBEAT_SECONDS = 5.0
    # How often a follower looks for a new dashboard to relay while it waits out the interval.
    RELAY_POLL_SECONDS = 5.0

    def __init__(self, collection_interval: int = 60):
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
        
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                self._task = loop.create_task(self._collection_loop())
                self._heartbeat_task = loop.create_task(self._heartbeat_loop())
            else:
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
        
        # Short delay so the server finishes starting.
        await asyncio.sleep(2)

        # The first cycle runs at once and warms the cache; a separate warm-up call here would
        # sweep the whole fleet twice back to back on every start.
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
        """Collect metrics through the dashboard endpoint's own code path.

        Every worker first checks its cluster connections; only the leader builds the dashboard.
        """
        if not self._lxd_service:
            try:
                from app.services.lxd_service import get_lxd_service
                self._lxd_service = get_lxd_service()
            except Exception as e:
                logger.warning(f"Could not get LXD service: {e}")
                return
        
        try:
            self._leading = self._claim_collection_lease()

            # Connection health check first, in every worker: each one holds its own cluster
            # connections, which drop on timeouts, network issues or container restarts.
            await self._ensure_cluster_connections()

            if not self._leading:
                # important: a follower never builds the dashboard, even on a cache miss.
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

            # Pre-warm API caches on the leader only, in the background, one run at a time. The
            # dashboard sweep takes about 30 s and the prewarm another 10 to 40; run in line they
            # would stretch a cycle past the page's 90 s stale mark.
            if self._leading and (self._prewarm_task is None or self._prewarm_task.done()):
                self._prewarm_task = asyncio.create_task(self._prewarm_instances_cache())

        except Exception as e:
            logger.warning(f"Background metrics collection: {e}")

    async def _ensure_cluster_connections(self):
        """Run the connection sweep off the event loop.

        important: the body below is entirely blocking (Vault reads, TLS handshakes, LXD
        probes) and it runs on a timer. Run inline, it stalls the loop on every cycle and
        pushes /health into timeouts. Background work must never be the reason a request
        stalls.
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
        """Connect every registered cluster that is not connected yet.

        This keeps a cluster's status "Online" instead of "Pending Trust". Connection info lives
        in memory and certificates come from Vault, never Redis. A valid connection is kept
        without any TTL refresh; get_client() validates it and reconnects when needed.
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
            
            # A fresh read: this sweep runs in a thread every 30 s and is the one caller that
            # should pay the Vault LIST budget, refreshing the list cache and the last-known
            # memo for every request on the loop, and recording an outage before a request can
            # meet it.
            cluster_ids = cert_manager.list_clusters(use_cache=False)

            # important: the whitelist behind get_node_names() is otherwise synced only at boot
            # and in the worker that served a registration, so every other worker would name a
            # cluster registered after boot unreachable until a restart. This sweep runs in every
            # worker outside any request context, so the Vault list it holds is the unfiltered
            # fleet. Guarded on the read having answered rather than on non-empty: a lost LIST
            # must not blank it, and an empty registry (every cluster deregistered) must still
            # clear it.
            if vault_status.registry_readable() is not False:
                self._lxd_service.connection_manager.sync_registered_clusters(cluster_ids)

            reconnected = 0
            manager = self._lxd_service.connection_manager
            shared = breaker_sync.shared_mode()
            leading = bool(getattr(self, "_leading", False))
            
            for cluster_id in cluster_ids:
                try:
                    # important: a cluster held down is redialed by the leader's dead-cluster probe
                    # (breaker_sync) rather than by every worker's healer. The leader leaves it to
                    # that probe; a follower adopts the fleet's down verdict while one is fresh,
                    # and falls back to its own redial below as soon as none is (no leader
                    # probing, or no Redis), so a recovery never waits on a probe that does not
                    # run.
                    if shared and manager.get_cached_client(cluster_id) is None:
                        if leading and fanout.known_down(cluster_id, manager) is not None:
                            continue
                        if not leading and breaker_sync.adopt_shared(cluster_id):
                            continue
                    # get_client() returns the pooled connection if it is valid, or reconnects
                    # from the stored connection info.
                    client = manager.get_client(cluster_id)
                    
                    if client is None:
                        # No stored connection info: connect from Vault.
                        if not manager.has_connection_info(cluster_id):
                            # important: the same connect path the read path uses.
                            # ensure_connection consults the breaker, reads only
                            # <cluster>/metadata (get_cluster_info would also pull /secrets,
                            # uncached), and records a refused certificate as unauthorized. It
                            # returns False instead of raising, including when the breaker
                            # refuses the attempt.
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
