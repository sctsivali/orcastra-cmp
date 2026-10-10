"""
Orcastra CMP Backend API
Main FastAPI application entry point.
"""

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.error_sanitizer import client_detail, reference_for, sanitize_error
from app.middleware.security import apply_security_headers
import uvicorn
import urllib3

# Suppress InsecureRequestWarning for unverified HTTPS requests
# This is expected behavior when connecting to LXD with self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from app.core import build_identity, vault_status
from app.core.config import get_settings
from app.core.logging_config import setup_logging
from app.middleware import (
    AccessLogMiddleware,
    RequestIdMiddleware,
    RequestSecurityMiddleware,
    SecurityHeadersMiddleware,
)
from app.routers import (
    vms_router, 
    images_router, 
    nodes_router, 
    projects_router, 
    tags_router,
    instances_router,
    profiles_router,
    networks_router,
    network_acls_router,
    storage_pools_router,
    storage_volumes_router,
    lxd_operations_router,
    lxd_warnings_router,
    lxd_server_router,
    identities_router,
    groups_router,
    ipam_router,
    my_keys_router,
)
from app.routers.terminal import router as terminal_router
from app.routers.system import router as system_router
from app.routers.cluster_registration import router as cluster_registration_router
from app.services.lxd_service import (
    get_lxd_service,
    LXDConnectionError,
    LXDLocalStorageError,
)

# Configure structured JSON logging
settings_for_logging = get_settings()
setup_logging(
    service_name="orcastra-backend",
    version=settings_for_logging.app_version or "0.0.0",
    log_level="DEBUG" if settings_for_logging.debug else "INFO",
    json_logs=not settings_for_logging.debug  # JSON in production, human-readable in dev
)
logger = logging.getLogger(__name__)


async def _recover_durable_instance_jobs_once() -> tuple[int, int]:
    """Run both durable instance-job reconcilers under their database leases."""
    from app.services.instance_operation_audit import drain_instance_operation_audits
    from app.services.instance_specifications import recover_instance_operation_jobs

    specification_jobs = 0
    create_jobs = 0
    try:
        await drain_instance_operation_audits()
    except Exception as exc:  # noqa: BLE001 - job reconciliation must still run
        logger.warning(
            "Could not replay durable instance-operation audits: %s",
            sanitize_error(str(exc)),
        )
    try:
        specification_jobs = await recover_instance_operation_jobs()
    except Exception as exc:  # noqa: BLE001 - create recovery must still get its turn
        logger.warning(
            "Could not recover instance specification jobs: %s",
            sanitize_error(str(exc)),
        )
    try:
        create_jobs = await get_lxd_service().recover_create_operation_jobs()
    except Exception as exc:  # noqa: BLE001 - specification recovery already completed
        logger.warning(
            "Could not recover instance create jobs: %s",
            sanitize_error(str(exc)),
        )
    try:
        # A recovered worker may have committed a terminal verdict after the first drain.
        await drain_instance_operation_audits()
    except Exception as exc:  # noqa: BLE001 - the next sweep retains the same event identity
        logger.warning(
            "Could not drain recovered instance-operation audits: %s",
            sanitize_error(str(exc)),
        )
    return specification_jobs, create_jobs


async def _durable_instance_job_recovery_loop() -> None:
    # A process can die immediately after renewing a lease. Startup cannot safely steal that
    # fresh lease from another API worker, so one shared loop revisits both job kinds after lease
    # expiry instead of leaving either kind stranded until another deployment.
    while True:
        await asyncio.sleep(60)
        try:
            await _recover_durable_instance_jobs_once()
        except Exception as exc:  # noqa: BLE001 - a transient DB/cluster fault is retryable
            logger.warning(
                "Could not run durable instance-job recovery: %s",
                sanitize_error(str(exc)),
            )


async def _recover_migrations_once() -> tuple[int, int]:
    """Reconcile durable migration jobs and deliver their audit outbox."""
    from app.services.migrations.orchestrator import (
        drain_migration_audits,
        recover_migration_jobs,
    )

    recovered = await recover_migration_jobs()
    delivered = await drain_migration_audits()
    return recovered, delivered


