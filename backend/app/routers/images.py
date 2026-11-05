"""
API Router for LXD image operations.
Provides endpoints for listing available images on each node.
"""

from fastapi import APIRouter, HTTPException, Depends, Path, Query
from typing import Any, Dict, List, NamedTuple, Optional, Set
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.models.schemas import (
    ImageInfo,
    ImageListResponse,
    RemoteImageInfo,
    RemoteImageListResponse,
    ErrorResponse,
)
from app.services.lxd_service import (
    LXDService,
    LXDConnectionError,
    LXDOperationError,
    get_lxd_service,
)
from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.lxd_errors import lxd_http_error
from app.core.single_flight import single_flight
from app.core.cache import get_cache_manager, get_resource_list_generation
from app.core.dependencies import (
    AuthenticatedUser,
    check_read_only_mode,
    require_partner,
    require_tenant,
)
from app.core.org_scope import (
    check_tenant_permission,
    get_scope_cache_key,
    org_scope_dependency,
    project_is_visible,
    refuse_default_operation,
    verify_cluster_access,
)
from app.core.tenant_visibility import tenant_acl_active, tenant_may_see_project
from app.services.activity_logger import get_activity_logger
from app.services.lxd.fanout import failure_text
from app.services.remote_image_catalog import _fetch_remote_catalog, _REMOTE_FETCH_TIMEOUT
from app.services.migration_fence import (
    MigrationFenceConflict,
    assert_project_not_migration_fenced,
)
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/images",
    tags=["Images"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

# Thread pool for parallel operations
_executor = ThreadPoolExecutor(max_workers=5)

# A delete waits on a real LXD operation that has to unlink the image's storage volume, so it
# is slower than a read but nowhere near an instance create.
IMAGE_DELETE_TIMEOUT = 60.0
IMAGE_DELETE_MAX_IN_FLIGHT = 5
# One node's instance list, every project. The dashboard's preflight waits 8 s on it, so the
# read has to be that cheap on a healthy node; a node that is not answering trips the breaker
# well before this.
IMAGE_USAGE_TIMEOUT = 20.0
# How many dependent instances a 409 names before it stops listing and starts counting. Long
# enough to act on, short enough that the message stays readable in a toast.
DEPENDENTS_NAMED_IN_ERROR = 5


# The project whose image store a caller asks about. A create in that project can only use
# what its store holds, so the create form reads with it; the Images page reads every store.
# note: the same shape as PROJECT_NAME_RE, spelled with anchors pydantic's regex engine accepts.
_PROJECT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$"
PROJECT_QUERY = Query(
    "default",
    pattern=_PROJECT_PATTERN,
    description="The project whose image store to read: its own when it isolates images, else default",
)
PROJECT_QUERY_OPTIONAL = Query(
    None,
    pattern=_PROJECT_PATTERN,
    description="Restrict to the store one project reads; omit to list every store in the cluster",
)


async def _fetch_images_for_node(
    lxd_service: LXDService, node_name: str, project: Optional[str] = None
) -> ImageListResponse:
    """Fetch images for a single node, from every store or from the one `project` reads."""
    try:
        listing = await lxd_service.list_images(node_name, project)
        # important: the Partner project bound, per row. An all-projects read on a cluster two
        # organisations share carries the other organisation's stores.
        images = [i for i in listing.images if project_is_visible(node_name, i.project)]
        return ImageListResponse(
            host=node_name,
            images=images,
            total=len(images),
            project=listing.project,
            complete=listing.complete,
        )
    except LXDOperationError as e:
        # The cluster answered and refused: an unknown project, a store it cannot read. That is
        # not an outage and must not be labelled as one.
        logger.warning(f"Image read on {node_name} refused: {e}")
        return ImageListResponse(
            host=node_name,
            images=[],
            total=0,
            reachable=True,
            error=failure_text(node_name, e),
            project=project,
        )
    except Exception as e:
        # important: this except, not the gather below, is the path a dead cluster takes.
        # The connection breaker short-circuits before any network call, list_images
        # raises, and this returns a perfectly valid empty response. Labelling it is the
        # whole point: an unlabelled empty host is indistinguishable from a host with no
        # images, which is what made the create form say "no images available" for a
        # cluster that was simply down.
        logger.warning(f"Could not list images from {node_name}: {e}")
        return ImageListResponse(
            host=node_name,
            images=[],
            total=0,
            reachable=False,
            error=failure_text(node_name, e),
        )



async def _allowed_image_fingerprints_by_host(
    lxd_service: LXDService,
) -> Optional[Dict[str, Set[str]]]:
    """Return image fingerprints used by tenant-visible instances."""
    if not tenant_acl_active():
        return None

    all_vms, _ = await lxd_service.list_all_vms()
    allowed: Dict[str, Set[str]] = {}
    for vm in all_vms:
        if not check_tenant_permission(vm.host, vm.project or "default", vm.name, "view"):
            continue
        config = vm.config or {}
        fingerprint = config.get("volatile.base_image") or config.get("image.fingerprint")
        if fingerprint:
            allowed.setdefault(vm.host, set()).add(fingerprint)
    return allowed


def _filter_images_for_tenant(
    response: ImageListResponse,
    allowed: Optional[Dict[str, Set[str]]],
) -> ImageListResponse:
    if allowed is None:
        return response
    host_fingerprints = allowed.get(response.host, set())
    images = [
        image for image in response.images
        # Ownership as well as usage: a tenant sees an image only from a store it may see.
        if tenant_may_see_project(response.host, image.project)
        and any(
            image.fingerprint.startswith(fingerprint) or fingerprint.startswith(image.fingerprint)
            for fingerprint in host_fingerprints
        )
    ]
    # important: carry reachability through the tenant filter. Rebuilding the model
    # without it would drop the label for scoped users only, so admins would see an
    # unreachable host correctly and tenants would see it as empty.
    return ImageListResponse(
        host=response.host,
        images=images,
        total=len(images),
        reachable=response.reachable,
        error=response.error,
        project=response.project,
        complete=response.complete,
    )


@router.get(
    "",
    response_model=List[ImageListResponse],
    summary="List images from all clusters",
    description="Get a list of available images from all connected clusters.",
    responses={
        200: {"description": "Images listed successfully"},
        500: {"model": ErrorResponse, "description": "Server error"},
    }
)
@single_flight("images:list")
async def list_all_images(
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
    project: Optional[str] = PROJECT_QUERY_OPTIONAL,
):
    """List images from every LXD node: every store, or the store `project` reads."""
    # Try cache first (60 second cache)
    cache = get_cache_manager()
    cache_key = get_scope_cache_key(f"images:list:all:project={project or '*'}")
    cached_data = None if fresh else cache.get("api_responses", cache_key)
    if cached_data is not None:
        logger.debug("Returning cached images list")
        # Reconstruct Pydantic models from cached dict
        return [ImageListResponse(**item) for item in cached_data]
    
    list_generation = get_resource_list_generation(cache, "images")

    # Fetch images from all nodes in parallel
    node_names = lxd_service.get_node_names()
    allowed_images = await _allowed_image_fingerprints_by_host(lxd_service)
    tasks = [_fetch_images_for_node(lxd_service, node_name, project) for node_name in node_names]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Filter out exceptions
    response = []
    answered = 0
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error(f"Error fetching images from node: {result}")
            host = node_names[i] if i < len(node_names) else "unknown"
            response.append(ImageListResponse(
                host=host,
                images=[],
                total=0,
                reachable=False,
                error=failure_text(host, result),
            ))
        else:
            # important: count hosts that actually answered, not calls that returned.
            # _fetch_images_for_node catches its own failures, so every call "returns"
            # and an earlier version of this counter never reached zero, which made the
            # do-not-cache-an-outage guard below structurally present and inert.
            if result.reachable:
                answered += 1
            response.append(_filter_images_for_tenant(result, allowed_images))

    # important: a node that failed now carries reachable=False, so a caller can tell it
    # apart from a node with no images. Caching a fleet where nothing answered would
    # still pin an outage for 180s, and a forced refresh during one would replace a good
    # catalog with it, so that case is still not remembered.
    # note: a partial failure is still cached, so one dead cluster does not make every
    # request pay the full fan-out. The cached entry carries that host's reachable=False,
    # so it reads as unreachable rather than empty for the life of the TTL.
    cache_data = [item.model_dump() for item in response]
    if answered:
        if get_resource_list_generation(cache, "images") == list_generation:
            cache.set("api_responses", cache_key, cache_data, ttl=180)
            logger.debug("Cached images list for 180 seconds")
        else:
            logger.info("Not caching images list: an image mutation completed during the read")
    else:
        logger.warning("Not caching images list: no node answered")
    
    return response


@router.get(
    "/remote",
    response_model=RemoteImageListResponse,
    summary="Browse images from the remote image catalogs",
    description="List images aggregated from the configured remote simplestreams "
                "catalogs (images.lxd.canonical.com and cloud-images.ubuntu.com), one "
                "row per distribution/release/variant/architecture/type. Cached 1h. "
                "Selected images are pulled on instance creation.",
    responses={
        200: {"description": "Remote images listed successfully"},
    }
)
async def list_remote_images(fresh: bool = False):
    """Browse the aggregated remote image catalogs (cached 1h)."""
    cache = get_cache_manager()
    cache_key = "images:remote:all"
    images = None if fresh else cache.get("api_responses", cache_key)
    if images is None:
        # Cold cache: each remote has its own 15s/90s urllib timeouts, so bound
        # the whole fetch and fall back to an empty (uncached) result so the
        # picker stays responsive and a later request can retry.
        try:
            images = await asyncio.wait_for(
                asyncio.to_thread(_fetch_remote_catalog),
                timeout=_REMOTE_FETCH_TIMEOUT,
            )
            cache.set("api_responses", cache_key, images, ttl=3600)
        except asyncio.TimeoutError:
            logger.warning(
                "Remote image catalog fetch timed out after %ss; returning empty",
                _REMOTE_FETCH_TIMEOUT,
            )
            images = []

    return RemoteImageListResponse(
        images=[RemoteImageInfo(**img) for img in images],
        total=len(images),
    )


@router.get(
    "/{host}",
    response_model=ImageListResponse,
    summary="List images from a specific cluster",
    description="Get a list of available images from a specific cluster.",
    responses={
        200: {"description": "Images listed successfully"},
        400: {"model": ErrorResponse, "description": "Invalid cluster"},
        500: {"model": ErrorResponse, "description": "Server error"},
    }
)
async def list_node_images(
    host: str,
    lxd_service: LXDService = Depends(get_lxd_service),
    project: Optional[str] = PROJECT_QUERY_OPTIONAL,
    all_projects: bool = Query(
        False,
        description="Every image store in the cluster, each row naming its store; excludes `project`.",
    ),
):
    """The images `project` can use on one LXD node: what its own store holds when it
    isolates images, else the shared default store. `project` on the response names which.
    With `all_projects`, every store the caller may see, rows stamped with theirs."""
    verify_cluster_access(host)
    if all_projects and project:
        raise HTTPException(status_code=422, detail="all_projects excludes project")
    try:
        # Validate host
        valid_nodes = lxd_service.get_node_names()
        if host not in valid_nodes:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid cluster. Available clusters: {valid_nodes}"
            )

        if all_projects:
            listing = await lxd_service.list_images(host, None)
            rows = [i for i in listing.images if project_is_visible(host, i.project)]
        else:
            listing = await lxd_service.list_images(host, project or "default")
            _store_must_be_visible(host, listing.project or "default")
            rows = listing.images
        result = ImageListResponse(
            host=host,
            images=rows,
            total=len(rows),
            project=listing.project,
            complete=listing.complete,
        )
        allowed_images = await _allowed_image_fingerprints_by_host(lxd_service)

        return _filter_images_for_tenant(result, allowed_images)
        
    except HTTPException:
        raise
    except LXDConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error listing images from {host}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _image_fingerprint_of(vm) -> Optional[str]:
    """The fingerprint an instance was built from, or None.

    Same two keys, in the same order, as the tenant filter above. They must not drift: one
    deciding an image is unused while the other decides it is visible is exactly the kind of
    disagreement that makes a delete look safe when it is not.
    """
    config = getattr(vm, "config", None) or {}
    return config.get("volatile.base_image") or config.get("image.fingerprint")


