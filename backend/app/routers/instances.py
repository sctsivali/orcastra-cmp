"""
API Router for Instance operations.
Provides endpoints for managing LXD instances (containers and VMs).
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional
from urllib.parse import urlparse
import asyncio
import logging

from pydantic import BaseModel, Field
from pylxd.exceptions import LXDAPIException, ClientConnectionFailed

from app.models.schemas import (
    VMListResponse,
    VMInfo,
    InstanceCreateRequest,
    VMCreateResponse,
    VMActionRequest,
    VMActionResponse,
    VMStatus,
    InstanceMetrics,
)
from app.services.lxd_service import (
    LXDService,
    LXDConnectionError,
    LXDOperationError,
    LXDGracefulTimeoutError,
    get_lxd_service,
)
from app.services.lxd.exceptions import LXDProjectLimitError
from app.core.single_flight import single_flight
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.core.org_scope import (
    org_scope_dependency,
    verify_cluster_access,
    get_scope_cache_key,
    check_tenant_permission,
    verify_tenant_permission,
    project_is_visible,
)
from app.core.config import get_settings
from app.services.orcahub_webhook import schedule_orcahub_event
from app.core.cache import get_cache_manager
from app.core.lxd_errors import lxd_http_error
from app.services.activity_logger import get_activity_logger
from app.services.tag_service import TagService
from app.services.lxd.instance_devices import (
    DeviceNotFound,
    nic_network_of,
    resolve_device,
    set_nic_address_sync,
)
from app.services.lxd.nic_preflight import NicPreflightError, check_address_for_network
from app.routers.tags import get_tag_service_dep
from app.core.lxd_path import safe_segment
import requests

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/instances",
    tags=["Instances"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)


def _validate_image_server(image_server: Optional[str]) -> None:
    """Guard against SSRF on instance creation.

    `image_server` is handed straight to the LXD daemon, which then fetches the
    image from that URL. Since `POST /instances` only requires Partner, an
    unvalidated value lets a Partner point the daemon at internal/metadata
    endpoints. Restrict it to the same public simplestreams catalogs the image
    picker uses (the `/images/remote` allowlist), over https only.
    """
    if not image_server:
        return
    from app.routers.images import REMOTE_IMAGE_SERVERS

    allowed_hosts = {
        urlparse(s["server"]).hostname for s in REMOTE_IMAGE_SERVERS
    }
    parsed = urlparse(image_server)
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid image_server. Allowed remote image servers: "
                + ", ".join(sorted(h for h in allowed_hosts if h))
            ),
        )


@router.get(
    "",
    response_model=VMListResponse,
    summary="List all instances",
    description="Get a list of all instances (containers and VMs) across all connected LXD nodes.",
)
@single_flight("instances:list")
async def list_instances(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    status: Optional[VMStatus] = Query(None, description="Filter by instance status"),
    instance_type: Optional[str] = Query(None, description="Filter by type: container or virtual-machine"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    limit: int = Query(0, ge=0, le=1000, description="Page size (0 = return all)"),
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """List all instances across all nodes with optional filters."""
    if host:
        verify_cluster_access(host)
    # Try cache first (scope+ACL aware, 30s TTL)
    cache = get_cache_manager()
    cache_key = get_scope_cache_key(f"instances:list:host={host}:status={status}:type={instance_type}:o={offset}:l={limit}")
    cached_response = None if fresh else cache.get("api_responses", cache_key)
    if cached_response is not None:
        logger.debug(f"Returning cached instances list for {cache_key}")
        return VMListResponse(**cached_response)
    
    try:
        generation = cache.instance_list_generation()
        all_vms, connected_nodes = await lxd_service.list_all_vms()
        
        # Filter by host if provided
        if host:
            all_vms = [vm for vm in all_vms if vm.host == host]
        
        # Filter by status if provided
        if status:
            all_vms = [vm for vm in all_vms if vm.status == status]
        
        # Filter by instance type if provided
        if instance_type:
            all_vms = [vm for vm in all_vms if vm.type == instance_type]
        
        # Apply tenant ACL filtering (project + instance level)
        #
        # important: the ACL alone does not cover a Partner. get_tenant_acl_rules returns None
        # for Admin and Partner, so check_tenant_permission passes everything for them, and a
        # Partner is scoped by cluster rather than by project. This list has always spanned
        # every project, so on a cluster carrying two organizations it named the other
        # organization's instances. project_is_visible answers True whenever no bound applies,
        # so this is inert for an Admin and on any single-org cluster, and only narrows where
        # the bound is real. The same gate already guards the network, profile and ACL lists.
        all_vms = [
            vm for vm in all_vms
            if project_is_visible(vm.host, vm.project or "default")
            and check_tenant_permission(
                vm.host,
                vm.project or "default",
                vm.name,
                "view",
            )
        ]
        
        # important: connected_nodes is what answered, not what exists. Both it and
        # get_node_names() pass through apply_cluster_filter, so the difference is the
        # registered clusters this operator can see that stayed silent. Without it the
        # list just gets shorter and nothing says why.
        unreachable_nodes = (
            [] if host else sorted(set(lxd_service.get_node_names()) - set(connected_nodes))
        )
        response = VMListResponse(
            total=len(all_vms),
            vms=all_vms[offset:offset + limit] if limit > 0 else all_vms,
            nodes=connected_nodes,
            unreachable_nodes=unreachable_nodes,
            offset=offset,
            limit=limit,
            has_more=(offset + limit < len(all_vms)) if limit > 0 else False,
        )
        
        # important: do not remember an answer nobody gave. When every cluster is
        # unreachable the connection breaker short-circuits the fan-out and this
        # endpoint still returns 200 with an empty list, so caching it would pin
        # emptiness for the whole TTL and, on a forced refresh, overwrite good
        # data the operator could still see. No node answered means no result.
        if connected_nodes:
            if cache.instance_list_generation() != generation:
                # important: an invalidation ran while this read was in flight (a create
                # landed, a background start finished). What was read is already the old
                # picture; writing it back would pin it for the TTL right after the clearance
                # meant to remove it.
                logger.info("Not caching instances list: an instance changed during the read")
            else:
                cache.set("api_responses", cache_key, response.model_dump(), ttl=30)
                logger.debug(f"Cached instances list for 30 seconds: {cache_key}")
        else:
            logger.warning("Not caching instances list: no cluster answered")
        
        return response
    except LXDConnectionError as e:
        logger.warning(f"LXD connection error: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Error listing instances: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{instance_name}",
    response_model=VMInfo,
    summary="Get instance details",
    description="Get detailed information about a specific instance.",
)
async def get_instance(
    instance_name: str,
    host: str = Query(..., description="Target host node"),
    project: Optional[str] = Query(
        None,
        description=(
            "LXD project the instance belongs to. A name is unique per project, not per host, "
            "so omitting this searches every project and returns the first match."
        ),
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific instance."""
    verify_cluster_access(host)
    try:
        vm = await lxd_service.get_vm(instance_name, host, project)
        if not vm:
            raise HTTPException(status_code=404, detail=f"Instance {instance_name} not found on {host}")
        # enforce tenant ACL on individual instance access
        verify_tenant_permission(host, vm.project or "default", vm.name, "view")
        return vm
    except HTTPException:
        # 404 (not found) / 403 (ACL). Don't let the generic handler mask these.
        raise
    except LXDConnectionError as e:
        logger.warning(f"LXD connection error: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting instance {instance_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "",
    response_model=VMCreateResponse,
    summary="Create new instance",
    description="Create a new instance (container or VM).",
)
async def create_instance(
    request: InstanceCreateRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Create a new instance."""
    verify_cluster_access(request.host)
    # important: bound the project before anything reads it. The NIC pre-flight's refusal
    # names the networks available in the target project, which without this gate turns a
    # deliberately bad create into an enumeration oracle for another organization's networks.
    if not project_is_visible(request.host, request.project or "default"):
        raise HTTPException(status_code=403, detail="Access denied to this project")
    _validate_image_server(request.image_server)
    try:
        result = await lxd_service.create_instance(
            name=request.name,
            host=request.host,
            image=request.image,
            instance_type=request.instance_type,
            profiles=request.profiles,
            config=request.config,
            cloud_init=request.cloud_init,
            devices=request.devices,
            start_on_create=request.start_on_create,
            project=request.project,
            image_server=request.image_server,
            image_protocol=request.image_protocol,
            description=request.description,
            source_type=request.source_type,
        )

        # important: "accepted", not "success". LXD takes the create as a background operation
        # and only synchronous validation has run by the time we get here, so the instance may
        # still be refused seconds later. Recording success at this point produced two
        # contradictory audit records for one create, the optimistic one here and the real
        # verdict from the watcher, with nothing to say which was final. The terminal record is
        # written by _watch_create_operation / _finalize_and_start once the operation settles.
        activity_logger.log_activity(
            action="instance.create",
            target_type="instance",
            target_id=request.name,
            target_host=request.host,
            result="accepted",
            details={
                "image": request.image,
                "instance_type": request.instance_type,
                "profiles": request.profiles,
                "project": request.project,
                "start_on_create": request.start_on_create,
                "operation_id": result,
            }
        )
        
        # note: instance.created is NOT announced here. The create is still queued at this
        # point, so announcing it would tell OrcaHub an instance exists moments before LXD may
        # refuse it, with no correction to follow. It is emitted from _judge_create_operation
        # instead, beside the add-on event that already moved there for the same reason.
        # Non-blocking: create_vm returns the operation id; the instance pulls/
        # creates in the background and appears via the polling instance list.
        # Readers hold a cached list; without this the change is invisible until the TTL.
        get_cache_manager().invalidate_instance_cache()
        return VMCreateResponse(
            success=True,
            message=f"Creating instance '{request.name}' on {request.host}",
            vm=None,
            operation_id=result,
        )
    except LXDOperationError as e:
        logger.error(f"LXD operation error: {e}")

        # Log activity: instance creation failure
        activity_logger.log_activity(
            action="instance.create",
            target_type="instance",
            target_id=request.name,
            target_host=request.host,
            result="failure",
            error_code="LXD_OPERATION_ERROR",
            error_message=str(e),
            details={"image": request.image, "instance_type": request.instance_type}
        )

        # A project limit refusal names a field the form can mark; precedent for a dict
        # detail is the rebuild route's snapshot refusal.
        if isinstance(e, LXDProjectLimitError):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "PROJECT_LIMIT_REACHED",
                    "message": str(e),
                    "limit": e.hit.limit,
                    "project": e.hit.project,
                    "max": e.hit.maximum,
                },
            )

        # Map to a meaningful status instead of a blanket 500.
        low = str(e).lower()
        if "already exists" in low:
            status = 409
        elif "not found" in low:
            status = 404
        elif "connect" in low or "not available" in low or "could not open project" in low:
            status = 503
        else:
            status = 400
        raise HTTPException(status_code=status, detail=str(e))
    except LXDConnectionError as e:
        # Cluster unreachable (e.g. during the duplicate-name exists() check or
        # the create POST). 503, matching the other instance endpoints.
        logger.warning(f"LXD connection error creating instance: {e}")
        activity_logger.log_activity(
            action="instance.create",
            target_type="instance",
            target_id=request.name,
            target_host=request.host,
            result="failure",
            error_code="LXD_CONNECTION_ERROR",
            error_message=str(e),
        )
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating instance: {e}")

        # Log activity: instance creation failure
        activity_logger.log_activity(
            action="instance.create",
            target_type="instance",
            target_id=request.name,
            target_host=request.host,
            result="failure",
            error_code="UNEXPECTED_ERROR",
            error_message=str(e)
        )
        
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/{instance_name}/start",
    response_model=VMActionResponse,
    summary="Start instance",
    description="Start a stopped instance.",
)
async def start_instance(
    instance_name: str,
    request: VMActionRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Start an instance."""
    verify_cluster_access(request.host)
    # important: ONE project value is authorized and acted on. This checked the ACL against
    # `request.project or "default"` and then handed `request.project` to the service, which
    # searches EVERY project when it is None and returns the first hit. A caller holding
    # `operate` on `default` was therefore authorized there while the action ran against
    # whichever project LXD listed first, which on a shared cluster is another tenant's instance.
    project = request.project or "default"
    # important: enforce BEFORE the try - a 403 raised inside it would be swallowed and re-raised 500.
    if get_settings().tenant_perm_enforcement_enabled:
        verify_tenant_permission(request.host, project, instance_name, "operate")
    try:
        vm = await lxd_service.start_vm(
            instance_name,
            request.host,
            force=False,
            project=project,
        )
        get_cache_manager().invalidate_instance_cache()
        
        # Log activity: instance start success
        activity_logger.log_activity(
            action="instance.start",
            target_type="instance",
            target_id=instance_name,
            target_host=request.host,
            result="success"
        )
        
        return VMActionResponse(
            success=True,
            message=f"Instance {instance_name} started successfully",
            vm_name=instance_name,
            action="start",
            new_status=vm.status
        )
    except LXDConnectionError as e:
        # The cluster did not answer. Without this the retyped exception falls to
        # `except Exception` below and answers 500, which pages an engineer for an
        # offline cluster.
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        # important: map the daemon's answer instead of blanket-500ing it, the same way delete
        # already does. The case that made this visible is a create/start race: LXD answers
        # `Instance is busy running a "create" operation`, which is a conflict the caller can
        # retry, and DELETE returned 409 for it while START returned 500 for the identical
        # condition. An operator clicking Start a moment too early should not be told the
        # server broke.
        activity_logger.log_activity(
            action="instance.start",
            target_type="instance",
            target_id=instance_name,
            target_host=request.host,
            result="failure",
            error_message=str(e),
        )
        raise lxd_http_error(e, "Instance", instance_name)
    except Exception as e:
        logger.error(f"Error starting instance {instance_name}: {e}")
        
        # Log activity: instance start failure
        activity_logger.log_activity(
            action="instance.start",
            target_type="instance",
            target_id=instance_name,
            target_host=request.host,
            result="failure",
            error_message=str(e)
        )
        
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/{instance_name}/stop",
    response_model=VMActionResponse,
    summary="Stop instance",
    description="Stop a running instance.",
)
async def stop_instance(
    instance_name: str,
    request: VMActionRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Stop an instance."""
    verify_cluster_access(request.host)
    # important: ONE project value is authorized and acted on. This checked the ACL against
    # `request.project or "default"` and then handed `request.project` to the service, which
    # searches EVERY project when it is None and returns the first hit. A caller holding
    # `operate` on `default` was therefore authorized there while the action ran against
    # whichever project LXD listed first, which on a shared cluster is another tenant's instance.
    project = request.project or "default"
    if get_settings().tenant_perm_enforcement_enabled:
        verify_tenant_permission(request.host, project, instance_name, "operate")
    try:
        await lxd_service.stop_vm(
            instance_name, 
            request.host, 
            force=request.force,
            timeout=request.timeout,
            project=project,
        )
        get_cache_manager().invalidate_instance_cache()
        
        # Log activity: instance stop success
        activity_logger.log_activity(
            action="instance.stop",
            target_type="instance",
            target_id=instance_name,
            target_host=request.host,
            result="success",
            details={"force": request.force, "timeout": request.timeout}
        )
        
        return VMActionResponse(
            success=True,
            message=f"Instance {instance_name} stopped successfully",
            vm_name=instance_name,
            action="stop",
            new_status=VMStatus.STOPPED  # Stop always results in Stopped
        )
    except LXDConnectionError as e:
        # The cluster did not answer. Without this the retyped exception falls to
        # `except Exception` below and answers 500, which pages an engineer for an
        # offline cluster.
        raise HTTPException(status_code=503, detail=str(e))
    except LXDGracefulTimeoutError as e:
        # The guest ignored the ACPI shutdown (commonly a VM with no OS, or one
        # sitting at a boot menu). Recoverable by retrying with force, so say so
        # rather than reporting a bare 500.
        logger.warning(f"Graceful stop timed out for {instance_name}: {e}")
        activity_logger.log_activity(
            action="instance.stop",
            target_type="instance",
            target_id=instance_name,
            target_host=request.host,
            result="failure",
            error_code="GRACEFUL_TIMEOUT",
            error_message=str(e),
        )
        raise HTTPException(status_code=504, detail=str(e))
    except Exception as e:
        logger.error(f"Error stopping instance {instance_name}: {e}")
        
        # Log activity: instance stop failure
        activity_logger.log_activity(
            action="instance.stop",
            target_type="instance",
            target_id=instance_name,
            target_host=request.host,
            result="failure",
            error_message=str(e)
        )
        
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/{instance_name}/restart",
    response_model=VMActionResponse,
    summary="Restart instance",
    description="Restart a running instance.",
)
async def restart_instance(
    instance_name: str,
    request: VMActionRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """Restart an instance."""
    verify_cluster_access(request.host)
    # important: ONE project value is authorized and acted on. This checked the ACL against
    # `request.project or "default"` and then handed `request.project` to the service, which
    # searches EVERY project when it is None and returns the first hit. A caller holding
    # `operate` on `default` was therefore authorized there while the action ran against
    # whichever project LXD listed first, which on a shared cluster is another tenant's instance.
    project = request.project or "default"
    if get_settings().tenant_perm_enforcement_enabled:
        verify_tenant_permission(request.host, project, instance_name, "operate")
    try:
        await lxd_service.restart_vm(
            instance_name,
            request.host,
            force=request.force,
            timeout=request.timeout,
            project=project,
        )
        get_cache_manager().invalidate_instance_cache()
        
        # Log activity: instance restart success
        activity_logger.log_activity(
            action="instance.restart",
            target_type="instance",
            target_id=instance_name,
            target_host=request.host,
            result="success",
            details={"force": request.force, "timeout": request.timeout}
        )
        
        return VMActionResponse(
            success=True,
            message=f"Instance {instance_name} restarted successfully",
            vm_name=instance_name,
            action="restart",
            new_status=VMStatus.RUNNING  # Restart ends in Running
        )
    except LXDConnectionError as e:
        # The cluster did not answer. Without this the retyped exception falls to
        # `except Exception` below and answers 500, which pages an engineer for an
        # offline cluster.
        raise HTTPException(status_code=503, detail=str(e))
    except LXDGracefulTimeoutError as e:
        # The guest ignored the ACPI shutdown (commonly a VM with no OS, or one
        # sitting at a boot menu). Recoverable by retrying with force, so say so
        # rather than reporting a bare 500.
        logger.warning(f"Graceful restart timed out for {instance_name}: {e}")
        activity_logger.log_activity(
            action="instance.restart",
            target_type="instance",
            target_id=instance_name,
            target_host=request.host,
            result="failure",
            error_code="GRACEFUL_TIMEOUT",
            error_message=str(e),
        )
        raise HTTPException(status_code=504, detail=str(e))
    except Exception as e:
        logger.error(f"Error restarting instance {instance_name}: {e}")
        
        # Log activity: instance restart failure
        activity_logger.log_activity(
            action="instance.restart",
            target_type="instance",
            target_id=instance_name,
            target_host=request.host,
            result="failure",
            error_message=str(e)
        )
        
        raise HTTPException(status_code=500, detail=str(e))


@router.delete(
    "/{instance_name}",
    response_model=VMActionResponse,
    summary="Delete instance",
    description="Delete an instance.",
)
async def delete_instance(
    instance_name: str,
    host: str = Query(..., description="Target host node"),
    project: Optional[str] = Query(None, description="LXD project name"),
    force: bool = Query(False, description="Force delete a running instance"),
    lxd_service: LXDService = Depends(get_lxd_service),
    tag_service: TagService = Depends(get_tag_service_dep),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Delete an instance."""
    verify_cluster_access(host)
    try:
        # important: everything below is stamped with the project the deletion ACTUALLY used.
        # A caller that named none was resolved across projects and then had its tags, webhook,
        # upstream event and audit row written against "default", naming a project the deletion
        # never touched.
        deleted_project = await lxd_service.delete_vm(
            instance_name, host, force=force, project=project
        )
        get_cache_manager().invalidate_instance_cache()

        # Announce the lifecycle change to any subscribed add-on. Nothing emitted these events
        # until now, so the catalogue was a promise with no sender behind it.
        from app.services.webhooks.emitter import emit_resource_event

        await emit_resource_event(
            event_type="orcastra.instance.deleted.v1",
            cluster_id=host,
            resource_type="instance",
            resource_name=instance_name,
            project=deleted_project,
        )


        # Clean up the instance's tag rows so the tag store doesn't drift from the
        # deleted container. Best-effort, because the instance is already gone.
        try:
            await tag_service.remove_resource_assignments(
                "instance", instance_name,
                resource_host=host, resource_project=deleted_project,
            )
        except Exception as tag_err:
            logger.warning(
                f"Failed to clean up tags for deleted instance {instance_name}: {tag_err}"
            )

        # Log activity: instance deletion success
        activity_logger.log_activity(
            action="instance.delete",
            target_type="instance",
            target_id=instance_name,
            target_host=host,
            result="success",
            details={"force": force, "project": deleted_project},
        )

        schedule_orcahub_event("instance.deleted", host, project, instance_name)
        return VMActionResponse(
            success=True,
            message=f"Instance {instance_name} deleted successfully",
            vm_name=instance_name,
            action="delete",
        )
    except LXDConnectionError as e:
        logger.warning(f"LXD connection error deleting {instance_name}: {e}")
        raise HTTPException(status_code=503, detail=str(e))
    except LXDOperationError as e:
        # Map daemon errors to meaningful codes instead of a blanket 500.
        msg = str(e)
        low = msg.lower()
        if "not found" in low:
            status = 404
        elif "running" in low or "stop it" in low:
            status = 409  # running instance, retry with force=true
        elif "connect" in low or "not available" in low:
            status = 503
        else:
            status = 400
        activity_logger.log_activity(
            action="instance.delete",
            target_type="instance",
            target_id=instance_name,
            target_host=host,
            result="failure",
            error_message=msg,
        )
        raise HTTPException(status_code=status, detail=msg)
    except Exception as e:
        logger.error(f"Error deleting instance {instance_name}: {e}")

        # Log activity: instance deletion failure
        activity_logger.log_activity(
            action="instance.delete",
            target_type="instance",
            target_id=instance_name,
            target_host=host,
            result="failure",
            error_message=str(e)
        )

        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{instance_name}/metrics",
    response_model=InstanceMetrics,
    summary="Get instance metrics",
    description="Get real-time metrics for an instance.",
)
async def get_instance_metrics(
    instance_name: str,
    host: str = Query(..., description="Target host node"),
    project: Optional[str] = Query(
        None,
        description=(
            "LXD project the instance lives in. A name is unique per project, not per host, so "
            "omitting this reads the instance of that name in `default`."
        ),
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get metrics for a specific instance."""
    verify_cluster_access(host)
    # important: this route had no tenant check at all, and the reader behind it discarded the
    # project and searched every one of them. A tenant scoped to a single instance on a shared
    # cluster could therefore read any other tenant's live CPU, memory, disk and network by
    # asking for their instance by name. Authorized and read on ONE project value, like the
    # power routes above.
    scoped_project = project or "default"
    if get_settings().tenant_perm_enforcement_enabled:
        verify_tenant_permission(host, scoped_project, instance_name, "view")
    try:
        metrics = await lxd_service.get_instance_metrics(host, instance_name, scoped_project)
        if not metrics:
            raise HTTPException(status_code=404, detail=f"Instance {instance_name} not found on {host}")
        return metrics
    except LXDConnectionError as e:
        # The cluster did not answer. Without this the retyped exception falls to
        # `except Exception` below and answers 500, which pages an engineer for an
        # offline cluster.
        raise HTTPException(status_code=503, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting metrics for {instance_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class NicAddressRequest(BaseModel):
    """Set or clear the fixed IPv4 on one network interface."""

    host: str = Field(..., description="Target host node")
    project: Optional[str] = Field("default", description="LXD project holding the instance")
    ipv4_address: Optional[str] = Field(
        None,
        description=(
            "The address to reserve for this interface. Empty or null hands the interface "
            "back to DHCP."
        ),
    )


@router.patch(
    "/{instance_name}/devices/{device_name}/address",
    response_model=dict,
    summary="Set a network interface's fixed IPv4",
    description=(
        "Reserve a specific IPv4 for one of an instance's network interfaces, or clear it "
        "back to DHCP. Overrides a profile-inherited interface onto the instance."
    ),
)
async def set_instance_nic_address(
    instance_name: str,
    device_name: str,
    request: NicAddressRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Pin or release a fixed IPv4 on an instance's interface."""
    verify_cluster_access(request.host)
    project = request.project or "default"
    # important: the project bound, not only the cluster one. A Partner is scoped by cluster
    # alone, so without this a shared cluster lets one organization rewrite a device on
    # another organization's instance.
    if not project_is_visible(request.host, project):
        raise HTTPException(status_code=403, detail="Access denied to this project")
    # important: enforce BEFORE the try - a 403 raised inside it would be swallowed and
    # re-raised as a 500. Same ordering as start/stop.
    if get_settings().tenant_perm_enforcement_enabled:
        verify_tenant_permission(request.host, project, instance_name, "operate")

    client = lxd_service.get_client_with_reconnect(request.host)
    if client is None:
        raise HTTPException(
            status_code=503, detail=f"Could not connect to {request.host}"
        )

    try:
        current, _inherited = await asyncio.to_thread(
            _read_instance_device, client, instance_name, project, device_name
        )
        # important: this reads the cluster over HTTP with a (3, 25) socket timeout, so it
        # belongs off the loop like the read above it and the write below it. Called directly
        # it parks a uvicorn worker for up to 56s on a slow daemon, and this deployment runs
        # WEB_CONCURRENCY=4, so that is a quarter of the API including health checks.
        await asyncio.to_thread(
            check_address_for_network,
            client,
            project,
            nic_network_of(current),
            request.ipv4_address or "",
        )
        written = await asyncio.to_thread(
            set_nic_address_sync,
            client,
            instance_name,
            project,
            device_name,
            request.ipv4_address,
        )
    except DeviceNotFound:
        raise HTTPException(
            status_code=404,
            detail=f"Instance '{instance_name}' has no interface called '{device_name}'.",
        ) from None
    except NicPreflightError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except (LXDAPIException, ClientConnectionFailed) as exc:
        logger.error(f"LXD refused the device write on {instance_name}: {exc}")
        activity_logger.log_activity(
            action="instance.nic.address",
            target_type="instance",
            target_id=instance_name,
            target_host=request.host,
            result="failure",
            error_message=str(exc),
        )
        raise HTTPException(
            status_code=400, detail=f"The cluster refused the change: {exc}"
        ) from None
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
        # important: ConnectionError is not redundant here, it is the case that actually
        # happens. The pooled session mounts urllib3 Retry, and with a Retry object mounted a
        # read timeout surfaces as MaxRetryError, which requests converts to ConnectionError,
        # NOT to ReadTimeout. Measured against a peer that accepts and never answers: a bare
        # session raises ReadTimeout, the same call through this repo's adapter raises
        # ConnectionError. Catching Timeout alone left this clause unreachable and a slow
        # cluster answering a bare 500.
        #
        # A socket timeout on this path means the daemon took too long, not that we are
        # broken. The write may still be applying, so say that rather than answering 500 and
        # leaving the operator unsure whether to retry.
        logger.warning(f"Timed out on the device write for {instance_name}: {exc}")
        activity_logger.log_activity(
            action="instance.nic.address",
            target_type="instance",
            target_id=instance_name,
            target_host=request.host,
            result="failure",
            error_code="LXD_TIMEOUT",
            error_message=str(exc),
        )
        get_cache_manager().invalidate_instance_cache()
        raise HTTPException(
            status_code=504,
            detail=(
                f"{request.host} did not finish the change in time. It may still be applying, "
                "so refresh the instance before retrying."
            ),
        ) from None

    get_cache_manager().invalidate_instance_cache()
    activity_logger.log_activity(
        action="instance.nic.address",
        target_type="instance",
        target_id=instance_name,
        target_host=request.host,
        result="success",
        details={
            "device": device_name,
            "project": project,
            "ipv4_address": request.ipv4_address or "",
        },
    )
    return {
        "success": True,
        "message": (
            f"{device_name} now uses {request.ipv4_address}"
            if request.ipv4_address
            else f"{device_name} returned to DHCP"
        ),
        "device": written,
    }


def _read_instance_device(client, name: str, project: str, device_name: str):
    """The device as the instance sees it, off the event loop."""
    params = {"project": project} if project and project != "default" else {}
    data = client.api.instances[safe_segment(name)].get(params=params).json().get("metadata") or {}
    return resolve_device(data, device_name)
