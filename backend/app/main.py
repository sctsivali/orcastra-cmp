"""
Orcastra Dashboard Backend API
Main FastAPI application entry point.
"""

import logging
import time
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
import uvicorn
import urllib3

# Suppress InsecureRequestWarning for unverified HTTPS requests
# This is expected behavior when connecting to LXD with self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from app.core.config import get_settings
from app.core.logging_config import setup_logging
from app.middleware import RequestIdMiddleware, AccessLogMiddleware
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
from app.services.lxd_service import get_lxd_service, LXDConnectionError

# Configure structured JSON logging
settings_for_logging = get_settings()
setup_logging(
    service_name="orcastra-backend",
    version=settings_for_logging.app_version or "0.0.0",
    log_level="DEBUG" if settings_for_logging.debug else "INFO",
    json_logs=not settings_for_logging.debug  # JSON in production, human-readable in dev
)
logger = logging.getLogger(__name__)

# Rate limiting storage - bounded dict to prevent memory leaks
# key: bucket_key (user_id or IP), value: list of timestamps
_MAX_RATE_BUCKETS = 10_000
rate_limit_storage: dict[str, list[float]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup and shutdown."""
    # Startup
    logger.info("Starting Orcastra Dashboard API...")
    settings = get_settings()
    
    # Pre-warm JWKS cache to avoid blocking first auth request (~5s savings)
    if settings.auth_enabled and settings.authentik_issuer:
        try:
            from app.core.auth import _jwks_client
            await _jwks_client.warm_up(settings)
        except Exception as jwks_err:
            logger.warning(f"JWKS pre-warm failed (will lazy-load on first request): {jwks_err}")
    
    # Initialize PostgreSQL database
    try:
        from app.core.database import init_db
        await init_db()
        logger.info("PostgreSQL database initialized")
    except Exception as db_err:
        logger.warning(f"Could not initialize database: {db_err}")
    
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
                
                # List clusters from Vault
                cluster_ids = cert_manager.list_clusters()
                logger.info(f"Found {len(cluster_ids)} registered clusters in Vault")
                
                # Sync registered cluster whitelist into connection_manager
                # to ensure get_node_names() only returns registered nodes
                lxd_service.connection_manager.sync_registered_clusters(cluster_ids)
                
                # Connect to ALL registered clusters in parallel for faster startup
                # This ensures clusters stay connected even after Docker restart
                import asyncio
                
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
        
        statuses = lxd_service.check_nodes_status()
        
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
    
    # Hydrate metrics cache from PostgreSQL (fills chart data after restart)
    try:
        from app.services.metrics_history_service import load_metrics_history
        from app.core.cache import get_cache_manager
        history = await load_metrics_history(points=8640, scope="all")
        if history:
            cache = get_cache_manager()
            cache.set("monitoring_history", "monitoring:history:scope=all", history, ttl=86400)
            logger.info(f"Hydrated metrics cache from DB ({len(history)} points)")
    except Exception as hydrate_err:
        logger.debug(f"Metrics cache hydration skipped: {hydrate_err}")
    
    # Start session tracker cleanup loop
    try:
        from app.services.session_tracker import get_session_tracker
        session_tracker = get_session_tracker()
        await session_tracker.start_cleanup_loop()
        logger.info("Session tracker cleanup loop started")
    except Exception as tracker_err:
        logger.warning(f"Could not start session tracker: {tracker_err}")
    
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
    
    logger.info("Orcastra Dashboard API started successfully")
    
    yield
    
    # Shutdown
    logger.info("Shutting down Orcastra Dashboard API...")
    
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
        get_session_tracker().stop_cleanup()
        logger.info("Session tracker cleanup stopped")
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
## Orcastra Dashboard API

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
    
    # Add Access Log middleware (logs HTTP requests in structured JSON)
    app.add_middleware(
        AccessLogMiddleware,
        service_name="orcastra-backend",
        version=settings.app_version or "0.0.0"
    )
    
    # Add Request ID middleware (must be added before access log to generate IDs)
    app.add_middleware(RequestIdMiddleware)
    
    # Add security headers middleware
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response
    
    # Add rate limiting middleware
    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        if not settings.rate_limit_enabled:
            return await call_next(request)
        
        # skip preflight requests - they must never be rate-limited
        if request.method == "OPTIONS":
            return await call_next(request)
        
        # use authenticated user (JWT sub) as bucket key when available,
        # fallback to client IP - avoids all Docker users sharing one bucket
        bucket_key = request.client.host
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            import jwt as _jwt
            try:
                payload = _jwt.decode(
                    auth_header[7:],
                    options={"verify_signature": False},  # just extract sub
                )
                bucket_key = f"user:{payload.get('sub', bucket_key)}"
            except Exception:
                pass  # fallback to IP
        
        current_time = time.time()
        
        # Evict oldest bucket if storage is at capacity (prevents unbounded growth)
        if bucket_key not in rate_limit_storage and len(rate_limit_storage) >= _MAX_RATE_BUCKETS:
            oldest_key = min(rate_limit_storage, key=lambda k: rate_limit_storage[k][-1] if rate_limit_storage[k] else 0)
            del rate_limit_storage[oldest_key]
        
        # Ensure bucket exists
        if bucket_key not in rate_limit_storage:
            rate_limit_storage[bucket_key] = []
        
        # Clean old requests (older than 1 minute)
        rate_limit_storage[bucket_key] = [
            req_time for req_time in rate_limit_storage[bucket_key]
            if current_time - req_time < 60
        ]
        
        # Periodically prune stale empty buckets (every ~100 requests)
        if len(rate_limit_storage) > 100 and hash(bucket_key) % 100 == 0:
            stale_keys = [
                k for k, v in rate_limit_storage.items()
                if not v or (current_time - v[-1] > 120)
            ]
            for k in stale_keys:
                if k != bucket_key:
                    del rate_limit_storage[k]
        
        # Check rate limit - return JSONResponse instead of raising so CORS
        # headers are preserved (HTTPException bypasses CORS middleware)
        if len(rate_limit_storage[bucket_key]) >= settings.rate_limit_requests:
            logger.warning(f"Rate limit exceeded for: {bucket_key}")
            origin = request.headers.get("origin", "")
            response = JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Please try again later."},
            )
            # attach CORS headers so the browser can read the error
            if origin:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Retry-After"] = "30"
            return response
        
        # Add current request
        rate_limit_storage[bucket_key].append(current_time)
        
        return await call_next(request)
    
    # Register routers
    app.include_router(vms_router, prefix=settings.api_prefix)
    app.include_router(images_router, prefix=settings.api_prefix)
    app.include_router(nodes_router, prefix=settings.api_prefix)
    app.include_router(projects_router, prefix=settings.api_prefix)
    app.include_router(tags_router, prefix=settings.api_prefix)
    app.include_router(system_router, prefix=settings.api_prefix)
    
    # LXD Feature Routers
    app.include_router(instances_router, prefix=settings.api_prefix)
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
    
    # Console Router (VGA/Graphics console access)
    from app.routers.console import router as console_router
    app.include_router(console_router, prefix=settings.api_prefix)
    
    # Organization Router (Three-Tier RBAC)
    from app.routers.organizations import router as organizations_router
    app.include_router(organizations_router, prefix=settings.api_prefix)
    
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
        """Health check endpoint."""
        return {"status": "healthy", "service": "orcastra-dashboard-api"}
    
    # Root endpoint
    @app.get("/", tags=["Root"])
    async def root():
        """Root endpoint with API information."""
        response = {
            "name": settings.api_title,
            "version": settings.app_version,
            "build_date": getattr(settings, 'build_date', None),
            "build_commit": getattr(settings, 'build_commit', None),
            "health": "/health",
        }
        if is_debug:
            response["docs"] = "/docs"
        return response
    
    # Global exception handler
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
