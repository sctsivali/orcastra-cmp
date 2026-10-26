"""
API Router for Storage Volume operations.
Provides endpoints for managing LXD storage volumes.
"""

from fastapi import APIRouter, HTTPException, Depends, Path, Query, Body
from typing import Literal, Optional, List, Dict
import logging
from datetime import datetime, timezone
import asyncio
from concurrent.futures import ThreadPoolExecutor

from pylxd.exceptions import LXDAPIException

from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.fanout import NodeRead, collect, unattempted
from app.services.lxd.project_scope import (
    ALL_PROJECTS_READ_TIMEOUT,
    DEFAULT_PROJECT,
    list_pool_volumes,
    resolve_owner,
)
from app.models.schemas import FanoutReachability
from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
    require_client,
)
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.core.host_detail import volume_for_tier, volume_list_for_tier
from app.core.task_scope import caller_tier
from app.services.orcahub_webhook import schedule_orcahub_event
from app.core.org_scope import (
    org_scope_dependency,
    verify_cluster_access,
    project_is_visible,
)
from urllib.parse import unquote
from app.core.lxd_errors import lxd_http_error
from app.core.lxd_names import (
    LXD_NAME_PATTERN,
    LXD_VOLUME_NAME_DESCRIPTION,
    LXD_VOLUME_NAME_PATTERN,
)
from app.core.tenant_visibility import (
    filter_visible_lxd_references,
    parse_lxd_reference,
    tenant_acl_active,
)
from app.core.cache import get_cache_manager, get_resource_list_generation
from pydantic import BaseModel, Field
from app.services.activity_logger import get_activity_logger
from app.core.lxd_path import safe_segment
from app.services.lxd.lazy_client import client_for
from app.core.database import get_db
from app.core import swr
from app.core.swr import Built
from app.services.shared_cluster_audit import serialize_cluster_configuration
from app.services.migration_fence import (
    MigrationFenceConflict,
    assert_project_not_migration_fenced,
)
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

# A body naming a known-down cluster is right today and may be wrong once the healer
# reconnects it, so it is remembered for one healer cadence rather than three minutes.
DEGRADED_CACHE_TTL = 60

