"""Instance snapshot endpoints (dashboard, JWT).

Operator rollback points, not the tenant snapshot product. OrcaHub sells snapshots as a metered
per-GB service and drives them through `/integrations/raw-proxy`; this surface exists for the
different job of an operator taking a restore point before risky maintenance, which previously
meant leaving the console and running `lxc snapshot` over SSH.

Three rules follow from that split and are load-bearing:

* Everything created here is named `ops-...` and carries an LXD `expires_at`, so LXD prunes it
  whether or not this service is running, and so the snapshot is identifiable as operator work
  by anyone reading the cluster.
* `snapshots.schedule` and `snapshots.expiry` are read and never written. They are OrcaHub's
  retention mechanism for a schedule a tenant pays for, and a write here would leave its ledger
  disagreeing with the cluster.
* A source carrying an external schedule is badged and the caller warned, but not blocked.
  Refusing does not prevent the snapshot, it moves it back to an SSH session where neither
  product can see it.
"""
import logging
from typing import Annotated, Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.core.config import get_settings
from app.core.dependencies import (
    AuthenticatedUser,
    check_read_only_mode,
    require_tenant,
)
from app.core.lxd_names import LXD_NAME_DESCRIPTION, LXD_NAME_PATTERN
from app.core.cache import get_cache_manager
from app.core.lxd_errors import lxd_http_error
from app.core.org_scope import (
    org_scope_dependency,
    verify_cluster_access,
    verify_tenant_permission,
)
from app.core.single_flight import rebuild_lock, release_lock
from app.core.snapshot_billing_gate import audit_billing_override, enforce_billing_impact
from app.core.snapshot_ownership_gate import enforce_snapshot_ownership
from app.core.snapshot_safety_gate import take_safety_snapshot
from app.core.tenant_visibility import tenant_acl_active
from app.core.blocking import run_blocking
from app.services.activity_logger import get_activity_logger
from app.services.lxd import snapshots as snapshots_service
from app.services.lxd.availability import require_client
from app.services.lxd.lazy_client import client_for
from app.services.lxd.proxy import PROXY_MAX_IN_FLIGHT, proxy_executor
from app.services.lxd_service import LXDService, get_lxd_service
from app.models.schemas import (
    SnapshotActionResponse,
    SnapshotCreateRequest,
    SnapshotListResponse,
    SnapshotRestoreRequest,
)
from app.services.orcahub_webhook import schedule_orcahub_event

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/instances",
    tags=["Instances - Snapshots"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)

_LXD_CALL_TIMEOUT = 30


def _blocking(fn, *args, label: str, **kwargs):
    """Every LXD call here goes off the event loop on the shared proxy pool.

    important: the pooled client's own socket timeout is what actually frees the worker
    thread. run_blocking only frees the caller, so a call without a bounded socket read
    would leak the thread even though the request returned promptly.
    """
    return run_blocking(
        fn,
        *args,
        timeout=_LXD_CALL_TIMEOUT,
        label=label,
        executor=proxy_executor,
        max_in_flight=PROXY_MAX_IN_FLIGHT,
        **kwargs,
    )


def _assert_snapshot_surface_enabled() -> None:
    """Refuse a tenant outright while this deployment has not enabled tenant snapshots.

    Split from _enforce_snapshot_access because the two halves answer different questions and the
    fleet read needs only this one. Asking the scope-specific half there would 403 a tenant who
    holds grants on two of five clusters, when the right answer is those two clusters' rows.
    """
    if not tenant_acl_active():
        return
    if not get_settings().snapshot_tenant_enforcement_enabled:
        raise HTTPException(
            status_code=403,
            detail="Snapshot actions are not enabled for tenant accounts on this deployment.",
        )


def _enforce_snapshot_access(
    host: str,
    project: str,
    instance_name: Optional[str],
    *,
    need_operate: bool = False,
) -> None:
    """Authorize a snapshot action for the current caller.

    A no-op for Admin and Partner, who carry no tenant ACL. For a tenant the `snapshot`
    permission is honoured only when its own flag is on: the permission has been granted by
    OrcaHub's sync for a long time while nothing enforced it, so a large number of live grants
    would otherwise have activated the moment this shipped.

    important: called before the try in every handler. Raised inside one, the 403 is swallowed
    by the generic except and re-raised as a 500, which reads as a broken server rather than a
    refusal.
    """
    _assert_snapshot_surface_enabled()
    verify_tenant_permission(host, project, instance_name, "snapshot")
    if need_operate:
        # Restore stops the instance and discards everything written since the snapshot. That is
        # a lifecycle action, so it needs the lifecycle permission on top of the snapshot one.
        verify_tenant_permission(host, project, instance_name, "operate")


