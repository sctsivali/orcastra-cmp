"""
API Router for LXD Project operations.
Provides endpoints for listing and creating projects.
"""

from fastapi import APIRouter, HTTPException, Depends
from typing import Any, Awaitable, Callable, Dict, List, Optional
import logging
from datetime import datetime, timezone
import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import re

from app.models.schemas import (
    ProjectInfo,
    ProjectListResponse,
    ProjectCreateRequest,
    ProjectCreateResponse,
    ProjectUpdateRequest,
    ProjectUpdateResponse,
    ErrorResponse,
    ProjectUsageResponse,
    ProjectUsageLimits,
    ProjectUsageUsed,
)
from app.services.lxd_service import (
    LXDService,
    LXDConnectionError,
    LXDOperationError,
    get_lxd_service,
)
from app.services.lxd.exceptions import (
    LXDLocalUnavailableError,
    LXDOperationUnknownError,
    LXDProjectNotFoundError,
)
from app.services.instance_security_policy import (
    ClusterWritePolicy,
    InstanceSecurityPolicyViolation,
    prepare_project_create_config,
    resolve_cluster_write_policy,
    validate_target_project_isolation,
    validate_project_update,
)
from app.services.shared_cluster_audit import serialize_cluster_configuration
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.api_key_capabilities import is_valid_project_name
from app.core.database import get_db
from app.core.rbac import Role, get_highest_role, resolve_roles
from app.services.orcahub_webhook import schedule_orcahub_event
from app.core.org_scope import (
    filter_projects_by_acl,
    get_scope_cache_key,
    org_scope_dependency,
    org_to_hold_project_grant,
    project_is_visible,
    verify_cluster_access,
    visible_projects_for,
)
from app.services import project_grants
from app.core.registry_guard import require_known_registry
from app.core.cache import get_cache_manager, get_resource_list_generation
from app.core import swr
from app.core.swr import Built
from app.services.activity_logger import get_activity_logger
from app.services.migration_fence import (
    MigrationFenceConflict,
    assert_project_not_migration_fenced,
)
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# The create form reads this once per project pick; dependent writes drop it.
PROJECT_USAGE_CACHE_TTL = 30
PROJECT_LIST_CACHE_KIND = "projects"
PROJECT_USAGE_CACHE_KIND = "project_usage"
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/projects",
    tags=["Projects"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# Shared thread pool for blocking operations
_projects_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="projects_")

# The LXD project reader admits at most 10 blocking calls per worker. Keep fleet reads below
# that ceiling so one cold fleet request cannot reject its own later clusters and so the
# selected-cluster picker retains capacity while the fleet cache is rebuilding.
_PROJECT_FLEET_BATCH_SIZE = 8
_PROJECT_RESOURCE_WALL_SECONDS = 90.0
_PROJECT_RESOURCE_MAX_IN_FLIGHT = 10
_PROJECT_RESOURCE_MAX_IN_FLIGHT_PER_CLUSTER = 1


async def _bounded_node_resources(lxd_service: LXDService, host: str) -> Dict[str, Any]:
    """Read capacity off-loop with global, per-cluster, wall, and socket bounds."""
    # The pooled LXD session applies its default connect/read socket timeout to every nested
    # call made by get_node_resources. This outer bound controls request latency and admission.
    return await run_blocking(
        lxd_service.get_node_resources,
        host,
        timeout=_PROJECT_RESOURCE_WALL_SECONDS,
        label=f"project.capacity[{host}]",
        executor=_projects_executor,
        max_in_flight=_PROJECT_RESOURCE_MAX_IN_FLIGHT,
        concurrency_key=host,
        max_in_flight_per_key=_PROJECT_RESOURCE_MAX_IN_FLIGHT_PER_CLUSTER,
    )


async def _finish_critical_write(awaitable: Awaitable[Any]) -> tuple[Any, bool]:
    """Finish an admitted daemon write even if its HTTP requester disconnects.

    The caller keeps the ownership lock around this helper. Returning the cancellation marker
    lets it persist any matching organization grant before propagating cancellation.
    """
    task = asyncio.create_task(awaitable)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                return task.result(), cancelled


