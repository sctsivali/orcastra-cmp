"""
API Router for Instance operations.
Provides endpoints for managing LXD instances (containers and VMs).
"""

from fastapi import APIRouter, HTTPException, Depends, Header, Query
from typing import Literal, Optional
from urllib.parse import urlparse
import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field
from pylxd.exceptions import LXDAPIException, ClientConnectionFailed
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schemas import (
    VMListResponse,
    VMInfo,
    InstanceCreateRequest,
    InstanceCreatePreflightIssue,
    InstanceCreatePreflightResponse,
    VMCreateResponse,
    VMActionRequest,
    VMActionResponse,
    VMStatus,
    InstanceMetrics,
    InstanceLimitSpecification,
    InstanceRootDiskSpecification,
    InstanceSpecification,
    InstanceSpecificationsResponse,
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
from app.core.blocking import BlockingPoolFull, BlockingTimeout
from app.core.database import get_db
from app.core.error_sanitizer import sanitize_error
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.core.api_key_capabilities import is_valid_project_name
from app.core.org_scope import (
    can_view_instance,
    check_tenant_permission,
    filter_projects_by_acl,
    get_scope_cache_key,
    org_scope_dependency,
    project_is_visible,
    verify_cluster_access,
    verify_instance_view_access,
    verify_project_access,
    verify_tenant_permission,
)
from app.core.config import get_settings
from app.services.orcahub_webhook import schedule_orcahub_event
from app.core.cache import get_cache_manager
from app.core.lxd_errors import lxd_http_error
from app.services.activity_logger import get_activity_logger
from app.services.instance_operation_audit import drain_instance_operation_audits
from app.services.tag_service import TagService
from app.services.lxd.instance_devices import (
    DeviceNotFound,
    nic_network_of,
    resolve_device,
    set_nic_address_sync,
)
from app.services.lxd.nic_preflight import NicPreflightError, check_address_for_network
from app.services.lxd.create_preflight import (
    CreatePreflightEvaluation,
    CreatePreflightUnavailable,
    evaluate_instance_create,
)
from app.services.lxd.instance import CreateSubmissionUnknown
from app.services.instance_security_policy import (
    InstanceSecurityPolicyViolation,
    resolve_cluster_write_policy,
    validate_instance_payload,
    validate_target_project_isolation,
)
from app.services.instance_job_identity import resolve_instance_job_organization
from app.services.shared_cluster_audit import serialize_cluster_configuration
from app.services.instance_specifications import (
    SpecificationConflict,
    create_operation_job,
    get_operation_job_by_request,
)
from app.services.migration_fence import (
    MigrationFenceConflict,
    assert_instance_not_migration_fenced,
    assert_project_not_migration_fenced,
    guard_instance_mutation,
)
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


class InstanceWritePolicyResponse(BaseModel):
    """The safe UI projection of the actor's instance-write surface on one cluster."""

    model_config = ConfigDict(extra="forbid", strict=True)

    host: str
    mode: Literal["full", "safe", "view_only"]
    can_edit_specifications: bool
    can_use_advanced_create: bool


def _limit_specification(
    vm: VMInfo,
    config_key: str,
    effective_value: Optional[str],
) -> InstanceLimitSpecification:
    configured = (vm.config or {}).get(config_key) if isinstance(vm.config, dict) else None
    if configured not in (None, ""):
        return InstanceLimitSpecification(value=str(configured), source="explicit")
    if vm.effective_limits_known:
        if effective_value not in (None, ""):
            return InstanceLimitSpecification(value=str(effective_value), source="inherited")
        # LXD VMs have daemon defaults even when neither the instance nor a profile names a
        # limit. Containers do not. Keep that provenance distinct from both "unlimited" and
        # an inherited value so the UI does not invent host capacity or misstate the purchase.
        if vm.type == "virtual-machine":
            defaults = {"limits.cpu": "1", "limits.memory": "1GiB"}
            if config_key in defaults:
                return InstanceLimitSpecification(
                    value=defaults[config_key], source="default"
                )
        return InstanceLimitSpecification(value=None, source="unlimited")
    return InstanceLimitSpecification(value=None, source="unknown")


def _root_device(devices: Optional[dict]) -> Optional[dict]:
    if not isinstance(devices, dict):
        return None
    for device in devices.values():
        if not isinstance(device, dict) or device.get("type") != "disk":
            continue
        # Device names are operator-controlled. A data disk can legitimately be named "root";
        # only the mount path identifies the instance root disk.
        if device.get("path") == "/":
            return device
    return None


def _root_disk_specification(vm: VMInfo) -> InstanceRootDiskSpecification:
    explicit = _root_device(vm.devices)
    effective = _root_device(vm.expanded_devices)
    pool = (effective or explicit or {}).get("pool")
    explicit_size = (explicit or {}).get("size")
    effective_size = (effective or {}).get("size")
    if explicit_size not in (None, ""):
        return InstanceRootDiskSpecification(
            value=str(explicit_size), pool=pool, source="explicit"
        )
    if effective_size not in (None, ""):
        return InstanceRootDiskSpecification(
            value=str(effective_size), pool=pool, source="inherited"
        )
    if vm.expanded_devices_known and effective is not None:
        return InstanceRootDiskSpecification(value=None, pool=pool, source="pool_default")
    return InstanceRootDiskSpecification(value=None, pool=pool, source="unknown")


def _instance_specification(vm: VMInfo) -> InstanceSpecification:
    expanded = vm.expanded_devices if isinstance(vm.expanded_devices, dict) else {}
    gpu_devices = sorted(
        name
        for name, device in expanded.items()
        if isinstance(device, dict) and device.get("type") == "gpu"
    )
    return InstanceSpecification(
        name=vm.name,
        host=vm.host,
        project=vm.project or "default",
        status=vm.status,
        type=vm.type,
        architecture=vm.architecture or None,
        cpu=_limit_specification(vm, "limits.cpu", vm.effective_cpu_limit),
        memory=_limit_specification(vm, "limits.memory", vm.effective_memory_limit),
        root_disk=_root_disk_specification(vm),
        gpu_count=len(gpu_devices),
        gpu_devices=gpu_devices,
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
    from app.services.remote_image_catalog import REMOTE_IMAGE_SERVERS

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
    "/write-policy",
    response_model=InstanceWritePolicyResponse,
    summary="Get instance write capabilities for one cluster",
)
async def get_instance_write_policy(
    host: str = Query(..., min_length=1, max_length=255),
    user: AuthenticatedUser = Depends(require_tenant()),
    db: AsyncSession = Depends(get_db),
) -> InstanceWritePolicyResponse:
    """Project the central server policy without exposing organization membership details."""
    verify_cluster_access(host)
    policy = await resolve_cluster_write_policy(user, host, db)
    if policy.has_full_access:
        mode: Literal["full", "safe", "view_only"] = "full"
    elif policy.requires_safe_allowlist:
        mode = "safe"
    else:
        mode = "view_only"
    return InstanceWritePolicyResponse(
        host=host,
        mode=mode,
        can_edit_specifications=mode in {"full", "safe"},
        can_use_advanced_create=mode == "full",
    )


@router.get(
    "",
    response_model=VMListResponse,
    summary="List all instances",
    description="Get a list of all instances (containers and VMs) across all connected clusters.",
)
@single_flight("instances:list")
async def list_instances(
    host: Optional[str] = Query(None, description="Filter by a specific cluster"),
    project: Optional[str] = Query(None, description="LXD project to list. Omit for every project the caller may see."),
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
    if project and not is_valid_project_name(project):
        raise HTTPException(status_code=422, detail="Invalid project name")
    if host and project and (
        not project_is_visible(host, project)
        or not filter_projects_by_acl(host, [project])
    ):
        # Keep the list endpoint's established anti-enumeration behavior: a project outside
        # the Partner bound or Tenant grant is indistinguishable from an empty project. This
        # gate must precede the scoped LXD read; per-instance Tenant permissions are still
        # applied to the returned rows below.
        return VMListResponse(
            total=0,
            vms=[],
            nodes=[],
            unreachable_nodes=[],
            offset=offset,
            limit=limit,
            has_more=False,
        )
    # Try cache first (scope+ACL aware, 30s TTL). The project is part of the key, as it is
    # for profiles: two callers asking for two projects must not share one body.
    cache = get_cache_manager()
    # v2 keeps rolling deployments from serving a scoped-host 200/empty response cached by a
    # build that could not distinguish an empty project from an unreachable selected cluster.
    cache_key = get_scope_cache_key(
        f"instances:list:v2:host={host}:project={project or '*'}:status={status}:type={instance_type}:o={offset}:l={limit}"
    )
    cached_response = None if fresh else cache.get("api_responses", cache_key)
    if cached_response is not None:
        logger.debug(f"Returning cached instances list for {cache_key}")
        return VMListResponse(**cached_response)
    
    try:
        generation = cache.instance_list_generation()
        if host and project:
            all_vms, connected_nodes, incomplete_nodes = (
                await lxd_service.list_vms_in_project_with_failures(host, project)
            )
        else:
            all_vms, connected_nodes, incomplete_nodes = (
                await lxd_service.list_all_vms_with_failures()
            )
        
        # Filter by host if provided
        if host:
            all_vms = [vm for vm in all_vms if vm.host == host]
            # The caller already passed verify_cluster_access above. Echo only that authorized
            # target, never failures from the rest of the org-wide inventory fan-out.
            selected_host_connected = host in connected_nodes
            selected_host_incomplete = host in incomplete_nodes
            if not selected_host_connected:
                all_vms = []
            response_nodes = [host] if selected_host_connected else []
            unreachable_nodes = (
                [host]
                if not selected_host_connected or selected_host_incomplete
                else []
            )
        else:
            response_nodes = connected_nodes
            # Keep the established denominator as a compatibility guard for service doubles and
            # older mixed-version workers. The detailed fan-out also names clusters that answered
            # only partially, which the former two-value contract silently lost.
            unreachable_nodes = sorted(
                (set(lxd_service.get_node_names()) - set(connected_nodes))
                | set(incomplete_nodes)
            )

        # Keep a final project filter for the partially scoped fleet path and as defense in
        # depth if a service implementation ever returns rows outside the requested project.
        if project:
            all_vms = [vm for vm in all_vms if (vm.project or "default") == project]
        
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
        
        response = VMListResponse(
            total=len(all_vms),
            vms=all_vms[offset:offset + limit] if limit > 0 else all_vms,
            nodes=response_nodes,
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
        # For a scoped request, an answer from some other cluster is irrelevant. Caching that
        # empty response would hide recovery of the selected host for the full TTL.
        cacheable_answer = (
            host in connected_nodes if host is not None else bool(connected_nodes)
        )
        if cacheable_answer:
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
    "/specifications",
    response_model=InstanceSpecificationsResponse,
    summary="List visible instance specifications",
    description=(
        "Return a safe projection of the effective CPU, memory, root disk, and GPU "
        "configuration for every instance the caller may view."
    ),
)
@single_flight("instances:specifications")
async def list_instance_specifications(
    lxd_service: LXDService = Depends(get_lxd_service),
    fresh: bool = False,
):
    """List actual LXD specifications without exposing raw instance configuration."""
    cache = get_cache_manager()
    cache_key = get_scope_cache_key("instances:specifications")
    cached = None if fresh else cache.get("api_responses", cache_key)
    if cached is not None:
        return InstanceSpecificationsResponse(**cached)

    try:
        generation = cache.instance_list_generation()
        all_vms, connected_nodes, incomplete_nodes = (
            await lxd_service.list_all_vms_with_failures()
        )
        visible = [
            vm
            for vm in all_vms
            if can_view_instance(vm.host, vm.project or "default", vm.name)
        ]
        specifications = sorted(
            (_instance_specification(vm) for vm in visible),
            key=lambda item: (item.host, item.project, item.name),
        )
        # The bounded inventory fan-out already names every registered cluster that failed or
        # answered partially. Reading get_node_names() here repeated a synchronous Vault call on
        # the event loop after all LXD work had been offloaded.
        unreachable = sorted(set(incomplete_nodes))
        response = InstanceSpecificationsResponse(
            total=len(specifications),
            instances=specifications,
            unreachable_nodes=unreachable,
            collected_at=datetime.now(timezone.utc),
        )

        # No cluster answer is an outage report, not a stable empty inventory. A partial answer
        # is cacheable because `unreachable_nodes` preserves exactly what was missing.
        if connected_nodes:
            if cache.instance_list_generation() != generation:
                logger.info(
                    "Not caching instance specifications: an instance changed during the read"
                )
            else:
                cache.set(
                    "api_responses",
                    cache_key,
                    response.model_dump(mode="json"),
                    ttl=30,
                )
        return response
    except LXDConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error listing instance specifications: %s", exc)
        raise HTTPException(status_code=500, detail="Could not read instance specifications")


@router.get(
    "/{instance_name}",
    response_model=VMInfo,
    summary="Get instance details",
    description="Get detailed information about a specific instance.",
)
async def get_instance(
    instance_name: str,
    host: str = Query(..., description="Target cluster"),
    project: Optional[str] = Query(
        None,
        description=(
            "LXD project the instance belongs to. A name is unique per project, not per cluster, "
            "so omitting this searches only projects visible to the caller."
        ),
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get one visible instance, searching only authorized projects when omitted."""
    scoped_project: Optional[str] = None
    project_filter = None
    if project is None:
        verify_cluster_access(host)
        project_filter = lambda candidate: can_view_instance(  # noqa: E731
            host, candidate, instance_name
        )
    else:
        scoped_project = verify_instance_view_access(host, project, instance_name)
    try:
        if project_filter is None:
            vm = await lxd_service.get_vm(instance_name, host, scoped_project)
        else:
            vm = await lxd_service.get_vm(
                instance_name,
                host,
                scoped_project,
                project_filter=project_filter,
            )
        if not vm:
            raise HTTPException(
                status_code=404,
                detail=f"Instance {instance_name} not found in cluster '{host}'",
            )
        verify_instance_view_access(host, vm.project or "default", vm.name)
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


async def _run_create_preflight(
    request: InstanceCreateRequest,
    lxd_service: LXDService,
) -> CreatePreflightEvaluation:
    try:
        return await evaluate_instance_create(lxd_service, request)
    except (BlockingPoolFull, BlockingTimeout) as exc:
        logger.warning("Create preflight capacity exhausted: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "CREATE_PREFLIGHT_UNAVAILABLE",
                "message": "Instance preflight is busy or timed out. Try again shortly.",
            },
        ) from exc
    except (CreatePreflightUnavailable, LXDConnectionError) as exc:
        safe_error = sanitize_error(str(exc), limit=500)
        logger.warning("Create preflight unavailable: %s", safe_error)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "CREATE_PREFLIGHT_UNAVAILABLE",
                "message": safe_error,
            },
        ) from exc


def _create_idempotency_key(value: str) -> str:
    key = value.strip()
    if (
        not key
        or len(key) > 128
        or any(ord(character) < 33 or ord(character) > 126 for character in key)
    ):
        raise HTTPException(
            status_code=422,
            detail="Idempotency-Key must contain between 1 and 128 characters.",
        )
    return key


def _create_request_hash(request: InstanceCreateRequest) -> str:
    """Hash the complete request without persisting its secret-bearing fields."""
    material = request.model_dump(mode="json")
    material["acknowledgements"] = sorted(set(material.get("acknowledgements") or []))
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_create_tags(request: InstanceCreateRequest) -> InstanceCreateRequest:
    """Canonicalize ``user.tags`` before preflight, hashing, or daemon submission.

    LXD stores tags as one comma-separated config value while PostgreSQL stores one row per
    tag. Normalizing once at the request boundary keeps both representations and the durable
    idempotency hash aligned. An explicitly present but empty segment is malformed; callers
    that do not want tags should omit ``user.tags``.
    """
    config = dict(request.config or {})
    if "user.tags" not in config:
        return request

    normalized: set[str] = set()
    for raw_tag in config["user.tags"].split(","):
        tag = raw_tag.strip().lower()
        valid, error = TagService.validate_tag_name(tag)
        if not valid:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "INVALID_INSTANCE_TAG",
                    "field": "config.user.tags",
                    "message": error,
                },
            )
        normalized.add(tag)

    config["user.tags"] = ",".join(sorted(normalized))
    return request.model_copy(update={"config": config})


def _create_requester(user: AuthenticatedUser) -> str:
    requester = str(user.user_id or "").strip()
    if requester:
        return requester
    requester = str(user.email or "").strip()
    if requester:
        return requester
    raise HTTPException(status_code=401, detail="Authenticated user identity is unavailable")


def _safe_create_job_request(request: InstanceCreateRequest) -> dict:
    """The minimum non-sensitive create intent needed by recovery."""
    tags_value = (request.config or {}).get("user.tags", "")
    return {
        "instance_type": request.instance_type,
        "source_type": request.source_type,
        "start_on_create": bool(request.start_on_create),
        "profiles": list(request.profiles or ["default"]),
        "networkless_acknowledged": "networkless" in request.acknowledgements,
        "tags": tags_value.split(",") if tags_value else [],
    }


@router.post(
    "/preflight",
    response_model=InstanceCreatePreflightResponse,
    summary="Preflight an instance create",
    description="Validate effective profiles, devices, and networks without changing LXD.",
)
async def preflight_create_instance(
    request: InstanceCreateRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    db: AsyncSession = Depends(get_db),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Return blockers and warnings after project authorization and authoritative reads."""
    project = verify_project_access(request.host, request.project)
    request = request.model_copy(update={"project": project})
    request = _normalize_create_tags(request)
    _validate_image_server(request.image_server)
    async with serialize_cluster_configuration(db, request.host):
        evaluation = await _run_create_preflight(request, lxd_service)
        response = evaluation.response
        try:
            policy = await resolve_cluster_write_policy(_rbac, request.host, db)
            validate_target_project_isolation(policy, evaluation.target_project_config)
            validate_instance_payload(
                policy,
                config=evaluation.effective_config,
                devices=evaluation.effective_devices,
            )
        except InstanceSecurityPolicyViolation as exc:
            detail = exc.as_detail()
            violations = detail.get("violations") or []
            response = response.model_copy(
                update={
                    "ready": False,
                    "blockers": [
                        *response.blockers,
                        InstanceCreatePreflightIssue(
                            code=str(detail.get("code") or "SECURITY_POLICY_VIOLATION"),
                            field=(
                                str(violations[0].get("field"))
                                if violations and violations[0].get("field")
                                else None
                            ),
                            message=str(
                                detail.get("message")
                                or "This configuration is not permitted by cluster policy."
                            ),
                        ),
                    ],
                }
            )
    return response


def _enforce_create_preflight(
    request: InstanceCreateRequest,
    preflight: InstanceCreatePreflightResponse,
) -> None:
    if request.plan_revision is not None and request.plan_revision != preflight.plan_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "STALE_CREATE_PLAN",
                "message": (
                    "Profiles, devices, networks, quotas, or instance availability changed "
                    "after preflight. Review the updated plan before creating."
                ),
                "plan_revision": preflight.plan_revision,
            },
        )
    if preflight.blockers:
        codes = {issue.code for issue in preflight.blockers}
        status_code = 409 if "INSTANCE_ALREADY_EXISTS" in codes else 400
        raise HTTPException(
            status_code=status_code,
            detail={
                "code": "CREATE_PREFLIGHT_FAILED",
                "message": "The instance cannot be created until the blockers are resolved.",
                "blockers": [issue.model_dump(mode="json") for issue in preflight.blockers],
                "plan_revision": preflight.plan_revision,
            },
        )
    missing_acknowledgements = sorted(
        set(preflight.required_acknowledgements) - set(request.acknowledgements)
    )
    if missing_acknowledgements:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ACKNOWLEDGEMENT_REQUIRED",
                "message": "Review and acknowledge the create warnings before continuing.",
                "acknowledgements": missing_acknowledgements,
                "warnings": [issue.model_dump(mode="json") for issue in preflight.warnings],
                "plan_revision": preflight.plan_revision,
            },
        )


