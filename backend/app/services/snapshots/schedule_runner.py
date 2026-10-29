"""One fire of one schedule: authorize, prune, create, record.

The order is the design. Everything decidable without touching a cluster runs first, every read
runs before any write, and retention runs before the create rather than after it.

Prune-before-create is not a preference. Peak occupancy under prune-after-create is `keep_last + 1`,
and that peak lands at exactly the instant a concurrent manual snapshot would be refused by
`assert_capacity`. Pruning first means the schedule's ceiling is `keep_last`, strictly below the
instance's five, and the two spare slots stay the operator's.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.cache import get_cache_manager
from app.core.lxd_names import LXD_NAME_PATTERN
from app.core.request_context import RequestContext, set_request_context
from app.services.activity_logger import get_activity_logger
from app.services.lxd import snapshots as snapshots_service
from app.services.lxd.lazy_client import client_for
from app.services.migration_fence import (
    MigrationFenceConflict,
    guard_instance_mutation,
)
from app.services.snapshots import fleet_sizes
from app.services.snapshots import schedule_authority as authority
from app.services.snapshots.naming import build_scheduled_snapshot_name, scheduled_snapshot_prefix

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

# important: its own pool, never `proxy_executor`. That one is eight workers shared with the paid
# integration's raw proxy, and the isolation argument is symmetric: a schedule firing across the
# fleet must not occupy the workers OrcaHub is waiting on, and OrcaHub must not be able to starve
# every schedule by being busy.
_SCHEDULE_WORKERS = 4
_SCHEDULE_MAX_IN_FLIGHT = 8
_SCHEDULE_EXECUTOR = ThreadPoolExecutor(
    max_workers=_SCHEDULE_WORKERS, thread_name_prefix="snapshot-schedule"
)

# A snapshot on a `dir` pool is a full copy of the instance, so the settle wait is the same 20 the
# operator path uses rather than something tighter.
_LXD_CALL_TIMEOUT = 30.0
_SETTLE_TIMEOUT = 20

_NAME_RE = re.compile(LXD_NAME_PATTERN)

# What LXD says when it already holds the name. Confirmed against a live daemon rather than assumed:
# `Failed creating instance snapshot record "<name>": Snapshot "<instance>/<name>" already exists`,
# delivered asynchronously as the operation's error, never as an HTTP status.
_ALREADY_EXISTS = "already exists"


def _invalidate_snapshot_views(schedule: Any) -> None:
    """Best-effort refresh after LXD has already decided the snapshot outcome.

    Only the scheduled instance's cached sizes are forgotten; an hourly schedule used to make the
    next Snapshots page read re-measure every holder on every cluster.
    """
    try:
        cache = get_cache_manager()
        cache.invalidate_resource_cache(
            "snapshots_fleet",
            "storage_pool_resources",
        )
        fleet_sizes.forget_holder(cache, schedule.cluster_id, schedule.project, schedule.instance_name)
    except Exception as exc:  # noqa: BLE001 - cache failure cannot rewrite an LXD verdict
        logger.warning("Could not invalidate scheduled snapshot caches: %s", exc)


@dataclass
class RunOutcome:
    """What happened, in the vocabulary the caller records and acts on."""

    result: str  # success | replayed | refused | failed | deferred
    snapshot: Optional[str] = None
    pruned: List[str] = field(default_factory=list)
    refusal: Optional[authority.Refusal] = None
    error: Optional[str] = None
    terminal: bool = False

    @property
    def counts_as_failure(self) -> bool:
        """`deferred` deliberately does not.

        A shed call is this service being busy, not the cluster being unhealthy. Counting it toward
        the pause threshold would let a busy console pause an operator's schedules.
        """
        return self.result in ("refused", "failed")


def adopt_schedule_context(schedule: Any) -> None:
    """Make the audit trail name the human who authorized this, not the process.

    important: a background task has no RequestContext, and `log_activity` fills the gap with an
    all-null actor and no warning. For the product's only unattended write path that is total loss
    of attribution, and it is invisible: nothing raises, the row is simply blank where the person
    should be.

    A synthetic "system" actor would be worse than blank, because it looks correct. The actor is
    the operator who created the schedule; `automated` in the details is what distinguishes their
    scheduled snapshot from one they took by hand this morning.

    Set per run, not once at start-up: a ContextVar written inside one asyncio Task does not reach
    another, and there is no reset helper, so this is overwrite-per-cycle by design.
    """
    set_request_context(
        RequestContext(
            user_id=schedule.owner_email,
            user_role="scheduler",
            extra={
                "schedule_id": schedule.id,
                "automated": True,
                "actor_source": "schedule_owner",
            },
        )
    )


def _audit(schedule: Any, action: str, result: str, **details: Any) -> None:
    try:
        activity_logger.log_activity(
            action=action,
            target_type="instance",
            target_id=schedule.instance_name,
            target_host=schedule.cluster_id,
            target_project=schedule.project,
            result=result,
            details={
                "schedule_id": schedule.id,
                "schedule_label": schedule.label,
                "automated": True,
                "requester": schedule.owner_email,
                **details,
            },
        )
    except Exception:  # pragma: no cover - a failed audit write must not fail the run
        logger.warning("Could not write the audit record for %s", action, exc_info=True)


def _stored_names_are_addressable(schedule: Any) -> Optional[authority.Refusal]:
    """Re-validate what will become a request path, at the moment it is used.

    important: these three strings were validated when the schedule was created and are used months
    later, by a code path the validating router is not part of. pylxd builds URLs by concatenation
    and escapes nothing, so a segment carrying `?` ends the path and moves the request to a
    different resource. Anything that edits a row afterwards, a migration, an admin fixup, a bug,
    puts an unvalidated string on the wire. Validation at creation is not validation at use.
    """
    for field_name in ("cluster_id", "project", "instance_name"):
        value = getattr(schedule, field_name, None) or ""
        if not _NAME_RE.match(value):
            return authority.Refusal(
                code="schedule_target_is_not_addressable",
                message=(
                    f"The stored {field_name} '{value}' is not a name this console can address, "
                    "so nothing was sent to the cluster."
                ),
                terminal=True,
            )
    return None


async def _blocking(fn, *args, label: str, **kwargs):
    return await run_blocking(
        fn,
        *args,
        timeout=_LXD_CALL_TIMEOUT,
        label=f"schedule[{label}]",
        executor=_SCHEDULE_EXECUTOR,
        max_in_flight=_SCHEDULE_MAX_IN_FLIGHT,
        **kwargs,
    )


def _select_own(rows: List[Dict[str, Any]], prefix: str) -> List[Dict[str, Any]]:
    """This schedule's snapshots, oldest first, and nobody else's."""
    mine = [row for row in rows if str(row.get("name") or "").startswith(prefix)]
    return sorted(mine, key=lambda row: str(row.get("created_at") or ""))


def _follow_renewed_pin(schedule: Any, current: Any) -> None:
    """Move a schedule onto the current pin once a public-CA renewal was accepted.

    A schedule lives longer than the bounded renewal lineage, so it carries the newest pin forward
    on every run instead of relying on the lineage to remember the one it was created against.
    The row is committed with the run's outcome.
    """
    from app.services.lxd_certs.pinning import PinState

    if isinstance(current, PinState) and schedule.cluster_fingerprint != current.fingerprint:
        logger.info(
            "Schedule %s follows the renewed certificate of %s",
            getattr(schedule, "id", "?"), schedule.cluster_id,
        )
        schedule.cluster_fingerprint = current.fingerprint


async def run_one(
    db: Any,
    schedule: Any,
    *,
    now: datetime,
    lxd_service: Any,
    fingerprint_reader=None,
) -> RunOutcome:
    """Serialize the complete scheduled snapshot run with migration admission."""
    adopt_schedule_context(schedule)
    # Validate stored path segments before constructing the migration guard identity. An empty
    # or malformed stored name is a terminal schedule refusal, not a transient fence outage.
    refusal = _stored_names_are_addressable(schedule)
    if refusal is not None:
        return RunOutcome(result="refused", refusal=refusal, terminal=refusal.terminal)
    try:
        async with guard_instance_mutation(
            db,
            cluster_id=schedule.cluster_id,
            project=schedule.project,
            instance_name=schedule.instance_name,
        ):
            return await _run_one_guarded(
                db,
                schedule,
                now=now,
                lxd_service=lxd_service,
                fingerprint_reader=fingerprint_reader,
            )
    except MigrationFenceConflict as exc:
        # A migration fence is expected to clear after rollback or cleanup. Keep the schedule
        # due without counting the migration window as a schedule failure.
        return RunOutcome(result="deferred", error=exc.detail)


async def _run_one_guarded(
    db: Any,
    schedule: Any,
    *,
    now: datetime,
    lxd_service: Any,
    fingerprint_reader=None,
) -> RunOutcome:
    """Fire one schedule for its `due_slot`, or say why not.

    Never raises for a business outcome. The caller records the outcome against the row, and a
    scheduler loop that has to catch exceptions to stay alive is a loop that will one day stop.
    """
    adopt_schedule_context(schedule)

    refusal = await authority.db_refusal_for(db, schedule)
    if refusal is None:
        refusal = _stored_names_are_addressable(schedule)
    if refusal is None and schedule.cluster_fingerprint:
        reader = fingerprint_reader
        if reader is None:
            from app.services.break_glass_service import cluster_pin_state

            reader = cluster_pin_state
        current = await reader(schedule.cluster_id)
        refusal = authority.fingerprint_refusal(schedule.cluster_fingerprint, current)
        if refusal is None:
            _follow_renewed_pin(schedule, current)
    if refusal is not None:
        return RunOutcome(result="refused", refusal=refusal, terminal=refusal.terminal)

    client = await client_for(lxd_service, schedule.cluster_id, label="schedule")
    if client is None:
        # Unreachable is not unauthorized. The slot is held and tried again.
        return RunOutcome(
            result="failed",
            error=f"Cluster '{schedule.cluster_id}' could not be reached.",
        )

    try:
        config, policy = await _blocking(
            snapshots_service.read_instance_config_and_policy,
            client,
            schedule.instance_name,
            schedule.project,
            label=f"read {schedule.instance_name}",
        )
        present = True
    except BlockingPoolFull:
        return RunOutcome(result="deferred", error="the schedule pool is saturated")
    except BlockingTimeout:
        return RunOutcome(result="failed", error="reading the instance timed out")
    except Exception as exc:  # noqa: BLE001 - a missing instance is a business outcome
        if "not found" in str(exc).lower():
            config, policy, present = {}, {}, False
        else:
            return RunOutcome(result="failed", error=str(exc))

    refusal = authority.cluster_refusal_for(schedule, policy, config, present)
    if refusal is not None:
        return RunOutcome(
            result="refused",
            refusal=refusal,
            terminal=authority.refusal_is_terminal(refusal, schedule.consecutive_failures),
        )

    slot = schedule.due_slot or now
    name = build_scheduled_snapshot_name(schedule.id, schedule.label, slot)
    prefix = scheduled_snapshot_prefix(schedule.id)

    try:
        rows = await _blocking(
            snapshots_service.list_instance_snapshots,
            client,
            schedule.instance_name,
            schedule.project,
            label=f"list {schedule.instance_name}",
        )
    except BlockingPoolFull:
        return RunOutcome(result="deferred", error="the schedule pool is saturated")
    except Exception as exc:  # noqa: BLE001
        return RunOutcome(result="failed", error=f"listing snapshots failed: {exc}")

    # Retention, before the create, leaving room for the one about to be taken.
    pruned: List[str] = []
    own = _select_own(rows, prefix)
    while len(own) > max(0, schedule.keep_last - 1):
        oldest = own.pop(0)
        victim = str(oldest.get("name"))
        if victim == name:
            # The replay case: this slot's snapshot is already on the cluster. Never delete it to
            # make room for itself.
            own.insert(0, oldest)
            break
        try:
            op = await _blocking(
                snapshots_service.delete_instance_snapshot,
                client,
                schedule.instance_name,
                schedule.project,
                victim,
                label=f"prune {victim}",
            )
            if op:
                settled = await _blocking(
                    snapshots_service.wait_for_operation,
                    client,
                    op,
                    schedule.project,
                    _SETTLE_TIMEOUT,
                    label=f"settle prune {victim}",
                )
                # note: a failed delete used to surface only as the wait raising. The wait now
                # reads the failure back, so it is raised here to keep the failure audit below.
                if settled.get("failed"):
                    raise RuntimeError(settled.get("err") or f"the daemon did not delete {victim}")
            pruned.append(victim)
            _invalidate_snapshot_views(schedule)
            _audit(schedule, "instance.snapshot.schedule.pruned", "success", snapshot=victim)
        except Exception as exc:  # noqa: BLE001
            # A prune that failed is not a reason to skip the snapshot: `expires_at` on every row
            # is the backstop, and skipping would lose a restore point to protect a cleanup.
            logger.warning("Pruning %s failed: %s", victim, exc)
            _audit(
                schedule,
                "instance.snapshot.schedule.pruned",
                "failure",
                snapshot=victim,
                error=str(exc),
            )
            break

    try:
        await _blocking(
            snapshots_service.assert_capacity,
            client,
            schedule.instance_name,
            schedule.project,
            label=f"capacity {schedule.instance_name}",
        )
    except snapshots_service.SnapshotLimitError as exc:
        held = [str(r.get("name")) for r in rows if snapshots_service.is_operator_snapshot(str(r.get("name") or ""))]
        _audit(
            schedule,
            "instance.snapshot.schedule.run",
            "denied",
            error=str(exc),
            operator_snapshots=held,
        )
        return RunOutcome(result="failed", error=str(exc))
    except BlockingPoolFull:
        return RunOutcome(result="deferred", error="the schedule pool is saturated")
    except Exception as exc:  # noqa: BLE001
        return RunOutcome(result="failed", error=str(exc))

    expires_at = snapshots_service.default_expiry(schedule.expiry_days)
    try:
        operation_id = await _blocking(
            snapshots_service.create_instance_snapshot,
            client,
            schedule.instance_name,
            schedule.project,
            name,
            stateful=False,
            expires_at=expires_at,
            label=f"create {name}",
        )
    except BlockingPoolFull:
        return RunOutcome(result="deferred", error="the schedule pool is saturated")
    except Exception as exc:  # noqa: BLE001
        return _classify_create_failure(schedule, name, pruned, str(exc))

    if not operation_id:
        # Synchronous 200: nothing to wait for and the work is done.
        _invalidate_snapshot_views(schedule)
        _audit(schedule, "instance.snapshot.schedule.run", "success", snapshot=name)
        return RunOutcome(result="success", snapshot=name, pruned=pruned)

    try:
        outcome = await _blocking(
            snapshots_service.wait_for_operation,
            client,
            operation_id,
            schedule.project,
            _SETTLE_TIMEOUT,
            label=f"settle {name}",
        )
    except Exception as exc:  # noqa: BLE001
        # Completion is unverifiable, so the slot is HELD. The name is derived from the slot, so a
        # retry either finds the snapshot already there or takes it; either way there is one.
        return RunOutcome(
            result="failed", error=f"could not confirm the snapshot settled: {exc}"
        )

    if outcome.get("failed"):
        return _classify_create_failure(schedule, name, pruned, outcome.get("err") or "")

    _audit(
        schedule,
        "instance.snapshot.schedule.run",
        "success",
        snapshot=name,
        expires_at=expires_at,
        settled=bool(outcome.get("settled")),
        pruned=pruned,
    )
    _invalidate_snapshot_views(schedule)
    return RunOutcome(result="success", snapshot=name, pruned=pruned)


def _classify_create_failure(
    schedule: Any, name: str, pruned: List[str], error: str
) -> RunOutcome:
    """A refused duplicate is this slot already done, not a failure.

    important: the daemon reports it asynchronously as the operation's error, never as an HTTP
    status, so nothing above this can see it. Recognising it is what makes a worker that died after
    the daemon created the snapshot, but before its row was written, idempotent rather than a
    source of second copies.
    """
    if _ALREADY_EXISTS in (error or "").lower():
        _invalidate_snapshot_views(schedule)
        _audit(
            schedule,
            "instance.snapshot.schedule.run",
            "success",
            snapshot=name,
            replayed=True,
        )
        return RunOutcome(result="replayed", snapshot=name, pruned=pruned)
    _audit(schedule, "instance.snapshot.schedule.run", "failure", snapshot=name, error=error)
    return RunOutcome(result="failed", snapshot=name, pruned=pruned, error=error)