def _store_must_be_visible(host: str, store: str) -> None:
    """Refuse unless the caller may see the image store that actually answers.

    important: the store, not the project the caller named. A project that shares the host's
    default store answers with default-store rows, which a bounded Partner may see even when
    the project itself is outside their bound; refusing on the name would take away the
    catalogue a create in that project can still use. A project that keeps its own store is
    its own store's name, and that is what the bound is checked against.
    """
    if not project_is_visible(host, store):
        raise HTTPException(status_code=403, detail="Access denied to this image store")


async def _resolved_store(lxd_service: LXDService, host: str, project: str) -> str:
    try:
        return await run_blocking(
            lxd_service.image_store_of,
            host,
            project,
            timeout=IMAGE_USAGE_TIMEOUT,
            label=f"image_store_of[{host}/{project}]",
            executor=_executor,
            max_in_flight=IMAGE_DELETE_MAX_IN_FLIGHT,
        )
    except (BlockingTimeout, BlockingPoolFull):
        raise HTTPException(
            status_code=503,
            detail=f"Could not resolve the image store in cluster '{host}'",
        ) from None
    except LXDConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e)) from None


class _Dependents(NamedTuple):
    """Instances built from an image: the names the caller may see, and how many they may not."""
    names: List[str]
    hidden: int

    def __bool__(self) -> bool:
        return bool(self.names) or self.hidden > 0


