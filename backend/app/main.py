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
)
from app.routers.system import router as system_router
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
    
    # Initialize LXD service and check connections
    try:
        lxd_service = get_lxd_service()
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
    except Exception as e:
        logger.error(f"Error during LXD service initialization: {e}")
    
    logger.info("Orcastra Dashboard API started successfully")
    
    yield
    
    # Shutdown
    logger.info("Shutting down Orcastra Dashboard API...")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()
    
    app = FastAPI(
        title=settings.api_title,
        version=settings.api_version,
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
    cors_origins = settings.get_cors_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True if cors_origins != ["*"] else False,
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
            "version": settings.api_version,
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