router = APIRouter(
    prefix="/storage/volumes",
    tags=["Storage Volumes"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# Thread pool for parallel operations
_executor = ThreadPoolExecutor(max_workers=5)


# The volume types LXD itself defines. A closed vocabulary rather than a pattern, because the set is
# finite and known: `core/tenant_visibility.py` already owns two of these to tell an instance's own
# root disk from a custom volume.
#
# important: this is a REQUEST-BOUNDARY guard, not decoration. `volume_type`, `volume_name` and
# `pool` are interpolated into an LXD request path by pylxd, which builds URLs by string
# concatenation and escapes nothing, so each one is a path segment rather than data. A `?` ends the
# path and moves the request to a different resource, and `requests` collapses `..` before sending,
# so a caller controls the path STRUCTURE and not merely one segment. `pool` is the widest of the
# three: a path segment cannot carry `/`, but a query parameter carries it verbatim.
#
# core/lxd_names.py:1-22 records the rule this restores: refuse the name at the boundary, before any
# client is built, any cluster is contacted, or any authorization decision is made.
VolumeType = Literal["custom", "container", "virtual-machine", "image"]


class StorageVolumeInfo(BaseModel):
    """Storage volume information."""
    name: str = Field(..., description="Volume name")
    type: str = Field(..., description="Volume type")
    pool: str = Field(..., description="Storage pool name")
    description: str = Field("", description="Volume description")
    config: Dict[str, str] = Field(default_factory=dict, description="Volume configuration")
    used_by: List[str] = Field(default_factory=list, description="Resources using this volume")
    node: str = Field("", description="Cluster where the volume is defined")
    content_type: str = Field("", description="Content type")
    project: str = Field(DEFAULT_PROJECT, description="Project that holds the volume")
    location: Optional[str] = Field(
        "",
        description="Cluster member that holds the volume; empty for a shared pool or a single host, null for a Tenant",
    )


class StorageVolumeCreateRequest(BaseModel):
    """Request body for creating a storage volume."""
    name: str = Field(
        ..., pattern=LXD_VOLUME_NAME_PATTERN, description=f"Volume name. {LXD_VOLUME_NAME_DESCRIPTION}"
    )
    pool: str = Field(
        ..., pattern=LXD_VOLUME_NAME_PATTERN, description=f"Storage pool. {LXD_VOLUME_NAME_DESCRIPTION}"
    )
    type: VolumeType = Field("custom", description="Volume type")
    description: str = Field("", description="Volume description")
    config: Dict[str, str] = Field(default_factory=dict, description="Volume configuration")
    content_type: str = Field("filesystem", description="Content type")


class StorageVolumeListResponse(FanoutReachability):
    """Response for listing storage volumes."""
    total: int = Field(..., description="Total number of volumes")
    volumes: List[StorageVolumeInfo] = Field(..., description="List of storage volumes")
    nodes: List[str] = Field(..., description="Clusters included in this response")


def _volume_node(client, pool: str, volume_type: str, volume_name: str):
    """The raw API node for one volume, so a project or a member can be named on the call."""
    return client.api["storage-pools"][safe_segment(pool)]["volumes"][safe_segment(volume_type)][safe_segment(volume_name)]


def _address_params(project: Optional[str], location: Optional[str]) -> Dict[str, str]:
    """Query parameters that address a volume outside the default project or on one member.

    note: the list now carries `project` and `location` on every row (the volumes of a
    per-member pool exist once per member, and other projects hold their own). A detail,
    update or delete that names only (pool, type, name) would land on the default project's
    copy, so callers pass the row's own values back.
    """
    params: Dict[str, str] = {}
    if project and project != DEFAULT_PROJECT:
        params["project"] = project
    if location:
        params["target"] = location
    return params


def _pool_names(client, pool_filter: Optional[str]) -> List[str]:
    """The pools to read, from one recursion=0 listing (or the filter alone)."""
    if pool_filter:
        return [pool_filter]
    response = client.api.storage_pools.get(
        params={"recursion": 0}, timeout=ALL_PROJECTS_READ_TIMEOUT
    )
    names: List[str] = []
    for entry in response.json().get("metadata") or []:
        if isinstance(entry, str):
            # the URL carries the name percent-escaped; safe_segment escapes it again later
            names.append(unquote(entry.rstrip("/").rsplit("/", 1)[-1].split("?")[0]))
        elif isinstance(entry, dict) and entry.get("name"):
            names.append(entry["name"])
    return names


def _fetch_volumes_for_node(
    lxd_service: LXDService, node_name: str, pool_filter: Optional[str]
) -> NodeRead[StorageVolumeInfo]:
    """Every volume on one cluster, one request per pool. Runs in the thread pool.

    important: pylxd's collection manager listed at recursion 0 in the default project and
    then synced every volume one request at a time. On a clustered host a per-member pool
    lists its volumes as `.../volumes/custom/backups?target=member-a`, which the sync
    could not read, and the first refusal dropped the rest of that pool. Nothing is synced
    now, every project is read, and a pool that cannot be read is named, not skipped.
    """
    client = lxd_service.connection_manager.get_client(node_name)
    if client is None:
        # important: unreachable is not empty. See the sibling routers.
        raise LXDConnectionError(f"Could not connect to {node_name}")
    read: NodeRead[StorageVolumeInfo] = NodeRead()
    for pool_name in _pool_names(client, pool_filter):
        try:
            rows, complete = list_pool_volumes(client, pool_name)
        except LXDAPIException as exc:
            # note: only LXD's own refusal is a per-pool problem; a transport error means the
            # rest of the cluster cannot be read either and is classified by the caller.
            read.lost(f"pool {pool_name}: LXD answered {getattr(exc.response, 'status_code', '?')}: "
                      f"{' '.join(str(exc).split())[:200]}")
            continue
        if not complete:
            read.lost(f"pool {pool_name}: not every project could be read")
        for row in rows:
            try:
                read.rows.append(StorageVolumeInfo(
                    name=row.get("name") or "",
                    type=row.get("type") or "",
                    pool=pool_name,
                    description=row.get("description") or "",
                    config=row.get("config") or {},
                    used_by=row.get("used_by") or [],
                    content_type=row.get("content_type") or "",
                    node=node_name,
                    project=row.get("project") or DEFAULT_PROJECT,
                    location=row.get("location") or "",
                ))
            except Exception as exc:  # noqa: BLE001 - one malformed row is named, not fatal
                read.lost(f"volume {row.get('name', '?')} in pool {pool_name}: {type(exc).__name__}")
    return read


def _invalidate_volume_lists() -> None:
    """A volume write changes the volume list and the pool rows that count and list it."""
    cache = get_cache_manager()
    # one family per call: some cache stand-ins accept a single kind
    for kind in ("storage_volumes", "storage_pools", "storage_pool_resources"):
        cache.invalidate_resource_cache(kind)


@router.get(
    "",
    response_model=StorageVolumeListResponse,
    summary="List all storage volumes",
    description="Get a list of all storage volumes from connected clusters.",
)
async def list_storage_volumes(
    host: Optional[str] = Query(
        None,
        pattern=LXD_NAME_PATTERN,
        description="Filter by a specific cluster",
    ),
    pool: Optional[str] = Query(None, pattern=LXD_VOLUME_NAME_PATTERN, description="Filter by storage pool"),
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """List all storage volumes."""
    if host:
        verify_cluster_access(host)
    # 180 s soft TTL (60 s when a cluster is down), then served for SWR_MAX_STALE_SECONDS while
    # one background read replaces it.
    cache = get_cache_manager()

    async def build() -> Built:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        
        # Fetch volumes from all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_executor, _fetch_volumes_for_node, lxd_service, node_name, pool)
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # important: two different things make a node absent, and both must be named.
        # A node can fail during the fan-out, or never be asked at all, because
        # connection_manager.get_node_names() returns only nodes with a live client while
        # the fleet is whatever Vault has registered. Reporting only the first kind left a
        # dead cluster silently missing rather than merely empty.
        seeded: List[str] = (
            [] if host else sorted(set(lxd_service.get_node_names()) - set(nodes_to_query))
        )
        unreachable_nodes: List[str] = list(seeded)
        fan = collect(
            nodes_to_query, results, noun="storage volumes", reasons_visible=not tenant_acl_active()
        )
        unreachable_nodes.extend(fan.unreachable_nodes)
        # important: the rows now span every project, and a Partner is scoped by cluster, not
        # by project. On a cluster bound to two organizations this is the only thing keeping
        # one organization's volumes out of the other's console. The cache key already
        # carries the bound (get_scope_cache_key), so the cached body is per scope.
        all_volumes = [v for v in fan.rows if project_is_visible(v.node, v.project)]
        # important: the seeding names two kinds of cluster, and only one of them may be
        # remembered. A cluster this worker tried and found down is a correct answer, cached
        # as it always was (a shorter TTL, so recovery shows within the healer's cadence). A
        # cluster this worker has never tried, registered seconds ago, must not be: the cache
        # is shared by every worker, and the one that did connect it would then serve
        # "unreachable" for the whole TTL. See fanout.unattempted.
        cacheable = fan.cacheable_despite_outages and not unattempted(
            seeded, lxd_service.connection_manager
        )
        ttl = DEGRADED_CACHE_TTL if unreachable_nodes else 180

        # used_by is held to the caller's bound for every role; only a tenant loses the row
        # when nothing they may see uses it.
        for volume in all_volumes:
            volume.used_by = filter_visible_lxd_references(volume.node, volume.used_by)

        if tenant_acl_active():
            all_volumes = [volume for volume in all_volumes if volume.used_by]
        
        # Store in variable first
        response = StorageVolumeListResponse(
            total=len(all_volumes),
            volumes=all_volumes,
            nodes=lxd_service.connection_manager.get_node_names(),
            unreachable_nodes=unreachable_nodes,
            partial_nodes=fan.partial_nodes,
            node_errors=fan.node_errors,
            collected_at=datetime.now(timezone.utc),
        )
        
        # important: an outage must not be remembered as the answer, and neither may a
        # partial one: a cluster that failed its listing was cached as "no volumes" for
        # 180 seconds. See instances.py.
        if not cacheable:
            logger.warning("Not caching storage volumes list: %s", fan.node_errors or "no node answered")
        return Built(value=response, body=response.model_dump(), fresh_for=ttl if cacheable else None)

    try:
        served = await swr.serve(
            cache=cache,
            base_key=f"storage_volumes:list:e1:host={host}:pool={pool}",
            fresh=fresh,
            build=build,
            generation=lambda: get_resource_list_generation(cache, "storage_volumes"),
            label="storage volumes list",
        )
        return volume_list_for_tier(served.as_model(StorageVolumeListResponse), caller_tier())
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing storage volumes: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{volume_type}/{volume_name}",
    response_model=StorageVolumeInfo,
    summary="Get storage volume details",
    description="Get detailed information about a specific storage volume.",
)
async def get_storage_volume(
    volume_type: VolumeType,
    volume_name: str = Path(
        ..., pattern=LXD_VOLUME_NAME_PATTERN, description=LXD_VOLUME_NAME_DESCRIPTION
    ),
    host: str = Query(..., pattern=LXD_NAME_PATTERN, description="Target cluster"),
    pool: str = Query(..., pattern=LXD_VOLUME_NAME_PATTERN, description="Storage pool name"),
    project: Optional[str] = Query(
        None, pattern=LXD_NAME_PATTERN, description="Project that holds the volume (the row's project)"
    ),
    location: Optional[str] = Query(
        None, pattern=LXD_NAME_PATTERN, description="Cluster member that holds it (the row's location)"
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific storage volume."""
    verify_cluster_access(host)
    if not project_is_visible(host, project or DEFAULT_PROJECT):
        raise HTTPException(status_code=403, detail="Access denied to this project")
    if caller_tier() == "tenant":
        # important: a per-member pool answers only from the member holding the volume, so a
        # Tenant's ?location= would turn 200 against 404 into a way to find the member. An
        # untargeted read reaches the holder when the name is unique.
        location = None
    try:
        client = require_client(await client_for(lxd_service, host), host)
        row = _volume_node(client, pool, volume_type, volume_name).get(
            params=_address_params(project, location) or None
        ).json().get("metadata") or {}
        used_by = filter_visible_lxd_references(host, row.get("used_by") or [])
        if tenant_acl_active() and not used_by:
            raise HTTPException(status_code=403, detail="Access denied to this storage volume")
        
        loc = row.get("location") or ""
        return volume_for_tier(StorageVolumeInfo(
            name=row.get("name") or volume_name,
            type=row.get("type") or volume_type,
            pool=pool,
            description=row.get("description") or "",
            config=row.get("config") or {},
            used_by=used_by,
            content_type=row.get("content_type") or "",
            node=host,
            project=row.get("project") or project or DEFAULT_PROJECT,
            location="" if loc == "none" else loc,
        ), caller_tier())
    except HTTPException:
        # broad handler below converts it into a 500
        # important: let require_client's 503 through. Without this the
        # so an offline cluster does not read as a server fault.
        raise
    except Exception as e:
        logger.error(f"Error getting storage volume {volume_name}: {e}")
        raise HTTPException(status_code=404, detail=f"Storage volume {volume_name} not found")


@router.post(
    "",
    response_model=dict,
    summary="Create storage volume",
    description="Create a new storage volume.",
)
async def create_storage_volume(
    request: StorageVolumeCreateRequest,
    host: str = Query(..., pattern=LXD_NAME_PATTERN, description="Target cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Create a new storage volume."""
    verify_cluster_access(host)
    if not project_is_visible(host, DEFAULT_PROJECT):
        raise HTTPException(status_code=403, detail="Access denied to this project")
    try:
        async with serialize_cluster_configuration(db, host):
            try:
                await assert_project_not_migration_fenced(
                    cluster_id=host,
                    project=DEFAULT_PROJECT,
                    db=db,
                )
            except MigrationFenceConflict as exc:
                raise HTTPException(status_code=409, detail=exc.detail) from exc
            client = require_client(await client_for(lxd_service, host), host)
            storage_pool = client.storage_pools.get(safe_segment(request.pool))

            volume = storage_pool.volumes.create({
                "name": request.name,
                "type": request.type,
                "config": request.config,
                "description": request.description,
                "content_type": request.content_type
            })
        
        # Announce the lifecycle change to any subscribed add-on. Volumes have no operation in
        # the integrations proxy vocabulary, so this router is their only source: without it two
        # of the nine catalogued events could never fire at all.
        from app.services.webhooks.emitter import emit_resource_event

        await emit_resource_event(
            event_type="orcastra.volume.created.v1",
            cluster_id=host,
            resource_type="volume",
            resource_name=request.name,
        )

        # Log activity: storage volume creation success
        activity_logger.log_activity(
            action="storage.volume.create",
            target_type="storage_volume",
            target_id=request.name,
            target_host=host,
            result="success",
            details={"pool": request.pool, "type": request.type, "content_type": request.content_type}
        )
        
        schedule_orcahub_event("volume.created", host, getattr(volume, "project", None), request.name, resource_type="VOLUME")
        # Readers hold a cached list; without this the change is invisible until the TTL.
        _invalidate_volume_lists()
        return {
            "success": True,
            "message": f"Storage volume {request.name} created successfully",
            "volume": StorageVolumeInfo(
                name=volume.name,
                type=volume.type,
                pool=request.pool,
                description=getattr(volume, 'description', '') or "",
                config=volume.config or {},
                used_by=volume.used_by or [],
                content_type=getattr(volume, 'content_type', ''),
                node=host
            )
        }
    except HTTPException:
        # broad handler below converts it into a 500
        # important: let require_client's 503 through. Without this the
        # so an offline cluster does not read as a server fault.
        raise
    except Exception as e:
        logger.error(f"Error creating storage volume: {e}")
        # Log activity: storage volume creation failure
        activity_logger.log_activity(
            action="storage.volume.create",
            target_type="storage_volume",
            target_id=request.name,
            target_host=host,
            result="failure",
            error_message=str(e)
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.put(
    "/{volume_type}/{volume_name}",
    response_model=dict,
    summary="Update storage volume",
    description="Update an existing storage volume.",
)
async def update_storage_volume(
    volume_type: VolumeType,
    volume_name: str = Path(
        ..., pattern=LXD_VOLUME_NAME_PATTERN, description=LXD_VOLUME_NAME_DESCRIPTION
    ),
    host: str = Query(..., pattern=LXD_NAME_PATTERN, description="Target cluster"),
    pool: str = Query(..., pattern=LXD_VOLUME_NAME_PATTERN, description="Storage pool name"),
    config: Optional[Dict[str, str]] = Body(None),
    description: Optional[str] = Body(None),
    project: Optional[str] = Query(
        None, pattern=LXD_NAME_PATTERN, description="Project that holds the volume (the row's project)"
    ),
    location: Optional[str] = Query(
        None, pattern=LXD_NAME_PATTERN, description="Cluster member that holds it (the row's location)"
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Update a storage volume."""
    verify_cluster_access(host)
    requested_project = project or DEFAULT_PROJECT
    if not project_is_visible(host, requested_project):
        raise HTTPException(status_code=403, detail="Access denied to this project")
    try:
        async with serialize_cluster_configuration(db, host):
            client = require_client(await client_for(lxd_service, host), host)
            owner = resolve_owner(client, "storage-volumes", project)
            if not project_is_visible(host, owner):
                raise HTTPException(status_code=403, detail="Access denied to this project")
            for fenced_project in sorted({requested_project, owner}):
                try:
                    await assert_project_not_migration_fenced(
                        cluster_id=host,
                        project=fenced_project,
                        db=db,
                    )
                except MigrationFenceConflict as exc:
                    raise HTTPException(status_code=409, detail=exc.detail) from exc
            node = _volume_node(client, pool, volume_type, volume_name)
            params = _address_params(owner, location) or None
            current = node.get(params=params).json().get("metadata") or {}
            body: Dict[str, object] = {}
            if config is not None:
                body["config"] = config
            elif current.get("config") is not None:
                body["config"] = current.get("config")
            if description is not None:
                body["description"] = description
            elif current.get("description") is not None:
                body["description"] = current.get("description")
            # PATCH would merge; the previous pylxd save() sent the whole object, which is PUT
            node.put(json=body, params=params)
        
        # Readers hold a cached list; without this the change is invisible until the TTL.
        _invalidate_volume_lists()
        return {
            "success": True,
            "message": f"Storage volume {volume_name} updated successfully"
        }
    except HTTPException:
        # broad handler below converts it into a 500
        # important: let require_client's 503 through. Without this the
        # so an offline cluster does not read as a server fault.
        raise
    except Exception as e:
        logger.error(f"Error updating storage volume {volume_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# How many attachments the 409 names before it stops listing and starts counting.
_ATTACHMENTS_NAMED_IN_ERROR = 5


def _in_use_detail(volume_name: str, references: List[str]) -> str:
    """Name what is holding a volume, from its raw LXD used_by references.

    The references are API paths, ex; /1.0/instances/web-01?project=acme. Showing those to an
    operator is showing them the wire, so they are resolved through the same parser the tenant
    visibility filter uses rather than a second regex written here.
    """
    names = []
    for reference in references:
        _project, instance = parse_lxd_reference(reference)
        names.append(instance or reference)
    shown = ", ".join(names[:_ATTACHMENTS_NAMED_IN_ERROR])
    rest = len(names) - _ATTACHMENTS_NAMED_IN_ERROR
    if rest > 0:
        shown = f"{shown} and {rest} more"
    return (
        f"Storage volume '{volume_name}' is attached to {len(names)} instance(s): {shown}. "
        "Detach it from each before deleting."
    )


@router.delete(
    "/{volume_type}/{volume_name}",
    response_model=dict,
    summary="Delete storage volume",
    description=(
        "Delete a storage volume. Refuses with 409 when the volume is still attached to an "
        "instance; there is no force, because LXD cannot remove an attached volume."
    ),
    responses={
        200: {"description": "Volume deleted"},
        403: {"description": "Cluster out of scope"},
        404: {"description": "No such volume on that cluster"},
        409: {"description": "Volume still attached to an instance"},
        503: {"description": "Cluster unreachable"},
    },
)
async def delete_storage_volume(
    volume_type: VolumeType,
    volume_name: str = Path(
        ..., pattern=LXD_VOLUME_NAME_PATTERN, description=LXD_VOLUME_NAME_DESCRIPTION
    ),
    host: str = Query(..., pattern=LXD_NAME_PATTERN, description="Target cluster"),
    pool: str = Query(..., pattern=LXD_VOLUME_NAME_PATTERN, description="Storage pool name"),
    project: Optional[str] = Query(
        None, pattern=LXD_NAME_PATTERN, description="Project that holds the volume (the row's project)"
    ),
    location: Optional[str] = Query(
        None, pattern=LXD_NAME_PATTERN, description="Cluster member that holds it (the row's location)"
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Delete a storage volume."""
    verify_cluster_access(host)
    requested_project = project or DEFAULT_PROJECT
    if not project_is_visible(host, requested_project):
        raise HTTPException(status_code=403, detail="Access denied to this project")
    try:
        async with serialize_cluster_configuration(db, host):
            client = require_client(await client_for(lxd_service, host), host)
            owner = resolve_owner(client, "storage-volumes", project)
            if not project_is_visible(host, owner):
                raise HTTPException(status_code=403, detail="Access denied to this project")
            for fenced_project in sorted({requested_project, owner}):
                try:
                    await assert_project_not_migration_fenced(
                        cluster_id=host,
                        project=fenced_project,
                        db=db,
                    )
                except MigrationFenceConflict as exc:
                    raise HTTPException(status_code=409, detail=exc.detail) from exc
            node = _volume_node(client, pool, volume_type, volume_name)
            params = _address_params(owner, location) or None

            # important: refuse before asking the daemon. LXD rejects an attached volume anyway,
            # but its message arrived here as a bare Exception and left as a 500, so an operator
            # error read as a server fault and the fix it needs was never stated. Unlike an image,
            # there is no force: an attached volume genuinely cannot be removed.
            current = node.get(params=params).json().get("metadata") or {}
            attached = list(current.get("used_by") or [])
            if attached:
                raise HTTPException(status_code=409, detail=_in_use_detail(volume_name, attached))

            # important: on a clustered host a per-member pool holds one volume of this name per
            # member. Without a target LXD refuses the ambiguity; the caller passes the row's
            # location, and this never guesses a member.
            node.delete(params=params)

        # Announce the lifecycle change to any subscribed add-on. Volumes have no operation in
        # the integrations proxy vocabulary, so this router is their only source: without it two
        # of the nine catalogued events could never fire at all.
        from app.services.webhooks.emitter import emit_resource_event

        await emit_resource_event(
            event_type="orcastra.volume.deleted.v1",
            cluster_id=host,
            resource_type="volume",
            resource_name=volume_name,
        )

        # Log activity: storage volume deletion success
        activity_logger.log_activity(
            action="storage.volume.delete",
            target_type="storage_volume",
            target_id=volume_name,
            target_host=host,
            result="success",
            details={"pool": pool, "type": volume_type}
        )
        
        schedule_orcahub_event(
            "volume.deleted",
            host,
            owner,
            volume_name,
            resource_type="VOLUME",
        )
        # Readers hold a cached list; without this the change is invisible until the TTL.
        _invalidate_volume_lists()
        return {
            "success": True,
            "message": f"Storage volume {volume_name} deleted successfully"
        }
    except HTTPException:
        # broad handler below converts it into a 500
        # important: let require_client's 503 and the 409 above through. Without this the
        # so an offline cluster does not read as a server fault.
        raise
    except Exception as e:
        logger.error(f"Error deleting storage volume {volume_name}: {e}")
        # Log activity: storage volume deletion failure
        activity_logger.log_activity(
            action="storage.volume.delete",
            target_type="storage_volume",
            target_id=volume_name,
            target_host=host,
            result="failure",
            error_message=str(e)
        )
        # note: was a blanket 500. The pre-check above catches the common in-use case, but the
        # daemon still owns the race, and a name it cannot resolve is a 404 rather than a fault.
        raise lxd_http_error(e, "Storage volume", volume_name)