async def _migration_recovery_loop(interval_seconds: int) -> None:
    """Keep leases, retention transitions, reconciliation, and audits moving."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await _recover_migrations_once()
        except Exception as exc:  # noqa: BLE001 - a later lease sweep retries safely
            logger.warning(
                "Could not run Migration Center recovery: %s",
                sanitize_error(str(exc)),
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup and shutdown."""
    # Startup
    logger.info("Starting Orcastra CMP API...")
    settings = get_settings()
    instance_operation_recovery_task = None
    migration_recovery_task = None

    # important: the per-key budget only isolates integrations from each other while it is
    # strictly below the per-IP one. Buckets are evaluated together and the request is refused by
    # whichever is reached first, so at or above it the IP bucket always refuses first and the
    # per-key bucket never binds. Nothing fails when that happens: the feature is simply absent,
    # and a chatty key goes back to spending everyone else's budget. It is a two-variable
    # relationship an operator can break by tuning either one, so it is checked out loud here
    # rather than left to whoever reads the config comment.
    if (
        settings.rate_limit_enabled
        and settings.rate_limit_api_key_requests > 0
        and settings.rate_limit_api_key_requests >= settings.rate_limit_requests
    ):
        logger.warning(
            "RATE_LIMIT_API_KEY_REQUESTS (%s) is not below RATE_LIMIT_REQUESTS (%s), so the "
            "per-API-key budget can never be reached and one integration can still exhaust the "
            "budget every other integration behind the same address depends on. Set it below "
            "the per-IP limit, or to 0 to disable it deliberately.",
            settings.rate_limit_api_key_requests,
            settings.rate_limit_requests,
        )

    # Pre-warm JWKS cache to avoid blocking first auth request (~5s savings)
    if settings.auth_enabled and settings.authentik_issuer:
        try:
            from app.core.auth import _jwks_client
            await _jwks_client.warm_up(settings)
        except Exception as jwks_err:
            logger.warning(f"JWKS pre-warm failed (will lazy-load on first request): {jwks_err}")
    
    # Verify PostgreSQL is reachable and Alembic-managed (migrations run in the entrypoint)
    try:
        from app.core.database import init_db
        await init_db()
        logger.info("PostgreSQL database ready")
    except Exception as db_err:
        logger.warning(f"Could not verify database schema: {db_err}")

    # The activity sink copies every audit record into the task journal. Started here, before
    # the Vault connect below, so the lxd.connection.* records that connect emits are journaled.
    try:
        if settings.task_journal_enabled:
            from app.core.database import async_session_maker
            from app.services.task_journal.sink import get_journal_sink

            get_journal_sink().start(asyncio.get_running_loop(), async_session_maker)
            logger.info("Task journal activity sink started")
    except Exception as sink_err:
        logger.error("Could not start the task journal sink: %s", sink_err, exc_info=True)

    # Initialize LXD service and check connections
    try:
        lxd_service = get_lxd_service()
        
        # Initialize Vault connection and cache cluster list (don't connect all clusters at startup)
        if settings.vault_enabled and settings.vault_token:
            try:
                from app.services.lxd_cert_manager import LXDCertificateManager
                from app.core.vault_client import get_vault_client
                from app.core.cache import get_cache_manager
                
                vault_client = get_vault_client(settings.vault_addr, settings.vault_token)
                cert_manager = LXDCertificateManager(
                    vault_client=vault_client,
                    clusters_path=settings.vault_clusters_path
                )
                
                # List clusters from Vault, off the loop: the LIST carries a 13 s budget and
                # the health probe must keep answering while it runs.
                cluster_ids = await asyncio.to_thread(cert_manager.list_clusters)

                # important: an empty list from a Vault that never answered is not the same fact
                # as an empty registry, and list_clusters cannot raise because thirteen callers
                # depend on it returning a list. A sealed Vault answers 503 to LIST, so without
                # this check startup logged "Found 0 registered clusters", "Connected 0/0" and
                # "started successfully", then handed the empty list to sync_registered_clusters
                # which set the whitelist to the empty set. get_node_names() intersects against
                # that, so every cluster vanished from the API even once the metrics collector
                # reconnected one. Observed in the field, that state persisted for weeks.
                if vault_status.registry_readable() is False:
                    logger.error(
                        "Vault did not answer the cluster registry read. Not syncing the "
                        "registered-cluster whitelist, because an empty list here would hide "
                        "every cluster rather than report the fault. Check whether Vault is "
                        "sealed."
                    )
                    cluster_ids = []
                else:
                    logger.info(f"Found {len(cluster_ids)} registered clusters in Vault")
                    # Sync registered cluster whitelist into connection_manager
                    # to ensure get_node_names() only returns registered nodes
                    lxd_service.connection_manager.sync_registered_clusters(cluster_ids)
                
                # Connect to ALL registered clusters in parallel for faster startup
                # This ensures clusters stay connected even after Docker restart
                
                async def _connect_cluster(cluster_id: str) -> tuple[str, bool, str | None]:
                    """Connect to a single cluster; returns (id, success, error)."""
                    try:
                        connected, error_msg = await asyncio.to_thread(
                            cert_manager.verify_connection, cluster_id
                        )
                        if connected:
                            cluster_info = await asyncio.to_thread(
                                cert_manager.get_cluster_info, cluster_id
                            )
                            cert_data = await asyncio.to_thread(
                                cert_manager.get_cluster_certificate, cluster_id
                            )
                            lxd_service.connection_manager.connect_with_vault_cert(
                                cluster_id=cluster_id,
                                endpoint=cluster_info["metadata"]["endpoint"],
                                cert_content=cert_data["client.crt"],
                                key_content=cert_data["client.key"],
                                verify=False
                            )
                            logger.info(f"Connected to cluster: {cluster_id}")
                            return (cluster_id, True, None)
                        else:
                            logger.debug(f"Cluster {cluster_id} not authenticated: {error_msg}")
                            return (cluster_id, False, error_msg or "Not authenticated")
                    except Exception as e:
                        logger.warning(f"Could not connect cluster {cluster_id}: {e}")
                        return (cluster_id, False, str(e))
                
                results = await asyncio.gather(
                    *[_connect_cluster(cid) for cid in cluster_ids],
                    return_exceptions=True,
                )
                
                connected_count = sum(1 for r in results if isinstance(r, tuple) and r[1])
                failed_clusters = [
                    (r[0], r[2]) for r in results
                    if isinstance(r, tuple) and not r[1]
                ]
                
                logger.info(f"Startup: Connected {connected_count}/{len(cluster_ids)} clusters")
                if failed_clusters:
                    logger.info(f"Pending trust/failed clusters: {[c[0] for c in failed_clusters]}")
                        
            except Exception as vault_err:
                logger.warning(f"Could not initialize Vault clusters: {vault_err}")
        
        # important: bounded, parallel readiness check - never let a dead/slow
        # cluster wedge startup. The parallel _connect_cluster pass above already
        # populated the connection pool; this only logs per-node status.
        try:
            statuses = await asyncio.wait_for(
                lxd_service.check_nodes_status_async(), timeout=30
            )
        except asyncio.TimeoutError:
            logger.warning("Node status check timed out at startup; continuing without it")
            statuses = []

        for status in statuses:
            if status.connected:
                logger.info(
                    f"Connected to LXD node: {status.name} "
                    f"(version: {status.server_version})"
                )
            else:
                logger.warning(
                    f"Could not connect to LXD node: {status.name} - {status.error}"
                )
        
        # Start background metrics collector
        try:
            from app.services.metrics_collector import start_background_collector
            await start_background_collector(lxd_service)
            logger.info("Background metrics collector started")
        except Exception as collector_err:
            logger.warning(f"Could not start background metrics collector: {collector_err}")
    
    except Exception as e:
        logger.error(f"Error during LXD service initialization: {e}")
    
    # Start metrics retention cleanup loop (purges snapshots older than 7 days)
    try:
        from app.services.metrics_history_service import run_retention_loop
        asyncio.create_task(run_retention_loop(interval_hours=6, retention_days=7))
        logger.info("Metrics retention cleanup loop started (7-day retention, every 6h)")
    except Exception as retention_err:
        logger.warning(f"Could not start metrics retention loop: {retention_err}")

    # Pre-warm the remote image catalog (Custom + Ubuntu, ~17 MB) in the
    # background so the first "Select base image" dialog open is instant.
    try:
        from app.services.remote_image_catalog import prewarm_remote_images_cache
        asyncio.create_task(asyncio.to_thread(prewarm_remote_images_cache))
        logger.info("Remote image catalog pre-warm scheduled")
    except Exception as prewarm_err:
        logger.warning(f"Could not schedule remote image catalog pre-warm: {prewarm_err}")

    # Recover custom-image imports stranded at 'importing' by a worker restart/crash (the import is a
    # detached in-process task; without this a rebuild leaves them stuck until OrcaHub's 30-min deadline).
    try:
        from app.routers.image_uploads import start_import_recovery
        start_import_recovery()
        logger.info("Custom-image import recovery loop started")

        # Same guarantee for a rebuild whose driver died with the process it ran in. The job is
        # an unreferenced asyncio task, so a restart loses it while the row stays mid-flight and
        # the instance stays stopped, with nothing to revisit either.
        from app.routers.instance_rebuild import recover_stranded_rebuilds
        stranded = await recover_stranded_rebuilds()
        if stranded:
            logger.warning("Marked %d stranded rebuild job(s) failed at startup", stranded)

        # Specification changes are resumable rather than marked failed. Each row is claimed
        # through a database lease, so every API worker may run this recovery pass safely.
        resumed, create_resumed = await _recover_durable_instance_jobs_once()
        if resumed:
            logger.warning("Scheduled recovery for %d instance operation job(s)", resumed)

        # Create jobs use the same durable table but reconcile through the LXD instance
        # manager. Recovery always reads the exact project/name and any recorded operation
        # before acting; it never submits a second create.
        if create_resumed:
            logger.warning(
                "Scheduled recovery for %d durable instance create job(s)",
                create_resumed,
            )
    except Exception as recovery_err:
        logger.warning(f"Could not start import recovery loop: {recovery_err}")

    # The atomic leases make this safe in every API worker. It covers jobs whose lease belonged
    # to a process that died moments before startup and therefore was not stale in the first pass.
    instance_operation_recovery_task = asyncio.create_task(
        _durable_instance_job_recovery_loop()
    )

    # Recovery is independent from new-work admission. Turning the rollout gate off must not
    # strand leases, retained sources, audit events, or the resource fences of an existing job.
    try:
        recovered, delivered = await _recover_migrations_once()
        if recovered:
            logger.info("Scheduled recovery for %d migration job(s)", recovered)
        if delivered:
            logger.info("Delivered %d migration audit event(s)", delivered)
    except Exception as migration_recovery_err:  # noqa: BLE001
        logger.warning(
            "Could not start Migration Center recovery: %s",
            sanitize_error(str(migration_recovery_err)),
        )
    migration_recovery_task = asyncio.create_task(
        _migration_recovery_loop(settings.migration_recovery_tick_seconds)
    )

    # Same recovery guarantee for stranded custom-ISO imports (see iso_uploads.py).
    try:
        from app.routers.iso_uploads import start_import_recovery as start_iso_import_recovery
        start_iso_import_recovery()
        logger.info("Custom-ISO import recovery loop started")
    except Exception as iso_recovery_err:
        logger.warning(f"Could not start ISO import recovery loop: {iso_recovery_err}")

    # Reclaim expired upload sessions on a timer.
    #
    # important: this sweep used to run only from inside create_upload, which made it
    # unreachable exactly when it mattered. The active-session cap is per API key, so a key
    # whose two slots are held by dead rows has every create refused before it can trigger the
    # sweep that would free them. Two failed PATCHes locked an integration out for six hours.
    try:
        from app.routers.image_uploads import start_expiry_sweep
        from app.routers.iso_uploads import start_expiry_sweep as start_iso_expiry_sweep
        start_expiry_sweep()
        start_iso_expiry_sweep()
        logger.info("Upload expiry sweeps started")

        # Webhook delivery. Every worker runs it: rows are claimed with FOR UPDATE SKIP LOCKED,
        # so four workers pulling from the queue is the parallelism rather than a race, and a
        # worker dying mid-delivery is recovered by the reaper rather than stranding the row.
        if settings.extensions_enabled:
            from app.core.database import async_session_maker
            from app.routers.integrations import get_vault_integrations_client
            from app.services.webhooks.dispatcher import start_dispatcher

            start_dispatcher(async_session_maker, get_vault_integrations_client)
            logger.info("Webhook dispatcher started")

            # The slow half of an installation's defence. The request path refuses a suspended or
            # missing installation from a cached snapshot; this is what narrows a grant after the
            # organization loses a cluster, and reclaims a credential whose row is gone. Both
            # passes are idempotent, so every worker runs it without a leader lock.
            from app.services.extension_reconcile import start_reconcile_sweep

            start_reconcile_sweep(
                async_session_maker,
                get_vault_integrations_client,
                interval=float(settings.extension_reconcile_interval_seconds),
            )
            logger.info(
                "Extension reconcile sweep started (%ss)",
                settings.extension_reconcile_interval_seconds,
            )
    except Exception as sweep_err:
        # note: this block starts the upload sweeps, the webhook dispatcher and the reconcile
        # sweep, so naming only the sweeps sent anyone debugging a dead dispatcher to the wrong
        # subsystem. The exception is still swallowed on purpose: a background task failing to
        # start must not stop the API from serving.
        logger.warning(f"Could not start a background task: {sweep_err}", exc_info=True)

    # important: its own try, not the one above. That block already starts the upload sweeps, the
    # webhook dispatcher and the reconcile sweep, and its handler once named only the sweeps, which
    # sent someone debugging a dead dispatcher to the wrong subsystem. A fourth subsystem sharing
    # it repeats that.
    try:
        if settings.snapshot_schedules_enabled:
            from app.core.database import async_session_maker
            from app.services.snapshots.schedule_scheduler import start_scheduler

            # Every worker runs this. Rows are claimed with FOR UPDATE SKIP LOCKED, so four workers
            # pulling from the queue is the parallelism rather than a race, and no leader is
            # elected: the lease this codebase already has deliberately fails open, so a Redis
            # outage would make all four believe they lead and every schedule would fire four times.
            start_scheduler(
                async_session_maker,
                get_lxd_service,
                interval=float(settings.snapshot_schedule_tick_seconds),
            )
            logger.info(
                "Snapshot schedule loop started (%ss)", settings.snapshot_schedule_tick_seconds
            )
    except Exception as schedule_err:
        logger.error("Could not start the snapshot schedule loop: %s", schedule_err, exc_info=True)

    # The task journal follower (one worker, elected on a Postgres advisory lock) and the prune
    # loop (every worker, one wins each pass). Its own try, for the reason the block above gives.
    try:
        if settings.task_journal_enabled:
            from app.core.database import DATABASE_URL, async_session_maker
            from app.services.task_journal.follower import start_follower
            from app.services.task_journal.prune import start_prune_loop

            start_follower(
                async_session_maker,
                get_lxd_service,
                retry_seconds=settings.task_journal_follower_retry_seconds,
                database_url=DATABASE_URL,
            )
            start_prune_loop(async_session_maker)
            logger.info(
                "Task journal follower standing by (retry %ss) and prune loop started",
                settings.task_journal_follower_retry_seconds,
            )
    except Exception as journal_err:
        logger.error("Could not start the task journal follower: %s", journal_err, exc_info=True)

    # Removing the LXD trust entries of certificates the dashboard replaced. Every worker runs the
    # loop; a per-cluster Postgres lock decides who acts on a cluster each pass, and each worker
    # also moves its own pooled connection onto the current certificate first. Its own try, for
    # the reason the blocks above give.
    try:
        if settings.lxd_trust_cleanup_enabled and settings.vault_enabled:
            from app.core.database import async_session_maker
            from app.services.lxd_certs.trust_cleanup import start_trust_cleanup_loop

            start_trust_cleanup_loop(
                async_session_maker, interval=float(settings.lxd_trust_cleanup_tick_seconds)
            )
            logger.info(
                "LXD trust cleanup loop started (%ss, grace %ss)",
                settings.lxd_trust_cleanup_tick_seconds,
                settings.lxd_trust_cleanup_grace_seconds,
            )
    except Exception as cleanup_err:
        logger.error("Could not start the LXD trust cleanup loop: %s", cleanup_err, exc_info=True)

    # Releasing this worker's breaker once another worker proves a cluster reachable again, so a
    # repaired cluster stops answering 503 here within seconds instead of after the trip and the
    # next healer pass. Every worker runs it; it does no I/O while nothing has failed here.
    try:
        if settings.vault_enabled:
            from app.services.lxd.breaker_sync import start_breaker_sync_loop

            start_breaker_sync_loop()
            logger.info("LXD breaker sync loop started")
    except Exception as sync_err:
        logger.error("Could not start the LXD breaker sync loop: %s", sync_err, exc_info=True)

    # Fill the admin chart's history before the first read: the previous release's string key
    # once, then what Postgres holds that the store lacks. Both merge by timestamp, so neither
    # can overwrite a point the collector already wrote.
    try:
        from app.core.cache import get_cache_manager
        from app.routers.monitoring import COLLECTED_SCOPE_KEY
        from app.services.monitoring_history import history_key
        from app.services.monitoring_history_store import hydrate_from_postgres, import_previous_history

        cache = get_cache_manager()
        admin_history = history_key(COLLECTED_SCOPE_KEY)
        await import_previous_history(cache, admin_history)
        await hydrate_from_postgres(cache, admin_history)
    except Exception as hydrate_err:
        logger.warning("Monitoring history refill skipped at startup: %s", hydrate_err)
    
    # Start session tracker cleanup loop
    try:
        from app.services.session_tracker import get_session_tracker
        from app.services.session_registry import get_session_registry
        session_tracker = get_session_tracker()
        await session_tracker.start_cleanup_loop()

        # important: the Redis client is built HERE and nowhere else. redis.asyncio
        # binds its pool to the running loop lazily, so one created at import time
        # attaches to the wrong loop and fails intermittently under load.
        if settings.redis_enabled:
            await get_session_registry().connect(settings.redis_url)
        await session_tracker.start_publisher()
        logger.info("Session tracker cleanup loop and publisher started")
    except Exception as tracker_err:
        logger.warning(f"Could not start session tracker: {tracker_err}")
    
    # WebSocket tickets: their own block, so a failure above can never leave the store on its
    # per-process fallback while four workers serve sockets. Redis is used whenever it answers;
    # with REDIS_ENABLED an unreachable Redis refuses tickets instead of falling back.
    try:
        from app.core.ws_ticket_store import get_ws_ticket_store

        if settings.redis_url:
            await get_ws_ticket_store().connect(settings.redis_url, required=settings.redis_enabled)
    except Exception as ticket_err:
        logger.error("WebSocket ticket store did not start: %s", ticket_err)

    # Initialize default tag categories
    try:
        from app.core.database import async_session_maker
        from app.services.tag_service import TagService
        
        async with async_session_maker() as session:
            tag_service = TagService(db=session)
            created_count = await tag_service.initialize_default_categories()
            if created_count > 0:
                logger.info(f"Initialized {created_count} default tag categories")
            else:
                logger.debug("Default tag categories already exist")
    except Exception as tag_err:
        logger.warning(f"Could not initialize tag categories: {tag_err}")
    
    # important: the MCP session manager's task group is created by run(), and Starlette does not
    # propagate lifespan into a mounted sub-app, which is how the SDK would normally start it.
    # Without this every MCP request raises "Task group is not initialized".
    _mcp_stack = AsyncExitStack()
    _mcp_manager = getattr(app.state, "mcp_session_manager", None)
    if _mcp_manager is not None:
        try:
            await _mcp_stack.enter_async_context(_mcp_manager.run())
            logger.info("MCP session manager started")
        except Exception as mcp_err:
            logger.error(f"MCP session manager failed to start: {mcp_err}")

    logger.info("Orcastra CMP API started successfully")
    
    yield

    # note: guarded, because this runs BEFORE the rest of shutdown. An exception escaping here
    # would skip the database close and the background collectors below, turning one subsystem's
    # failure into a dirty shutdown of every other. Same rule the audit paths already follow.
    try:
        await _mcp_stack.aclose()
    except Exception as mcp_stop_err:
        logger.warning(f"MCP session manager did not stop cleanly: {mcp_stop_err}")

    # Shutdown
    logger.info("Shutting down Orcastra CMP API...")

    if instance_operation_recovery_task is not None:
        instance_operation_recovery_task.cancel()
        await asyncio.gather(instance_operation_recovery_task, return_exceptions=True)

    if migration_recovery_task is not None:
        migration_recovery_task.cancel()
        await asyncio.gather(migration_recovery_task, return_exceptions=True)

    # Stop the task journal writers first: the follower releases its lock so another worker can
    # take over, and the sink drains before the database engine below is disposed.
    try:
        from app.services.task_journal.follower import stop_follower
        from app.services.task_journal.prune import stop_prune_loop
        from app.services.task_journal.sink import get_journal_sink

        await stop_follower()
        stop_prune_loop()
        await get_journal_sink().stop()
        logger.info("Task journal follower, prune loop and sink stopped")
    except Exception:
        pass

    # Close archive service client
    try:
        from app.services.archive_service import _archive_service
        if _archive_service is not None:
            await _archive_service.close()
            logger.info("Archive service client closed")
    except Exception:
        pass
    
    # Close database connection
    try:
        from app.core.database import close_db
        await close_db()
        logger.info("Database connection closed")
    except Exception:
        pass
    
    # Stop background collector
    try:
        from app.services.metrics_collector import stop_background_collector
        stop_background_collector()
        logger.info("Background metrics collector stopped")
    except Exception:
        pass
    
    # Stop session tracker cleanup
    try:
        from app.services.session_tracker import get_session_tracker
        from app.services.session_registry import get_session_registry
        tracker = get_session_tracker()
        tracker.stop_cleanup()
        tracker.stop_publisher()
        await get_session_registry().aclose()
        from app.core.ws_ticket_store import get_ws_ticket_store

        await get_ws_ticket_store().aclose()
        logger.info("Session tracker cleanup and publisher stopped")
    except Exception:
        pass

    try:
        from app.services.lxd_certs.trust_cleanup import stop_trust_cleanup_loop
        stop_trust_cleanup_loop()
    except Exception:
        pass

    try:
        from app.services.lxd.breaker_sync import stop_breaker_sync_loop
        stop_breaker_sync_loop()
    except Exception:
        pass

    # Stop the snapshot schedule loop. A handle is kept for exactly this, unlike the rebuild
    # driver beside it, so the loop is cancelled rather than left holding a session while the
    # process tears down.
    try:
        from app.services.snapshots.schedule_scheduler import stop_scheduler
        await stop_scheduler()
        logger.info("Snapshot schedule loop stopped")
    except Exception:
        pass

    # Tear down the LXD events hub (per-node upstream WebSockets + tasks)
    try:
        from app.services.lxd.events_hub import get_events_hub
        await get_events_hub().shutdown()
        logger.info("LXD events hub stopped")
    except Exception:
        pass


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()
    
    # disable API docs in production (when DEBUG=false)
    is_debug = settings.debug if hasattr(settings, 'debug') else False
    
    app = FastAPI(
        title=settings.api_title,
        version=settings.app_version,
        description="""
## Orcastra CMP API

A unified REST API for managing virtual machines across multiple nodes.

This API uses certificate-based authentication to connect to nodes.
Ensure your client certificates are properly configured.
        """,
        docs_url="/docs" if is_debug else None,
        redoc_url="/redoc" if is_debug else None,
        openapi_url="/openapi.json" if is_debug else None,
        lifespan=lifespan,
    )
    
    # Configure CORS with environment-based origins
    # When CORS_ORIGINS is "*", we use a custom approach to support credentials
    cors_origins = settings.get_cors_origins()

    # important: wildcard CORS with credentials is dangerous in production.
    # Block it when DEBUG=false to prevent credential leakage to arbitrary origins.
    if cors_origins == ["*"] and not is_debug:
        import logging as _logging
        _logging.getLogger(__name__).error(
            "SECURITY: CORS_ORIGINS='*' is not allowed in production (DEBUG=false). "
            "Set CORS_ORIGINS to explicit allowed origins. Refusing to start."
        )
        raise RuntimeError(
            "CORS_ORIGINS='*' is forbidden in production. "
            "Set DEBUG=true for development or specify explicit CORS_ORIGINS."
        )

    if cors_origins == ["*"]:
        # For wildcard CORS with credentials support, we need custom middleware
        # that echoes back the request origin (more permissive, use only in dev/trusted environments)
        @app.middleware("http")
        async def cors_allow_all_with_credentials(request: Request, call_next):
            # Handle preflight requests
            if request.method == "OPTIONS":
                origin = request.headers.get("origin", "*")
                response = JSONResponse(content={}, status_code=200)
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Allow-Credentials"] = "true"
                response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, PATCH, OPTIONS"
                response.headers["Access-Control-Allow-Headers"] = request.headers.get(
                    "access-control-request-headers", "*"
                )
                response.headers["Access-Control-Max-Age"] = "86400"
                return response
            
            # Handle actual requests
            response = await call_next(request)
            origin = request.headers.get("origin")
            if origin:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Allow-Credentials"] = "true"
            return response
    else:
        # Use standard CORSMiddleware for specific origins
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
            allow_headers=["*"],
            expose_headers=["*"],
        )
    
    # Add GZip compression middleware
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    
    # Block scanner probes and enforce IP plus subnet-aware throttling before routing
    app.add_middleware(RequestSecurityMiddleware, settings=settings)
    
    # Add Access Log middleware (logs HTTP requests in structured JSON)
    app.add_middleware(
        AccessLogMiddleware,
        service_name="orcastra-backend",
        version=settings.app_version or "0.0.0"
    )
    
    # Add Request ID middleware (must be added before access log to generate IDs)
    app.add_middleware(RequestIdMiddleware)
    
    # Add security headers outermost so blocked responses are hardened too
    app.add_middleware(SecurityHeadersMiddleware)
    
    # Register routers
    app.include_router(vms_router, prefix=settings.api_prefix)
    app.include_router(images_router, prefix=settings.api_prefix)
    app.include_router(nodes_router, prefix=settings.api_prefix)
    app.include_router(projects_router, prefix=settings.api_prefix)
    app.include_router(tags_router, prefix=settings.api_prefix)
    app.include_router(system_router, prefix=settings.api_prefix)
    
    # LXD Feature Routers
    app.include_router(instances_router, prefix=settings.api_prefix)
    from app.routers.instance_specifications import router as instance_specifications_router
    app.include_router(instance_specifications_router, prefix=settings.api_prefix)
    app.include_router(profiles_router, prefix=settings.api_prefix)
    app.include_router(networks_router, prefix=settings.api_prefix)
    app.include_router(network_acls_router, prefix=settings.api_prefix)
    app.include_router(ipam_router, prefix=settings.api_prefix)
    app.include_router(storage_pools_router, prefix=settings.api_prefix)
    app.include_router(storage_volumes_router, prefix=settings.api_prefix)
    app.include_router(lxd_operations_router, prefix=settings.api_prefix)
    app.include_router(lxd_warnings_router, prefix=settings.api_prefix)
    app.include_router(lxd_server_router, prefix=settings.api_prefix)
    app.include_router(identities_router, prefix=settings.api_prefix)
    app.include_router(groups_router, prefix=settings.api_prefix)
    app.include_router(my_keys_router, prefix=settings.api_prefix)
    
    # Integrations Router (External Apps - Orcahub)
    from app.routers.integrations import router as integrations_router, external_router as integrations_external_router
    app.include_router(integrations_router, prefix=settings.api_prefix)
    app.include_router(integrations_external_router, prefix=settings.api_prefix)

    # MCP server. Same credential as external_router, different protocol. Appended to the raw
    # router rather than include_router'd because the endpoint is an ASGI callable, not an
    # APIRoute, and mounting it as a sub-app would cost a 307 on every JSON-RPC POST.
    if settings.extensions_enabled:
        # important: the external router is included FIRST. Both routers sit on the /extensions
        # prefix, and the authoring router owns /extensions/{slug}. Registered the other way
        # round, an add-on calling /extensions/self would be matched by the slug route and
        # answered 401 by a JWT dependency it has no way to satisfy. The installations router
        # goes ahead of it for the same reason: /extensions/installations would otherwise be
        # read as an add-on named 'installations'. The reserved-slug list is the second half
        # of the same guard, and test_a_literal_route_is_not_read_as_a_slug is the third.
        from app.routers.extensions_external import external_router as extensions_external_router
        from app.routers.extensions import router as extensions_router
        from app.routers.extension_installs import router as extension_installs_router
        from app.routers.extension_webhooks import router as extension_webhooks_router

        app.include_router(extensions_external_router, prefix=settings.api_prefix)
        app.include_router(extension_installs_router, prefix=settings.api_prefix)
        app.include_router(extension_webhooks_router, prefix=settings.api_prefix)
        app.include_router(extensions_router, prefix=settings.api_prefix)
        logger.info("Extensions surface enabled at %s/extensions", settings.api_prefix)

    if settings.public_openapi_enabled:
        # important: this does NOT re-enable the internal document. `openapi_url` above stays
        # None in production; this is a separate, curated document describing only the surface
        # an add-on may call, selected by which routes authenticate with an API key.
        from app.routers.public_docs import router as public_docs_router

        app.include_router(public_docs_router, prefix=settings.api_prefix)
        if not is_debug and not settings.public_api_base_url:
            logger.error(
                "PUBLIC_API_BASE_URL is empty, so the add-on API document has no servers entry "
                "and generated clients from it will not work. Set it to the absolute base URL "
                "integrators reach this deployment on."
            )

    if settings.mcp_enabled:
        # important: appended as a plain Route. NOT include_router, and NOT app.mount(). Three
        # separate things break if this is tidied into either, and none of them fails loudly:
        #
        # 1. Lifespan. Starlette does not propagate lifespan into a mounted sub-app, so the SDK's
        #    session manager never gets its task group and every request raises "Task group is not
        #    initialized". That is why the manager is built here and run from this app's lifespan.
        # 2. Rate limiting. mount() compiles to a prefix route, so a POST to the canonical path is
        #    answered with a 307 to the trailing-slash form, and both the redirect and the retry
        #    pass through RequestSecurityMiddleware. That spends two IP rate-limit tokens per
        #    JSON-RPC message.
        # 3. Route discovery. From FastAPI 0.141 include_router stores a nested router rather than
        #    copying routes onto the parent: measured on this app, 50 top-level entries of which
        #    only 3 expose `.path`. A plain Route stays at the top level, which is what keeps
        #    `test_the_mcp_server_authenticates_every_call` meaningful rather than vacuous. The
        #    host venv is on 0.128, which flattens, so a regression here would be invisible
        #    locally and real in the image.
        from app.mcp import build_mcp_route
        _mcp_route, _mcp_manager = build_mcp_route(settings)
        app.state.mcp_session_manager = _mcp_manager
        app.router.routes.append(_mcp_route)
        logger.info("MCP server mounted at %s/mcp", settings.api_prefix)
    else:
        app.state.mcp_session_manager = None

    from app.routers.image_uploads import router as image_uploads_router
    app.include_router(image_uploads_router, prefix=settings.api_prefix)

    from app.routers.iso_uploads import router as iso_uploads_router
    app.include_router(iso_uploads_router, prefix=settings.api_prefix)

    # Dashboard (JWT) ISO upload, same machinery with user-session auth
    from app.routers.iso_uploads_dashboard import router as iso_uploads_dashboard_router
    app.include_router(iso_uploads_dashboard_router, prefix=settings.api_prefix)

    # Dashboard (JWT) custom-image upload, same machinery with user-session auth
    from app.routers.image_uploads_dashboard import router as image_uploads_dashboard_router
    app.include_router(image_uploads_dashboard_router, prefix=settings.api_prefix)

    # Instance rebuild (JWT): reinstall root disk, preserve identity/config/volumes
    from app.routers.instance_rebuild import router as instance_rebuild_router
    app.include_router(instance_rebuild_router, prefix=settings.api_prefix)

    # Instance snapshots (JWT): operator rollback points, distinct from OrcaHub's paid product
    from app.routers.instance_snapshots import router as instance_snapshots_router
    app.include_router(instance_snapshots_router, prefix=settings.api_prefix)
    from app.routers.snapshots_fleet import router as snapshots_fleet_router
    app.include_router(snapshots_fleet_router, prefix=settings.api_prefix)
    if settings.snapshot_schedules_enabled:
        from app.routers.snapshot_schedules import router as snapshot_schedules_router
        app.include_router(snapshot_schedules_router, prefix=settings.api_prefix)

    # Instance mobility (JWT): copy a workload to another project, member or cluster
    from app.routers.instance_mobility import router as instance_mobility_router
    app.include_router(instance_mobility_router, prefix=settings.api_prefix)

    from app.routers.migrations import router as migrations_router

    app.include_router(migrations_router, prefix=settings.api_prefix)

    # Cluster Registration Router (Vault Integration)
    app.include_router(cluster_registration_router, prefix=settings.api_prefix)
    
    # Certificate Manager Router
    from app.routers.cert_manager import router as cert_manager_router
    app.include_router(cert_manager_router, prefix=settings.api_prefix)
    
    # User Preferences Router (PostgreSQL)
    from app.routers.user_preferences import router as user_preferences_router
    app.include_router(user_preferences_router, prefix=settings.api_prefix)
    
    # Cluster Members Router (MicroCloud)
    from app.routers.cluster_members import router as cluster_members_router
    app.include_router(cluster_members_router, prefix=settings.api_prefix)
    
    # Regions Router (Geographic overview)
    from app.routers.regions import router as regions_router
    app.include_router(regions_router, prefix=settings.api_prefix)
    
    # Monitoring Router (Real-time metrics dashboard)
    from app.routers.monitoring import router as monitoring_router, ws_router as monitoring_ws_router
    app.include_router(monitoring_router, prefix=settings.api_prefix)
    app.include_router(monitoring_ws_router, prefix=settings.api_prefix)
    
    # File Transfer Router (Upload files to LXD instances)
    from app.routers.file_transfer import router as file_transfer_router
    app.include_router(file_transfer_router, prefix=settings.api_prefix)
    
    # Terminal Router (WebSocket terminal access)
    app.include_router(terminal_router, prefix=settings.api_prefix)

    # Events Router (WebSocket: realtime LXD operation/creation progress)
    from app.routers.events import router as events_router
    app.include_router(events_router, prefix=settings.api_prefix)

    # WebSocket tickets: the single-use credential every socket above opens with
    from app.routers.ws_tickets import router as ws_tickets_router
    app.include_router(ws_tickets_router, prefix=settings.api_prefix)

    # Task journal (the bottom panel): the socket first, then the REST routers. Mounted only
    # when the journal is on, so a deployment that turned it off has no surface at all.
    if settings.task_journal_enabled:
        from app.routers.cluster_log import router as cluster_log_router
        from app.routers.tasks import router as tasks_router
        from app.routers.tasks_ws import router as tasks_ws_router
        app.include_router(tasks_ws_router, prefix=settings.api_prefix)
        app.include_router(tasks_router, prefix=settings.api_prefix)
        app.include_router(cluster_log_router, prefix=settings.api_prefix)

    # Console Router (VGA/Graphics console access)
    from app.routers.console import router as console_router
    app.include_router(console_router, prefix=settings.api_prefix)
    
    # Organization Router (Three-Tier RBAC)
    from app.routers.organizations import router as organizations_router
    app.include_router(organizations_router, prefix=settings.api_prefix)

    # Break-Glass Router (least-privilege admin access to tenant console/terminal)
    from app.routers.break_glass import router as break_glass_router
    app.include_router(break_glass_router, prefix=settings.api_prefix)

    # The organization's own consent to desktop control. Mounted beside break-glass because the
    # two answer the same shape of question: who authorised this, and can they withdraw it.
    from app.routers.desktop_policy import router as desktop_policy_router
    app.include_router(desktop_policy_router, prefix=settings.api_prefix)

    # User Groups Router (Admin user assignment + Authentik sync)
    from app.routers.user_groups import router as user_groups_router
    app.include_router(user_groups_router, prefix=settings.api_prefix)
    
    # Tenant Access Policy Router (ACL system)
    from app.routers.tenant_access import router as tenant_access_router
    app.include_router(tenant_access_router, prefix=settings.api_prefix)
    
    # Archive Router (Log compression & compliance archive)
    if settings.opensearch_archive_enabled:
        from app.routers.archive import router as archive_router
        app.include_router(archive_router, prefix=settings.api_prefix)
        logger.info("Archive management API enabled")
    
    # Health check endpoint
    @app.get("/health", tags=["Health"])
    async def health_check():
        """Health check endpoint.

        important: always answers 200, including when temp storage is degraded. The container
        healthcheck in docker-compose keys on this status code, so reporting unhealthy here would
        restart the backend in a loop. The restart would even appear to work, because it clears
        the tmpfs, which is precisely how this class of fault stays invisible. Reporting the
        number instead is what lets monitoring see it coming.
        """
        from app.services.lxd import cert_files

        temp_storage = cert_files.describe_usage()
        if temp_storage.get("available") is False:
            logger.warning(
                "Backend temp storage at %s is unreadable: %s",
                temp_storage.get("path"),
                temp_storage.get("error"),
            )
        if temp_storage.get("degraded"):
            logger.warning(
                "Backend temp storage at %s%% of %s MiB (%s). LXD TLS credential writes fail "
                "when this reaches 100%%, and a failed write is not a cluster outage.",
                temp_storage.get("percent_used"),
                temp_storage.get("total_mib"),
                temp_storage.get("path"),
            )
        # This unauthenticated route publishes an allowlist. Error text can repeat the path or
        # include other filesystem details even when the `path` field itself is omitted.

        # important: reported as a field, never as a status code, for the same reason as
        # temp_storage above. A sealed Vault makes the cluster registry unreadable, and the
        # empty list that comes back is indistinguishable from an empty deployment unless
        # something says so. This is the only signal that tells them apart from outside the
        # process. `error` carries a class name and a short reason, never a Vault address.
        vault = vault_status.snapshot()
        if vault.get("registry_readable") is False:
            if vault.get("registry_source") == "last_known":
                logger.error(
                    "Vault cluster registry is unreadable (%s); the last known registry is being "
                    "served until Vault answers again. Check whether Vault is sealed or the "
                    "token's policy denies LIST on the clusters path.",
                    vault.get("error"),
                )
            else:
                logger.error(
                    "Vault cluster registry is unreadable (%s) and nothing is known. Every cluster "
                    "will appear absent until this clears; check whether Vault is sealed.",
                    vault.get("error"),
                )
        # important: "service" is an identifier monitoring keys on, and Authentik registers
        # the same string as the OAuth2 audience. It does not follow the product name.
        # important: the version and nothing else. f270eb0 took the build commit and date off the
        # unauthenticated surface for the reason stated above, and that decision stands. The version
        # is already published to these same callers by `/` below, so repeating it here discloses
        # nothing new while giving a monitoring probe the build without a second request. The full
        # identity lives behind a credential, on the integrations validate and whoami responses.
        return {
            "status": "healthy",
            "service": "orcastra-dashboard-api",
            **build_identity.public(),
            "temp_storage": cert_files.public_usage(temp_storage),
            "vault": vault,
        }
    
    # Root endpoint
    @app.get("/", tags=["Root"])
    async def root():
        """Root endpoint with API information."""
        # note: the build commit and date name the exact source an unauthenticated caller is
        # talking to. The version stays because clients and the footer read it.
        #
        # Through the helper, not `settings.app_version` directly: reading the setting raw answered
        # the empty string on an unstamped build while `/health` answered null for the same build,
        # which is exactly the drift a single reader exists to prevent.
        response = {
            "name": settings.api_title,
            **build_identity.public(),
            "health": "/health",
        }
        if is_debug:
            response["docs"] = "/docs"
        return response
    
    @app.exception_handler(StarletteHTTPException)
    async def sanitized_http_exception_handler(request: Request, exc: StarletteHTTPException):
        """Answer server faults with a reference instead of the exception text.

        important: 169 handlers do `raise HTTPException(status_code=500, detail=str(e))`, and the
        frontend surfaces `detail` verbatim as `error.message`. The text is written by pylxd,
        requests, asyncpg and hvac, and carries control-plane URLs and ports, TLS failures naming
        certificates, backend filesystem paths, Vault paths and SQL fragments. Most of those
        routers are reachable by a tenant.

        Only 5xx is rewritten. A 4xx is the caller's own request described back to them and is
        the message an integrator needs; replacing those would trade a real leak for a support
        burden. The full text still reaches the server log, tied to the reference the client is
        given, so an operator loses no diagnostic ability.
        """
        if exc.status_code < 500:
            return await http_exception_handler(request, exc)

        reference, full = reference_for(exc)
        logger.error(
            "Server fault %s on %s %s: %s",
            reference, request.method, request.url.path, full,
        )

        # important: 500 and the availability statuses are not the same thing, and treating them
        # alike was wrong in both directions. A 500 is an unexpected fault whose text is a driver
        # or database exception, and there is nothing in it a caller can act on, so it is replaced
        # outright. A 502/503/504 is a deliberate answer this application wrote: "cluster X
        # recently unreachable: certificate not trusted", "proxy saturated, retry shortly". That
        # is exactly what an operator needs, and replacing it turned a diagnosis into a shrug.
        # Those keep their message with URLs and paths stripped out of it.
        if exc.status_code in (502, 503, 504):
            # important: these are the only structured 5xx contracts the browser acts on. A
            # create can reach LXD and then lose its response, while a settled daemon rejection is
            # safe to retry. Flattening both envelopes makes those outcomes indistinguishable.
            # Preserve only the allowlisted discriminator and a sanitised human message, never
            # arbitrary fields supplied by another handler.
            snapshot_error_codes = {
                "snapshot_outcome_unknown",
                "snapshot_terminal_failure",
            }
            if (
                exc.status_code == 502
                and isinstance(exc.detail, dict)
                and exc.detail.get("error") in snapshot_error_codes
            ):
                error_code = exc.detail["error"]
                raw_message = exc.detail.get("message")
                default_message = (
                    "Check this instance's snapshots before trying again."
                    if error_code == "snapshot_outcome_unknown"
                    else "The cluster did not complete the snapshot."
                )
                detail = {
                    "error": error_code,
                    "message": sanitize_error(
                        raw_message if isinstance(raw_message, str) else default_message
                    ),
                }
            else:
                detail = sanitize_error(str(exc.detail))
        else:
            detail = client_detail(reference)

        response = JSONResponse(
            status_code=exc.status_code,
            content={"detail": detail, "reference": reference},
            headers=getattr(exc, "headers", None),
        )
        return apply_security_headers(response)

    # Global exception handler
    @app.exception_handler(LXDLocalStorageError)
    async def lxd_local_storage_error_handler(
        request: Request,
        exc: LXDLocalStorageError
    ):
        """Handle a backend-side failure to write LXD TLS material.

        Separate from the connection handler below on purpose. This is the backend's own disk,
        not the cluster's reachability, and conflating the two told operators every hypervisor
        was offline while they were all healthy.
        """
        logger.error(f"LXD local storage error: {exc}")
        return JSONResponse(
            status_code=503,
            content={
                "error": "Backend Storage Unavailable",
                "detail": str(exc),
                "code": "BACKEND_TEMP_STORAGE_FULL",
            },
        )

    @app.exception_handler(LXDConnectionError)
    async def lxd_connection_error_handler(
        request: Request, 
        exc: LXDConnectionError
    ):
        """Handle LXD connection errors."""
        logger.error(f"LXD connection error: {exc}")
        return JSONResponse(
            status_code=503,
            content={
                "error": "LXD Connection Error",
                "detail": str(exc),
                "code": "LXD_CONNECTION_ERROR",
            },
        )
    
    return app


# Create application instance
app = create_app()


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=4050,
        reload=settings.debug,
        log_level="info",
    )
