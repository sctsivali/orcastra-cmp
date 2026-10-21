"""
Instance/VM operations module.
Handles create, start, stop, restart, delete, and list operations for instances.
"""

import logging
import asyncio
import time
import secrets
import socket
from typing import Callable, List, NamedTuple, Tuple, Optional, Dict, Any
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pylxd.exceptions import LXDAPIException, ClientConnectionFailed, NotFound
from requests import exceptions as requests_exc
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schemas import VMInfo
from app.models.db_models import InstanceOperationJob, Tag, TagAssignment, TagCategory
from app.services.lxd.nic_preflight import NicPreflightError, check_nics
from app.core.config import get_settings
from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.cache import get_cache_manager
from app.core.database import async_session_maker
from app.core.error_sanitizer import sanitize_error
from app.services.instance_operation_audit import (
    drain_instance_operation_audits,
    enqueue_instance_operation_audit,
)
from app.services.migration_fence import (
    assert_instance_not_migration_fenced,
    assert_project_not_migration_fenced,
)
from . import health
from .project_limits import project_limit_from_lxd_error
from .exceptions import (
    LXDConnectionError,
    LXDLocalUnavailableError,
    LXDLocalStorageError,
    LXDOperationError,
    LXDGracefulTimeoutError,
    LXDSnapshotExistsError,
    LXDProjectLimitError,
)
from .utils import (
    parse_vm_status,
    extract_ip_addresses,
    extract_ip_addresses_from_state,
    image_source_key,
    find_instance_in_projects,
    jsonable_lxd,
    normalize_cluster_member,
)
from .resources import ResourceManager
from .connection import LXDConnectionManager
from .project_scope import list_across_projects, list_in_project
from .audit import audit_operation
from app.core.lxd_path import safe_segment

logger = logging.getLogger(__name__)
_NO_CLIENT_OVERRIDE = object()
_CREATE_EVENT_TIMEOUT_SECONDS = 2.0


def _announce_instance_created(host: str, project: str, name: str) -> None:
    """Tell OrcaHub an instance exists, once it actually does.

    important: paired with the add-on event above it and emitted for the same reason. The router
    announced this the moment LXD accepted the request, so a create LXD then refused still told
    OrcaHub an instance existed, with no correction to follow. On a cluster owned by two
    organisations it said so twice. Proven live: a create that failed with "Image not provided
    for instance creation" emitted instance.created to org 122 and org 6 seventy milliseconds
    later.

    Fire-and-forget by contract, so a webhook problem cannot fail a create that worked.
    """
    from app.services.orcahub_webhook import schedule_orcahub_event

    schedule_orcahub_event("instance.created", host, project, name)