async def _gather_project_reads(
    hosts: List[str],
    reader: Callable[[str], Awaitable[Any]],
) -> List[Any]:
    """Read a fleet in ordered batches without saturating the project worker pool."""
    results: List[Any] = []
    for start in range(0, len(hosts), _PROJECT_FLEET_BATCH_SIZE):
        batch = hosts[start:start + _PROJECT_FLEET_BATCH_SIZE]
        results.extend(await asyncio.gather(
            *(reader(host) for host in batch),
            return_exceptions=True,
        ))
        if any(isinstance(result, LXDLocalUnavailableError) for result in results):
            break
    return results


_SIZE_PATTERN = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)?$",
    re.IGNORECASE,
)
_SIZE_FACTORS = {
    "B": 1,
    "KB": 10**3,
    "MB": 10**6,
    "GB": 10**9,
    "TB": 10**12,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
}


def _parse_size_to_bytes(size_str: str) -> int:
    """Parse LXD byte sizes without conflating decimal and binary units."""
    text = size_str.strip()
    match = _SIZE_PATTERN.fullmatch(text)
    if not match:
        raise ValueError(
            "Invalid size format. Use B, KB, MB, GB, TB, KiB, MiB, GiB, or TiB."
        )

    value = Decimal(match.group(1))
    unit = (match.group(2) or "B").upper()
    return int(value * _SIZE_FACTORS[unit])


def _format_size(bytes_value: int, units: tuple[tuple[str, int], ...]) -> str:
    """Format a byte amount using the semantic unit family supplied by the caller."""
    value = max(0, int(bytes_value))
    for unit, factor in units:
        if value >= factor:
            amount = Decimal(value) / Decimal(factor)
            rendered = f"{amount:.2f}".rstrip("0").rstrip(".")
            return f"{rendered} {unit}"
    return f"{value} B"


def _format_memory(bytes_value: int) -> str:
    return _format_size(
        bytes_value,
        (("TiB", 1024**4), ("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)),
    )


def _format_storage(bytes_value: int) -> str:
    return _format_size(
        bytes_value,
        (("TB", 10**12), ("GB", 10**9), ("MB", 10**6), ("KB", 10**3)),
    )


def _invalidate_project_caches() -> None:
    """Fence and clear every cached project projection affected by a project write."""
    get_cache_manager().invalidate_resource_cache(
        PROJECT_LIST_CACHE_KIND,
        PROJECT_USAGE_CACHE_KIND,
    )



def _refuse_cap_above_hardware(
    resources: Dict[str, Any],
    *,
    max_cpu: Optional[int],
    ram_bytes: Optional[int],
    storage_bytes: Optional[int],
) -> None:
    """Refuse a project limit larger than the hardware it would run on, naming both numbers.

    Compared against totals on purpose: cpu_available is threads minus allocation and goes to
    zero on an over-committed cluster, memory_available is live free memory, and neither is
    what a cap means. On a clustered host the totals are the cluster's (see
    LXDService.get_node_resources).
    """
    if max_cpu is not None and max_cpu > resources.get("cpu_total", 0):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Project allocation ({max_cpu} vCPU) exceeds this cluster's capacity of "
                f"{resources.get('cpu_total', 0)} hardware threads"
            ),
        )
    if ram_bytes is not None and ram_bytes > resources.get("memory_total", 0):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Memory limit ({_format_memory(ram_bytes)}) exceeds this cluster's capacity "
                f"of {_format_memory(resources.get('memory_total', 0))}"
            ),
        )
    if storage_bytes is not None and storage_bytes > resources.get("storage_total", 0):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Storage limit ({_format_storage(storage_bytes)}) exceeds this cluster's "
                f"capacity of {_format_storage(resources.get('storage_total', 0))}"
            ),
        )

