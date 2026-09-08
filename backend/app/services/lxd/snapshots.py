"""
Snapshot operations for instances and custom storage volumes.

This module is the one place Orcastra performs snapshot work against LXD. Two callers reach it
with deliberately different rules, and the difference matters:

* The dashboard (operator) surface, which only ever creates `ops-` prefixed snapshots with a
  bounded lifetime, and refuses names that belong to OrcaHub.
* The integrations proxy, which tunnels OrcaHub's own paid snapshot product and must keep
  accepting the names OrcaHub chooses (`snap-%Y%m%d-%H%M` for its LXD-native schedules, and
  `pre-restore-*` for its restore safety copies).

So the LXD calls are shared and the naming policy is not. `assert_operator_snapshot_name` is
applied by the dashboard router only; calling it on the proxy path would break a live product.

important: every request here is raw `client.api.*` with an explicit `project` parameter. pylxd's
`instance.snapshots` collection cannot carry a project, so the model API silently answers about
the client's bound project instead of the one the caller authorized. The same reasoning is
recorded at instance.py:87.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Everything Orcastra creates carries this prefix, so an operator snapshot is identifiable on the
# cluster itself by anyone looking, including OrcaHub's reconcile.
OPS_PREFIX = "ops-"

# Prefixes OrcaHub owns. `pre-restore-` is not merely taken: OrcaHub's rolling safety prune
# physically deletes snapshots matching it, so an Orcastra snapshot named that way would be
# removed by another product without warning. `snap-` is the pattern its LXD-native schedules
# generate, and reusing it would make an operator snapshot indistinguishable from a paid one.
RESERVED_PREFIXES = ("pre-restore-", "snap-")

# An operator rollback point is for hours or days of maintenance, not months. Expressed to LXD as
# `expires_at` so LXD prunes it with no help from Orcastra: the cleanup survives this service
# being down, redeployed, or removed entirely.
DEFAULT_EXPIRY_DAYS = 7
MAX_EXPIRY_DAYS = 7

# Counted from the cluster rather than a local table, so the number cannot drift from reality.
MAX_OPS_SNAPSHOTS_PER_INSTANCE = 5

# LXD instance/snapshot names: letters, digits and dashes, 63 characters at most. Enforced here
# rather than left to LXD because the name is interpolated into a request path.
#
# note: `\Z` rather than `$`, because Python's `$` matches before a trailing newline. Defence in
# depth, not the operative guard: `assert_operator_snapshot_name` strips before it matches, so the
# newline is gone by then either way. It matters if that normalisation is ever removed. The
# request-boundary pattern in core/lxd_names.py is evaluated by a different engine whose `$` is
# already end-of-text.
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}\Z")

# How many snapshot-holding instances get a second read for accurate sizes on the fleet view.
_SIZE_ENRICHMENT_LIMIT = 40

# LXD reports "no expiry" as a zero time rather than null.
_NO_EXPIRY_PREFIX = "0001-01-01"

# The instance config keys that describe an LXD-native snapshot schedule. Orcastra reads these
# and never writes them: they are OrcaHub's retention mechanism, and a write here would leave its
# ledger disagreeing with the cluster about a schedule a tenant is paying for.
POLICY_KEYS = (
    "snapshots.schedule",
    "snapshots.schedule.stopped",
    "snapshots.expiry",
    "snapshots.pattern",
)


class SnapshotNameError(ValueError):
    """A snapshot name was refused before any request reached the cluster."""


class SnapshotLimitError(RuntimeError):
    """The per-instance operator snapshot cap would be exceeded."""


class LXDOperationUnreadable(RuntimeError):
    """The daemon accepted the request but named no operation, so completion is unverifiable."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _compact_stamp(moment: Optional[datetime] = None) -> str:
    return (moment or _utc_now()).strftime("%Y%m%dT%H%M%SZ")


def is_operator_snapshot(name: str) -> bool:
    return (name or "").startswith(OPS_PREFIX)