def _validated_target(snapshot_name: str) -> str:
    """Refuse a name that cannot safely become a request path, and return the validated form.

    important: input validation only. It used to carry the ownership rule too, which is what
    refused an operator their own hand-taken snapshot; ownership is now `_gate_ownership`. pylxd
    interpolates the name into the LXD URL with no escaping (`_APINode.__getitem__`), so this
    applies to every class, not only the ones the console owns.

    important: the RETURN VALUE is what the caller must use. The validator normalises before it
    checks, so discarding it meant a name was approved in one form and sent to the daemon in
    another: `" ops-target "` was validated as `ops-target` and then addressed with its spaces
    intact, which is a 404 on a snapshot that exists.
    """
    try:
        return snapshots_service.assert_snapshot_name(snapshot_name)
    except snapshots_service.SnapshotNameError as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "snapshot_name_invalid",
                "message": str(exc),
                "field": "snapshot_name",
            },
        ) from exc


def _gate_ownership(
    snapshot_name: str,
    action: str,
    instance_name: str,
    host: str,
    project: str,
    *,
    confirm_snapshot: Optional[str] = None,
    manual_already_confirmed: bool = False,
    acknowledged_upstream: bool = False,
    reason: Optional[str] = None,
) -> Optional[str]:
    """Ownership decision plus the audit the refusal owes.

    important: a denial here used to be invisible. `_gate_billing` records `result="denied"` and
    this one raised straight through, so the most alert-worthy refusal on the surface left no
    trace. Same wrapper shape, for the same reason.
    """
    origin = snapshots_service.classify_snapshot_origin(snapshot_name)
    try:
        return enforce_snapshot_ownership(
            snapshot_name,
            instance_name,
            action,
            confirm_snapshot=confirm_snapshot,
            manual_already_confirmed=manual_already_confirmed,
            acknowledged_upstream=acknowledged_upstream,
            reason=reason,
        )
    except HTTPException as exc:
        code = exc.detail.get("error") if isinstance(exc.detail, dict) else "snapshot_refused"
        _audit(
            f"instance.snapshot.{action}", instance_name, host, project,
            result="denied", error_message=code,
            details={"snapshot": snapshot_name, "snapshot_origin": origin.value},
        )
        raise


