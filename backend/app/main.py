"""
Orcastra Dashboard Backend API
Main FastAPI application entry point.
"""

import logging
import time
from contextlib import asynccontextmanager
from collections import defaultdict
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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Rate limiting storage (in-memory, consider Redis for production)
rate_limit_storage = defaultdict(list)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup and shutdown."""
    # Startup
    logger.info("Starting Orcastra Dashboard API...")
    settings = get_settings()
    
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
                
                # IMPORTANT: Connect to ALL registered clusters on startup
                # This ensures clusters stay connected even after Docker restart
                # Without this, clusters show as "Pending Trust" after container restarts
                connected_count = 0
                failed_clusters = []
                
                for cluster_id in cluster_ids:
                    try:
                        # Verify connection is possible (certificate is trusted)
                        connected, error_msg = cert_manager.verify_connection(cluster_id)
                        if connected:
                            cluster_info = cert_manager.get_cluster_info(cluster_id)
                            cert_data = cert_manager.get_cluster_certificate(cluster_id)
                            
                            # Add to connection manager for subsequent API calls
                            lxd_service.connection_manager.connect_with_vault_cert(
                                cluster_id=cluster_id,
                                endpoint=cluster_info["metadata"]["endpoint"],
                                cert_content=cert_data["client.crt"],
                                key_content=cert_data["client.key"],
                                verify=False
                            )
                            connected_count += 1
                            logger.info(f"Connected to cluster: {cluster_id}")
                        else:
                            # Cluster certificate not yet trusted - this is expected for new registrations
                            failed_clusters.append((cluster_id, error_msg or "Not authenticated"))
                            logger.debug(f"Cluster {cluster_id} not authenticated: {error_msg}")
                    except Exception as cluster_err:
                        failed_clusters.append((cluster_id, str(cluster_err)))
                        logger.warning(f"Could not connect cluster {cluster_id}: {cluster_err}")
                
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


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()
    
    app = FastAPI(
        title=settings.api_title,
        version=settings.app_version,
        description="""
## Orcastra Dashboard API

A unified REST API for managing virtual machines across multiple nodes.

This API uses certificate-based authentication to connect to nodes.
Ensure your client certificates are properly configured.
        """,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    
    # Configure CORS with environment-based origins
    # When CORS_ORIGINS is "*", we use a custom approach to support credentials
    cors_origins = settings.get_cors_origins()
    
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
        
        client_ip = request.client.host
        current_time = time.time()
        
        # Clean old requests (older than 1 minute)
        rate_limit_storage[client_ip] = [
            req_time for req_time in rate_limit_storage[client_ip]
            if current_time - req_time < 60
        ]
        
        # Check rate limit
        if len(rate_limit_storage[client_ip]) >= settings.rate_limit_requests:
            logger.warning(f"Rate limit exceeded for IP: {client_ip}")
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded. Please try again later."
            )
        
        # Add current request
        rate_limit_storage[client_ip].append(current_time)
        
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
    from app.routers.integrations import router as integrations_router
    app.include_router(integrations_router, prefix=settings.api_prefix)
    
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
    
    # Monitoring Router (Real-time metrics dashboard)
    from app.routers.monitoring import router as monitoring_router
    app.include_router(monitoring_router, prefix=settings.api_prefix)
    
    # Terminal Router (WebSocket terminal access)
    app.include_router(terminal_router, prefix=settings.api_prefix)
    
    # Console Router (VGA/Graphics console access)
    from app.routers.console import router as console_router
    app.include_router(console_router, prefix=settings.api_prefix)
    
    # Health check endpoint
    @app.get("/health", tags=["Health"])
    async def health_check():
        """Health check endpoint."""
        return {"status": "healthy", "service": "orcastra-dashboard-api"}
    
    # Root endpoint
    @app.get("/", tags=["Root"])
    async def root():
        """Root endpoint with API information."""
        return {
            "name": settings.api_title,
            "version": settings.app_version,
            "build_date": getattr(settings, 'build_date', None),
            "build_commit": getattr(settings, 'build_commit', None),
            "docs": "/docs",
            "health": "/health",
        }
    
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