async def _instances_using_image(
    lxd_service: LXDService, host: str, fingerprint: str, store: str
) -> _Dependents:
    """Instances on `host` created from `fingerprint` out of `store`.

    Only the instances whose project reads `store` count: the same fingerprint can sit in a
    project's own store and in the default store, and deleting one copy does not touch the
    other's instances. `volatile.base_image` is the fingerprint at create time, so an
    instance whose project flipped `features.images` afterwards is attributed to the store
    its project reads today; that under-counts only for a project that switched stores
    after the instance was built.

    The names are held to the caller's project bound: a bounded Partner on a shared cluster
    learns that instances in projects outside the bound exist and how many, never what they
    are called. The refusal still counts them, because the image is their base image
    whoever owns them.

    note: one node, not the fleet. This used to go through ``list_all_vms``, which fans out to
    every cluster the caller may see; on a fleet with a dead member or a large partner cloud
    that took longer than the dashboard's preflight budget, so the confirm dialog fell back to
    its own tenant-filtered count and offered a delete with no dependents to acknowledge. The
    callers have already passed ``verify_cluster_access(host)``, so a single-node read stays
    inside the caller's scope. A node that does not answer is an error, never an empty list:
    a delete must not proceed on the strength of a read that did not happen.
    """
    try:
        vms, connected = await run_blocking(
            lxd_service.instance_manager.fetch_node_instances,
            host,
            timeout=IMAGE_USAGE_TIMEOUT,
            label=f"image_usage[{host}/{fingerprint[:12]}]",
            executor=_executor,
            max_in_flight=IMAGE_DELETE_MAX_IN_FLIGHT,
        )
    except BlockingTimeout:
        raise LXDConnectionError(
            f"Reading the instances on {host} exceeded {IMAGE_USAGE_TIMEOUT:.0f}s"
        ) from None
    except BlockingPoolFull:
        raise LXDConnectionError("Image operations saturated; please retry shortly") from None
    if not connected:
        raise LXDConnectionError(f"Could not read the instances on {host}")
    built: List[Any] = []
    for vm in vms:
        used = _image_fingerprint_of(vm)
        if not used:
            continue
        # LXD stores volatile.base_image as the full digest, but an instance created from a
        # short fingerprint can carry the prefix. Compare both ways, as the tenant filter does.
        if used == fingerprint or used.startswith(fingerprint) or fingerprint.startswith(used):
            built.append(vm)
    if not built:
        return _Dependents([], 0)
    stores = await run_blocking(
        lxd_service.image_stores_of,
        host,
        {getattr(vm, "project", None) or "default" for vm in built},
        timeout=IMAGE_USAGE_TIMEOUT,
        label=f"image_stores_of[{host}]",
        executor=_executor,
        max_in_flight=IMAGE_DELETE_MAX_IN_FLIGHT,
    )
    names = []
    hidden = 0
    for vm in built:
        project = getattr(vm, "project", None) or "default"
        if stores.get(project, "default") != store:
            continue
        name = getattr(vm, "name", "") or ""
        if not name:
            continue
        if project_is_visible(host, project):
            names.append(name)
        else:
            hidden += 1
    return _Dependents(sorted(names), hidden)