def assert_operator_snapshot_name(name: str) -> str:
    """Validate a name the operator surface is about to create or act on.

    Applied by the dashboard router only. The proxy path must keep accepting OrcaHub's names.
    """
    candidate = (name or "").strip()
    if not candidate:
        raise SnapshotNameError("Snapshot name is required.")
    if not _NAME_RE.match(candidate):
        raise SnapshotNameError(
            "Snapshot name may contain only letters, digits and dashes, must start with a "
            "letter or digit, and may be at most 63 characters."
        )
    for reserved in RESERVED_PREFIXES:
        if candidate.startswith(reserved):
            raise SnapshotNameError(
                f"'{reserved}' is reserved for snapshots managed elsewhere. "
                f"Operator snapshots are named '{OPS_PREFIX}...'."
            )
    if not candidate.startswith(OPS_PREFIX):
        raise SnapshotNameError(
            f"Operator snapshots must start with '{OPS_PREFIX}'."
        )
    return candidate


def build_operator_snapshot_name(label: str, moment: Optional[datetime] = None) -> str:
    """Compose `ops-<slug>-<stamp>` from a human label, trimmed to fit LXD's 63 characters."""
    slug = re.sub(r"[^a-zA-Z0-9-]+", "-", (label or "").strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    stamp = _compact_stamp(moment)
    room = 63 - len(OPS_PREFIX) - len(stamp) - 1
    if room < 1:  # pragma: no cover - only reachable if the stamp format changes
        raise SnapshotNameError("Snapshot name template leaves no room for a label.")
    slug = (slug[:room] or "snapshot"[:room]).strip("-") or "snapshot"[:room]
    return f"{OPS_PREFIX}{slug}-{stamp}"


def build_safety_snapshot_name(instance_name: str, moment: Optional[datetime] = None) -> str:
    """Name for the automatic pre-restore copy.

    Deliberately `ops-pre-...` and never `pre-restore-...`: the latter is OrcaHub's, and its
    rolling prune deletes what it matches.
    """
    return build_operator_snapshot_name(f"pre-{instance_name}", moment)


def default_expiry(days: int = DEFAULT_EXPIRY_DAYS) -> str:
    """An RFC3339 expiry LXD will prune on its own."""
    bounded = max(1, min(int(days), MAX_EXPIRY_DAYS))
    return (_utc_now() + timedelta(days=bounded)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _project_params(project: Optional[str], **extra: Any) -> Dict[str, Any]:
    params: Dict[str, Any] = {"project": project or "default"}
    params.update({k: v for k, v in extra.items() if v is not None})
    return params


def _metadata(response: Any) -> Any:
    body = response.json()
    return body.get("metadata") if isinstance(body, dict) else None


def _normalize(entry: Any, source_name: str) -> Dict[str, Any]:
    """One snapshot as the API returns it, whether LXD sent a URL or a full record."""
    if isinstance(entry, str):
        return {"name": entry.rsplit("/", 1)[-1], "source_name": source_name}
    raw_name = entry.get("name") or ""
    expires_at = entry.get("expires_at")
    if isinstance(expires_at, str) and expires_at.startswith(_NO_EXPIRY_PREFIX):
        expires_at = None
    # LXD reports -1 when it did not compute a size, which the bulk listing never does. Passing
    # that through would put a negative byte count in the API and render as a real value in any
    # consumer that only checks for null.
    size = entry.get("size")
    if not isinstance(size, int) or size < 0:
        size = None
    return {
        "name": raw_name.rsplit("/", 1)[-1],
        "source_name": source_name,
        "created_at": entry.get("created_at"),
        "expires_at": expires_at,
        "stateful": entry.get("stateful"),
        "size": size,
        "architecture": entry.get("architecture"),
        "operator_managed": is_operator_snapshot(raw_name.rsplit("/", 1)[-1]),
    }


# ── instance snapshots ─────────────────────────────────────────────────────────────────────


def list_instance_snapshots(client, instance_name: str, project: str) -> List[Dict[str, Any]]:
    """Every snapshot on an instance, with size and expiry where LXD reports them.

    `recursion=1` asks LXD for whole records instead of URLs. A cluster that refuses recursion
    still answers the plain form, so the caller gets names rather than an error.
    """
    response = client.api.instances[instance_name].snapshots.get(
        params=_project_params(project, recursion=1)
    )
    return [_normalize(entry, instance_name) for entry in (_metadata(response) or [])]


def list_project_snapshots(client, project: str) -> List[Dict[str, Any]]:
    """Every snapshot in a project, in one request.

    `recursion=2` folds each instance's snapshots into the instance listing, so a project of
    forty instances costs one call rather than forty-one. LXD omits the `snapshots` key entirely
    for an instance that has none, which is why this reads it defensively rather than expecting
    an empty list.
    """
    response = client.api.instances.get(params=_project_params(project, recursion=2))
    rows: List[Dict[str, Any]] = []
    holders: List[str] = []
    for instance in _metadata(response) or []:
        if not isinstance(instance, dict):
            continue
        source_name = instance.get("name") or ""
        snapshots = instance.get("snapshots") or []
        if snapshots:
            holders.append(source_name)
        # recursion=2 already carries each instance's config, so the source's schedule costs
        # nothing extra here. Without it the fleet view cannot tell which rows sit on a source
        # someone else bills, which is the one fact the billing gate acts on.
        source_policy = _policy_from_config(instance.get("config") or {})
        for entry in snapshots:
            row = _normalize(entry, source_name)
            row["project"] = project
            row["source_status"] = instance.get("status")
            row["source_externally_managed"] = source_policy["externally_managed"]
            row["source_schedule"] = source_policy["schedule"]
            row["source_expiry"] = source_policy["expiry"]
            rows.append(row)

    # The bulk listing answers `size: -1` for every snapshot, so a fleet view built on it alone
    # shows a dash in the one column that tells an operator which snapshot is worth deleting.
    # Sizes come from a second read per instance THAT HOLDS SNAPSHOTS, which is a far smaller
    # number than the instance count: on this fleet, one instance in a project of three. Bounded
    # so a project where everything is snapshotted cannot turn one page load into a fan-out.
    for source_name in holders[:_SIZE_ENRICHMENT_LIMIT]:
        try:
            detailed = {
                s["name"]: s.get("size")
                for s in list_instance_snapshots(client, source_name, project)
            }
        except Exception as exc:  # noqa: BLE001 - a missing size is not a failed listing
            logger.debug("Could not read snapshot sizes for %s: %s", source_name, exc)
            continue
        for row in rows:
            if row["source_name"] == source_name and row.get("size") is None:
                row["size"] = detailed.get(row["name"])

    if len(holders) > _SIZE_ENRICHMENT_LIMIT:
        logger.info(
            "Snapshot sizes read for %d of %d instances in %s; the rest report no size",
            _SIZE_ENRICHMENT_LIMIT, len(holders), project,
        )
    return rows


def count_operator_snapshots(client, instance_name: str, project: str) -> int:
    return sum(
        1
        for snapshot in list_instance_snapshots(client, instance_name, project)
        if is_operator_snapshot(snapshot["name"])
    )


def assert_capacity(client, instance_name: str, project: str) -> None:
    """Refuse before creating when the instance already holds the operator cap.

    Counts what is on the cluster, not what Orcastra remembers, so a snapshot taken by hand or
    already expired cannot make the count wrong in either direction.
    """
    held = count_operator_snapshots(client, instance_name, project)
    if held >= MAX_OPS_SNAPSHOTS_PER_INSTANCE:
        raise SnapshotLimitError(
            f"{instance_name} already holds {held} operator snapshots, the maximum is "
            f"{MAX_OPS_SNAPSHOTS_PER_INSTANCE}. Delete one before taking another."
        )


def create_instance_snapshot_request(
    instance_api,
    snapshot_name: str,
    *,
    stateful: bool = False,
    expires_at: Optional[str] = None,
):
    """POST the create and hand the raw response back for the caller to shape.

    Shared with the proxy, which turns the 202 into its own accepted envelope.
    """
    body: Dict[str, Any] = {"name": snapshot_name, "stateful": bool(stateful)}
    if expires_at:
        body["expires_at"] = expires_at
    return instance_api.snapshots.post(json=body)


def create_instance_snapshot(
    client,
    instance_name: str,
    project: str,
    snapshot_name: str,
    *,
    stateful: bool = False,
    expires_at: Optional[str] = None,
):
    response = client.api.instances[instance_name].snapshots.post(
        json={
            "name": snapshot_name,
            "stateful": bool(stateful),
            **({"expires_at": expires_at} if expires_at else {}),
        },
        params=_project_params(project),
    )
    return _operation_id(response)


def restore_instance_snapshot(client, instance_name: str, project: str, snapshot_name: str):
    response = client.api.instances[instance_name].put(
        json={"restore": snapshot_name}, params=_project_params(project)
    )
    return _operation_id(response)


def delete_instance_snapshot(client, instance_name: str, project: str, snapshot_name: str):
    response = client.api.instances[instance_name].snapshots[snapshot_name].delete(
        params=_project_params(project)
    )
    return _operation_id(response)


def wait_for_operation(client, operation_id: str, project: str, timeout: int = 20) -> Dict[str, Any]:
    """Block until an LXD operation settles, or the timeout expires.

    important: without this a create returns before the snapshot exists, and the caller's very
    next read finds nothing. That is not a cosmetic race. It makes a 201 mean "asked for" rather
    than "there", which is the difference between a control that works and one that only appears
    to, and it is the shape of defect this codebase keeps shipping.

    A timeout is not a failure. On a copy-on-write pool a snapshot is instant; on a `dir` pool it
    is a full copy of the instance and can legitimately outlast any sensible request budget. The
    caller is handed the operation to poll rather than a lie in either direction.

    important: the socket window has to outlast the wait this asks LXD for. The pooled client is
    built with a read timeout of 8 seconds, so asking the daemon to hold the response for 20 and
    saying nothing about the socket meant the read died first, every time, and the caller was told
    "still running" for an operation that had in fact already failed. The 502-on-failure branch
    could then only ever catch a failure that surfaced inside 8 seconds. Same rule, and the same
    reason, as guest_output.py:120-122 and the proxy's own client factory.
    """
    response = client.api.operations[operation_id].wait.get(
        params=_project_params(project, timeout=timeout),
        timeout=(3, int(timeout) + 10),
    )
    metadata = _metadata(response) or {}
    status = metadata.get("status")
    return {
        "status": status,
        "err": metadata.get("err") or None,
        "settled": status in ("Success", "Failure", "Cancelled"),
        # important: Cancelled is a settled non-success. Handlers used to compare against
        # "Failure" alone, so an operation the daemon cancelled (a restart mid-queue, or an
        # operator running `lxc operation delete`) was reported to the caller as done.
        "failed": status in ("Failure", "Cancelled"),
    }


def _operation_id(response: Any) -> Optional[str]:
    """The LXD operation id of an async (202) response, or None when synchronous.

    important: `None` means "there is nothing to wait for", and the caller acts on that by
    reporting the action complete. That is right for a synchronous 200, where the work really is
    done, and wrong for a 202 whose body we could not read: the daemon accepted work that is still
    running and the caller would announce success. So an accepted response that yields no id is
    raised rather than silently flattened into the synchronous case.
    """
    status = getattr(response, "status_code", None)
    try:
        body = response.json()
    except Exception as exc:  # noqa: BLE001
        if status == 202:
            raise LXDOperationUnreadable(
                "The cluster accepted the request but its response could not be read, so there is "
                "no operation to wait on."
            ) from exc
        return None
    if not isinstance(body, dict):
        if status == 202:
            raise LXDOperationUnreadable(
                "The cluster accepted the request but answered with no operation to wait on."
            )
        return None
    operation = body.get("operation")
    if isinstance(operation, str) and operation:
        return operation.rsplit("/", 1)[-1]
    metadata = body.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("id"), str):
        return metadata["id"]
    if status == 202:
        raise LXDOperationUnreadable(
            "The cluster accepted the request but named no operation, so completion cannot be "
            "confirmed."
        )
    return None


# ── schedule policy (read only) ────────────────────────────────────────────────────────────


def read_instance_policy(client, instance_name: str, project: str) -> Dict[str, Any]:
    """The LXD-native snapshot schedule on an instance, for display only.

    A non-empty `snapshots.schedule` means something else is creating snapshots here on a timer,
    in practice OrcaHub's paid product. The operator surface shows that and never writes it.
    """
    response = client.api.instances[instance_name].get(params=_project_params(project))
    config = (_metadata(response) or {}).get("config") or {}
    return _policy_from_config(config)


def _policy_from_config(config: Dict[str, Any]) -> Dict[str, Any]:
    values = {key: config.get(key) for key in POLICY_KEYS}
    return {
        "schedule": values.get("snapshots.schedule"),
        "schedule_stopped": values.get("snapshots.schedule.stopped"),
        "expiry": values.get("snapshots.expiry"),
        "pattern": values.get("snapshots.pattern"),
        # The single fact the UI acts on: is a timer other than this operator driving snapshots
        # on this source. If so the surface badges it and warns before creating.
        "externally_managed": bool(values.get("snapshots.schedule")),
    }


# ── volume snapshots ───────────────────────────────────────────────────────────────────────


def _volume_api(client, pool: str, volume_name: str, volume_type: str = "custom"):
    return client.api.storage_pools[pool].volumes[volume_type][volume_name]


def list_volume_snapshots(
    client, pool: str, volume_name: str, project: str, volume_type: str = "custom"
) -> List[Dict[str, Any]]:
    response = _volume_api(client, pool, volume_name, volume_type).snapshots.get(
        params=_project_params(project, recursion=1)
    )
    return [_normalize(entry, volume_name) for entry in (_metadata(response) or [])]


def create_volume_snapshot(
    client,
    pool: str,
    volume_name: str,
    project: str,
    snapshot_name: str,
    *,
    expires_at: Optional[str] = None,
    volume_type: str = "custom",
):
    body: Dict[str, Any] = {"name": snapshot_name}
    if expires_at:
        body["expires_at"] = expires_at
    response = _volume_api(client, pool, volume_name, volume_type).snapshots.post(
        json=body, params=_project_params(project)
    )
    return _operation_id(response)


def restore_volume_snapshot(
    client,
    pool: str,
    volume_name: str,
    project: str,
    snapshot_name: str,
    volume_type: str = "custom",
):
    response = _volume_api(client, pool, volume_name, volume_type).put(
        json={"restore": snapshot_name}, params=_project_params(project)
    )
    return _operation_id(response)


def delete_volume_snapshot(
    client,
    pool: str,
    volume_name: str,
    project: str,
    snapshot_name: str,
    volume_type: str = "custom",
):
    response = (
        _volume_api(client, pool, volume_name, volume_type)
        .snapshots[snapshot_name]
        .delete(params=_project_params(project))
    )
    return _operation_id(response)


def read_volume_policy(
    client, pool: str, volume_name: str, project: str, volume_type: str = "custom"
) -> Dict[str, Any]:
    response = _volume_api(client, pool, volume_name, volume_type).get(
        params=_project_params(project)
    )
    config = (_metadata(response) or {}).get("config") or {}
    return _policy_from_config(config)


def count_operator_volume_snapshots(
    client, pool: str, volume_name: str, project: str, volume_type: str = "custom"
) -> int:
    return sum(
        1
        for snapshot in list_volume_snapshots(client, pool, volume_name, project, volume_type)
        if is_operator_snapshot(snapshot["name"])
    )


def assert_volume_capacity(
    client, pool: str, volume_name: str, project: str, volume_type: str = "custom"
) -> None:
    held = count_operator_volume_snapshots(client, pool, volume_name, project, volume_type)
    if held >= MAX_OPS_SNAPSHOTS_PER_INSTANCE:
        raise SnapshotLimitError(
            f"{volume_name} already holds {held} operator snapshots, the maximum is "
            f"{MAX_OPS_SNAPSHOTS_PER_INSTANCE}. Delete one before taking another."
        )