@router.get(
    "",
    response_model=List[ProjectListResponse],
    summary="List all projects from all clusters",
    description="Get a list of all projects across all connected clusters.",
    responses={
        200: {"description": "List of projects retrieved successfully"},
        503: {"model": ErrorResponse, "description": "Local inventory reader unavailable"},
        500: {"model": ErrorResponse, "description": "Server error"},
    }
)
async def list_all_projects(
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """List all projects across all LXD nodes."""
    # 30 s soft TTL, then served for SWR_MAX_STALE_SECONDS while one background read replaces it.
    cache = get_cache_manager()

    async def build() -> Built:
        node_names = lxd_service.get_node_names()
        # An unreadable registry with nothing known is not "no projects" (issue #15).
        require_known_registry(node_names)
        logger.info(f"Fetching projects from {len(node_names)} nodes in parallel")
        
        # Helper to fetch projects for a single host
        async def fetch_host_projects(host: str) -> Optional[ProjectListResponse]:
            try:
                projects_data = await lxd_service.list_projects(host)
                projects = [ProjectInfo(**proj) for proj in projects_data]
                return ProjectListResponse(
                    host=host,
                    projects=projects,
                    total=len(projects),
                )
            except LXDLocalUnavailableError:
                raise
            except Exception as e:
                # important: return the host, labelled, rather than None. Dropping it made
                # an unreachable cluster vanish from the response entirely, so the dashboard
                # project count silently fell with nothing to explain it.
                logger.warning(f"Could not list projects from {host}: {e}")
                return ProjectListResponse(
                    host=host,
                    projects=[],
                    total=0,
                    reachable=False,
                    error=str(e) or e.__class__.__name__,
                )
        
        # Bound each wave below the service pool's admission limit. A single fleet refresh
        # must not make healthy later clusters look unreachable or starve a direct picker read.
        results = await _gather_project_reads(node_names, fetch_host_projects)
        
        local_failure = next(
            (result for result in results if isinstance(result, LXDLocalUnavailableError)),
            None,
        )
        if local_failure is not None:
            raise local_failure

        # Filter valid results
        all_projects = []
        answered = 0
        for index, result in enumerate(results):
            if isinstance(result, ProjectListResponse):
                if result.reachable:
                    answered += 1
                # apply tenant ACL filtering per host, then the Partner project bound. The
                # cached body is already keyed per bound through get_scope_cache_key.
                allowed_names = filter_projects_by_acl(result.host, [p.name for p in result.projects])
                filtered = [
                    p for p in result.projects
                    if p.name in allowed_names and project_is_visible(result.host, p.name)
                ]
                all_projects.append(ProjectListResponse(
                    host=result.host,
                    projects=filtered,
                    total=len(filtered),
                    reachable=result.reachable,
                    error=result.error,
                ))
            elif isinstance(result, Exception):
                logger.warning(f"Project fetch error: {result}")
                all_projects.append(ProjectListResponse(
                    host=node_names[index] if index < len(node_names) else "unknown",
                    projects=[],
                    total=0,
                    reachable=False,
                    error=str(result) or result.__class__.__name__,
                ))

        # important: an outage must not be remembered as the answer. See instances.py.
        if not answered:
            logger.warning("Not caching projects list: no node answered")
        return Built(
            value=all_projects,
            body=[proj.model_dump() for proj in all_projects],
            fresh_for=30 if answered else None,
        )

    try:
        # e1: the body moved into the stale-while-revalidate envelope. The list stays a bare
        # array, so its age travels only in the X-Orcastra-Cache-Age header.
        served = await swr.serve(
            cache=cache,
            base_key="projects:list:all:e1",
            fresh=fresh,
            build=build,
            generation=lambda: get_resource_list_generation(cache, PROJECT_LIST_CACHE_KIND),
            label="projects list",
        )
        if served.built:
            return served.value
        return [ProjectListResponse(**item) for item in served.body]

    except HTTPException:
        raise
    except LXDLocalUnavailableError as e:
        logger.warning("Project inventory is locally unavailable: %s", e)
        raise HTTPException(
            status_code=503,
            detail="Project inventory is temporarily unavailable",
        )
    except Exception as e:
        logger.error(f"Error listing all projects: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{host}",
    response_model=ProjectListResponse,
    summary="List all projects",
    description="Get a list of all projects in a specific cluster.",
    responses={
        200: {"description": "List of projects retrieved successfully"},
        503: {"model": ErrorResponse, "description": "LXD connection error"},
        500: {"model": ErrorResponse, "description": "Server error"},
    }
)
async def list_projects(
    host: str,
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all projects on a specific LXD host."""
    verify_cluster_access(host)
    try:
        projects_data = await lxd_service.list_projects(host)
        
        projects = [ProjectInfo(**proj) for proj in projects_data]
        
        # apply tenant ACL filtering, then the Partner project bound
        allowed_names = filter_projects_by_acl(host, [p.name for p in projects])
        projects = [p for p in projects if p.name in allowed_names and project_is_visible(host, p.name)]
        
        return ProjectListResponse(
            host=host,
            projects=projects,
            total=len(projects),
        )
        
    except LXDConnectionError as e:
        logger.warning(f"LXD connection error: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        logger.error(f"LXD operation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Error listing projects: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{host}/{project_name}/usage",
    response_model=ProjectUsageResponse,
    summary="Project limit usage",
    description=(
        "How much of each of the project's limits its instances already allocate, summed the "
        "way LXD sums it (every instance whatever its state, expanded config). Aggregates only."
    ),
    responses={
        200: {"description": "Usage read"},
        404: {"model": ErrorResponse, "description": "Project not visible to the caller"},
        503: {"model": ErrorResponse, "description": "LXD connection error"},
    },
)
async def get_project_usage(
    host: str,
    project_name: str,
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """Usage against the project's limits, for the create-instance form's quota line."""
    verify_cluster_access(host)
    # Same silence as the list route about a project the caller may not see.
    if not project_is_visible(host, project_name) or not filter_projects_by_acl(host, [project_name]):
        raise HTTPException(
            status_code=404,
            detail=f"Project '{project_name}' not found in cluster '{host}'",
        )

    cache = get_cache_manager()
    cache_key = get_scope_cache_key(f"project_usage:host={host}:project={project_name}")
    cached = None if fresh else cache.get("api_responses", cache_key)
    if cached is not None:
        return ProjectUsageResponse(**cached)
    usage_generation = get_resource_list_generation(cache, PROJECT_USAGE_CACHE_KIND)
    try:
        usage = await lxd_service.get_project_usage(host, project_name)
    except HTTPException:
        raise
    except LXDConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except LXDProjectNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Project '{project_name}' not found in cluster '{host}'",
        )
    except LXDOperationError as e:
        raise HTTPException(status_code=500, detail=str(e))
    response = ProjectUsageResponse(
        host=host,
        project=project_name,
        limits=ProjectUsageLimits(**usage.limits),
        used=ProjectUsageUsed(**usage.used),
        complete=usage.complete,
        collected_at=datetime.now(timezone.utc),
    )
    # Short, and dropped by invalidate_instance_cache, so a create or delete refreshes it.
    if (
        usage.complete
        and get_resource_list_generation(cache, PROJECT_USAGE_CACHE_KIND)
        == usage_generation
    ):
        cache.set("api_responses", cache_key, response.model_dump(mode="json"), ttl=PROJECT_USAGE_CACHE_TTL)
    elif usage.complete:
        logger.info(
            "Not caching usage for project %s on %s: a dependent mutation completed during the read",
            project_name,
            host,
        )
    return response


def _refuse_a_project_outside_the_bound(host: str, project_name: str) -> None:
    """404, the usage route's choice: a 403 on a guessed name would confirm that another
    organization has a project by that name on the shared cluster."""
    if not project_is_visible(host, project_name) or not filter_projects_by_acl(host, [project_name]):
        raise HTTPException(
            status_code=404,
            detail=f"Project '{project_name}' not found in cluster '{host}'",
        )


def _policy_refusal(exc: InstanceSecurityPolicyViolation) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.as_detail())


async def _cluster_write_policy(
    user: AuthenticatedUser,
    host: str,
    db: AsyncSession,
) -> ClusterWritePolicy:
    """Resolve current ownership, refusing closed when the database cannot prove it."""
    try:
        return await resolve_cluster_write_policy(user, host, db)
    except InstanceSecurityPolicyViolation as exc:
        raise _policy_refusal(exc) from exc
    except Exception as exc:
        logger.error("Could not verify write policy for cluster %s: %s", host, type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="Cluster ownership could not be verified. No change was sent.",
        ) from exc


@router.post(
    "",
    response_model=ProjectCreateResponse,
    summary="Create a new project",
    description="Create a new project in a specific cluster.",
    responses={
        201: {"description": "Project created successfully"},
        400: {"model": ErrorResponse, "description": "Invalid request"},
        503: {"model": ErrorResponse, "description": "LXD connection error"},
        500: {"model": ErrorResponse, "description": "Server error"},
    },
    status_code=201,
)
async def create_project(
    request: ProjectCreateRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Create a new project on a specific LXD host.

    A Partner's project is recorded in their organization's managed policy right after LXD
    creates it, so on a cluster shared with another organization the project is inside their
    bound from the next request on. The rule can only ever name a project this request brought
    into existence: LXD refuses a duplicate name before the grant runs.
    """
    verify_cluster_access(request.host)
    if not is_valid_project_name(request.name):
        raise HTTPException(status_code=422, detail="Invalid project name")
    grant_org: Optional[int] = None
    try:
        # important: a project limit is a cap, not a reservation. LXD allows any limit and any
        # over-commit, so the only figure that makes a cap nonsensical is the hardware total.
        # Comparing against cpu_available (threads minus allocation) refused every project on
        # an over-committed cluster with "exceeds available CPU (-227)".
        try:
            node_resources = await _bounded_node_resources(lxd_service, request.host)
            _refuse_cap_above_hardware(
                node_resources,
                max_cpu=request.max_cpu,
                ram_bytes=_parse_size_to_bytes(request.max_ram),
                storage_bytes=_parse_size_to_bytes(request.max_storage),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (
            BlockingPoolFull,
            BlockingTimeout,
            LXDConnectionError,
            LXDOperationError,
        ):
            # if we can't get resources, just log and continue
            logger.warning(f"Could not validate resources for {request.host}, proceeding anyway")

        async with serialize_cluster_configuration(db, request.host):
            policy = await _cluster_write_policy(user, request.host, db)
            project_config = prepare_project_create_config(policy, request.config or {})
            validate_target_project_isolation(policy, project_config)

            try:
                await assert_project_not_migration_fenced(
                    cluster_id=request.host,
                    project=request.name,
                    db=db,
                )
            except MigrationFenceConflict as exc:
                raise HTTPException(status_code=409, detail=exc.detail) from exc

            if get_highest_role(resolve_roles(user)) == Role.PARTNER:
                grant_org = await org_to_hold_project_grant(user.email, request.host, db)
                if grant_org is None and visible_projects_for(request.host) is not None:
                    # Refused before LXD is touched: created first, the project would be one
                    # its creator can neither see nor delete.
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            "Your account is not a member of an organization bound to cluster "
                            f"'{request.host}', so a project created there could not be shown "
                            "to you"
                        ),
                    )

            async def submit_and_record_grant() -> tuple[Dict[str, Any], str]:
                project_data = await lxd_service.create_project(
                    host=request.host,
                    name=request.name,
                    description=request.description,
                    config=project_config,
                    max_cpu=request.max_cpu,
                    max_ram=request.max_ram,
                    max_storage=request.max_storage,
                )
                message = (
                    f"Project '{request.name}' created successfully in cluster "
                    f"'{request.host}'"
                )
                if grant_org is not None:
                    message += await project_grants.record_created_project(
                        db,
                        organization_id=grant_org,
                        cluster_id=request.host,
                        project_name=request.name,
                        created_by=user.email,
                    )
                return project_data, message

            critical_result, request_cancelled = await _finish_critical_write(
                submit_and_record_grant()
            )
            project_data, message = critical_result
            if request_cancelled:
                # The daemon result and matching grant are now consistent. Preserve normal
                # cancellation semantics only after the security-critical pair is complete.
                raise asyncio.CancelledError

        project = ProjectInfo(**project_data)

        # Advance the generations before clearing the list and quota families. A read that
        # began before this create must not restore a stale response after the sweep.
        _invalidate_project_caches()
        

        # Announce the lifecycle change to any subscribed add-on. Nothing emitted these events
        # until now, so the catalogue was a promise with no sender behind it.
        from app.services.webhooks.emitter import emit_resource_event

        await emit_resource_event(
            event_type="orcastra.project.created.v1",
            cluster_id=request.host,
            resource_type="project",
            resource_name=request.name,
        )

        # Log activity for audit trail
        activity_logger.log_activity(
            action="project.create",
            target_type="project",
            target_id=request.name,
            target_host=request.host,
            result="success",
            details={
                "description": request.description,
                "max_cpu": request.max_cpu,
                "max_ram": request.max_ram,
                "max_storage": request.max_storage,
            }
        )
        
        schedule_orcahub_event("project.created", request.host, request.name, request.name, resource_type="PROJECT")
        return ProjectCreateResponse(
            success=True,
            message=message,
            project=project,
        )
        
    except LXDConnectionError as e:
        logger.warning(f"LXD connection error: {e}")
        activity_logger.log_activity(
            action="project.create",
            target_type="project",
            target_id=request.name,
            target_host=request.host,
            result="failure",
            error_code="LXDConnectionError",
            error_message=str(e),
        )
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationUnknownError as e:
        logger.warning(
            "Project create outcome is unknown for %s on %s",
            request.name,
            request.host,
        )
        activity_logger.log_activity(
            action="project.create",
            target_type="project",
            target_id=request.name,
            target_host=request.host,
            result="unknown",
            error_code="LXDOperationUnknownError",
            error_message="Daemon outcome could not be confirmed",
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "project_write_unknown",
                "message": (
                    "The daemon outcome could not be confirmed. Check the exact project "
                    "state before retrying."
                ),
            },
        ) from e
    except LXDOperationError as e:
        logger.error(f"LXD operation error: {e}")
        activity_logger.log_activity(
            action="project.create",
            target_type="project",
            target_id=request.name,
            target_host=request.host,
            result="failure",
            error_code="LXDOperationError",
            error_message=str(e),
        )
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        # important: let a deliberate status through. This try raises HTTPException
        # itself, and the broad handler below rewrote it, so an unreachable cluster
        # answered 500 or 404 instead of 503 and a tenant ACL denial answered 500
        # instead of 403.
        raise
    except InstanceSecurityPolicyViolation as exc:
        raise _policy_refusal(exc) from exc
    except Exception as e:
        logger.error(f"Error creating project: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch(
    "/{host}/{project_name}",
    response_model=ProjectUpdateResponse,
    summary="Update a project",
    description="Update project description and resource limits.",
    responses={
        200: {"model": ProjectUpdateResponse, "description": "Project updated successfully"},
        400: {"model": ErrorResponse, "description": "Invalid request or insufficient resources"},
        503: {"model": ErrorResponse, "description": "LXD connection error"},
        500: {"model": ErrorResponse, "description": "Server error"},
    }
)
async def update_project(
    host: str,
    project_name: str,
    request: ProjectUpdateRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Update a project's description and resource limits."""
    verify_cluster_access(host)
    _refuse_a_project_outside_the_bound(host, project_name)
    try:
        async with serialize_cluster_configuration(db, host):
            policy = await _cluster_write_policy(user, host, db)

            # This exact daemon record is both the update target and the isolation evidence.
            # Keep the read, admission, and write under the same ownership lock.
            projects = await lxd_service.list_projects(host)
            project = next((p for p in projects if p["name"] == project_name), None)

            if not project:
                raise HTTPException(
                    status_code=404,
                    detail=f"Project '{project_name}' not found in cluster '{host}'"
                )
            validate_project_update(policy, project.get("config") or {})

            # If resource limits are being updated (non-zero), validate against available
            # resources. 0 clears a limit and None leaves it unchanged.
            has_real_limits = (
                (request.max_cpu is not None and request.max_cpu > 0) or
                (request.max_ram is not None and request.max_ram != "0") or
                (request.max_storage is not None and request.max_storage != "0")
            )
            available = None
            if has_real_limits:
                # important: the quota precheck must not be able to fail the update. Reading
                # node resources is a live call, so a transient failure must not mislabel the
                # typed request as invalid.
                try:
                    available = await _bounded_node_resources(lxd_service, host)
                except (
                    BlockingPoolFull,
                    BlockingTimeout,
                    LXDConnectionError,
                    LXDOperationError,
                ):
                    logger.warning(
                        "Could not validate resources for %s, proceeding with the update anyway",
                        host,
                    )

            if has_real_limits and available is not None:
                try:
                    _refuse_cap_above_hardware(
                        available,
                        max_cpu=(
                            request.max_cpu
                            if request.max_cpu and request.max_cpu > 0
                            else None
                        ),
                        ram_bytes=(
                            _parse_size_to_bytes(request.max_ram)
                            if request.max_ram and request.max_ram != "0"
                            else None
                        ),
                        storage_bytes=(
                            _parse_size_to_bytes(request.max_storage)
                            if request.max_storage and request.max_storage != "0"
                            else None
                        ),
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc

            try:
                await assert_project_not_migration_fenced(
                    cluster_id=host,
                    project=project_name,
                    db=db,
                )
            except MigrationFenceConflict as exc:
                raise HTTPException(status_code=409, detail=exc.detail) from exc

            project_data, request_cancelled = await _finish_critical_write(
                lxd_service.update_project(
                    host=host,
                    project_name=project_name,
                    description=request.description,
                    max_cpu=request.max_cpu,
                    max_ram=request.max_ram,
                    max_storage=request.max_storage,
                )
            )
            if request_cancelled:
                raise asyncio.CancelledError
        
        # Limits change both the list projection and the create form's quota line.
        _invalidate_project_caches()
        logger.debug(f"Invalidated project cache after updating '{project_name}' on {host}")
        
        project = ProjectInfo(**project_data)
        
        return ProjectUpdateResponse(
            success=True,
            message=f"Project '{project_name}' updated successfully in cluster '{host}'",
            project=project,
        )
        
    except HTTPException:
        raise
    except InstanceSecurityPolicyViolation as exc:
        raise _policy_refusal(exc) from exc
    except LXDConnectionError as e:
        logger.warning(f"LXD connection error: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationUnknownError as e:
        logger.warning("Project update outcome is unknown for %s on %s", project_name, host)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "project_write_unknown",
                "message": (
                    "The daemon outcome could not be confirmed. Check the exact project "
                    "state before retrying."
                ),
            },
        ) from e
    except LXDOperationError as e:
        logger.error(f"LXD operation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error updating project: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete(
    "/{host}/{project_name}",
    summary="Delete a project",
    description="Delete a project from a specific cluster.",
    responses={
        200: {"description": "Project deleted successfully"},
        400: {"model": ErrorResponse, "description": "Invalid request"},
        503: {"model": ErrorResponse, "description": "LXD connection error"},
        500: {"model": ErrorResponse, "description": "Server error"},
    }
)
async def delete_project(
    host: str,
    project_name: str,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Delete a project from a specific LXD host."""
    verify_cluster_access(host)
    _refuse_a_project_outside_the_bound(host, project_name)
    try:
        async with serialize_cluster_configuration(db, host):
            try:
                await assert_project_not_migration_fenced(
                    cluster_id=host,
                    project=project_name,
                    db=db,
                )
            except MigrationFenceConflict as exc:
                raise HTTPException(status_code=409, detail=exc.detail) from exc

            async def delete_and_revoke_grants() -> str:
                await lxd_service.delete_project(host, project_name)
                return await project_grants.forget_deleted_project(
                    db,
                    cluster_id=host,
                    project_name=project_name,
                )

            revoke_note, request_cancelled = await _finish_critical_write(
                delete_and_revoke_grants()
            )
            if request_cancelled:
                raise asyncio.CancelledError

        # Announce the lifecycle change to any subscribed add-on. Nothing emitted these events
        # until now, so the catalogue was a promise with no sender behind it.
        from app.services.webhooks.emitter import emit_resource_event

        await emit_resource_event(
            event_type="orcastra.project.deleted.v1",
            cluster_id=host,
            resource_type="project",
            resource_name=project_name,
        )

        
        # Clear same-name quota data as well, so a later recreate cannot inherit it.
        _invalidate_project_caches()
        logger.debug(f"Invalidated project cache after deleting '{project_name}' from {host}")
        
        # Log activity for audit trail
        activity_logger.log_activity(
            action="project.delete",
            target_type="project",
            target_id=project_name,
            target_host=host,
            result="success",
        )
        
        schedule_orcahub_event("project.deleted", host, project_name, project_name, resource_type="PROJECT")
        return {
            "success": True,
            "message": (
                f"Project '{project_name}' deleted successfully from cluster '{host}'"
                + revoke_note
            )
        }

    except HTTPException:
        raise
    except LXDConnectionError as e:
        logger.warning(f"LXD connection error: {e}")
        activity_logger.log_activity(
            action="project.delete",
            target_type="project",
            target_id=project_name,
            target_host=host,
            result="failure",
            error_code="LXDConnectionError",
            error_message=str(e),
        )
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        logger.error(f"LXD operation error: {e}")
        activity_logger.log_activity(
            action="project.delete",
            target_type="project",
            target_id=project_name,
            target_host=host,
            result="failure",
            error_code="LXDOperationError",
            error_message=str(e),
        )
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error deleting project: {e}")
        raise HTTPException(status_code=500, detail=str(e))