def _dependents_detail(fingerprint: str, dependents: _Dependents) -> str:
    names = dependents.names
    total = len(names) + dependents.hidden
    shown = ", ".join(names[:DEPENDENTS_NAMED_IN_ERROR])
    rest = total - min(len(names), DEPENDENTS_NAMED_IN_ERROR)
    if rest > 0:
        shown = f"{shown} and {rest} more" if shown else f"{rest} in projects you cannot see"
    return (
        f"Image {fingerprint[:12]} is the base image for {total} instance(s): {shown}. "
        "Those instances keep running if it is deleted, but the image can no longer be used "
        "to launch or refresh. Repeat with force=true to delete it anyway."
    )


class ImageUsageResponse(BaseModel):
    """What is standing between an image and its deletion."""
    host: str = Field(..., description="Cluster the image lives on")
    project: str = Field("default", description="The project whose store the image is read from")
    fingerprint: str = Field(..., description="Full image fingerprint")
    dependents: List[str] = Field(
        ..., description="Instances on that cluster created from this image, as far as the caller may see"
    )
    hidden_dependents: int = Field(
        0, description="Instances created from it in projects the caller may not see"
    )


@router.get(
    "/{fingerprint}/usage",
    response_model=ImageUsageResponse,
    summary="Instances built from an image",
    description=(
        "Names the instances on one cluster that were created from an image. Read this before "
        "offering a delete: the answer is authoritative, where a count derived from a "
        "tenant-filtered instance list can be an undercount."
    ),
    responses={
        403: {"model": ErrorResponse, "description": "Cluster out of scope"},
        503: {"model": ErrorResponse, "description": "Cluster unreachable"},
    },
)
async def image_usage(
    fingerprint: str = Path(..., pattern="^[0-9a-f]{64}$", description="Full image fingerprint"),
    host: str = Query(..., description="Target cluster"),
    project: str = PROJECT_QUERY,
    lxd_service: LXDService = Depends(get_lxd_service),
    # important: partner, not tenant. This names instances across the whole org scope without
    # the tenant ACL narrowing, which is correct for the operator who may delete the image and
    # would be a disclosure surface for anyone who may not.
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """List the instances on `host` that were created from `fingerprint` out of the store
    `project` reads. Instances whose project reads another store holding the same fingerprint
    are not this store's dependents."""
    verify_cluster_access(host)
    store = await _resolved_store(lxd_service, host, project)
    _store_must_be_visible(host, store)
    try:
        dependents = await _instances_using_image(lxd_service, host, fingerprint, store)
        return ImageUsageResponse(
            host=host,
            project=project,
            fingerprint=fingerprint,
            dependents=dependents.names,
            hidden_dependents=dependents.hidden,
        )
    except HTTPException:
        raise
    except LXDConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Error reading image usage for {fingerprint} on {host}: {e}")
        raise HTTPException(status_code=500, detail="Could not determine image usage")


@router.delete(
    "/{fingerprint}",
    response_model=dict,
    summary="Delete an image",
    description=(
        "Delete an image from one cluster's image store. Refuses with 409 when instances on "
        "that cluster were created from it, unless force is set. Deleting an image does not "
        "affect instances already built from it. What is lost is the ability to launch or "
        "refresh from that fingerprint."
    ),
    responses={
        200: {"description": "Image deleted"},
        403: {"model": ErrorResponse, "description": "Cluster out of scope"},
        404: {"model": ErrorResponse, "description": "No such image on that cluster"},
        409: {"model": ErrorResponse, "description": "Instances depend on this image"},
        503: {"model": ErrorResponse, "description": "Cluster unreachable"},
    },
)
async def delete_image(
    # important: the full 64-character digest only. LXD resolves a prefix, so accepting one
    # would let a short string delete whichever image happened to match it first.
    fingerprint: str = Path(..., pattern="^[0-9a-f]{64}$", description="Full image fingerprint"),
    host: str = Query(..., description="Target cluster"),
    force: bool = Query(False, description="Delete even when instances depend on this image"),
    project: str = PROJECT_QUERY,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Delete an image from the store `project` reads on a cluster."""
    verify_cluster_access(host)

    dependents = _Dependents([], 0)
    image: Optional[ImageInfo] = None
    try:
        try:
            image = await run_blocking(
                lxd_service.get_image,
                host,
                fingerprint,
                project,
                timeout=IMAGE_DELETE_TIMEOUT,
                label=f"get_image[{host}/{fingerprint[:12]}]",
                executor=_executor,
                max_in_flight=IMAGE_DELETE_MAX_IN_FLIGHT,
            )
        except BlockingTimeout:
            raise HTTPException(
                status_code=504,
                detail=f"Reading the image in cluster '{host}' timed out",
            ) from None
        except BlockingPoolFull:
            raise HTTPException(
                status_code=503, detail="Image operations saturated; please retry shortly"
            ) from None

        if image is None:
            _store_must_be_visible(host, await _resolved_store(lxd_service, host, project))
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Image '{fingerprint[:12]}' not found in the {project} image store "
                    f"in cluster '{host}'"
                ),
            )
        _store_must_be_visible(host, image.project)
        # The store `default` serves is view only where another party holds the cluster.
        refuse_default_operation(host, image.project)
        try:
            await assert_project_not_migration_fenced(
                cluster_id=host,
                project=image.project,
            )
        except MigrationFenceConflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc

        # important: the refusal is here, not only in the UI. The dashboard's own dependent
        # count is tenant-filtered, and an API key reaching this route has no UI at all.
        dependents = await _instances_using_image(lxd_service, host, fingerprint, image.project)
        if dependents and not force:
            raise HTTPException(status_code=409, detail=_dependents_detail(fingerprint, dependents))

        try:
            deleted = await run_blocking(
                lxd_service.delete_image,
                host,
                fingerprint,
                project,
                timeout=IMAGE_DELETE_TIMEOUT,
                label=f"delete_image[{host}/{fingerprint[:12]}]",
                executor=_executor,
                max_in_flight=IMAGE_DELETE_MAX_IN_FLIGHT,
            )
        except BlockingTimeout as timeout_error:
            # the delete goes on in its worker thread; clear the list when it actually ends
            timeout_error.when_finished(
                lambda: get_cache_manager().invalidate_resource_cache("images")
            )
            raise HTTPException(
                status_code=504,
                detail=(
                    f"Deleting the image in cluster '{host}' exceeded "
                    f"{IMAGE_DELETE_TIMEOUT:.0f}s and may "
                    "still be in progress; re-check the image list before retrying"
                ),
            ) from None
        except BlockingPoolFull:
            raise HTTPException(
                status_code=503, detail="Image operations saturated; please retry shortly"
            ) from None

        if not deleted:
            # It existed a moment ago and does not now. Someone else removed it, which is the
            # outcome the caller asked for, so this is not an error.
            logger.info(f"Image {fingerprint[:12]} on {host} was already gone")

        activity_logger.log_activity(
            action="image.delete",
            target_type="image",
            target_id=fingerprint,
            target_host=host,
            result="success",
            details={
                "force": force,
                "project": image.project,
                "dependents": len(dependents.names) + dependents.hidden,
                "aliases": image.aliases or [],
                "cached": image.cached,
                "public": image.public,
                "type": image.type,
                "already_gone": not deleted,
            },
        )
        # Readers hold a cached list for up to 180s, so without this the image is still on
        # screen after a successful delete. The scope digest is a suffix of the key, so one
        # pattern clears every scope's copy rather than only the caller's.
        get_cache_manager().invalidate_resource_cache("images")
        return {
            "success": True,
            "message": (
                f"Image {fingerprint[:12]} deleted from the {image.project} image store "
                f"in cluster '{host}'"
            ),
            "forced": bool(force and dependents),
        }
    except HTTPException:
        # important: let the 403, 404, 409 and the guards' 503 through. Without this the broad
        # handler below turns every one of them into a 500.
        raise
    except LXDConnectionError as e:
        activity_logger.log_activity(
            action="image.delete", target_type="image", target_id=fingerprint,
            target_host=host, result="failure", error_message=str(e),
        )
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Error deleting image {fingerprint} from {host}: {e}")
        activity_logger.log_activity(
            action="image.delete",
            target_type="image",
            target_id=fingerprint,
            target_host=host,
            result="failure",
            error_message=str(e),
            details={"force": force, "dependents": len(dependents.names) + dependents.hidden},
        )
        raise lxd_http_error(e, "Image", fingerprint[:12])