@router.post(
    "",
    response_model=VMCreateResponse,
    status_code=202,
    summary="Create new instance",
    description="Create a new instance (container or VM).",
)
async def create_instance(
    request: InstanceCreateRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    lxd_service: LXDService = Depends(get_lxd_service),
    db: AsyncSession = Depends(get_db),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Create a new instance."""
    # important: bound the project before anything reads it. The NIC pre-flight's refusal
    # names the networks available in the target project, which without this gate turns a
    # deliberately bad create into an enumeration oracle for another organization's networks.
    project = verify_project_access(request.host, request.project)
    request = request.model_copy(update={"project": project})
    request = _normalize_create_tags(request)
    _validate_image_server(request.image_server)
    key = _create_idempotency_key(idempotency_key)
    requester = _create_requester(_rbac)
    digest = _create_request_hash(request)

    # Retry lookup precedes daemon reads. The caller is re-authorized for the recorded scope,
    # then receives the durable result without risking a second create while the cluster is
    # offline or the first operation is still settling.
    replay = await get_operation_job_by_request(requester, key)
    if replay is not None:
        if (
            replay.kind != "create"
            or replay.cluster_id != request.host
            or replay.project != request.project
            or replay.instance_name != request.name
            or replay.payload_hash != digest
        ):
            raise HTTPException(
                status_code=409,
                detail="This Idempotency-Key was already used for a different request.",
            )
        return VMCreateResponse(
            success=True,
            message=f"Instance create job '{replay.job_id}' already exists",
            vm=None,
            operation_id=replay.operation_id,
            job_id=replay.job_id,
            status="unknown" if replay.status == "unknown" else "accepted",
        )

    # The ownership lock closes the race between an exclusive Partner write and another
    # request sharing the same cluster. Authoritative preflight, project-isolation admission,
    # durable job commit, and daemon submission all happen while the same lock is held.
    async with serialize_cluster_configuration(db, request.host):
        try:
            # Instance migrations lock the whole project hierarchy because quotas, profiles,
            # networks, and volume attachments are part of their approved preflight. A create
            # under a different name would still drift that hierarchy.
            await assert_project_not_migration_fenced(
                cluster_id=request.host,
                project=request.project,
                db=db,
            )
            await assert_instance_not_migration_fenced(
                cluster_id=request.host,
                project=request.project,
                instance_name=request.name,
                db=db,
            )
        except MigrationFenceConflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        evaluation = await _run_create_preflight(request, lxd_service)
        preflight = evaluation.response
        _enforce_create_preflight(request, preflight)

        try:
            policy = await resolve_cluster_write_policy(_rbac, request.host, db)
            validate_target_project_isolation(
                policy, evaluation.target_project_config
            )
            validate_instance_payload(
                policy,
                config=evaluation.effective_config,
                devices=evaluation.effective_devices,
            )
        except InstanceSecurityPolicyViolation as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.as_detail()) from exc

        job_organization = await resolve_instance_job_organization(
            db,
            cluster_id=request.host,
            project=request.project,
        )

        try:
            job, created = await create_operation_job(
                kind="create",
                host=request.host,
                project=request.project,
                instance_name=request.name,
                requester=requester,
                target_organization_id=job_organization.organization_id,
                target_organization_slug=job_organization.organization_slug,
                target_organization_resolution=job_organization.resolution,
                idempotency_key=key,
                payload_hash=digest,
                requested=_safe_create_job_request(request),
                plan_revision=preflight.plan_revision,
                instance_type=request.instance_type,
            )
        except SpecificationConflict as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        if not created:
            return VMCreateResponse(
                success=True,
                message=f"Instance create job '{job.job_id}' already exists",
                vm=None,
                operation_id=job.operation_id,
                job_id=job.job_id,
                status="unknown" if job.status == "unknown" else "accepted",
            )

        # The accepted event was enqueued atomically with the durable job. Delivery is best
        # effort here; startup recovery uses the same stable event ID if this process exits.
        try:
            await drain_instance_operation_audits(job_id=job.job_id)
        except Exception as exc:  # noqa: BLE001 - startup recovery replays the outbox
            logger.warning(
                "Could not drain accepted audit for create job %s: %s",
                job.job_id,
                sanitize_error(str(exc)),
            )

        try:
            operation_id = await lxd_service.create_instance(
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
                job_id=job.job_id,
            )
        except CreateSubmissionUnknown as exc:
            logger.warning(
                "Create submission outcome is unknown for %s/%s/%s (job=%s): %s",
                request.host,
                request.project,
                request.name,
                job.job_id,
                sanitize_error(str(exc)),
            )
            return VMCreateResponse(
                success=True,
                message=(
                    "The cluster may have accepted the create. The durable job will reconcile "
                    "actual state; do not submit it again."
                ),
                vm=None,
                operation_id=exc.operation_id,
                job_id=job.job_id,
                status="unknown",
            )
        except LXDOperationError as exc:
            safe_error = sanitize_error(str(exc), limit=1000)
            logger.error("LXD operation error creating %s: %s", request.name, safe_error)
            await lxd_service.fail_create_job(
                job.job_id,
                error=safe_error,
                error_code="LXD_OPERATION_ERROR",
            )
            if isinstance(exc, LXDProjectLimitError):
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "PROJECT_LIMIT_REACHED",
                        "message": safe_error,
                        "limit": exc.hit.limit,
                        "project": exc.hit.project,
                        "max": exc.hit.maximum,
                    },
                )
            low = safe_error.lower()
            if "already exists" in low:
                status_code = 409
            elif "not found" in low:
                status_code = 404
            elif (
                "connect" in low
                or "not available" in low
                or "could not open project" in low
            ):
                status_code = 503
            else:
                status_code = 400
            raise HTTPException(status_code=status_code, detail=safe_error)
        except LXDConnectionError as exc:
            safe_error = sanitize_error(str(exc), limit=1000)
            logger.warning("LXD connection error creating %s: %s", request.name, safe_error)
            await lxd_service.fail_create_job(
                job.job_id,
                error=safe_error,
                error_code="LXD_CONNECTION_ERROR",
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "LXD_UNAVAILABLE",
                    "message": "The target cluster is unavailable. The create was not submitted.",
                    "job_id": job.job_id,
                },
            )
        except Exception as exc:
            safe_error = sanitize_error(str(exc), limit=1000)
            logger.error("Error creating instance %s: %s", request.name, safe_error)
            await lxd_service.fail_create_job(
                job.job_id,
                error=safe_error,
                error_code="UNEXPECTED_ERROR",
            )
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "CREATE_FAILED",
                    "message": "The instance create could not be submitted.",
                    "job_id": job.job_id,
                },
            )

        # The exact job watcher, not this accepted response, publishes instance.created and
        # invalidates cached inventory after operation terminal plus readback.
        return VMCreateResponse(
            success=True,
            message=f"Creating instance '{request.name}' in cluster '{request.host}'",
            vm=None,
            operation_id=operation_id,
            job_id=job.job_id,
        )


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
    # important: ONE project value is authorized and acted on. This checked the ACL against
    # `request.project or "default"` and then handed `request.project` to the service, which
    # searches EVERY project when it is None and returns the first hit. A caller holding
    # `operate` on `default` was therefore authorized there while the action ran against
    # whichever project LXD listed first, which on a shared cluster is another tenant's instance.
    project = verify_instance_view_access(request.host, request.project, instance_name)
    # important: enforce BEFORE the try - a 403 raised inside it would be swallowed and re-raised 500.
    if get_settings().tenant_perm_enforcement_enabled:
        verify_tenant_permission(request.host, project, instance_name, "operate")
    try:
        async with guard_instance_mutation(
            cluster_id=request.host,
            project=project,
            instance_name=instance_name,
        ):
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
            target_project=project,
            result="success"
        )
        
        return VMActionResponse(
            success=True,
            message=f"Instance {instance_name} started successfully",
            vm_name=instance_name,
            action="start",
            new_status=vm.status
        )
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
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
            target_project=project,
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
            target_project=project,
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
    # important: ONE project value is authorized and acted on. This checked the ACL against
    # `request.project or "default"` and then handed `request.project` to the service, which
    # searches EVERY project when it is None and returns the first hit. A caller holding
    # `operate` on `default` was therefore authorized there while the action ran against
    # whichever project LXD listed first, which on a shared cluster is another tenant's instance.
    project = verify_instance_view_access(request.host, request.project, instance_name)
    if get_settings().tenant_perm_enforcement_enabled:
        verify_tenant_permission(request.host, project, instance_name, "operate")
    try:
        async with guard_instance_mutation(
            cluster_id=request.host,
            project=project,
            instance_name=instance_name,
        ):
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
            target_project=project,
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
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
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
            target_project=project,
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
            target_project=project,
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
    # important: ONE project value is authorized and acted on. This checked the ACL against
    # `request.project or "default"` and then handed `request.project` to the service, which
    # searches EVERY project when it is None and returns the first hit. A caller holding
    # `operate` on `default` was therefore authorized there while the action ran against
    # whichever project LXD listed first, which on a shared cluster is another tenant's instance.
    project = verify_instance_view_access(request.host, request.project, instance_name)
    if get_settings().tenant_perm_enforcement_enabled:
        verify_tenant_permission(request.host, project, instance_name, "operate")
    try:
        async with guard_instance_mutation(
            cluster_id=request.host,
            project=project,
            instance_name=instance_name,
        ):
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
            target_project=project,
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
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
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
            target_project=project,
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
            target_project=project,
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
    host: str = Query(..., description="Target cluster"),
    project: Optional[str] = Query(None, description="LXD project name"),
    force: bool = Query(False, description="Force delete a running instance"),
    lxd_service: LXDService = Depends(get_lxd_service),
    tag_service: TagService = Depends(get_tag_service_dep),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Delete an instance."""
    project = verify_project_access(host, project)
    try:
        # important: everything below is stamped with the project the deletion ACTUALLY used.
        # A caller that named none was resolved across projects and then had its tags, webhook,
        # upstream event and audit row written against "default", naming a project the deletion
        # never touched.
        async with guard_instance_mutation(
            cluster_id=host,
            project=project,
            instance_name=instance_name,
        ):
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
            target_project=deleted_project,
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
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
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
            target_project=project or "default",
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
            target_project=project or "default",
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
    host: str = Query(..., description="Target cluster"),
    project: Optional[str] = Query(
        None,
        description=(
            "LXD project the instance lives in. A name is unique per project, not per cluster, so "
            "omitting this reads the instance of that name in `default`."
        ),
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get metrics for a specific instance."""
    scoped_project = verify_instance_view_access(host, project, instance_name)
    try:
        metrics = await lxd_service.get_instance_metrics(host, instance_name, scoped_project)
        if not metrics:
            raise HTTPException(
                status_code=404,
                detail=f"Instance {instance_name} not found in cluster '{host}'",
            )
        return metrics
    except LXDConnectionError as e:
        # The cluster did not answer. Without this the retyped exception falls to
        # `except Exception` below and answers 500, which pages an engineer for an
        # offline cluster.
        raise HTTPException(status_code=503, detail=str(e))
    except (BlockingTimeout, BlockingPoolFull) as e:
        logger.warning("Instance metrics temporarily unavailable: %s", e)
        raise HTTPException(status_code=503, detail="Instance metrics are temporarily unavailable")
    except HTTPException:
        raise
    except LXDOperationError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error getting metrics for {instance_name}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class NicAddressRequest(BaseModel):
    """Set or clear the fixed IPv4 on one network interface."""

    host: str = Field(..., description="Target cluster")
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

    try:
        # The device preflight and write share migration's cluster serialization boundary.
        # This closes the check-then-act window where a migration could commit its hierarchy
        # fence after the old standalone check but before the daemon PATCH.
        async with guard_instance_mutation(
            cluster_id=request.host,
            project=project,
            instance_name=instance_name,
        ):
            client = lxd_service.get_client_with_reconnect(request.host)
            if client is None:
                raise HTTPException(
                    status_code=503,
                    detail=f"Could not connect to cluster '{request.host}'",
                )
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
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
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
            target_project=project,
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
            target_project=project,
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
        target_project=project,
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
