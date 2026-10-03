"""
System settings and configuration endpoints.
"""

import asyncio
from typing import Optional
from typing import Literal
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core import build_identity
from app.core.config import get_settings
from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.dependencies import require_tenant, require_partner, require_admin, AuthenticatedUser
# note: the resource-usage models moved to app.models.system_resources; they are re-exported
# here so an importer of app.routers.system keeps working.
from app.models.system_resources import (  # noqa: F401
    ContainerCPU,
    ContainerInfo,
    ContainerMemory,
    CPUInfo,
    DiskInfo,
    HostFilesystem,
    HostInfo,
    MemoryInfo,
    NetworkInterfaceInfo,
    OSInfo,
    ProcessInfo,
    PythonInfo,
    SystemResourceUsage,
    WorkerInfo,
    WorkersInfo,
)
from app.services.system_resources import collect_resource_usage

router = APIRouter(
    prefix="/system",
    tags=["System"],
    dependencies=[Depends(require_tenant())],
)

_cache_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="system_cache")
CacheNamespace = Literal["api_responses", "lxd_resources", "monitoring_history", "tags", "vault", "vault_list"]
CACHE_NAMESPACES = ("api_responses", "lxd_resources", "monitoring_history", "tags", "vault", "vault_list")


class CacheClearResponse(BaseModel):
    message: str
    namespaces: list[str]


class SystemSettings(BaseModel):
    """System settings response model."""
    read_only_mode: bool
    api_version: str
    debug: bool
    # note: a capability, not a preference. The schedule routes are only mounted when the flag is
    # on (main.py:689), so a console that cannot see the flag offers controls whose submit 404s.
    # This endpoint is already polled every 60 seconds for read_only_mode, so the console learns
    # what this deployment can do without a second request or a probe against a missing route.
    snapshot_schedules_enabled: bool
    # The task panel is mounted only when the journal is on; the console hides the dock otherwise.
    task_journal_enabled: bool
    # Migration rollout is deliberately staged. The center flag exposes read-only planning;
    # each mutation class has a separate, default-off production gate.
    migration_center_enabled: bool
    migration_durable_copy_enabled: bool
    migration_instance_move_enabled: bool
    migration_project_member_relocation_enabled: bool
    migration_project_cross_cluster_enabled: bool


class VersionInfo(BaseModel):
    """Comprehensive version information."""
    app_version: str
    api_version: str
    build_date: Optional[str] = None
    build_commit: Optional[str] = None
    environment: str


@router.get("/version", response_model=VersionInfo)
async def get_version():
    """
    Get comprehensive version information for the entire application.
    
    Returns version details including app version, API version, build metadata.
    This endpoint is used by the frontend to display version information.
    """
    settings = get_settings()
    # note: build_date and build_commit read through build_identity so an unstamped image answers
    # null rather than the empty string. The getattr calls this replaces were written before the
    # fields existed on Settings; once they landed with default="" they stopped returning None and
    # started returning "", which a client testing `if (build_commit)` reads as a value.
    # app_version and api_version stay raw strings: the frontend types both as required strings.
    return VersionInfo(
        app_version=settings.app_version,
        api_version=settings.api_version,
        build_date=build_identity.built_at(),
        build_commit=build_identity.commit(),
        environment="development" if settings.debug else "production"
    )


@router.get("/settings", response_model=SystemSettings)
async def get_system_settings():
    """
    Get current system settings.
    
    Returns information about the system configuration including
    whether read-only mode is enabled.
    """
    settings = get_settings()
    return SystemSettings(
        read_only_mode=settings.read_only_mode,
        api_version=settings.api_version,
        debug=settings.debug,
        snapshot_schedules_enabled=settings.snapshot_schedules_enabled,
        task_journal_enabled=settings.task_journal_enabled,
        migration_center_enabled=getattr(settings, "migration_center_enabled", False),
        migration_durable_copy_enabled=getattr(
            settings, "migration_durable_copy_enabled", False
        ),
        migration_instance_move_enabled=getattr(
            settings, "migration_instance_move_enabled", False
        ),
        migration_project_member_relocation_enabled=(
            getattr(settings, "project_relocation_enabled", False)
            and getattr(settings, "migration_project_member_relocation_enabled", False)
        ),
        migration_project_cross_cluster_enabled=(
            getattr(settings, "project_relocation_enabled", False)
            and getattr(settings, "migration_project_cross_cluster_enabled", False)
        ),
    )


@router.get("/cache/stats")
async def get_cache_stats():
    """
    Get cache statistics for monitoring performance.
    
    Returns hit/miss ratios and cache sizes for both Redis and in-memory caches.
    """
    try:
        from app.core.cache import get_cache_manager
        from app.core.encryption import get_encryption
        
        cache = get_cache_manager()
        encryption = get_encryption()
        
        stats = cache.get_stats()
        stats["encryption_enabled"] = encryption is not None
        stats["sensitive_namespaces"] = cache.sensitive_namespaces
        
        return {
            "enabled": True,
            "stats": stats
        }
    except Exception as e:
        return {
            "enabled": False,
            "error": str(e)
        }


@router.post("/cache/clear", response_model=CacheClearResponse)
async def clear_cache(
    namespace: Optional[CacheNamespace] = None,
    _rbac: AuthenticatedUser = Depends(require_admin()),
):
    """
    Clear cache for a specific namespace or all caches.
    """
    from app.core.cache import get_cache_manager
    cache = get_cache_manager()
    targets = (namespace,) if namespace else CACHE_NAMESPACES
    try:
        cleared, failed = await run_blocking(
            lambda: cache.clear_namespaces(targets), timeout=30.0,
            label="system.cache.clear", executor=_cache_executor, max_in_flight=2,
        )
    except (BlockingTimeout, BlockingPoolFull) as exc:
        raise HTTPException(status_code=503, detail="Cache clearing is temporarily unavailable. Retry later.") from exc
    if failed:
        raise HTTPException(status_code=503, detail={
            "message": "Cache clearing was incomplete. Retry the failed namespaces.",
            "namespaces": cleared, "failed_namespaces": failed,
        })
    return CacheClearResponse(message="Cache cleared", namespaces=cleared)


@router.get(
    "/resource-usage",
    response_model=SystemResourceUsage,
    dependencies=[Depends(require_partner())],
)
async def get_system_resource_usage():
    """
    Real-time resource usage in two honest scopes.

    Deployment-aware: `host` is the kernel's view of the machine, `container` is this
    deployment's cgroup scope (limits, what counts against them, the worker set). The
    top-level cpu/memory/disks/process/os blocks are kept for older clients and mix
    both scopes. Partner and above only; the panel names host devices and PIDs.
    """
    try:
        # note: the collector reads /proc and /sys and psutil; it never sleeps, but it is
        # sync work, so it runs on a thread rather than on the event loop.
        return await asyncio.to_thread(collect_resource_usage)
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="psutil is not installed. Please install it with: pip install psutil"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
