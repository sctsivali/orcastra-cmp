"""
API Router for LXD image operations.
Provides endpoints for listing available images on each node.
"""

from fastapi import APIRouter, HTTPException, Depends, Path, Query
from typing import Dict, List, Optional, Set
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
from app.core.cache import get_cache_manager
from app.core.dependencies import (
    AuthenticatedUser,
    check_read_only_mode,
    require_partner,
    require_tenant,
)
from app.core.org_scope import (
    org_scope_dependency,
    verify_cluster_access,
    get_scope_cache_key,
    check_tenant_permission,
    project_is_visible,
)
from app.core.tenant_visibility import tenant_acl_active, tenant_may_see_project
from app.services.activity_logger import get_activity_logger
from app.services.remote_image_catalog import _fetch_remote_catalog, _REMOTE_FETCH_TIMEOUT
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
    description="Restrict to the store one project reads; omitted, every store on the host is listed",
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
            host=node_name, images=[], total=0, reachable=True, error=str(e), project=project
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
            error=str(e) or e.__class__.__name__,
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
    summary="List images from all nodes",
    description="Get a list of available images from all connected LXD nodes.",
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
            response.append(ImageListResponse(
                host=node_names[i] if i < len(node_names) else "unknown",
                images=[],
                total=0,
                reachable=False,
                error=str(result) or result.__class__.__name__,
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
        cache.set("api_responses", cache_key, cache_data, ttl=180)
        logger.debug("Cached images list for 180 seconds")
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
    summary="List images from specific node",
    description="Get a list of available images from a specific LXD node.",
    responses={
        200: {"description": "Images listed successfully"},
        400: {"model": ErrorResponse, "description": "Invalid host"},
        500: {"model": ErrorResponse, "description": "Server error"},
    }
)
async def list_node_images(
    host: str,
    lxd_service: LXDService = Depends(get_lxd_service),
    project: str = PROJECT_QUERY,
):
    """The images `project` can use on one LXD node: what its own store holds when it
    isolates images, else the shared default store. `project` on the response names which."""
    verify_cluster_access(host)
    if not project_is_visible(host, project):
        raise HTTPException(status_code=403, detail="Access denied to this project")
    try:
        # Validate host
        valid_nodes = lxd_service.get_node_names()
        if host not in valid_nodes:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid host. Valid hosts: {valid_nodes}"
            )

        listing = await lxd_service.list_images(host, project)
        result = ImageListResponse(
            host=host,
            images=listing.images,
            total=len(listing.images),
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


async def _instances_using_image(
    lxd_service: LXDService, host: str, fingerprint: str
) -> List[str]:
    """Names of instances on `host` that were created from `fingerprint`.

    note: this pays a fleet-wide fan-out to answer a single-host question. It reuses the same
    call the tenant image filter already makes on every scoped list, so it is a proven path
    that respects org scope, and the connection breaker short-circuits clusters that are down.
    A delete is rare and deliberate; a cheaper bespoke pylxd scan here would be a second way
    of reading instance config, which is how the two keys above came to disagree elsewhere.
    """
    all_vms, _ = await lxd_service.list_all_vms()
    names = []
    for vm in all_vms:
        if getattr(vm, "host", None) != host:
            continue
        used = _image_fingerprint_of(vm)
        if not used:
            continue
        # LXD stores volatile.base_image as the full digest, but an instance created from a
        # short fingerprint can carry the prefix. Compare both ways, as the tenant filter does.
        if used == fingerprint or used.startswith(fingerprint) or fingerprint.startswith(used):
            names.append(getattr(vm, "name", "") or "")
    return sorted(n for n in names if n)


def _dependents_detail(fingerprint: str, names: List[str]) -> str:
    shown = ", ".join(names[:DEPENDENTS_NAMED_IN_ERROR])
    rest = len(names) - DEPENDENTS_NAMED_IN_ERROR
    if rest > 0:
        shown = f"{shown} and {rest} more"
    return (
        f"Image {fingerprint[:12]} is the base image for {len(names)} instance(s): {shown}. "
        "Those instances keep running if it is deleted, but the image can no longer be used "
        "to launch or refresh. Repeat with force=true to delete it anyway."
    )


class ImageUsageResponse(BaseModel):
    """What is standing between an image and its deletion."""
    host: str = Field(..., description="Cluster the image lives on")
    project: str = Field("default", description="The project whose store the image is read from")
    fingerprint: str = Field(..., description="Full image fingerprint")
    dependents: List[str] = Field(
        ..., description="Instances on that cluster created from this image"
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
    """List the instances on `host` that were created from `fingerprint`.

    note: dependents are counted across every project on the host. The same fingerprint can
    sit in two stores, so this can over-count for one of them; a refusal the operator can
    override with `force` is the safe direction.
    """
    verify_cluster_access(host)
    if not project_is_visible(host, project):
        raise HTTPException(status_code=403, detail="Access denied to this project")
    try:
        dependents = await _instances_using_image(lxd_service, host, fingerprint)
        return ImageUsageResponse(
            host=host, project=project, fingerprint=fingerprint, dependents=dependents
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
    if not project_is_visible(host, project):
        raise HTTPException(status_code=403, detail="Access denied to this project")

    dependents: List[str] = []
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
                status_code=504, detail=f"Reading the image on {host} timed out"
            ) from None
        except BlockingPoolFull:
            raise HTTPException(
                status_code=503, detail="Image operations saturated; please retry shortly"
            ) from None

        if image is None:
            raise HTTPException(
                status_code=404,
                detail=f"Image '{fingerprint[:12]}' not found in the {project} image store on {host}",
            )

        # important: the refusal is here, not only in the UI. The dashboard's own dependent
        # count is tenant-filtered, and an API key reaching this route has no UI at all.
        dependents = await _instances_using_image(lxd_service, host, fingerprint)
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
        except BlockingTimeout:
            raise HTTPException(
                status_code=504,
                detail=(
                    f"Deleting the image on {host} exceeded {IMAGE_DELETE_TIMEOUT:.0f}s and may "
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
                "dependents": len(dependents),
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
            "message": f"Image {fingerprint[:12]} deleted from the {image.project} image store on {host}",
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
            details={"force": force, "dependents": len(dependents)},
        )
        raise lxd_http_error(e, "Image", fingerprint[:12])