async def _emit_instance_created(
    host: str,
    project: str,
    name: str,
    operation_id: Optional[str],
) -> None:
    """Bound optional notification work so it cannot strand a verified create."""
    try:
        from app.services.webhooks.emitter import emit_resource_event

        await asyncio.wait_for(
            emit_resource_event(
                event_type="orcastra.instance.created.v1",
                cluster_id=host,
                resource_type="instance",
                resource_name=name,
                project=project,
                operation_id=operation_id,
            ),
            timeout=_CREATE_EVENT_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - notification cannot rewrite daemon truth
        logger.warning(
            "Could not enqueue the instance-created event for %s/%s/%s: %s",
            host,
            project,
            name,
            _create_error(exc),
        )
    try:
        _announce_instance_created(host, project, name)
    except Exception as exc:  # noqa: BLE001 - optional notification only
        logger.warning(
            "Could not schedule the OrcaHub create event for %s/%s/%s: %s",
            host,
            project,
            name,
            _create_error(exc),
        )

# The pooled client is built for fast failure detection (3s connect / 8s read).
# That read timeout is shorter than the graceful-shutdown waits callers ask for,
# so the HTTP read would expire before LXD's own operation timeout -- making the
# caller's `timeout` argument meaningless and surfacing a raw connection error
# instead of a clean result. Stop/restart therefore use a client whose read
# timeout outlives the requested wait by this margin.
_GRACEFUL_READ_MARGIN_SECONDS = 30
# How long a start may hold the HTTP response open. The LXD operation itself is usually
# quick, but a loaded cluster is not, and the pooled client only allows an 8s read.
_START_WAIT_SECONDS = 30

# Hard ceiling on the wait a caller can request, mirroring VMActionRequest's
# `le=600` bound. Clamped again here so any internal caller that bypasses the
# request schema still can't hold an LXD operation request (and its worker) open
# for an unbounded time.
_MAX_ACTION_TIMEOUT_SECONDS = 600

# Connect leg for the raw rebuild calls. Kept separate from the (long) read leg because
# requests applies a scalar timeout to both, and nothing justifies a multi-minute budget for
# completing a TCP+TLS handshake.
_REBUILD_CONNECT_TIMEOUT = 5

# A rebuild holds its worker for as long as LXD takes to reinstall the root disk, up to
# wait_timeout (default 1800s). asyncio.to_thread would put that on the loop's DEFAULT
# executor, which is also where FastAPI runs every sync route handler -- a handful of
# concurrent rebuilds would starve unrelated requests. Own pool, and shed past it rather
# than queue.
# A delete tears down the root disk, which on a `dir` pool is a full recursive remove. The pooled
# client's 8 second read is far short of that, so the wait gets its own window.
_DELETE_WAIT_SECONDS = 300

_rebuild_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lxd_rebuild")
_REBUILD_MAX_IN_FLIGHT = 8

# POST /vms is intentionally synchronous from the caller's perspective, but pylxd's
# connect/create(wait=True)/start/readback sequence is blocking. Keep it off the event loop and
# refuse excess work instead of building an unbounded 30-minute queue behind a slow image pull.
_CREATE_LXD_READ_TIMEOUT_SECONDS = 1800
_CREATE_WALL_TIMEOUT_SECONDS = (
    _CREATE_LXD_READ_TIMEOUT_SECONDS
    + _START_WAIT_SECONDS
    + _GRACEFUL_READ_MARGIN_SECONDS
)
_create_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lxd_create")
_CREATE_MAX_IN_FLIGHT = 4
_CREATE_MAX_IN_FLIGHT_PER_CLUSTER = 2
_CREATE_CONNECT_TIMEOUT_SECONDS = 20.0
_CREATE_SUBMIT_TIMEOUT_SECONDS = 45.0
_CREATE_READBACK_TIMEOUT_SECONDS = 35.0
# The heartbeat keeps the lease fresh during the longest bounded create wait. A crashed worker is
# reclaimable within five minutes, while ownership checks immediately before start and at terminal
# CAS prevent a delayed worker from double-starting or overwriting the recovery worker's verdict.
_CREATE_LEASE_SECONDS = 5 * 60
_CREATE_HEARTBEAT_SECONDS = 30
_CREATE_RECOVERY_GRACE_SECONDS = 30
_CREATE_ACTIVE_STATUSES = ("pending", "running", "unknown")
_CREATE_TERMINAL_STATUSES = ("success", "failure", "partial")
_CREATE_TAG_CATEGORY_PREFIXES = (
    "loc_",
    "reg_",
    "vend_",
    "own_",
    "env_",
    "app_",
    "sla_",
    "cost_",
)


class CreateSubmissionUnknown(LXDOperationError):
    """The create POST may have reached LXD, so a retry could duplicate the mutation."""

    def __init__(
        self,
        message: str,
        *,
        job_id: Optional[str] = None,
        operation_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.operation_id = operation_id


def _create_worker_id() -> str:
    return f"create-{secrets.token_hex(12)}"


def _create_error(exc: BaseException) -> str:
    return sanitize_error(str(exc), limit=1000) or type(exc).__name__


def _ambiguous_create_error(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            BlockingTimeout,
            ClientConnectionFailed,
            requests_exc.ConnectionError,
            requests_exc.Timeout,
            socket.timeout,
            TimeoutError,
        ),
    )


def _preserve_create_ambiguity(
    row: InstanceOperationJob,
    fallback: str,
) -> Dict[str, Any]:
    result = getattr(row, "result", None)
    if isinstance(result, dict) and result.get("ambiguous_action") in {"create", "start"}:
        return dict(result)
    return {"ambiguous_action": fallback}


def _requested_create_tags(row: InstanceOperationJob) -> list[str]:
    """Return the normalized, non-secret tag intent stored with a durable create."""
    requested = row.requested if isinstance(row.requested, dict) else {}
    raw_tags = requested.get("tags", [])
    if not isinstance(raw_tags, list) or any(
        not isinstance(tag, str) or not tag for tag in raw_tags
    ):
        raise ValueError("Durable create job contains invalid normalized tags")
    return sorted(set(raw_tags))


def _create_tag_display_name(tag: str) -> str:
    prefix = next(
        (candidate for candidate in _CREATE_TAG_CATEGORY_PREFIXES if tag.startswith(candidate)),
        None,
    )
    value = tag[len(prefix) :] if prefix else tag
    return value.replace("-", " ").replace("_", " ").title()


async def _upsert_materialized_create_tags(
    db: AsyncSession,
    row: InstanceOperationJob,
    tags: list[str],
) -> None:
    """Persist verified create tags inside the terminal job transaction.

    This deliberately uses the supplied session rather than ``TagService``. The latter commits
    internally and writes to LXD, either of which would split the tag assignment from the
    terminal CAS. LXD already received the canonical ``user.tags`` value in the create payload.
    """
    if not tags:
        return

    category_rows = (
        await db.execute(
            select(TagCategory.prefix, TagCategory.id).where(
                TagCategory.prefix.in_(_CREATE_TAG_CATEGORY_PREFIXES)
            )
        )
    ).all()
    category_ids = {prefix: category_id for prefix, category_id in category_rows}
    now = datetime.now(timezone.utc)

    for tag_name in tags:
        prefix = next(
            (
                candidate
                for candidate in _CREATE_TAG_CATEGORY_PREFIXES
                if tag_name.startswith(candidate)
            ),
            None,
        )
        inserted_tag_id = (
            await db.execute(
                pg_insert(Tag)
                .values(
                    name=tag_name,
                    display_name=_create_tag_display_name(tag_name),
                    category_id=category_ids.get(prefix),
                    is_system=False,
                    usage_count=0,
                    created_by=row.requester,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=[Tag.name])
                .returning(Tag.id)
            )
        ).scalar_one_or_none()
        tag_id = inserted_tag_id
        if tag_id is None:
            tag_id = (
                await db.execute(select(Tag.id).where(Tag.name == tag_name))
            ).scalar_one_or_none()
        if tag_id is None:
            raise RuntimeError(f"Tag '{tag_name}' could not be resolved after upsert")

        assignment_id = (
            await db.execute(
                pg_insert(TagAssignment)
                .values(
                    tag_id=tag_id,
                    resource_type="instance",
                    resource_id=row.instance_name,
                    resource_host=row.cluster_id,
                    resource_project=row.project,
                    assigned_by=row.requester,
                    assigned_at=now,
                )
                .on_conflict_do_nothing(constraint="uq_tag_assignment_resource")
                .returning(TagAssignment.id)
            )
        ).scalar_one_or_none()
        if assignment_id is not None:
            await db.execute(
                update(Tag)
                .where(Tag.id == tag_id)
                .values(usage_count=func.coalesce(Tag.usage_count, 0) + 1)
            )


async def _load_create_job(job_id: str) -> Optional[InstanceOperationJob]:
    async with async_session_maker() as db:
        return (
            await db.execute(
                select(InstanceOperationJob).where(
                    InstanceOperationJob.job_id == job_id,
                    InstanceOperationJob.kind == "create",
                )
            )
        ).scalar_one_or_none()


async def _claim_create_job(
    job_id: str,
    worker_id: str,
    *,
    include_unknown: bool = False,
) -> bool:
    """Claim one active create job without stealing a healthy worker's lease."""
    now = datetime.now(timezone.utc)
    stale = now - timedelta(seconds=_CREATE_LEASE_SECONDS)
    statuses = _CREATE_ACTIVE_STATUSES if include_unknown else ("pending", "running")
    async with async_session_maker() as db:
        result = await db.execute(
            update(InstanceOperationJob)
            .where(
                InstanceOperationJob.job_id == job_id,
                InstanceOperationJob.kind == "create",
                InstanceOperationJob.status.in_(statuses),
                or_(
                    InstanceOperationJob.locked_at.is_(None),
                    InstanceOperationJob.locked_at < stale,
                ),
            )
            .values(locked_by=worker_id, locked_at=now, updated_at=now)
        )
        await db.commit()
        return bool(result.rowcount)


async def _set_create_job(
    job_id: str,
    *,
    worker_id: Optional[str] = None,
    **values: Any,
) -> bool:
    """Update bookkeeping only. Cache invalidation belongs to verified state changes."""
    values["updated_at"] = datetime.now(timezone.utc)
    predicates = (
        InstanceOperationJob.job_id == job_id,
        InstanceOperationJob.kind == "create",
        InstanceOperationJob.status.in_(_CREATE_ACTIVE_STATUSES),
    )
    if worker_id is not None:
        predicates = (*predicates, InstanceOperationJob.locked_by == worker_id)
    async with async_session_maker() as db:
        operation_id = values.get("operation_id")
        if operation_id:
            row = (
                await db.execute(
                    select(InstanceOperationJob)
                    .where(*predicates)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            operation_ids = list(getattr(row, "operation_ids", None) or [])
            if operation_id not in operation_ids:
                operation_ids.append(operation_id)
            values["operation_ids"] = operation_ids
        result = await db.execute(
            update(InstanceOperationJob).where(*predicates).values(**values)
        )
        await db.commit()
        return bool(result.rowcount)


async def _create_lease_heartbeat(job_id: str, worker_id: str) -> None:
    """Keep a live worker's lease fresh while bounded daemon waits run in another thread."""
    while True:
        await asyncio.sleep(_CREATE_HEARTBEAT_SECONDS)
        try:
            renewed = await _set_create_job(
                job_id,
                worker_id=worker_id,
                locked_at=datetime.now(timezone.utc),
            )
        except Exception as exc:  # noqa: BLE001 - fencing still protects later mutations
            logger.warning(
                "Could not renew durable create lease for %s: %s",
                job_id,
                _create_error(exc),
            )
            continue
        if not renewed:
            return


async def _stop_create_heartbeat(task: Optional[asyncio.Task[Any]]) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _mark_create_unknown(
    row: InstanceOperationJob,
    *,
    operation_id: Optional[str],
    error: str,
    result: Optional[Dict[str, Any]] = None,
) -> bool:
    """Release the lease for reconciliation; unknown is not a terminal audit verdict."""
    return await _set_create_job(
        row.job_id,
        worker_id=getattr(row, "locked_by", None),
        phase="unknown",
        status="unknown",
        operation_id=operation_id or row.operation_id,
        result=result,
        error=sanitize_error(error, limit=1000),
        locked_by=None,
        locked_at=None,
        finished_at=None,
    )


async def _finish_create_job(
    row: InstanceOperationJob,
    status: str,
    *,
    operation_id: Optional[str],
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    invalidate_cache: bool = False,
    announce_created: bool = False,
) -> bool:
    """Write one terminal verdict and emit side effects only for the CAS winner."""
    if status not in _CREATE_TERMINAL_STATUSES:
        raise ValueError(f"Unsupported create terminal status: {status}")
    now = datetime.now(timezone.utc)
    safe_error = sanitize_error(error or "", limit=1000) or None
    final_operation_id = operation_id or row.operation_id
    materialized = announce_created and status in {"success", "partial"}
    materialized_tags: list[str] = []
    async with async_session_maker() as db:
        try:
            predicates = (
                InstanceOperationJob.job_id == row.job_id,
                InstanceOperationJob.kind == "create",
                InstanceOperationJob.status.in_(_CREATE_ACTIVE_STATUSES),
            )
            if owner := getattr(row, "locked_by", None):
                predicates = (*predicates, InstanceOperationJob.locked_by == owner)
            persisted = (
                await db.execute(
                    select(InstanceOperationJob)
                    .where(*predicates)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if persisted is None:
                return False
            operation_ids = list(getattr(persisted, "operation_ids", None) or [])
            if final_operation_id and final_operation_id not in operation_ids:
                operation_ids.append(final_operation_id)
            update_result = await db.execute(
                update(InstanceOperationJob).where(*predicates).values(
                    phase=status,
                    status=status,
                    operation_id=final_operation_id,
                    operation_ids=operation_ids,
                    result=result,
                    error=safe_error,
                    locked_by=None,
                    locked_at=None,
                    updated_at=now,
                    finished_at=now,
                )
            )
            if not update_result.rowcount:
                await db.rollback()
                return False

            if materialized:
                materialized_tags = _requested_create_tags(persisted)
                await _upsert_materialized_create_tags(db, persisted, materialized_tags)

            enqueue_instance_operation_audit(
                db,
                job_id=row.job_id,
                stage="terminal",
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    if invalidate_cache or materialized_tags:
        try:
            cache = get_cache_manager()
            if invalidate_cache:
                cache.invalidate_instance_cache()
            if materialized_tags:
                cache.invalidate_tag_cache()
        except Exception as exc:  # noqa: BLE001 - cache failure cannot change the verdict
            logger.warning(
                "Could not invalidate caches after create job %s: %s",
                row.job_id,
                _create_error(exc),
            )

    try:
        await drain_instance_operation_audits(job_id=row.job_id)
    except Exception as exc:  # noqa: BLE001 - the outbox will be replayed
        logger.warning(
            "Could not drain terminal audit for create job %s: %s",
            row.job_id,
            _create_error(exc),
        )

    if materialized:
        await _emit_instance_created(
            row.cluster_id,
            row.project,
            row.instance_name,
            final_operation_id,
        )
    return True


def _is_graceful_timeout(exc: Exception) -> bool:
    """True if `exc` means the guest never completed a graceful shutdown.

    Because `_rebind_for_wait` gives the client a read timeout of
    ``timeout + _GRACEFUL_READ_MARGIN_SECONDS``, the HTTP read always outlives
    LXD's own operation deadline, so the failure surfaces as LXD's message --
    'Failed shutting down instance, status is "Running": context deadline
    exceeded' -- and never as a raw HTTP read timeout. We therefore match only
    those two guest-side shapes. Matching the bare substring "timed out" would
    misclassify connection failures ('Connection to <ip> timed out', 'Connection
    pool timed out ...') as a guest ACPI failure and wrongly tell the operator to
    retry with force during a connectivity outage.
    """
    msg = str(exc).lower()
    return (
        "context deadline exceeded" in msg
        or "failed shutting down instance" in msg
    )


def _lxd_snapshot_names(client, name: str, project: str) -> List[str]:
    """List an instance's snapshot names via the raw LXD API.

    find_instance_in_projects returns an InstanceWrapper (raw-API backed), which
    has no pylxd `.snapshots`, so query GET /instances/{name}/snapshots directly.
    """
    try:
        resp = client.api.instances[safe_segment(name)].snapshots.get(params={"project": project})
        meta = resp.json().get("metadata") or []
        return [str(u).rstrip("/").rsplit("/", 1)[-1] for u in meta]
    except Exception as e:  # noqa: BLE001 - absent/uncreatable snapshots -> treat as none
        logger.warning("Could not list snapshots for %s: %s", name, e)
        return []

# Instance inventory and detail reads are synchronous all the way down: the cold path reads
# Vault, reconnects and validates a pylxd client, and the detail projection may call /state.
# Keep those paths off the event loop and give each one an explicit wall-clock and concurrency
# budget. The underlying transports already carry their socket bounds: Vault point reads use
# (2s, 3s), its registry LIST uses (3s, 10s), pooled LXD reads use (3s, 8s), and connection
# validation uses (3s, 5s). The wall budgets below outlive those sockets while still refusing a
# stalled or overloaded upstream promptly.
_INSTANCE_REGISTRY_TIMEOUT_SECONDS = 15.0
_INSTANCE_NODE_TIMEOUT_SECONDS = 30.0


class FleetInventory(NamedTuple):
    vms: List[VMInfo]
    connected: List[str]
    incomplete: List[str]
    # cluster -> "inventory" (its read ran past the wall) or "busy" (the pool was full)
    timed_out: Dict[str, str]
_INSTANCE_DETAIL_TIMEOUT_SECONDS = 30.0
_INSTANCE_INVENTORY_WORKERS = 20
_INSTANCE_DETAIL_WORKERS = 8
# A scoped state read may retry once without state. The two socket attempts use at most 9 of the
# worker's 30 seconds, leaving 21 seconds for response parsing, mapping, scheduling overhead and
# thread cancellation lag. Otherwise the request can return while its worker remains occupied.
_SCOPED_INSTANCE_PRELUDE_RESERVE_SECONDS = 21.0
_SCOPED_INSTANCE_STATE_READ_TIMEOUT = (2, 4)
_SCOPED_INSTANCE_LIST_READ_TIMEOUT = (1, 2)
_instance_inventory_executor = ThreadPoolExecutor(
    max_workers=_INSTANCE_INVENTORY_WORKERS,
    thread_name_prefix="instance_inventory_",
)
_instance_detail_executor = ThreadPoolExecutor(
    max_workers=_INSTANCE_DETAIL_WORKERS,
    thread_name_prefix="instance_detail_",
)

# Failed-node skip is delegated to the shared circuit breaker
# (app.services.lxd.health) so every connection path skips the same dead cluster.


# note: LXD reports the literal string "none" for an instance on a non-clustered target, not an
# absent value. Passing that through means the UI prints "none" in a Cluster member field, which
# reads as a member called none rather than as "this target is not clustered". Normalised once
# here so neither consumer has to know.
# Kept as a local name: the helper is shared with the GPU inventory (see utils).
_cluster_member = normalize_cluster_member


def _vm_info_from_row(
    row: Dict[str, Any],
    node_name: str,
    node_endpoint: str,
    *,
    read_at: Optional[float] = None,
) -> VMInfo:
    """One record of an all-projects, recursion=2 instance list to VMInfo.

    `project` is what the daemon stamped on the row (list_across_projects refuses rows
    without it); `state` is inline, so the addresses come from it without a second call, and
    a stopped instance's empty state yields none. `resources` stays unset on the list path.
    """
    config_data = row.get("config") or {}
    expanded_config = row.get("expanded_config")
    expanded_devices = row.get("expanded_devices")
    tags_str = config_data.get("user.tags", "")
    tags = [t.strip() for t in tags_str.split(",") if t.strip()] if tags_str else []
    live_state = _live_state(row, read_at)
    return VMInfo(
        name=row.get("name", ""),
        status=parse_vm_status(row.get("status", "Unknown"), row.get("status_code", 0)),
        status_code=row.get("status_code", 0),
        ip_addresses=extract_ip_addresses_from_state(row.get("state") or {}),
        host=node_name,
        host_endpoint=node_endpoint,
        architecture=row.get("architecture", ""),
        created_at=row.get("created_at"),
        last_used_at=row.get("last_used_at"),
        description=row.get("description", ""),
        type=row.get("type", "container"),
        resources=None,
        config=config_data,
        devices=row.get("devices") or {},
        expanded_devices=expanded_devices or {},
        effective_cpu_limit=(
            expanded_config.get("limits.cpu")
            if isinstance(expanded_config, dict)
            else None
        ),
        effective_memory_limit=(
            expanded_config.get("limits.memory")
            if isinstance(expanded_config, dict)
            else None
        ),
        effective_limits_known=isinstance(expanded_config, dict),
        expanded_devices_known=isinstance(expanded_devices, dict),
        profiles=row.get("profiles", []),
        tags=tags,
        project=row.get("project") or "default",
        location=_cluster_member(row.get("location")),
        live_state=live_state,
        live_state_read_at=read_at if live_state is not None else None,
    )


def _live_state(row: Dict[str, Any], read_at: Optional[float]) -> Optional[Dict[str, Any]]:
    """The inline state of a running row from a timed read, else None."""
    state = row.get("state")
    if read_at is None or row.get("status") != "Running" or not isinstance(state, dict):
        return None
    return state


def _list_instances_in_project(
    client,
    project: str,
    recursion: int,
    *,
    request_timeout,
) -> List[Dict[str, Any]]:
    """List one project, treating a missing project as a complete empty answer."""
    try:
        return list_in_project(
            client,
            "instances",
            project,
            recursion=recursion,
            request_timeout=request_timeout,
        )
    except (LXDAPIException, requests_exc.HTTPError) as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(exc, NotFound) or status_code == 404:
            return []
        raise


# Config keys some LXD versions refuse outright for a given instance type, mapped to the first
# major version known to refuse them.
#
# `security.secureboot` is the one that matters and it is genuinely two-sided, which is why it
# is filtered here rather than defaulted away in the form:
#
#   * LXD 5.x accepts it, and community VM images (Alpine, the daily Ubuntu builds) are not
#     Secure Boot signed, so WITHOUT `security.secureboot=false` they create fine and then
#     refuse to start: "The image used by this instance is incompatible with secureboot".
#     Verified live on 5.21.7. Only signed release images carry
#     `image.requirements.secureboot=false` and cope on their own.
#   * LXD 6.9 answers `"security.secureboot" is not supported for "virtual-machine"` and
#     refuses the create outright.
#
# So neither always-send nor never-send is correct, and the right answer depends on the target
# cluster. That is knowledge about the cluster, so it lives here, next to the client that knows
# which cluster it is, rather than in every caller that builds a config.
_KEYS_REFUSED_FROM_MAJOR = {
    "security.secureboot": 6,
}


def _server_major(client) -> Optional[int]:
    """Major version of the LXD this client is talking to, or None when unknown.

    pylxd caches the /1.0 body on the client as host_info, and _validate_connection refreshes
    it on every pooled revalidation, so this is free.
    """
    try:
        version = ((getattr(client, "host_info", None) or {})
                   .get("environment", {})
                   .get("server_version", ""))
        return int(str(version).split(".", 1)[0])
    except (AttributeError, IndexError, TypeError, ValueError):
        return None


def _drop_unsupported_config(client, instance_type: str, config: dict) -> dict:
    """Remove config keys the target LXD is known to refuse, leaving everything else alone.

    important: fail-safe direction is to KEEP the key. An unknown or unparseable version
    behaves like 5.x, which is the behaviour verified against a live cluster; dropping on
    uncertainty would silently reintroduce the unbootable-community-image case.
    """
    if instance_type == "container" or not config:
        return config
    major = _server_major(client)
    if major is None:
        return config
    kept = {}
    for key, value in config.items():
        refused_from = _KEYS_REFUSED_FROM_MAJOR.get(key)
        if refused_from is not None and major >= refused_from:
            logger.info(
                "Dropping %s: LXD %s refuses it for a virtual-machine, and sending it fails "
                "the create outright.", key, major,
            )
            continue
        kept[key] = value
    return kept


class InstanceManager:
    """
    Manages LXD instance operations.
    """
    
    def __init__(self, connection_manager: LXDConnectionManager, resource_manager: ResourceManager, cert_manager=None):
        self.connection_manager = connection_manager
        self.resource_manager = resource_manager
        self.cert_manager = cert_manager
        # Strong refs to fire-and-forget background tasks (e.g. auto-start after
        # create) so the event loop doesn't garbage-collect them mid-flight.
        self._bg_tasks: set = set()
    
    def _get_client_with_reconnect(self, node_name: str):
        """Get LXD client with automatic reconnection if stale.
        Respects org-scope: returns None for clusters outside user's org.
        """
        # Org-scope guard: prevent reconnecting to clusters outside the user's org
        from app.core.org_scope import get_current_allowed_clusters
        allowed = get_current_allowed_clusters()
        if allowed is not None and node_name not in allowed:
            return None

        client = self.connection_manager.get_client(node_name)
        
        # If client is None (stale connection), try to reconnect via cert_manager
        if client is None and self.cert_manager:
            logger.info(f"Connection to {node_name} is stale, attempting reconnect...")
            try:
                # Pass connection_manager for proper pooling
                client = self.cert_manager.connect_with_vault_cert(
                    node_name, 
                    connection_manager=self.connection_manager
                )
                logger.info(f"Successfully reconnected to {node_name}")
            except Exception as e:
                logger.warning(f"Reconnect failed for {node_name}: {e}")
                client = None
        
        return client
    
    def _get_node_endpoint(self, node_name: str) -> str:
        """Get node endpoint from Vault metadata with caching."""
        try:
            settings = get_settings()
            if self.cert_manager and self.cert_manager.vault:
                # Cache is automatically used in vault_read
                metadata = self.cert_manager.vault.vault_read(
                    f"{settings.vault_clusters_path}/{node_name}/metadata",
                    use_cache=True  # Explicitly enable cache
                )
                return metadata.get("endpoint", "unknown")
        except Exception as e:
            logger.warning(f"Failed to get endpoint for {node_name} from Vault: {e}")
        return "unknown"
    
    def _instance_to_vm_info(
        self, 
        instance, 
        node_name: str, 
        node_endpoint: str
    ) -> VMInfo:
        """Convert pylxd instance to VMInfo model."""
        # Parse tags from config
        tags_str = instance.config.get("user.tags", "")
        tags = [tag.strip() for tag in tags_str.split(",") if tag.strip()] if tags_str else []
        
        return VMInfo(
            name=instance.name,
            status=parse_vm_status(instance.status, instance.status_code),
            status_code=instance.status_code,
            ip_addresses=extract_ip_addresses(instance),
            host=node_name,
            host_endpoint=node_endpoint,
            project=getattr(instance, 'project', 'default'),
            architecture=instance.architecture,
            created_at=instance.created_at,
            last_used_at=instance.last_used_at,
            description=instance.description,
            type=instance.type,
            resources=self.resource_manager.get_resource_usage(instance),
            config=dict(instance.config) if instance.config else None,
            devices=jsonable_lxd(getattr(instance, "devices", None)) or {},
            expanded_devices=jsonable_lxd(getattr(instance, "expanded_devices", None)) or {},
            effective_cpu_limit=(
                (getattr(instance, "expanded_config", None) or {}).get("limits.cpu")
            ),
            effective_memory_limit=(
                (getattr(instance, "expanded_config", None) or {}).get("limits.memory")
            ),
            effective_limits_known=isinstance(
                getattr(instance, "expanded_config", None), dict
            ),
            expanded_devices_known=isinstance(
                getattr(instance, "expanded_devices", None), dict
            ),
            profiles=list(instance.profiles) if instance.profiles else [],
            tags=tags,
            location=_cluster_member(getattr(instance, "location", None)),
        )
    
    async def list_all_vms(self) -> Tuple[List[VMInfo], List[str]]:
        """List all instances while preserving the established two-value contract."""
        vms, connected, _incomplete = await self.list_all_vms_with_failures()
        return vms, connected

    def _inventory_node_names_blocking(self) -> List[str]:
        """Read the fleet registry from Vault, with the process registry as fallback."""
        if self.cert_manager:
            try:
                node_names = self.cert_manager.list_clusters()
                logger.debug("Found %s clusters in Vault: %s", len(node_names), node_names)
                return node_names
            except Exception as exc:
                logger.warning("Could not list Vault clusters: %s", exc)
        return self.connection_manager.get_node_names()

    def _fetch_inventory_node(
        self,
        node_name: str,
    ) -> Tuple[List[VMInfo], bool, bool]:
        """Read endpoint metadata and instance state in the same bounded worker."""
        endpoint = self._get_node_endpoint(node_name)
        return self._fetch_vms_from_node_detailed(node_name, endpoint)

    async def _fetch_inventory_node_bounded(
        self,
        node_name: str,
    ) -> Tuple[List[VMInfo], bool, bool]:
        return await run_blocking(
            self._fetch_inventory_node,
            node_name,
            timeout=_INSTANCE_NODE_TIMEOUT_SECONDS,
            label=f"instance.inventory[{node_name}]",
            executor=_instance_inventory_executor,
            max_in_flight=_INSTANCE_INVENTORY_WORKERS,
        )

    def _fetch_project_inventory_node(
        self,
        node_name: str,
        project: str,
    ) -> Tuple[List[VMInfo], bool, bool]:
        """Read one exact project on one cluster in a bounded inventory worker.

        The scoped list itself is the liveness probe, so do not precede it with ``get_client``'s
        validation/reconnect requests or start the second Vault-backed reconnect path. Those calls
        have independent socket budgets that can outlive this worker's wall cap. The background
        healer owns a genuinely cold worker; this read fails fast and is never cached, so Refresh
        can use the repaired connection.
        """
        client = self.connection_manager.get_cached_client(node_name)
        if client is None:
            if health.known_unreachable(node_name):
                raise LXDConnectionError("Instance inventory is temporarily unavailable")
            raise LXDLocalUnavailableError("Instance inventory is temporarily unavailable")
        endpoint = self._get_node_endpoint(node_name)
        return self._fetch_vms_from_node_detailed(
            node_name,
            endpoint,
            project=project,
            allow_vault_reconnect=False,
            client_override=client,
        )

    async def list_vms_in_project_with_failures(
        self,
        node_name: str,
        project: str,
    ) -> Tuple[List[VMInfo], List[str], List[str]]:
        """List one cluster/project without consulting or fanning out to the fleet registry."""
        try:
            vms, connected, complete = await run_blocking(
                self._fetch_project_inventory_node,
                node_name,
                project,
                timeout=_INSTANCE_NODE_TIMEOUT_SECONDS,
                label=f"instance.inventory[{node_name}/{project}]",
                executor=_instance_inventory_executor,
                max_in_flight=_INSTANCE_INVENTORY_WORKERS,
            )
        except (BlockingTimeout, BlockingPoolFull) as exc:
            raise LXDConnectionError(
                "Instance inventory is temporarily unavailable"
            ) from exc

        connected_nodes = [node_name] if connected else []
        incomplete_nodes = [node_name] if not connected or not complete else []
        return vms if connected else [], connected_nodes, incomplete_nodes

    async def _inventory_registry(self) -> List[str]:
        """The registered clusters this caller's scope may read. Raises when the registry itself
        cannot be read in time, which no partial answer can stand in for."""
        try:
            node_names = await run_blocking(
                self._inventory_node_names_blocking,
                timeout=_INSTANCE_REGISTRY_TIMEOUT_SECONDS,
                label="instance.inventory.registry",
                executor=_instance_inventory_executor,
                max_in_flight=_INSTANCE_INVENTORY_WORKERS,
            )
        except (BlockingTimeout, BlockingPoolFull) as exc:
            raise LXDConnectionError(
                "Instance inventory is temporarily unavailable"
            ) from exc

        from app.core.org_scope import apply_cluster_filter
        return apply_cluster_filter(node_names)

    async def _inventory_fan_out(
        self, node_names: List[str]
    ) -> Tuple[List[VMInfo], List[str], List[str], Dict[str, Exception]]:
        """Every cluster's instances, plus the clusters whose read hit the wall or a full pool."""
        all_vms: List[VMInfo] = []
        connected_nodes: List[str] = []
        incomplete_nodes: List[str] = []
        capacity_errors: Dict[str, Exception] = {}

        # One request must not saturate its own executor when the fleet has more clusters than
        # workers. Batches retain parallel fan-out while leaving the global in-flight cap honest
        # across concurrent organization scopes.
        for start in range(0, len(node_names), _INSTANCE_INVENTORY_WORKERS):
            batch = node_names[start : start + _INSTANCE_INVENTORY_WORKERS]
            results = await asyncio.gather(
                *(self._fetch_inventory_node_bounded(node_name) for node_name in batch),
                return_exceptions=True,
            )
            for node_name, result in zip(batch, results):
                if isinstance(result, Exception):
                    logger.warning("Error fetching VMs from %s: %s", node_name, result)
                    incomplete_nodes.append(node_name)
                    if isinstance(result, (BlockingTimeout, BlockingPoolFull)):
                        capacity_errors[node_name] = result
                    continue

                vms, connected, complete = result
                if connected:
                    connected_nodes.append(node_name)
                    all_vms.extend(vms)
                if not connected or not complete:
                    incomplete_nodes.append(node_name)
        return all_vms, connected_nodes, incomplete_nodes, capacity_errors

    async def list_all_vms_with_failures(
        self,
    ) -> Tuple[List[VMInfo], List[str], List[str]]:
        """
        List all VMs across all configured LXD nodes and all projects.
        Returns (vm_list, connected_nodes, incomplete_nodes). ``incomplete_nodes`` names every
        registered cluster that did not produce a complete upstream answer, including
        unreachable clusters, so callers do not need a second synchronous Vault read. Local
        executor timeout or saturation is raised as service unavailability instead.
        """
        node_names = await self._inventory_registry()
        if not node_names:
            logger.warning("No nodes found in Vault or connection manager")
            return [], [], []

        all_vms, connected_nodes, incomplete_nodes, capacity_errors = await self._inventory_fan_out(
            node_names
        )

        # Ordinary cluster failures return (connected=False) and remain a useful partial answer.
        # A wall timeout or saturated executor means this backend could not finish the bounded
        # read at all; do not cache that capacity incident as if the named cluster were offline.
        if capacity_errors:
            raise LXDConnectionError(
                "Instance inventory is temporarily unavailable"
            ) from next(iter(capacity_errors.values()))

        logger.info(f"Fetched {len(all_vms)} VMs from {len(connected_nodes)} nodes")
        return all_vms, connected_nodes, incomplete_nodes

    async def read_fleet_inventory(self) -> FleetInventory:
        """The fleet's instances for the monitoring sweep, which reports rather than raises.

        important: a cluster whose read ran past the wall, or found the pool full, is named in
        ``timed_out`` and the other clusters' answer stands. One hung cluster used to make the
        list above raise for the whole fleet, so the dashboard answered 503 and wrote no history
        point. Only a registry that cannot be read still raises.
        """
        node_names = await self._inventory_registry()
        if not node_names:
            return FleetInventory([], [], [], {})
        all_vms, connected_nodes, incomplete_nodes, capacity_errors = await self._inventory_fan_out(
            node_names
        )
        timed_out = {
            name: "busy" if isinstance(error, BlockingPoolFull) else "inventory"
            for name, error in capacity_errors.items()
        }
        return FleetInventory(all_vms, connected_nodes, incomplete_nodes, timed_out)
    
    def fetch_node_instances(
        self, node_name: str, node_endpoint: str = ""
    ) -> Tuple[List[VMInfo], bool]:
        """One node's instances across every project, for a caller that must not fan out.

        important: this exists so the API-key surface can read instances without going through
        ``list_all_vms``. An API-key request establishes no org scope, so
        ``get_current_allowed_clusters()`` returns None and the cluster filter inside
        ``get_client`` matches everything. A fleet-wide call from that context would therefore
        reach past the key's own grant. Callers there iterate the grant and ask for one node at
        a time, which is what this is.

        ``node_endpoint`` only fills ``VMInfo.host_endpoint``; it has no effect on the fetch.
        """
        vms, connected, _complete = self._fetch_vms_from_node_detailed(
            node_name,
            node_endpoint,
        )
        return vms, connected

    def _fetch_vms_from_node(
        self,
        node_name: str,
        node_endpoint: str,
    ) -> Tuple[List[VMInfo], bool]:
        """Compatibility wrapper for callers that do not consume partial-read detail."""
        vms, connected, _complete = self._fetch_vms_from_node_detailed(
            node_name,
            node_endpoint,
        )
        return vms, connected

    def _fetch_vms_from_node_detailed(
        self,
        node_name: str,
        node_endpoint: str,
        project: Optional[str] = None,
        allow_vault_reconnect: bool = True,
        client_override: Any = _NO_CLIENT_OVERRIDE,
    ) -> Tuple[List[VMInfo], bool, bool]:
        """
        Fetch VMs from a single node (synchronous, for thread pool execution).

        When ``project`` is supplied, read only that project. Otherwise retain the fleet
        inventory behavior and read every project on the cluster. ``allow_vault_reconnect`` is
        false only for the request-bounded picker path; ordinary fleet reads retain cold-worker
        recovery. ``client_override`` lets that scoped path use one already connected client
        without a redundant liveness probe.
        Returns (vm_list, is_connected, is_complete).
        """
        from app.services.lxd import health
        vms: List[VMInfo] = []
        complete = True

        # Skip clusters the shared breaker marked as recently failed.
        if health.is_tripped(node_name):
            logger.debug(f"Skipping {node_name} - recently failed (cached)")
            return [], False, False

        try:
            # Try to get or reconnect client
            client = (
                self.connection_manager.get_client(node_name)
                if client_override is _NO_CLIENT_OVERRIDE
                else client_override
            )
            
            if client is None and self.cert_manager and allow_vault_reconnect:
                try:
                    cluster_info = self.cert_manager.get_cluster_info(node_name)
                    cert_data = self.cert_manager.get_cluster_certificate(node_name)
                    
                    client = self.connection_manager.connect_with_vault_cert(
                        cluster_id=node_name,
                        endpoint=cluster_info["metadata"]["endpoint"],
                        cert_content=cert_data["client.crt"],
                        key_content=cert_data["client.key"],
                        verify=False
                    )
                    logger.info(f"Reconnected to {node_name}")
                except LXDLocalStorageError:
                    # See lxd_service.ensure_connection: the backend's own storage failed, so
                    # recording it here would mark a healthy node offline for the breaker TTL.
                    raise
                except Exception as e:
                    logger.warning(f"Reconnect failed for {node_name}: {e}")
                    # Preserve a more specific diagnosis. connect_with_vault_cert records
                    # KIND_UNAUTHORIZED for a host that answered and refused us, and a generic
                    # overwrite here would make it read as powered off everywhere.
                    if health.get_kind(node_name) is None:
                        health.record_failure(node_name, str(e))
                    return [], False, False
            
            if client is None:
                # important: do NOT trip the breaker on a missing client. connection.py's
                # _auto_reconnect already refuses to, and says why: an empty in-process cache is
                # the normal state for a cluster registered after this worker booted, and
                # tripping there "seals the only doors that could reopen it". Every path that
                # DID fail records its own kind before returning None, so the `get_kind is None`
                # guard that used to sit here fired only in the case that must never trip: a
                # cluster this worker has simply not connected to yet, or one the caller's org
                # scope excluded. The effect was a freshly registered cluster reading offline
                # across the product for a full breaker TTL, with no log line anywhere.
                logger.debug(
                    "No client for %s in this worker; reporting unreachable without tripping "
                    "the breaker", node_name
                )
                return [], False, False
            
            # State is attached at recursion=2 so the row mapper does not make a request per
            # running instance. A scoped read names its project directly; the fleet path keeps
            # the all-projects optimization and compatibility fallback.
            read_started = time.time()
            read_at: Optional[float] = None
            try:
                if project is None:
                    rows, complete = list_across_projects(
                        client,
                        "instances",
                        recursion=2,
                    )
                else:
                    rows = _list_instances_in_project(
                        client,
                        project,
                        2,
                        request_timeout=_SCOPED_INSTANCE_STATE_READ_TIMEOUT,
                    )
                # note: the midpoint of the list read. LXD gathers each row's state somewhere
                # inside that request, so the midpoint is off by at most half its duration.
                read_at = (read_started + time.time()) / 2
            except requests_exc.ReadTimeout:
                # A large partner cloud can outrun the read budget with state attached.
                # Listing without addresses beats reporting the cluster unreachable.
                logger.warning(
                    "Instance list with state on %s exceeded the read budget; re-reading "
                    "without state, addresses omitted", node_name,
                )
                if project is None:
                    rows, complete = list_across_projects(
                        client,
                        "instances",
                        recursion=1,
                    )
                else:
                    rows = _list_instances_in_project(
                        client,
                        project,
                        1,
                        request_timeout=_SCOPED_INSTANCE_LIST_READ_TIMEOUT,
                    )
            if not complete:
                logger.warning(
                    "Instance list on %s is partial: not every project could be read", node_name
                )
            for row in rows:
                try:
                    vms.append(_vm_info_from_row(row, node_name, node_endpoint, read_at=read_at))
                except Exception as e:
                    logger.error(
                        "Error processing instance %s/%s on %s: %s",
                        row.get("project"), row.get("name"), node_name, e,
                    )
                    complete = False
            return vms, True, complete
            
        except Exception as e:
            logger.warning(f"Error fetching VMs from {node_name}: {e}")
            return [], False, False
    
    async def get_vm(
        self,
        name: str,
        host: str,
        project: Optional[str] = None,
        project_filter: Optional[Callable[[str], bool]] = None,
    ) -> Optional[VMInfo]:
        """Get a specific VM by name and host.

        important: an instance name is unique per project, not per host. Without `project`,
        callers at an authorization boundary must pass `project_filter` so only visible projects
        are searched. Internal trusted callers may omit the filter when they intentionally need
        the legacy cross-project lookup.
        """
        try:
            return await run_blocking(
                self._get_vm_blocking,
                name,
                host,
                project,
                project_filter,
                timeout=_INSTANCE_DETAIL_TIMEOUT_SECONDS,
                label=f"instance.detail[{host}/{project or '*'}]",
                executor=_instance_detail_executor,
                max_in_flight=_INSTANCE_DETAIL_WORKERS,
            )
        except (BlockingTimeout, BlockingPoolFull) as exc:
            raise LXDConnectionError(
                "Instance detail is temporarily unavailable"
            ) from exc

    def _get_vm_blocking(
        self,
        name: str,
        host: str,
        project: Optional[str] = None,
        project_filter: Optional[Callable[[str], bool]] = None,
    ) -> Optional[VMInfo]:
        """Run reconnect, project lookup, state reads, and projection in one worker."""
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")
            endpoint = self._get_node_endpoint(host)

            if project_filter is None:
                instance, project_name = find_instance_in_projects(client, name, project)
            else:
                instance, project_name = find_instance_in_projects(
                    client,
                    name,
                    project,
                    project_filter=project_filter,
                )
            
            if instance is None:
                return None
            
            vm_info = self._instance_to_vm_info(instance, host, endpoint)
            # important: the resolved project goes on the model, not into the config map. It used
            # to be written as a synthetic config key that nothing reads, while VMInfo.project kept
            # the bound client's project. So every single-instance read of an instance outside
            # `default` answered project="default", and callers that trust the field (the detail
            # dialog's Snapshots tab, and the tenant ACL check in the instances router) acted on
            # the wrong project. The list path has always set it correctly, which is why only the
            # detail reads were affected.
            vm_info.project = project_name
            
            return vm_info
            
        except LXDConnectionError:
            raise
        except (requests_exc.RequestException, ClientConnectionFailed) as exc:
            raise LXDConnectionError(
                f"Could not read instance {name} from {host}"
            ) from exc
        except Exception as e:
            logger.error(f"Error getting VM {name} from {host}: {e}")
            raise LXDOperationError(f"Failed to get VM: {str(e)}")
    
    @audit_operation("lxd.instance.start", "instance")
    async def start_vm(
        self,
        name: str,
        host: str,
        force: bool = False,
        project: Optional[str] = None,
    ) -> VMInfo:
        """Start a VM (searches all projects)."""
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")
            endpoint = self._get_node_endpoint(host)
            
            instance, project_name = find_instance_in_projects(client, name, project)
            
            if instance is None:
                raise LXDOperationError(f"VM {name} not found on {host}")

            # The project comes from the authoritative daemon lookup above. Checking here,
            # immediately before the lifecycle decision, prevents a retained migration source
            # from being reported as safely startable even if it has drifted back to Running.
            await assert_instance_not_migration_fenced(
                cluster_id=host,
                project=project_name,
                instance_name=name,
            )

            if instance.status == "Running":
                vm_info = self._instance_to_vm_info(instance, host, endpoint)
            elif instance.status == "Frozen":
                raise LXDOperationError(
                    f"Instance {name} is frozen by billing policy and cannot be started. "
                    f"Please reactivate the subscription through the billing portal."
                )
            else:
                started = await asyncio.to_thread(
                    self._start_and_wait, instance, host, project_name
                )
                logger.info(f"Started VM {name} on {host} (project: {project_name})")
                vm_info = self._instance_to_vm_info(started, host, endpoint)
            
            # important: the resolved project goes on the model, not into the config map. It used
            # to be written as a synthetic config key that nothing reads, while VMInfo.project kept
            # the bound client's project. So every single-instance read of an instance outside
            # `default` answered project="default", and callers that trust the field (the detail
            # dialog's Snapshots tab, and the tenant ACL check in the instances router) acted on
            # the wrong project. The list path has always set it correctly, which is why only the
            # detail reads were affected.
            vm_info.project = project_name
            
            return vm_info
            
        except LXDAPIException as e:
            raise LXDOperationError(f"Failed to start VM: {str(e)}")
        except (requests_exc.ConnectionError, requests_exc.Timeout) as e:
            # important: the cluster stopped answering part-way through the start. That is an
            # upstream availability problem the caller can retry, not an internal fault, and
            # without this it reached the router's `except Exception` and became a 500 with a
            # reference id. Observed live: a read timeout at 8.9s while the instance went on
            # to reach Running, so the operator was told the server broke about an action
            # that had in fact succeeded.
            raise LXDConnectionError(
                f"Lost the connection to {host} while starting {name}: {e}"
            )
    
    def _rebind_for_wait(self, instance, host: str, project_name: str, timeout: int):
        """Re-fetch `instance` on a client whose read timeout outlives `timeout`.

        Returns the original instance unchanged if such a client can't be built,
        so a graceful stop/restart degrades to the old behaviour rather than
        failing outright.
        """
        try:
            waiting_client = self.connection_manager.get_project_client(
                host, project_name, (5, timeout + _GRACEFUL_READ_MARGIN_SECONDS)
            )
            if waiting_client is not None:
                return waiting_client.instances.get(safe_segment(instance.name))
        except Exception as e:  # noqa: BLE001 - fall back to the pooled client
            logger.warning(
                "Could not build a long-timeout client for %s on %s: %s", instance.name, host, e
            )
        return instance

    def _start_and_wait(self, instance, host: str, project_name: str):
        """Start `instance` on a client whose read timeout outlives the start.

        important: the pooled client allows an 8s read (DEFAULT_LXD_TIMEOUT) and
        `start(wait=True)` holds the HTTP response open for the whole LXD operation. A loaded
        cluster takes longer than that, so the READ timed out while the instance went on
        starting, and the caller reported a 500 for an action that had succeeded. Observed
        live at 8.9s against a busy cluster, with the VM reaching Running anyway.
        `stop`/`restart` already avoid this through `_rebind_for_wait`; start did not, which
        is the asymmetry this closes. Falls back to the pooled client when no long-timeout
        client can be built, so the worst case is the old behaviour.
        """
        target = self._rebind_for_wait(instance, host, project_name, _START_WAIT_SECONDS)
        target.start(wait=True)
        target.sync()
        return target

    @staticmethod
    def _graceful_timeout(name: str, action: str, timeout: int) -> LXDGracefulTimeoutError:
        # A graceful stop only works if software inside the guest acts on the ACPI
        # power event. A fully-booted OS does so in about a second; the cases below
        # never will, no matter how long the timeout.
        # note: the remedy comes before the causes, and the whole message fits the 300
        # characters a 5xx detail is cut to on the way out (core/error_sanitizer.py) with the
        # longest legal name (63) and timeout (600). The old order lost "retry with force" to
        # the cut on any name longer than a few characters.
        return LXDGracefulTimeoutError(
            f"Timed out after {timeout}s waiting for '{name}' to shut down gracefully: "
            f"nothing in the guest acted on the ACPI request. Retry the {action} with "
            f"force to power-cycle it. Causes: no OS, a boot menu or UEFI shell, early "
            f"boot, an OS ignoring ACPI."
        )

    @audit_operation("lxd.instance.stop", "instance")
    async def stop_vm(
        self,
        name: str,
        host: str,
        force: bool = False,
        timeout: int = 30,
        project: Optional[str] = None,
    ) -> VMInfo:
        """Stop a VM (searches all projects)."""
        timeout = max(1, min(timeout, _MAX_ACTION_TIMEOUT_SECONDS))
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")
            endpoint = self._get_node_endpoint(host)

            instance, project_name = find_instance_in_projects(client, name, project)

            if instance is None:
                raise LXDOperationError(f"VM {name} not found on {host}")

            if instance.status == "Stopped":
                vm_info = self._instance_to_vm_info(instance, host, endpoint)
            else:
                # A graceful stop blocks on the guest for up to `timeout`s. Those
                # pylxd calls are synchronous, so run them in a worker thread to
                # avoid stalling the event loop for the whole wait (a blank VM
                # burns the full timeout since nothing answers ACPI). A forced
                # stop is immediate but goes through the same path for uniformity.
                def _do_stop():
                    target = instance if force else self._rebind_for_wait(instance, host, project_name, timeout)
                    try:
                        target.stop(force=force, wait=True, timeout=timeout)
                    except Exception as e:
                        if not force and _is_graceful_timeout(e):
                            raise self._graceful_timeout(name, "stop", timeout) from e
                        raise
                    target.sync()
                    return target

                target = await asyncio.to_thread(_do_stop)
                logger.info(f"Stopped VM {name} on {host} (project: {project_name})")
                vm_info = self._instance_to_vm_info(target, host, endpoint)

            # important: the resolved project goes on the model, not into the config map. It used
            # to be written as a synthetic config key that nothing reads, while VMInfo.project kept
            # the bound client's project. So every single-instance read of an instance outside
            # `default` answered project="default", and callers that trust the field (the detail
            # dialog's Snapshots tab, and the tenant ACL check in the instances router) acted on
            # the wrong project. The list path has always set it correctly, which is why only the
            # detail reads were affected.
            vm_info.project = project_name

            return vm_info

        except LXDAPIException as e:
            raise LXDOperationError(f"Failed to stop VM: {str(e)}")
    
    @audit_operation("lxd.instance.restart", "instance")
    async def restart_vm(
        self, 
        name: str, 
        host: str, 
        force: bool = False,
        timeout: int = 30,
        project: Optional[str] = None,
    ) -> VMInfo:
        """Restart a VM (searches all projects)."""
        timeout = max(1, min(timeout, _MAX_ACTION_TIMEOUT_SECONDS))
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")
            endpoint = self._get_node_endpoint(host)

            instance, project_name = find_instance_in_projects(client, name, project)

            if instance is None:
                raise LXDOperationError(f"VM {name} not found on {host}")

            if instance.status == "Frozen":
                raise LXDOperationError(
                    f"Instance {name} is frozen by billing policy and cannot be restarted. "
                    f"Please reactivate the subscription through the billing portal."
                )

            await assert_instance_not_migration_fenced(
                cluster_id=host,
                project=project_name,
                instance_name=name,
            )

            # The graceful restart blocks on the guest shutdown for up to
            # `timeout`s; run the synchronous pylxd calls in a worker thread so the
            # event loop isn't stalled for the full wait. A forced restart is a
            # power-cycle but goes through the same path for uniformity.
            def _do_restart():
                target = instance if force else self._rebind_for_wait(instance, host, project_name, timeout)
                try:
                    target.restart(force=force, wait=True, timeout=timeout)
                except Exception as e:
                    if not force and _is_graceful_timeout(e):
                        raise self._graceful_timeout(name, "restart", timeout) from e
                    raise
                target.sync()
                return target

            target = await asyncio.to_thread(_do_restart)

            logger.info(f"Restarted VM {name} on {host} (project: {project_name})")
            vm_info = self._instance_to_vm_info(target, host, endpoint)

            # important: the resolved project goes on the model, not into the config map. It used
            # to be written as a synthetic config key that nothing reads, while VMInfo.project kept
            # the bound client's project. So every single-instance read of an instance outside
            # `default` answered project="default", and callers that trust the field (the detail
            # dialog's Snapshots tab, and the tenant ACL check in the instances router) acted on
            # the wrong project. The list path has always set it correctly, which is why only the
            # detail reads were affected.
            vm_info.project = project_name

            return vm_info

        except LXDAPIException as e:
            raise LXDOperationError(f"Failed to restart VM: {str(e)}")
    
    @audit_operation("lxd.instance.delete", "instance")
    async def delete_vm(
        self, 
        name: str, 
        host: str, 
        force: bool = False,
        project: Optional[str] = None,
    ) -> str:
        """Delete a VM, returning the project it was actually deleted from.

        important: the RESOLVED project, not the requested one. Callers stamp tags, webhooks,
        upstream events and the audit trail with it, and a caller that passed no project was
        stamping "default" for an instance deleted from somewhere else entirely, so the audit
        trail named a project the deletion never touched.
        """
        try:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")
            
            instance, project_name = find_instance_in_projects(client, name, project)
            
            if instance is None:
                raise LXDOperationError(f"VM {name} not found on {host}")

            await assert_instance_not_migration_fenced(
                cluster_id=host,
                project=project_name,
                instance_name=name,
            )

            # important: both waits run OFF the event loop and on a client whose socket window
            # outlives them. The pooled client reads for 8 seconds; tearing down a root disk on a
            # `dir` pool, or on a loaded host, takes longer. The read then died first, the
            # ReadTimeout surfaced as LXDOperationError and the caller was answered 500 for a
            # delete LXD had in fact completed, so the operator saw a server error and an instance
            # that was already gone. Same rule as stop and restart beside it, and the same rule the
            # snapshot wait states: a wait has to outlast what it is waiting for.
            def _do_delete():
                target = self._rebind_for_wait(instance, host, project_name, _DELETE_WAIT_SECONDS)
                if target.status in ("Running", "Frozen"):
                    target.stop(force=True, wait=True)
                target.delete(wait=True)

            # Stop an active instance before deleting when force is set. A Frozen
            # instance (billing-paused) is still active, so LXD refuses to delete
            # it until it is stopped -- force-stop covers both Running and Frozen.
            if instance.status in ("Running", "Frozen") and not force:
                raise LXDOperationError(
                    f"VM {name} is {instance.status.lower()}. Stop it first or use force=true"
                )

            await asyncio.to_thread(_do_delete)

            logger.info(f"Deleted VM {name} from {host} (project: {project_name})")
            return project_name
            
        except LXDAPIException as e:
            raise LXDOperationError(f"Failed to delete VM: {str(e)}")

    async def get_rebuild_preflight(self, name: str, host: str, project: Optional[str] = None) -> Dict[str, Any]:
        """Gather what the rebuild flow needs before it starts: the instance status,
        its snapshots (the block condition -- LXD refuses to rebuild an instance that
        has any), and a snapshot of the config + attached custom volumes for the job
        record. Blocking pylxd calls run off the event loop.
        """
        def _work() -> Dict[str, Any]:
            client = self._get_client_with_reconnect(host)
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")
            instance, project_name = find_instance_in_projects(client, name, project)
            if instance is None:
                raise LXDOperationError(f"Instance {name} not found on {host}")
            snapshots = _lxd_snapshot_names(client, name, project_name)
            data = instance._data or {}
            config = dict(data.get("config") or {})
            devices = dict(data.get("expanded_devices") or data.get("devices") or {})
            # Attached custom volumes survive a rebuild: disk devices other than the
            # root that reference a pool volume by source.
            attached = {
                dname: dcfg for dname, dcfg in devices.items()
                if dcfg.get("type") == "disk" and dname != "root" and dcfg.get("source")
            }
            base_image = config.get("image.description") or config.get("volatile.base_image")
            return {
                "project": project_name,
                "status": instance.status,
                "type": instance.type,
                "snapshots": snapshots,
                "config": config,
                "attached_volumes": attached,
                "base_image": base_image,
            }
        return await asyncio.to_thread(_work)

    async def rebuild_instance(
        self,
        name: str,
        host: str,
        source: Dict[str, Any],
        project: Optional[str] = None,
        cloud_init: Optional[str] = None,
        wait_timeout: int = 1800,
    ) -> str:
        """Rebuild the instance's root disk from `source` (LXD `POST .../rebuild`).

        Reinstalls the root disk while LXD preserves the instance identity, config
        and attached volumes. The instance must already be stopped and must have no
        snapshots (caller enforces both). Optionally sets cloud-init.user-data first
        so the fresh root disk picks it up on first boot. pylxd has no rebuild
        helper, so this drives the raw API and waits on the returned operation.
        Returns the LXD operation id.
        """
        client = self._get_client_with_reconnect(host)
        if client is None:
            raise LXDConnectionError(f"Could not connect to {host}")
        instance, project_name = find_instance_in_projects(client, name, project)
        if instance is None:
            raise LXDOperationError(f"Instance {name} not found on {host}")

        await assert_instance_not_migration_fenced(
            cluster_id=host,
            project=project_name,
            instance_name=name,
        )

        # Snapshot guard: LXD rejects a rebuild if any snapshot exists. Surface it
        # as a typed error the router maps to a clear 409 listing the snapshots.
        snapshots = await asyncio.to_thread(_lxd_snapshot_names, client, name, project_name)
        if snapshots:
            raise LXDSnapshotExistsError(snapshots)

        if cloud_init is not None:
            # PATCH merges config, so this only sets cloud-init.user-data. The new
            # root disk picks it up on first boot.
            def _set_ci():
                client.api.instances[safe_segment(name)].patch(
                    json={"config": {"cloud-init.user-data": cloud_init}},
                    params={"project": project_name},
                )
            await asyncio.to_thread(_set_ci)

        endpoint = client.api._api_endpoint  # e.g. https://host:8443/1.0
        session = client.api.session
        project_q = f"?project={project_name}" if project_name and project_name != "default" else ""

        def _do_rebuild() -> str:
            resp = session.post(
                f"{endpoint}/instances/{name}/rebuild{project_q}",
                json={"source": source},
                verify=False,
                timeout=(5, 120),
            )
            if resp.status_code >= 400:
                raise LXDOperationError(f"LXD rebuild failed: {resp.status_code} {resp.text[:300]}")
            op = resp.json()
            op_path = op.get("operation")  # /1.0/operations/<id>
            if not op_path:
                return ""  # completed synchronously
            op_id = op_path.rstrip("/").rsplit("/", 1)[-1]
            # (connect, read): a scalar would grant the same 1800s budget to the connect leg,
            # so a peer that never completes the handshake would pin this thread for half an
            # hour. The read leg genuinely needs to be long -- a rebuild is slow.
            wresp = session.get(f"{endpoint}{op_path[4:]}/wait", verify=False,
                                timeout=(_REBUILD_CONNECT_TIMEOUT, wait_timeout))
            wbody = wresp.json() or {}
            if wbody.get("type") == "error" or wbody.get("error"):
                raise LXDOperationError(f"LXD rebuild failed: {wbody.get('error') or 'unknown error'}")
            wdata = wbody.get("metadata") or {}
            if wdata.get("status") != "Success":
                raise LXDOperationError(
                    f"LXD rebuild operation failed: {wdata.get('err') or wdata.get('status') or 'unknown'}"
                )
            return op_id

        try:
            op_id = await run_blocking(
                _do_rebuild,
                # The socket timeouts above are what actually free the worker; this only
                # bounds how long the caller waits. Margin over wait_timeout so a normal
                # slow rebuild reports LXD's own result rather than this cap.
                timeout=wait_timeout + _GRACEFUL_READ_MARGIN_SECONDS,
                label=f"rebuild[{host}/{name}]",
                executor=_rebuild_executor,
                max_in_flight=_REBUILD_MAX_IN_FLIGHT,
            )
        except BlockingTimeout as exc:
            raise LXDOperationError(
                f"Rebuild of {name} on {host} did not complete within {wait_timeout}s"
            ) from exc
        except BlockingPoolFull as exc:
            raise LXDOperationError(
                f"Too many rebuilds in flight; retry {name} on {host} shortly"
            ) from exc
        logger.info("Rebuilt instance %s on %s (project=%s, op=%s)", name, host, project_name, op_id)
        return op_id

    @audit_operation(
        "lxd.instance.create", "instance",
        extract_details=lambda args, kwargs, result: {
            "image": kwargs.get("image"),
            "instance_type": kwargs.get("instance_type", "virtual-machine"),
            "profiles": kwargs.get("profiles"),
            "project": kwargs.get("project", "default"),
        }
    )
    async def create_vm(
        self,
        name: str,
        host: str,
        image: str,
        instance_type: str = "virtual-machine",
        profiles: Optional[List[str]] = None,
        config: Optional[Dict[str, str]] = None,
        cloud_init: Optional[str] = None,
        devices: Optional[Dict[str, Dict[str, str]]] = None,
        start_on_create: bool = True,
        project: str = "default",
    ) -> VMInfo:
        """Create a VM without running pylxd's blocking workflow on the event loop."""
        project = (project or "default").strip() or "default"
        await assert_project_not_migration_fenced(
            cluster_id=host,
            project=project,
        )
        await assert_instance_not_migration_fenced(
            cluster_id=host,
            project=project,
            instance_name=name,
        )
        try:
            return await run_blocking(
                self._create_vm_blocking,
                name,
                host,
                image,
                instance_type,
                profiles,
                config,
                cloud_init,
                devices,
                start_on_create,
                project,
                timeout=_CREATE_WALL_TIMEOUT_SECONDS,
                label=f"create-vm[{host}/{project}/{name}]",
                executor=_create_executor,
                max_in_flight=_CREATE_MAX_IN_FLIGHT,
                concurrency_key=host,
                max_in_flight_per_key=_CREATE_MAX_IN_FLIGHT_PER_CLUSTER,
            )
        except BlockingTimeout as exc:
            raise LXDOperationError(
                f"VM creation on {host} timed out; it may still complete, so check the "
                "instance list before retrying"
            ) from exc
        except BlockingPoolFull as exc:
            raise LXDOperationError(
                "Too many VM creations are already in progress on this backend; retry shortly"
            ) from exc

    def _create_vm_blocking(
        self,
        name: str,
        host: str,
        image: str,
        instance_type: str,
        profiles: Optional[List[str]],
        config: Optional[Dict[str, str]],
        cloud_init: Optional[str],
        devices: Optional[Dict[str, Dict[str, str]]],
        start_on_create: bool,
        project: str,
    ) -> VMInfo:
        """Run the complete blocking create/start/readback transaction in one worker."""
        try:
            # Keep connection lookup and possible certificate reconnect in this worker too.
            pooled_client = self._get_client_with_reconnect(host)
            if pooled_client is None:
                raise LXDConnectionError(f"Could not connect to {host}")

            project = (project or "default").strip() or "default"
            # pylxd binds a project to the client. Creating through the pooled client would
            # silently place an authorized non-default request in `default`, then stamp the
            # audit trail with the requested project. Build the scoped client before issuing
            # any create call. The fallback preserves legacy default-project deployments whose
            # connection manager predates short-lived project clients.
            client = self.connection_manager.get_project_client(
                host,
                project,
                (5, _CREATE_LXD_READ_TIMEOUT_SECONDS),
            )
            if client is None:
                if project != "default":
                    raise LXDOperationError(
                        f"Could not open project '{project}' on {host}"
                    )
                client = pooled_client
            endpoint = self._get_node_endpoint(host)

            # Build instance configuration
            instance_config = config or {}

            # Add cloud-init if provided
            if cloud_init:
                instance_config["user.user-data"] = cloud_init

            # Determine source configuration
            source = {"type": "image"}

            # A full fingerprint or the prefix `lxc image list` prints; anything else an alias
            source[image_source_key(image)] = image

            # Create instance definition
            instance_def = {
                "name": name,
                "source": source,
                "type": instance_type,
                "profiles": profiles or ["default"],
                "config": instance_config,
            }
            
            if devices:
                instance_def["devices"] = devices

            # Create the instance
            if instance_type == "virtual-machine":
                instance = client.virtual_machines.create(
                    instance_def, 
                    wait=True
                )
            else:
                instance = client.containers.create(
                    instance_def, 
                    wait=True
                )

            logger.info(f"Created {instance_type} {name} on {host}")

            # Start if requested
            if start_on_create:
                instance = self._start_and_wait(instance, host, project)
                logger.info(f"Started {name} on {host}")

            vm_info = self._instance_to_vm_info(instance, host, endpoint)
            # Some pylxd versions omit `project` on a freshly-created wrapper. The project
            # selected above is authoritative and must match the response and audit record.
            vm_info.project = project
            return vm_info

        except LXDAPIException as e:
            raise LXDOperationError(f"Failed to create VM: {_create_error(e)}")

    async def create_instance(
        self,
        name: str,
        host: str,
        image: Optional[str] = None,
        instance_type: str = "virtual-machine",
        profiles: Optional[List[str]] = None,
        config: Optional[Dict[str, str]] = None,
        cloud_init: Optional[str] = None,
        devices: Optional[Dict[str, Dict[str, str]]] = None,
        start_on_create: bool = True,
        project: str = "default",
        image_server: Optional[str] = None,
        image_protocol: Optional[str] = None,
        description: Optional[str] = None,
        source_type: str = "image",
        job_id: Optional[str] = None,
    ) -> Optional[str]:
        """Start creating a new instance (non-blocking).

        The create is issued as a background LXD operation and this returns the
        operation id immediately. The (potentially slow) image pull happens
        server-side, so the HTTP request never waits on it. The instance appears
        via the polling instance list. If image_server is set, the image is
        pulled from that remote simplestreams server by alias; otherwise it is
        resolved locally (by fingerprint or alias). When start_on_create is set,
        a background task waits for the create to finish and then starts it.

        This backs the dashboard's `POST /instances` flow. create_vm() is kept
        separate and unchanged for Orcahub's `POST /vms`.
        """
        project = (project or "default").strip() or "default"
        await assert_project_not_migration_fenced(
            cluster_id=host,
            project=project,
        )
        await assert_instance_not_migration_fenced(
            cluster_id=host,
            project=project,
            instance_name=name,
        )

        worker_id: Optional[str] = None
        job_row: Optional[InstanceOperationJob] = None
        heartbeat_task: Optional[asyncio.Task[Any]] = None
        heartbeat_transferred = False
        if job_id:
            worker_id = _create_worker_id()
            if not await _claim_create_job(job_id, worker_id):
                existing = await _load_create_job(job_id)
                if existing is not None and existing.status in _CREATE_TERMINAL_STATUSES:
                    return existing.operation_id
                raise LXDOperationError(
                    "The durable create job is already owned by another worker."
                )
            await _set_create_job(
                job_id,
                worker_id=worker_id,
                status="running",
                phase="preflighting",
                error=None,
            )
            job_row = await _load_create_job(job_id)
            if job_row is None:
                raise LXDOperationError("The durable create job could not be loaded.")
            heartbeat_task = asyncio.create_task(
                _create_lease_heartbeat(job_id, worker_id)
            )

        try:
            client = await run_blocking(
                self._get_client_with_reconnect,
                host,
                timeout=_CREATE_CONNECT_TIMEOUT_SECONDS,
                label=f"create connection[{host}/{project}/{name}]",
                executor=_create_executor,
                max_in_flight=_CREATE_MAX_IN_FLIGHT,
                concurrency_key=host,
                max_in_flight_per_key=_CREATE_MAX_IN_FLIGHT_PER_CLUSTER,
            )
            if client is None:
                raise LXDConnectionError(f"Could not connect to {host}")

            project = project or "default"
            # Dedicated client with a long read timeout: the create POST itself
            # returns fast, but the background start step waits on operations that
            # can take a while when a remote image is being pulled.
            # pylxd's constructor does blocking network I/O (validates the
            # endpoint), so build it off the event loop like the create POST.
            create_client = await run_blocking(
                self.connection_manager.get_project_client,
                host, project, (5, 1800),
                timeout=_CREATE_CONNECT_TIMEOUT_SECONDS,
                label=f"create project client[{host}/{project}/{name}]",
                executor=_create_executor,
                max_in_flight=_CREATE_MAX_IN_FLIGHT,
                concurrency_key=host,
                max_in_flight_per_key=_CREATE_MAX_IN_FLIGHT_PER_CLUSTER,
            )
            if create_client is None:
                if project != "default":
                    raise LXDOperationError(f"Could not open project '{project}' on {host}")
                create_client = client  # fall back to pooled (default project)

            # Fail fast on a duplicate name. LXD accepts a duplicate-name create as
            # an async 202 and only fails the background operation, which the
            # non-blocking flow can't surface cleanly (the pre-existing instance is
            # already in the list, so the failing placeholder looks "resolved").
            # A synchronous check gives the user an immediate, clear error.
            already_exists = await run_blocking(
                create_client.instances.exists,
                name,
                timeout=_CREATE_READBACK_TIMEOUT_SECONDS,
                label=f"create duplicate read[{host}/{project}/{name}]",
                executor=_create_executor,
                max_in_flight=_CREATE_MAX_IN_FLIGHT,
                concurrency_key=host,
                max_in_flight_per_key=_CREATE_MAX_IN_FLIGHT_PER_CLUSTER,
            )
            if already_exists:
                raise LXDOperationError(
                    f"An instance named '{name}' already exists in project '{project}' on {host}"
                )

            # important: fail fast on a NIC the project cannot reach. LXD accepts a create
            # like that as an async 202 and only fails the background operation, and the
            # console's placeholder row has nothing to clear it, so the instance sits at
            # "Creating..." with empty columns and no reason.
            #
            # The pooled client, not create_client: pylxd overwrites an explicit project
            # parameter with the client's own, so a project-bound client would check the
            # wrong project.
            try:
                await run_blocking(
                    check_nics,
                    client,
                    project,
                    devices,
                    profiles,
                    timeout=_CREATE_READBACK_TIMEOUT_SECONDS,
                    label=f"create NIC validation[{host}/{project}/{name}]",
                    executor=_create_executor,
                    max_in_flight=_CREATE_MAX_IN_FLIGHT,
                    concurrency_key=host,
                    max_in_flight_per_key=_CREATE_MAX_IN_FLIGHT_PER_CLUSTER,
                )
            except NicPreflightError as nic_err:
                raise LXDOperationError(str(nic_err)) from None


            # Build instance configuration
            instance_config = config or {}
            if cloud_init:
                instance_config["user.user-data"] = cloud_init

            # Determine source configuration
            if source_type == "none":
                # Blank VM: an empty root disk and no OS, equivalent to
                # `lxc init <name> --empty --vm`. There is nothing to boot yet --
                # an OS is installed afterwards by attaching an install ISO as
                # boot media and driving the VGA console.
                source = {"type": "none"}
            else:
                source = {"type": "image"}
                if image_server:
                    # Pull from a remote simplestreams server by alias.
                    source["mode"] = "pull"
                    source["server"] = image_server
                    source["protocol"] = image_protocol or "simplestreams"
                    source["alias"] = image
                else:  # local: a fingerprint (or its prefix) or an alias
                    source[image_source_key(image)] = image

            instance_def = {
                "name": name,
                "source": source,
                "type": instance_type,
                "profiles": profiles or ["default"],
                "config": _drop_unsupported_config(
                    create_client, instance_type, instance_config
                ),
            }
            if description:
                instance_def["description"] = description
            if devices:
                instance_def["devices"] = devices

            # Non-blocking create: POST kicks off a background LXD operation and
            # returns immediately. Synchronous validation errors (bad name, bad
            # pool, unknown image, etc.) still surface here as LXDAPIException.
            if job_id:
                updated = await _set_create_job(
                    job_id,
                    worker_id=worker_id,
                    status="running",
                    phase="submitting",
                )
                if not updated:
                    raise LXDOperationError(
                        "The durable create job lease was lost before submission."
                    )
            try:
                response = await run_blocking(
                    lambda: create_client.api.instances.post(json=instance_def),
                    timeout=_CREATE_SUBMIT_TIMEOUT_SECONDS,
                    label=f"submit create[{host}/{project}/{name}]",
                    executor=_create_executor,
                    max_in_flight=_CREATE_MAX_IN_FLIGHT,
                    concurrency_key=host,
                    max_in_flight_per_key=_CREATE_MAX_IN_FLIGHT_PER_CLUSTER,
                )
            except BlockingPoolFull:
                # run_blocking refuses before scheduling the POST, so the daemon cannot have
                # seen this request and the ordinary synchronous failure path is safe.
                raise
            except Exception as exc:
                if job_row is not None and _ambiguous_create_error(exc):
                    await _mark_create_unknown(
                        job_row,
                        operation_id=None,
                        error=(
                            "The connection was lost while submitting the create. The daemon "
                            "may have accepted it; reconciliation will read actual state."
                        ),
                        result={"ambiguous_action": "create"},
                    )
                    raise CreateSubmissionUnknown(
                        "The create submission outcome is unknown.", job_id=job_id
                    ) from exc
                raise
            operation_id = None
            try:
                op_url = response.json().get("operation")
                if op_url:
                    operation_id = op_url.rstrip("/").rsplit("/", 1)[-1]
            except Exception:
                pass

            if job_id:
                try:
                    updated = await _set_create_job(
                        job_id,
                        worker_id=worker_id,
                        status="running",
                        phase="verifying",
                        operation_id=operation_id,
                        error=None,
                    )
                except Exception as exc:  # noqa: BLE001 - accepted mutation is ambiguous
                    raise CreateSubmissionUnknown(
                        "The create was accepted, but its operation could not be recorded.",
                        job_id=job_id,
                        operation_id=operation_id,
                    ) from exc
                if not updated:
                    # The mutation is accepted. A missing bookkeeping write must never invite
                    # the caller to submit it again; the persisted pending/running row remains
                    # the recovery handle.
                    raise CreateSubmissionUnknown(
                        "The create was accepted, but its operation could not be recorded.",
                        job_id=job_id,
                        operation_id=operation_id,
                    )

            logger.info(
                f"Started creation of {instance_type} {name} on {host} "
                f"(project={project}, operation={operation_id})"
            )

            # "Create & Start": wait for the create operation in the background,
            # then start the instance, without blocking this request.
            if start_on_create:
                task = asyncio.create_task(
                    self._finalize_and_start(
                        create_client,
                        name,
                        operation_id,
                        host,
                        project,
                        job_id=job_id,
                        worker_id=worker_id,
                        heartbeat_task=heartbeat_task,
                    )
                )
                # Keep a strong ref until done so the loop can't GC it mid-run.
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)

            else:
                # A create without auto-start had nobody watching its operation, so an
                # asynchronous failure was silent. Watch it too, holding the same strong
                # reference the branch above does so the loop cannot collect it mid-run.
                watch = asyncio.create_task(
                    self._watch_create_operation(
                        create_client,
                        name,
                        operation_id,
                        host,
                        project,
                        job_id=job_id,
                        worker_id=worker_id,
                        heartbeat_task=heartbeat_task,
                    )
                )
                self._bg_tasks.add(watch)
                watch.add_done_callback(self._bg_tasks.discard)

            heartbeat_transferred = heartbeat_task is not None
            return operation_id

        except CreateSubmissionUnknown:
            raise
        except (ClientConnectionFailed, requests_exc.ConnectionError, requests_exc.Timeout) as e:
            # Cluster unreachable during exists()/POST. Surface as a connection
            # error so the router maps it to 503, not a blanket 500.
            raise LXDConnectionError(f"Failed to reach {host}: {e}")
        except (BlockingPoolFull, BlockingTimeout) as e:
            raise LXDConnectionError(
                f"Create capacity is temporarily unavailable on {host}"
            ) from e
        except LXDAPIException as e:
            # A project limit refusal is the one synchronous 4xx the form can act on: it names
            # a field. Everything else stays the free text it always was.
            hit = project_limit_from_lxd_error(str(e))
            if hit is not None:
                raise LXDProjectLimitError(f"Failed to create VM: {_create_error(e)}", hit)
            raise LXDOperationError(f"Failed to create VM: {_create_error(e)}")
        finally:
            if not heartbeat_transferred:
                await _stop_create_heartbeat(heartbeat_task)

    @staticmethod
    def _invalidate_lists_after_background_change(name: str, host: str) -> None:
        """Clear the list caches a background job has just made stale. Never raises."""
        try:
            from app.core.cache import get_cache_manager
            get_cache_manager().invalidate_instance_cache()
        except Exception as e:  # noqa: BLE001 - a cache miss is cheaper than a lost start
            logger.warning(f"Could not clear the instance list cache after {name} on {host} changed: {e}")

    @staticmethod
    def _create_instance_status(instance: Any) -> str:
        return str(getattr(instance, "status", None) or "Unknown")

    @staticmethod
    def _create_result(row: InstanceOperationJob, instance: Any) -> Dict[str, Any]:
        return {
            "instance_name": row.instance_name,
            "instance_status": InstanceManager._create_instance_status(instance),
            "instance_type": row.instance_type,
        }

    async def _read_created_instance(
        self,
        client: Any,
        row: InstanceOperationJob,
    ) -> tuple[Optional[Any], bool, Optional[str]]:
        """Read the exact target. ``complete`` distinguishes NotFound from no verdict."""
        try:
            instance = await run_blocking(
                client.instances.get,
                safe_segment(row.instance_name),
                timeout=_CREATE_READBACK_TIMEOUT_SECONDS,
                label=(
                    f"create readback[{row.cluster_id}/{row.project}/{row.instance_name}]"
                ),
                executor=_create_executor,
                max_in_flight=_CREATE_MAX_IN_FLIGHT,
                concurrency_key=row.cluster_id,
                max_in_flight_per_key=_CREATE_MAX_IN_FLIGHT_PER_CLUSTER,
            )
            return instance, True, None
        except NotFound:
            return None, True, None
        except LXDAPIException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 404 or "not found" in str(exc).lower():
                return None, True, None
            return None, False, _create_error(exc)
        except Exception as exc:  # noqa: BLE001 - unknown must be reconciled, never guessed
            return None, False, _create_error(exc)

    async def _wait_for_create_operation(
        self,
        client: Any,
        row: InstanceOperationJob,
        operation_id: str,
    ) -> tuple[str, Optional[str]]:
        try:
            await run_blocking(
                client.operations.wait_for_operation,
                operation_id,
                timeout=_CREATE_WALL_TIMEOUT_SECONDS,
                label=(
                    f"wait create[{row.cluster_id}/{row.project}/{row.instance_name}/"
                    f"{operation_id}]"
                ),
                executor=_create_executor,
                max_in_flight=_CREATE_MAX_IN_FLIGHT,
                concurrency_key=row.cluster_id,
                max_in_flight_per_key=_CREATE_MAX_IN_FLIGHT_PER_CLUSTER,
            )
            return "success", None
        except NotFound as exc:
            # Incus eventually expires operation records. Exact resource readback proves that
            # something exists at the requested identity, but cannot reconstruct the missing
            # terminal verdict, so reconciliation must never promote it to success or auto-start.
            return "expired", _create_error(exc)
        except LXDAPIException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 404:
                return "expired", _create_error(exc)
            return "failure", _create_error(exc)
        except Exception as exc:  # noqa: BLE001 - readback below decides ambiguous outcomes
            if _ambiguous_create_error(exc) or isinstance(exc, BlockingPoolFull):
                return "unknown", _create_error(exc)
            return "failure", _create_error(exc)

    @staticmethod
    def _submit_start_operation(client: Any, instance_name: str) -> Optional[str]:
        response = client.api.instances[safe_segment(instance_name)].state.put(
            json={"action": "start", "timeout": _START_WAIT_SECONDS, "force": False}
        )
        try:
            operation_url = response.json().get("operation")
        except Exception:  # noqa: BLE001 - accepted without correlation is still ambiguous
            return None
        return operation_url.rstrip("/").rsplit("/", 1)[-1] if operation_url else None

    async def _verify_durable_start(
        self,
        client: Any,
        row: InstanceOperationJob,
        *,
        create_operation_id: Optional[str],
        start_operation_id: Optional[str],
    ) -> None:
        """Verify a submitted start. This path never submits or replays the action."""
        state = {
            "ambiguous_action": "start",
            "create_operation_id": create_operation_id,
            "start_operation_id": start_operation_id,
        }
        if not start_operation_id:
            instance, read_complete, read_error = await self._read_created_instance(client, row)
            if read_complete and instance is not None:
                state.update(self._create_result(row, instance))
            await _mark_create_unknown(
                row,
                operation_id=create_operation_id,
                error=(
                    "The start request may have reached the daemon, but no operation id was "
                    "returned. Recovery will only read state and will not submit start again."
                    + (
                        f" Readback failed: {read_error or 'unknown error'}."
                        if not read_complete
                        else ""
                    )
                ),
                result=state,
            )
            return

        verdict, start_error = await self._wait_for_create_operation(
            client, row, start_operation_id
        )
        verified, read_complete, read_error = await self._read_created_instance(client, row)
        if not read_complete:
            await _mark_create_unknown(
                row,
                operation_id=start_operation_id,
                error=(
                    "The start operation has no verified final state: "
                    f"{read_error or start_error or 'unknown error'}"
                ),
                result=state,
            )
            return
        if verified is None:
            if verdict in {"unknown", "expired"}:
                await _mark_create_unknown(
                    row,
                    operation_id=start_operation_id,
                    error=(
                        "The auto-start operation has no verifiable terminal outcome and exact "
                        "readback does not currently show the instance. Recovery will not "
                        "submit start again."
                    ),
                    result=state,
                )
                return
            await _finish_create_job(
                row,
                "failure",
                operation_id=start_operation_id,
                error="The created instance disappeared before auto-start could be verified.",
                invalidate_cache=True,
            )
            return

        state.update(self._create_result(row, verified))
        if verdict == "unknown":
            await _mark_create_unknown(
                row,
                operation_id=start_operation_id,
                error=(
                    "The instance was created, but the submitted auto-start operation is "
                    f"still ambiguous: {start_error or 'no terminal verdict'}"
                ),
                result=state,
            )
            return

        terminal_state = dict(state)
        terminal_state.pop("ambiguous_action", None)
        if verdict == "failure":
            await _finish_create_job(
                row,
                "partial",
                operation_id=start_operation_id,
                result=terminal_state,
                error=(
                    "The instance exists, but the submitted auto-start operation reported "
                    f"failure: {start_error or 'unknown daemon error'}"
                ),
                invalidate_cache=True,
                announce_created=True,
            )
            return
        if verdict == "expired":
            await _finish_create_job(
                row,
                "partial",
                operation_id=start_operation_id,
                result=terminal_state,
                error=(
                    "The instance exists, but the auto-start operation record expired before "
                    "its terminal verdict could be verified. Orcastra did not submit start again."
                ),
                invalidate_cache=True,
                announce_created=True,
            )
            return
        if verdict != "success":
            await _mark_create_unknown(
                row,
                operation_id=start_operation_id,
                error="The auto-start operation returned an unrecognized verdict.",
                result=state,
            )
            return
        if self._create_instance_status(verified).lower() == "running":
            await _finish_create_job(
                row,
                "success",
                operation_id=start_operation_id,
                result=terminal_state,
                invalidate_cache=True,
                announce_created=True,
            )
            return

        await _finish_create_job(
            row,
            "partial",
            operation_id=start_operation_id,
            result=terminal_state,
            error=(
                "The auto-start operation reported success, but exact readback did not reach "
                f"Running: {self._create_instance_status(verified)}"
            ),
            invalidate_cache=True,
            announce_created=True,
        )

    async def _complete_durable_create(
        self,
        client: Any,
        row: InstanceOperationJob,
        instance: Any,
        operation_id: Optional[str],
        *,
        operation_verdict: str = "success",
        operation_error: Optional[str] = None,
    ) -> None:
        result = self._create_result(row, instance)
        if operation_verdict == "failure":
            await _finish_create_job(
                row,
                "partial",
                operation_id=operation_id,
                result=result,
                error=(
                    "The create operation reported failure, but exact readback found an "
                    f"instance: {operation_error or 'unknown daemon error'}"
                ),
                invalidate_cache=True,
                announce_created=True,
            )
            return

        if operation_verdict == "expired":
            await _finish_create_job(
                row,
                "partial",
                operation_id=operation_id,
                result=result,
                error=(
                    "The create operation record expired before its terminal verdict could be "
                    "verified, but exact readback found an instance. Orcastra did not submit "
                    "create or auto-start again."
                ),
                invalidate_cache=True,
                announce_created=True,
            )
            return

        if operation_verdict == "unknown":
            await _mark_create_unknown(
                row,
                operation_id=operation_id,
                error=(
                    "The create operation has no terminal verdict. Exact readback found the "
                    "instance, but reconciliation will not report success or submit auto-start."
                ),
                result={**result, "ambiguous_action": "create"},
            )
            return

        start_requested = bool((row.requested or {}).get("start_on_create"))
        current_status = self._create_instance_status(instance)
        if not start_requested or current_status.lower() == "running":
            await _finish_create_job(
                row,
                "success",
                operation_id=operation_id,
                result=result,
                invalidate_cache=True,
                announce_created=True,
            )
            return

        # Fence immediately before the external mutation. Persisting the action marker first is
        # what makes a crash between POST and operation-id storage recoverable without replay.
        start_state = {
            **result,
            "ambiguous_action": "start",
            "create_operation_id": operation_id,
            "start_operation_id": None,
        }
        owner = getattr(row, "locked_by", None)
        owns_job = await _set_create_job(
            row.job_id,
            worker_id=owner,
            phase="starting",
            status="running",
            result=start_state,
            locked_at=datetime.now(timezone.utc),
        )
        if not owns_job:
            # A recovery worker owns the lease now. It alone may decide whether start is needed.
            return

        try:
            start_operation_id = await run_blocking(
                self._submit_start_operation,
                client,
                row.instance_name,
                timeout=_CREATE_SUBMIT_TIMEOUT_SECONDS,
                label=(
                    f"submit auto-start[{row.cluster_id}/{row.project}/{row.instance_name}]"
                ),
                executor=_create_executor,
                max_in_flight=_CREATE_MAX_IN_FLIGHT,
                concurrency_key=row.cluster_id,
                max_in_flight_per_key=_CREATE_MAX_IN_FLIGHT_PER_CLUSTER,
            )
        except BlockingPoolFull as exc:
            # The action was not scheduled. This is a known partial result, not ambiguity.
            await _finish_create_job(
                row,
                "partial",
                operation_id=operation_id,
                result=result,
                error=f"The instance was created but start capacity was unavailable: {_create_error(exc)}",
                invalidate_cache=True,
                announce_created=True,
            )
            return
        except Exception as exc:  # noqa: BLE001 - the pre-POST marker prevents replay
            instance_after, complete, _ = await self._read_created_instance(client, row)
            if _ambiguous_create_error(exc):
                if complete and instance_after is not None:
                    start_state.update(self._create_result(row, instance_after))
                await _mark_create_unknown(
                    row,
                    operation_id=operation_id,
                    error=(
                        "The instance was created, but the auto-start submission outcome is "
                        f"unknown: {_create_error(exc)}"
                    ),
                    result=start_state,
                )
                return
            await _finish_create_job(
                row,
                "partial",
                operation_id=operation_id,
                result=(
                    self._create_result(row, instance_after)
                    if instance_after is not None
                    else result
                ),
                error=f"The instance was created but could not be started: {_create_error(exc)}",
                invalidate_cache=True,
                announce_created=True,
            )
            return

        start_state["start_operation_id"] = start_operation_id
        try:
            persisted = await _set_create_job(
                row.job_id,
                worker_id=owner,
                phase="starting",
                status="running",
                operation_id=start_operation_id or operation_id,
                result=start_state,
                locked_at=datetime.now(timezone.utc),
            )
        except Exception as exc:  # noqa: BLE001 - the start may already be accepted
            await _mark_create_unknown(
                row,
                operation_id=start_operation_id or operation_id,
                error=(
                    "The auto-start was accepted, but its operation could not be recorded: "
                    f"{_create_error(exc)}"
                ),
                result=start_state,
            )
            return
        if not persisted:
            # The pre-submit action marker remains durable. The lease winner will read state and
            # must not issue another start even when this operation id could not be attached.
            return
        await self._verify_durable_start(
            client,
            row,
            create_operation_id=operation_id,
            start_operation_id=start_operation_id,
        )

    async def _reconcile_durable_create(
        self,
        client: Any,
        job_id: str,
        *,
        worker_id: Optional[str],
        recovery: bool,
    ) -> None:
        """Reconcile operation plus exact readback. This function never submits a create."""
        row = await _load_create_job(job_id)
        if row is None or row.status in _CREATE_TERMINAL_STATUSES:
            return

        stored_result = getattr(row, "result", None)
        durable_result = stored_result if isinstance(stored_result, dict) else {}
        if row.phase == "starting" or durable_result.get("ambiguous_action") == "start":
            # Start may already have reached the daemon. Reconcile its operation and exact
            # readback only; never call the start endpoint a second time.
            create_operation_id = durable_result.get("create_operation_id")
            start_operation_id = durable_result.get("start_operation_id")
            if not start_operation_id and row.operation_id != create_operation_id:
                start_operation_id = row.operation_id
            await self._verify_durable_start(
                client,
                row,
                create_operation_id=create_operation_id,
                start_operation_id=start_operation_id,
            )
            return

        operation_id = row.operation_id
        # Recovery follows the same evidence order as the original worker. A resource can become
        # readable while its create operation is still running, so readback cannot replace a known
        # operation's terminal verdict. This path only observes state and never replays create.

        if operation_id:
            await _set_create_job(
                row.job_id,
                worker_id=worker_id,
                phase="verifying",
                status="running",
            )
            verdict, operation_error = await self._wait_for_create_operation(
                client, row, operation_id
            )
        else:
            verdict, operation_error = "unknown", "The create operation id is unavailable."

        instance, read_complete, read_error = await self._read_created_instance(client, row)
        if not read_complete:
            await _mark_create_unknown(
                row,
                operation_id=operation_id,
                error=(
                    "Neither the operation nor exact instance readback established a final "
                    f"create outcome: {read_error or operation_error or 'unknown error'}"
                ),
                result={"ambiguous_action": "create"},
            )
            return

        if instance is None:
            if verdict == "failure":
                await _finish_create_job(
                    row,
                    "failure",
                    operation_id=operation_id,
                    error=operation_error or "The daemon refused the create operation.",
                    invalidate_cache=True,
                )
                return
            if not operation_id and row.phase in {"pending", "preflighting"}:
                await _finish_create_job(
                    row,
                    "failure",
                    operation_id=None,
                    error="The backend stopped before submitting the create operation.",
                    invalidate_cache=True,
                )
                return
            await _mark_create_unknown(
                row,
                operation_id=operation_id,
                error=(
                    "The create operation has no terminal verdict and exact readback does not "
                    "yet show an instance. Reconciliation will not resubmit the create."
                ),
                result={"ambiguous_action": "create"},
            )
            return

        if not operation_id:
            # The daemon may still be applying an accepted create whose operation id was absent
            # or could not be persisted. Readback is useful evidence, but is not a terminal
            # verdict and cannot safely authorize success, failure, or auto-start.
            await _mark_create_unknown(
                row,
                operation_id=None,
                result={
                    **self._create_result(row, instance),
                    "ambiguous_action": "create",
                },
                error=(
                    "An instance with the requested identity exists, but the create operation "
                    "has no correlated terminal verdict. Recovery will not submit create or "
                    "auto-start again."
                ),
            )
            return

        await self._complete_durable_create(
            client,
            row,
            instance,
            operation_id,
            operation_verdict=verdict,
            operation_error=operation_error,
        )

    async def fail_create_job(
        self,
        job_id: str,
        *,
        error: str,
        error_code: str,
    ) -> None:
        """Finish a known synchronous refusal. No daemon state changed, so cache stays warm."""
        row = await _load_create_job(job_id)
        if row is None:
            return
        await _finish_create_job(
            row,
            "failure",
            operation_id=row.operation_id,
            result={"error_code": error_code},
            error=error,
            invalidate_cache=False,
        )

    async def _recover_create_job(self, job_id: str) -> None:
        worker_id = _create_worker_id()
        if not await _claim_create_job(job_id, worker_id, include_unknown=True):
            return
        row = await _load_create_job(job_id)
        if row is None:
            return
        heartbeat_task = asyncio.create_task(
            _create_lease_heartbeat(job_id, worker_id)
        )
        try:
            client = await run_blocking(
                self.connection_manager.get_project_client,
                row.cluster_id,
                row.project,
                (5, _CREATE_LXD_READ_TIMEOUT_SECONDS),
                timeout=_CREATE_CONNECT_TIMEOUT_SECONDS,
                label=(
                    f"recover create client[{row.cluster_id}/{row.project}/{row.instance_name}]"
                ),
                executor=_create_executor,
                max_in_flight=_CREATE_MAX_IN_FLIGHT,
                concurrency_key=row.cluster_id,
                max_in_flight_per_key=_CREATE_MAX_IN_FLIGHT_PER_CLUSTER,
            )
            if client is None and row.project == "default":
                client = await run_blocking(
                    self._get_client_with_reconnect,
                    row.cluster_id,
                    timeout=_CREATE_CONNECT_TIMEOUT_SECONDS,
                    label=f"recover create connection[{row.cluster_id}]",
                    executor=_create_executor,
                    max_in_flight=_CREATE_MAX_IN_FLIGHT,
                    concurrency_key=row.cluster_id,
                    max_in_flight_per_key=_CREATE_MAX_IN_FLIGHT_PER_CLUSTER,
                )
            if client is None:
                await _mark_create_unknown(
                    row,
                    operation_id=row.operation_id,
                    error=(
                        f"Could not open project '{row.project}' on {row.cluster_id}. "
                        "Recovery will retry without submitting another create."
                    ),
                    result=_preserve_create_ambiguity(row, "recovery"),
                )
            else:
                await self._reconcile_durable_create(
                    client,
                    job_id,
                    worker_id=worker_id,
                    recovery=True,
                )
        except Exception as exc:  # noqa: BLE001 - recovery must leave an inspectable job
            await _mark_create_unknown(
                row,
                operation_id=row.operation_id,
                error=(
                    "Create recovery could not read the cluster state: "
                    f"{_create_error(exc)}"
                ),
                result=_preserve_create_ambiguity(row, "recovery"),
            )
        finally:
            await _stop_create_heartbeat(heartbeat_task)

    async def _schedule_create_recovery_pass(self) -> int:
        now = datetime.now(timezone.utc)
        stale = now - timedelta(seconds=_CREATE_LEASE_SECONDS)
        unclaimed_before = now - timedelta(seconds=_CREATE_RECOVERY_GRACE_SECONDS)
        async with async_session_maker() as db:
            rows = (
                await db.execute(
                    select(InstanceOperationJob.job_id).where(
                        InstanceOperationJob.kind == "create",
                        InstanceOperationJob.status.in_(_CREATE_ACTIVE_STATUSES),
                        or_(
                            and_(
                                InstanceOperationJob.locked_at.is_(None),
                                InstanceOperationJob.updated_at < unclaimed_before,
                            ),
                            InstanceOperationJob.locked_at < stale,
                        ),
                    )
                )
            ).scalars().all()
        for job_id in rows:
            task = asyncio.create_task(self._recover_create_job(job_id))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        return len(rows)

    async def recover_create_operation_jobs(self) -> int:
        """Schedule one pass; main owns the single periodic loop for create and spec jobs."""
        return await self._schedule_create_recovery_pass()

    async def _judge_create_operation(self, client, name: str, operation_id: Optional[str],
                                      host: str, project: str) -> bool:
        """Wait for the queued create and write the terminal audit record. True if it landed.

        important: this is the single place a create is judged, and BOTH background branches go
        through it. They used to disagree. `_watch_create_operation` recorded a rejected create
        correctly but only ran when start_on_create was false, while the branch the form
        actually defaults to logged the same failure as `instance.autostart` and dropped the
        operation_id. So the guard written for this defect did not cover the path that hits it.

        The router records the create as `accepted` when LXD takes the request; this writes the
        verdict. One create, one final answer, either way.
        """
        if not operation_id:
            # Nothing queued to wait on. The synchronous path already answered.
            _announce_instance_created(host, project, name)
            return True

        from app.services.activity_logger import get_activity_logger

        try:
            await run_blocking(
                client.operations.wait_for_operation,
                operation_id,
                timeout=_CREATE_WALL_TIMEOUT_SECONDS,
                label=f"legacy create wait[{host}/{project}/{name}/{operation_id}]",
                executor=_create_executor,
                max_in_flight=_CREATE_MAX_IN_FLIGHT,
                concurrency_key=host,
                max_in_flight_per_key=_CREATE_MAX_IN_FLIGHT_PER_CLUSTER,
            )
        except Exception as e:
            safe_error = _create_error(e)
            logger.error(
                "Create operation failed for %s on %s (project=%s): %s",
                name,
                host,
                project,
                safe_error,
            )
            try:
                get_activity_logger().log_activity(
                    action="instance.create",
                    target_type="instance",
                    target_id=name,
                    target_host=host,
                    result="failure",
                    error_code="LXD_OPERATION_FAILED",
                    error_message=safe_error,
                    details={"project": project, "operation_id": operation_id},
                )
            except Exception:  # noqa: BLE001 - never let bookkeeping break the sweep
                pass
            return False

        logger.info(f"Create operation completed for {name} on {host} (project={project})")
        try:
            get_activity_logger().log_activity(
                action="instance.create",
                target_type="instance",
                target_id=name,
                target_host=host,
                result="success",
                details={"project": project, "operation_id": operation_id},
            )
        except Exception:  # noqa: BLE001
            pass

        # important: the add-on event is emitted HERE and not at the router, for the same reason
        # the terminal audit record is. The router answers "accepted" while LXD still has the
        # create queued, so announcing it there would tell every subscriber an instance exists
        # moments before LXD refuses it, with no correction to follow. This is the single place a
        # create is judged, so it is the only honest place to say it happened.
        await _emit_instance_created(host, project, name, operation_id)
        return True

    async def _watch_create_operation(
        self,
        client,
        name: str,
        operation_id: Optional[str],
        host: str,
        project: str,
        *,
        job_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        heartbeat_task: Optional[asyncio.Task[Any]] = None,
    ) -> None:
        """Background: record whether the create operation LXD queued actually succeeded.

        The no-autostart branch. Judging is shared with the autostart branch so the two cannot
        drift again; see _judge_create_operation.
        """
        if job_id:
            try:
                await self._reconcile_durable_create(
                    client,
                    job_id,
                    worker_id=worker_id,
                    recovery=False,
                )
            except Exception as exc:  # noqa: BLE001 - background errors stay visible in the job
                row = await _load_create_job(job_id)
                if row is not None:
                    await _mark_create_unknown(
                        row,
                        operation_id=operation_id,
                        error=f"Create verification could not finish: {_create_error(exc)}",
                        result=_preserve_create_ambiguity(row, "verification"),
                    )
            finally:
                await _stop_create_heartbeat(heartbeat_task)
            return
        await self._judge_create_operation(client, name, operation_id, host, project)

    async def _finalize_and_start(
        self,
        client,
        name: str,
        operation_id: Optional[str],
        host: str,
        project: str,
        *,
        job_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        heartbeat_task: Optional[asyncio.Task[Any]] = None,
    ) -> None:
        """Background: wait for the create operation to finish, then start the instance.

        important: the create is judged BEFORE the start is attempted, and the two failures are
        recorded under different actions. This used to be one try block covering both, logging
        everything as `instance.autostart`, which meant a create LXD rejected was filed as a
        start problem on an instance that had never existed, without its operation_id. And
        because the form defaults start_on_create to true, that was the path almost every
        operator took: `_watch_create_operation`, written specifically to catch a rejected
        create, only ran on the other branch.
        """
        if job_id:
            try:
                await self._reconcile_durable_create(
                    client,
                    job_id,
                    worker_id=worker_id,
                    recovery=False,
                )
            except Exception as exc:  # noqa: BLE001 - background errors stay visible in the job
                row = await _load_create_job(job_id)
                if row is not None:
                    await _mark_create_unknown(
                        row,
                        operation_id=operation_id,
                        error=f"Create/start verification could not finish: {_create_error(exc)}",
                        result=_preserve_create_ambiguity(row, "verification"),
                    )
            finally:
                await _stop_create_heartbeat(heartbeat_task)
            return

        # Phase 1: did the instance actually get created?
        if not await self._judge_create_operation(client, name, operation_id, host, project):
            # The instance does not exist, so there is nothing to start and no second failure
            # to report. The create failure has already been recorded.
            return

        # Phase 2: it exists. A failure from here is genuinely about starting it.
        try:
            instance = await run_blocking(
                client.instances.get,
                name,
                timeout=_CREATE_READBACK_TIMEOUT_SECONDS,
                label=f"legacy create readback[{host}/{project}/{name}]",
                executor=_create_executor,
                max_in_flight=_CREATE_MAX_IN_FLIGHT,
                concurrency_key=host,
                max_in_flight_per_key=_CREATE_MAX_IN_FLIGHT_PER_CLUSTER,
            )
            await run_blocking(
                self._start_and_wait,
                instance,
                host,
                project,
                timeout=_START_WAIT_SECONDS + _GRACEFUL_READ_MARGIN_SECONDS,
                label=f"legacy create auto-start[{host}/{project}/{name}]",
                executor=_create_executor,
                max_in_flight=_CREATE_MAX_IN_FLIGHT,
                concurrency_key=host,
                max_in_flight_per_key=_CREATE_MAX_IN_FLIGHT_PER_CLUSTER,
            )
            logger.info(f"Auto-started {name} on {host} (project={project})")
            # important: the list cache was cleared when the create was accepted, and the
            # console re-read it while the instance was still Stopped. Without clearing it
            # again here the row read Stopped for a full TTL after the cluster had it Running.
            self._invalidate_lists_after_background_change(name, host)
        except Exception as e:
            logger.error(
                f"Auto-start failed for {name} on {host} (project={project}): {e}"
            )
            # The instance exists but did not start. Distinct from a create failure: the
            # operator has something to look at and retry.
            try:
                from app.services.activity_logger import get_activity_logger
                get_activity_logger().log_activity(
                    action="instance.autostart",
                    target_type="instance",
                    target_id=name,
                    target_host=host,
                    result="failure",
                    error_message=str(e),
                    details={"project": project, "operation_id": operation_id},
                )
            except Exception:
                pass