async def _read_policy(client, instance_name: str, project: str) -> Dict[str, Any]:
    """Read the source's snapshot policy, mapping a cluster failure to a real status.

    Separate from the handler body so the billing gate can run outside the handler's try. A 403
    raised inside one is caught by the generic except and re-raised as a 500.
    """
    try:
        return await _blocking(
            snapshots_service.read_instance_policy,
            client,
            instance_name,
            project,
            label="snapshots.policy",
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise lxd_http_error(exc, "Instance", instance_name)


def _gate_billing(
    policy: Dict[str, Any],
    action: str,
    instance_name: str,
    host: str,
    project: str,
    *,
    acknowledged: bool,
    reason: Optional[str],
) -> Optional[str]:
    """Apply the billing gate and record a refusal.

    important: a refusal is audited. Being stopped on a billing-linked instance is the event most
    worth alerting on, and it used to travel out through `except HTTPException: raise` leaving no
    trace anywhere.
    """
    try:
        return enforce_billing_impact(
            policy, instance_name, acknowledged=acknowledged, reason=reason,
        )
    except HTTPException:
        _audit(
            f"instance.snapshot.{action}", instance_name, host, project,
            result="denied",
            error_message="billing_impact_acknowledgement_required",
            details={
                "snapshot_action": action,
                "external_schedule": policy.get("schedule"),
                "external_expiry": policy.get("expiry"),
            },
        )
        raise


async def _settle(client, operation_id: Optional[str], project: str) -> Dict[str, Any]:
    """Wait briefly for an LXD operation so the response describes reality.

    A snapshot on a copy-on-write pool settles in well under a second. On a `dir` pool it is a
    full copy and may not settle at all inside this window, which is reported as unsettled
    rather than as either success or failure.
    """
    if not operation_id:
        return {"status": None, "err": None, "settled": True}
    try:
        return await _blocking(
            snapshots_service.wait_for_operation,
            client,
            operation_id,
            project,
            label="snapshots.wait",
        )
    except Exception as exc:  # noqa: BLE001 - a failed wait is not a failed operation
        logger.debug("Could not wait on operation %s: %s", operation_id, exc)
        return {"status": None, "err": None, "settled": False}


async def _client(lxd_service: LXDService, host: str):
    return require_client(await client_for(lxd_service, host, label="snapshots"), host)


def _audit(action: str, instance_name: str, host: str, project: str, **kwargs: Any) -> None:
    activity_logger.log_activity(
        action=action,
        target_type="instance_snapshot",
        target_id=instance_name,
        target_host=host,
        target_project=project,
        **kwargs,
    )


@router.get("/{instance_name}/snapshots", response_model=SnapshotListResponse)
async def list_snapshots(
    instance_name: Annotated[
        str,
        Path(pattern=LXD_NAME_PATTERN, description=f"Instance to act on. {LXD_NAME_DESCRIPTION}"),
    ],
    host: str = Query(..., description="Cluster the instance lives on"),
    project: str = Query("default", description="LXD project the instance lives in"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    verify_cluster_access(host)
    _enforce_snapshot_access(host, project, instance_name)

    client = await _client(lxd_service, host)
    try:
        items = await _blocking(
            snapshots_service.list_instance_snapshots,
            client,
            instance_name,
            project,
            label="snapshots.list",
        )
        policy = await _blocking(
            snapshots_service.read_instance_policy,
            client,
            instance_name,
            project,
            label="snapshots.policy",
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - mapped to a meaningful status below
        raise lxd_http_error(exc, "Instance", instance_name)

    return SnapshotListResponse(total=len(items), items=items, policy=policy)


@router.post("/{instance_name}/snapshots", response_model=SnapshotActionResponse, status_code=201)
async def create_snapshot(
    instance_name: Annotated[
        str,
        Path(pattern=LXD_NAME_PATTERN, description=f"Instance to act on. {LXD_NAME_DESCRIPTION}"),
    ],
    body: SnapshotCreateRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_tenant()),
):
    verify_cluster_access(body.host)
    _enforce_snapshot_access(body.host, body.project, instance_name)

    client = await _client(lxd_service, body.host)
    policy = await _read_policy(client, instance_name, body.project)
    justification = _gate_billing(
        policy, "create", instance_name, body.host, body.project,
        acknowledged=body.acknowledge_billing_impact, reason=body.reason,
    )

    snapshot_name = snapshots_service.build_operator_snapshot_name(body.label)
    expires_at = snapshots_service.default_expiry(body.expiry_days)

    # important: the cap is read from the cluster and then acted on, which is two round trips.
    # Without this lock, concurrent creates for one instance all read the same pre-cap count and
    # all proceed. Scoped to one worker process like every lock in this codebase, so under
    # WEB_CONCURRENCY=4 the bound is the cap plus three rather than the cap exactly. That is a
    # guardrail against accumulation, not a security boundary, and each snapshot carries its own
    # expires_at, so LXD prunes the overshoot without help.
    lock_key = f"snapshot-create:{body.host}/{body.project}/{instance_name}"
    lock = rebuild_lock(lock_key)
    try:
        async with lock:
            try:
                await _blocking(
                    snapshots_service.assert_capacity,
                    client,
                    instance_name,
                    body.project,
                    label="snapshots.capacity",
                )
                operation_id = await _blocking(
                    snapshots_service.create_instance_snapshot,
                    client,
                    instance_name,
                    body.project,
                    snapshot_name,
                    stateful=body.stateful,
                    expires_at=expires_at,
                    label="snapshots.create",
                )
                outcome = await _settle(client, operation_id, body.project)
            except HTTPException:
                raise
            except snapshots_service.LXDOperationUnreadable as exc:
                # The daemon took the work and told us nothing we can wait on. That is an upstream
                # problem, not a bad request, and reporting success would be the exact failure this
                # surface exists to avoid.
                raise HTTPException(status_code=502, detail=str(exc))
            except snapshots_service.SnapshotLimitError as exc:
                _audit(
                    "instance.snapshot.create", instance_name, body.host, body.project,
                    result="denied", error_message=str(exc),
                )
                raise HTTPException(status_code=409, detail=str(exc))
            except snapshots_service.SnapshotNameError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
            except Exception as exc:  # noqa: BLE001
                _audit(
                    "instance.snapshot.create", instance_name, body.host, body.project,
                    result="failure", error_message=str(exc),
                )
                raise lxd_http_error(exc, "Instance", instance_name)
    finally:
        release_lock(lock_key)

    if outcome.get("failed"):
        # The daemon refused after accepting the request. Reporting 201 here would be the exact
        # defect this feature exists to avoid: a control that says it worked and did not.
        _audit(
            "instance.snapshot.create", instance_name, body.host, body.project,
            result="failure", error_message=outcome.get("err"),
            details={"snapshot": snapshot_name},
        )
        raise HTTPException(
            status_code=502,
            detail=f"The cluster refused the snapshot: {outcome.get('err') or 'unknown error'}",
        )

    _audit(
        "instance.snapshot.create", instance_name, body.host, body.project,
        result="success" if outcome.get("settled") else "accepted",
        details={
            "operation_id": operation_id,
            "snapshot": snapshot_name,
            "expires_at": expires_at,
            "stateful": body.stateful,
            "externally_managed_source": bool(policy.get("externally_managed")),
            "reason": (body.reason or "").strip() or None,
            "requester": user.email or user.user_id,
            "settled": outcome.get("settled"),
        },
    )
    if justification:
        audit_billing_override(
            "create", instance_name, body.host, body.project, policy, justification,
            user.email or user.user_id,
        )
    # OrcaHub's receiver already understands SNAPSHOT events; emitting means a snapshot taken
    # here is known to it immediately rather than being discovered an hour later by its sweep.
    schedule_orcahub_event(
        event_type="snapshot.created",
        cluster_id=body.host,
        project=body.project,
        resource_type="SNAPSHOT",
        name=snapshot_name,
    )
    # The fleet listing is cached for 30s. Without this an operator acts on a snapshot and
    # the row is still there when the page refreshes, which reads as a control that did
    # nothing. Cleared only on the success path: a refused operation changed no listing.
    get_cache_manager().invalidate_resource_cache("snapshots_fleet")
    return SnapshotActionResponse(
        success=True,
        instance_name=instance_name,
        snapshot=snapshot_name,
        operation_id=operation_id,
        message=(
            f"Snapshot {snapshot_name} created, expiring {expires_at}."
            if outcome.get("settled")
            else f"Snapshot {snapshot_name} is still being written. It will expire {expires_at}."
        ),
    )


@router.delete("/{instance_name}/snapshots/{snapshot_name}", response_model=SnapshotActionResponse)
async def delete_snapshot(
    instance_name: Annotated[
        str,
        Path(pattern=LXD_NAME_PATTERN, description=f"Instance to act on. {LXD_NAME_DESCRIPTION}"),
    ],
    snapshot_name: str,
    host: str = Query(..., description="Cluster the instance lives on"),
    project: str = Query("default", description="LXD project the instance lives in"),
    reason: Optional[str] = Query(
        None,
        max_length=280,
        description="Why the snapshot is being deleted. Required when the source is billed elsewhere",
    ),
    acknowledge_billing_impact: bool = Query(
        False,
        description="Proceed on a source whose snapshots are billed elsewhere. Requires a reason",
    ),
    confirm_snapshot: Optional[str] = Query(
        None,
        max_length=63,
        description="The snapshot's own name. Required when it was taken outside this console",
    ),
    acknowledge_upstream_snapshot: bool = Query(
        False,
        description=(
            "Proceed on a snapshot the upstream product owns. Requires a reason. Deliberately "
            "NOT the billing acknowledgement: they answer different questions"
        ),
    ),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_tenant()),
):
    verify_cluster_access(host)
    _enforce_snapshot_access(host, project, instance_name)

    # important: name safety, then ownership, both before a client is opened. Everything
    # decidable from the request alone is decided before anything touches the cluster, which is
    # what keeps a refusal free of round trips and stops an acknowledgement bought on one gate
    # from being spent on another.
    snapshot_name = _validated_target(snapshot_name)
    _gate_ownership(
        snapshot_name, "delete", instance_name, host, project,
        confirm_snapshot=confirm_snapshot,
        acknowledged_upstream=acknowledge_upstream_snapshot,
        reason=reason,
    )

    client = await _client(lxd_service, host)
    # Deleting lowers the storage the external schedule is measured on rather than raising it,
    # so this is the gentlest of the three actions. It is gated anyway: the rule an operator
    # has to hold is "anything on a billing-linked instance is acknowledged", and a rule with
    # an exception is one people have to remember the shape of.
    policy = await _read_policy(client, instance_name, project)
    justification = _gate_billing(
        policy, "delete", instance_name, host, project,
        acknowledged=acknowledge_billing_impact, reason=reason,
    )

    try:
        operation_id = await _blocking(
            snapshots_service.delete_instance_snapshot,
            client,
            instance_name,
            project,
            snapshot_name,
            label="snapshots.delete",
        )
        outcome = await _settle(client, operation_id, project)
    except HTTPException:
        raise
    except snapshots_service.LXDOperationUnreadable as exc:
        # The daemon took the work and told us nothing we can wait on. Upstream problem, not a bad
        # request, and reporting success would be the exact failure this surface exists to avoid.
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        _audit(
            "instance.snapshot.delete", instance_name, host, project,
            result="failure", error_message=str(exc), details={"snapshot": snapshot_name},
        )
        raise lxd_http_error(exc, "Snapshot", snapshot_name)

    if outcome.get("failed"):
        # The daemon accepted the request and then refused it. Answering 200 "deleted" here would
        # tell an operator a restore point is gone while it is still on the cluster.
        _audit(
            "instance.snapshot.delete", instance_name, host, project,
            result="failure", error_message=outcome.get("err"),
            details={"snapshot": snapshot_name},
        )
        raise HTTPException(
            status_code=502,
            detail=f"The cluster refused the deletion: {outcome.get('err') or 'unknown error'}",
        )

    _audit(
        "instance.snapshot.delete", instance_name, host, project,
        result="success" if outcome.get("settled") else "accepted",
        details={
            "operation_id": operation_id,
            "snapshot": snapshot_name,
            "requester": user.email or user.user_id,
            "settled": outcome.get("settled"),
        },
    )
    if justification:
        audit_billing_override(
            "delete", instance_name, host, project, policy, justification,
            user.email or user.user_id,
        )
    schedule_orcahub_event(
        event_type="snapshot.deleted",
        cluster_id=host,
        project=project,
        resource_type="SNAPSHOT",
        name=snapshot_name,
    )
    # The fleet listing is cached for 30s. Without this an operator acts on a snapshot and
    # the row is still there when the page refreshes, which reads as a control that did
    # nothing. Cleared only on the success path: a refused operation changed no listing.
    get_cache_manager().invalidate_resource_cache("snapshots_fleet")
    return SnapshotActionResponse(
        success=True,
        instance_name=instance_name,
        snapshot=snapshot_name,
        operation_id=operation_id,
        message=(
            f"Snapshot {snapshot_name} deleted."
            if outcome.get("settled")
            else f"Snapshot {snapshot_name} is being removed."
        ),
    )


@router.post(
    "/{instance_name}/snapshots/{snapshot_name}/restore",
    response_model=SnapshotActionResponse,
)
async def restore_snapshot(
    instance_name: Annotated[
        str,
        Path(pattern=LXD_NAME_PATTERN, description=f"Instance to act on. {LXD_NAME_DESCRIPTION}"),
    ],
    snapshot_name: str,
    body: SnapshotRestoreRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    user: AuthenticatedUser = Depends(require_tenant()),
):
    verify_cluster_access(body.host)
    _enforce_snapshot_access(body.host, body.project, instance_name, need_operate=True)

    if body.confirm_name != instance_name:
        raise HTTPException(
            status_code=422, detail="Confirmation name does not match the instance name"
        )

    # Name safety, then ownership, both before a client is opened and long before anything is
    # written. Restore already types the INSTANCE name above, which is the confirmation the
    # hand-taken class needs; the gate below asks for the snapshot's name only on delete, where
    # what an operator gets wrong is which row they clicked.
    snapshot_name = _validated_target(snapshot_name)
    _gate_ownership(
        snapshot_name, "restore", instance_name, body.host, body.project,
        manual_already_confirmed=True,
        acknowledged_upstream=body.acknowledge_upstream_snapshot,
        reason=body.reason,
    )

    client = await _client(lxd_service, body.host)
    # Gated before the safety snapshot, not after: a refused restore must not leave a snapshot
    # behind on a source whose storage someone else is billed for.
    policy = await _read_policy(client, instance_name, body.project)
    justification = _gate_billing(
        policy, "restore", instance_name, body.host, body.project,
        acknowledged=body.acknowledge_billing_impact, reason=body.reason,
    )

    safety_name: Optional[str] = None
    try:
        # important: the target has to exist before anything is written. Restoring a name that is
        # not on the instance fails at the daemon, and because the safety copy was taken first it
        # left a snapshot behind on a billed source for a restore that never ran.
        existing = await _blocking(
            snapshots_service.list_instance_snapshots,
            client,
            instance_name,
            body.project,
            label="snapshots.list",
        )
        if not any(row.get("name") == snapshot_name for row in existing):
            raise HTTPException(
                status_code=404,
                detail=f"Snapshot '{snapshot_name}' does not exist on {instance_name}.",
            )

        # Its own module beside the ownership and billing gates, because it is a third refusal
        # with the same shape: it decides whether the action may proceed and says why not.
        safety_name = await take_safety_snapshot(
            client,
            instance_name=instance_name,
            host=body.host,
            project=body.project,
            wanted=body.safety_snapshot,
            existing=existing,
            blocking=_blocking,
            settle=_settle,
            audit=lambda **kw: _audit(
                "instance.snapshot.restore", instance_name, body.host, body.project,
                details={"snapshot": snapshot_name, **kw.pop("details", {})}, **kw
            ),
        )

        operation_id = await _blocking(
            snapshots_service.restore_instance_snapshot,
            client,
            instance_name,
            body.project,
            snapshot_name,
            label="snapshots.restore",
        )
        outcome = await _settle(client, operation_id, body.project)
    except HTTPException:
        raise
    except snapshots_service.LXDOperationUnreadable as exc:
        # The daemon took the work and told us nothing we can wait on. Upstream problem, not a bad
        # request, and reporting success would be the exact failure this surface exists to avoid.
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        _audit(
            "instance.snapshot.restore", instance_name, body.host, body.project,
            result="failure",
            error_message=str(exc),
            details={"snapshot": snapshot_name, "safety_snapshot": safety_name},
        )
        raise lxd_http_error(exc, "Snapshot", snapshot_name)

    if outcome.get("failed"):
        # A restore stops the instance before rolling it back, so a daemon-side failure can leave
        # it stopped. Reporting success would send the operator away from a broken instance.
        _audit(
            "instance.snapshot.restore", instance_name, body.host, body.project,
            result="failure", error_message=outcome.get("err"),
            details={"snapshot": snapshot_name, "safety_snapshot": safety_name},
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"The cluster refused the restore: {outcome.get('err') or 'unknown error'}."
                + (f" Safety snapshot {safety_name} was taken and is still present." if safety_name else "")
            ),
        )

    _audit(
        "instance.snapshot.restore", instance_name, body.host, body.project,
        result="success" if outcome.get("settled") else "accepted",
        details={
            "operation_id": operation_id,
            "snapshot": snapshot_name,
            "safety_snapshot": safety_name,
            "requester": user.email or user.user_id,
            "settled": outcome.get("settled"),
        },
    )
    if justification:
        audit_billing_override(
            "restore", instance_name, body.host, body.project, policy, justification,
            user.email or user.user_id,
        )
    # The fleet listing is cached for 30s. Without this an operator acts on a snapshot and
    # the row is still there when the page refreshes, which reads as a control that did
    # nothing. Cleared only on the success path: a refused operation changed no listing.
    get_cache_manager().invalidate_resource_cache("snapshots_fleet")
    return SnapshotActionResponse(
        success=True,
        instance_name=instance_name,
        snapshot=snapshot_name,
        operation_id=operation_id,
        message=(
            (
                f"{instance_name} restored to {snapshot_name}."
                if outcome.get("settled")
                else f"Restoring {instance_name} to {snapshot_name}."
            )
            + (f" Safety snapshot {safety_name} taken first." if safety_name else "")
        ),
    )
