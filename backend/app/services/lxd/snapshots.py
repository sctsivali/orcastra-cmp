"""Instance snapshot operations against LXD.

Two callers share the LXD calls but not the naming policy. The dashboard (operator) surface
creates only `ops-` names with a bounded lifetime (`build_operator_snapshot_name`); the
integrations proxy tunnels OrcaHub's snapshot product and must keep accepting its names
(`snap-%Y%m%d-%H%M` for LXD-native schedules, `pre-restore-*` for restore safety copies), so
the operator rules (`assert_operator_snapshot_name`) must never run on the proxy path.

important: every request is raw `client.api.*` with an explicit `project`. pylxd's
`instance.snapshots` cannot carry one, so the model API would answer about the client's bound
project instead of the one the caller was authorized for.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pylxd.exceptions import LXDAPIException

from app.core.lxd_names import POLICY_CONFIG_KEYS
from app.core.lxd_path import safe_segment
from app.core.lxd_segment import lxd_segment

logger = logging.getLogger(__name__)

# Everything Orcastra creates carries this prefix, so an operator snapshot is identifiable on the
# cluster itself by anyone looking, including OrcaHub's reconcile.
OPS_PREFIX = "ops-"

# Prefixes OrcaHub owns. Its rolling safety prune deletes snapshots matching `pre-restore-`, so an
# Orcastra snapshot named that way would vanish without warning. `snap-` is what its LXD-native
# schedules generate, and reusing it would make an operator snapshot look like a paid one.
RESERVED_PREFIXES = ("pre-restore-", "snap-")

# An operator rollback point is for hours or days of maintenance. Sent to LXD as `expires_at`, so
# LXD prunes it itself and the cleanup survives this service being down, redeployed or removed.
DEFAULT_EXPIRY_DAYS = 7
MAX_EXPIRY_DAYS = 7

# Counted from the cluster rather than a local table, so the number cannot drift from reality.
MAX_OPS_SNAPSHOTS_PER_INSTANCE = 5

# LXD instance/snapshot names: letters, digits and dashes, 63 characters at most. Enforced here
# rather than left to LXD because the name is interpolated into a request path.
#
# `\Z` rather than `$`, because Python's `$` also matches before a trailing newline. Defence in
# depth: `assert_snapshot_name` strips before it matches. The request-boundary pattern in
# core/lxd_names.py runs on a different engine, whose `$` is already end-of-text.
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}\Z")

# LXD reports "no expiry" as a zero time rather than null.
_NO_EXPIRY_PREFIX = "0001-01-01"

# The instance config keys that describe an LXD-native snapshot schedule. Orcastra reads these
# and never writes them: they are OrcaHub's retention mechanism, and a write here would leave its
# ledger disagreeing with the cluster about a schedule a tenant is paying for. Defined once, in
# core/lxd_names.py, so the create request models can refuse them without importing this package.
POLICY_KEYS = POLICY_CONFIG_KEYS


class SnapshotNameError(ValueError):
    """A snapshot name was refused before any request reached the cluster."""


class SnapshotLimitError(RuntimeError):
    """The per-instance operator snapshot cap would be exceeded."""


class SnapshotProjectUnknown(RuntimeError):
    """An instance listing carried a snapshot but no project to attribute it to.

    A security signal: the project on a row is what `check_tenant_permission` is asked about, so
    attributing an unlabelled row to "default" would show a tenant holding a grant on `default`
    the snapshots of every other project on that cluster. The all-projects reader catches this and
    re-reads the cluster project by project, where the label comes from the request.
    """


class LXDOperationUnreadable(RuntimeError):
    """The daemon accepted the request but named no operation, so completion is unverifiable."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _compact_stamp(moment: Optional[datetime] = None) -> str:
    return (moment or _utc_now()).strftime("%Y%m%dT%H%M%SZ")


class SnapshotOrigin(str, Enum):
    """Who made a snapshot: this console, an operator outside it, or the upstream product.

    A snapshot taken by hand (ex; over SSH before a risky change) is the operator's own restore
    point, not another product's property, so it gets its own class.

    important: `MANUAL` is the fallback and so the most permissive class. Anything deciding a gate
    from this must validate the name first (`assert_snapshot_name`).
    """

    OPERATOR = "operator"   # ops-*            this console created it
    MANUAL = "manual"       # anything else    an operator created it, outside this console
    UPSTREAM = "upstream"   # RESERVED_PREFIXES, the metered product's namespaces


def classify_snapshot_origin(name: str) -> SnapshotOrigin:
    """Total, never raises. Safe on a name LXD handed back, where validation is not the question."""
    candidate = (name or "").strip()
    for reserved in RESERVED_PREFIXES:
        if candidate.startswith(reserved):
            return SnapshotOrigin.UPSTREAM
    if candidate.startswith(OPS_PREFIX):
        return SnapshotOrigin.OPERATOR
    return SnapshotOrigin.MANUAL


def is_operator_snapshot(name: str) -> bool:
    """Whether this console created it, the question the per-instance cap asks."""
    return classify_snapshot_origin(name) is SnapshotOrigin.OPERATOR


def assert_snapshot_name(name: str) -> str:
    """Refuse a name that cannot safely become a request path. Returns the validated form.

    important: the injection guard, applied to every origin class. pylxd builds request URLs by
    string concatenation and escapes nothing, so `ct-victim?` in a path segment ends the path and
    addresses a different resource. Kept apart from the ownership rule below, so widening
    ownership cannot widen this.
    """
    candidate = (name or "").strip()
    if not candidate:
        raise SnapshotNameError("Snapshot name is required.")
    if not _NAME_RE.match(candidate):
        raise SnapshotNameError(
            "Snapshot name may contain only letters, digits and dashes, must start with a "
            "letter or digit, and may be at most 63 characters."
        )
    return candidate


def assert_operator_snapshot_name(name: str) -> str:
    """Validate a name this console is about to create.

    Acting on a snapshot is decided by `core/snapshot_ownership_gate.py` from its origin. Creating
    is limited to `ops-` names, because any other name would look like a paid one or collide with
    a namespace OrcaHub's prune deletes.
    """
    candidate = assert_snapshot_name(name)
    origin = classify_snapshot_origin(candidate)
    if origin is SnapshotOrigin.UPSTREAM:
        reserved = next(p for p in RESERVED_PREFIXES if candidate.startswith(p))
        raise SnapshotNameError(
            f"'{reserved}' is reserved for snapshots managed elsewhere. "
            f"Operator snapshots are named '{OPS_PREFIX}...'."
        )
    if origin is not SnapshotOrigin.OPERATOR:
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

    `ops-pre-...`, never `pre-restore-...`: the latter is OrcaHub's, and its rolling prune
    deletes what it matches.
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
        # `origin` is the three-way answer the console acts on. `operator_managed` answers "did
        # this console create it", which the per-instance cap and several callers ask; that is a
        # different question from "may I act on it".
        "origin": classify_snapshot_origin(raw_name.rsplit("/", 1)[-1]).value,
        "operator_managed": is_operator_snapshot(raw_name.rsplit("/", 1)[-1]),
        # Set True by list_instance_snapshots, the one read that obtains a size. False means
        # nobody asked LXD for one; True with a null size means LXD was asked and reported none.
        "size_measured": False,
    }


# ── instance snapshots ─────────────────────────────────────────────────────────────────────


def list_instance_snapshots(
    client,
    instance_name: str,
    project: str,
    *,
    timeout: Optional[Tuple[int, int]] = None,
) -> List[Dict[str, Any]]:
    """Every snapshot on an instance, with size and expiry where LXD reports them.

    `recursion=1` asks LXD for whole records instead of URLs. A cluster that refuses recursion
    still answers the plain form, so the caller gets names rather than an error.

    `timeout` is the (connect, read) socket timeout for this one request. Left None, the pooled
    client's own default applies, which suits the per-instance route and the schedule runner. The
    fleet size phase passes its own, because the socket timeout is what frees the worker thread
    when a storage driver is slow to answer.
    """
    kwargs: Dict[str, Any] = {"params": _project_params(project, recursion=1)}
    if timeout is not None:
        kwargs["timeout"] = timeout
    response = client.api.instances[lxd_segment(instance_name, what="Instance name")].snapshots.get(**kwargs)
    rows = [_normalize(entry, instance_name) for entry in (_metadata(response) or [])]
    # important: stamped here, not in `_normalize`. The fleet size phase calls this read to obtain
    # a size, so a null size on this path means the driver reported nothing. `_normalize` defaults
    # to False because `rows_from_instance_listing` also calls it, and the bulk recursion=2 listing
    # answers -1 for every row. The console tells "Not measured" from "Not reported" by this flag.
    for row in rows:
        row["size_measured"] = True
    return rows


def rows_from_instance_listing(
    metadata: Any,
    project: Optional[str] = None,
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Snapshot rows out of an instance listing, plus the sources that hold any.

    `project` pins the label for a project-scoped read. Left None, each row takes the project LXD
    reported on its own instance, which is what an all-projects listing returns and the only label
    that can be trusted there.

    important: a row with no project raises rather than defaulting. The label is what the tenant
    ACL is asked about, so a wrong one is a cross-project leak.
    """
    rows: List[Dict[str, Any]] = []
    holders: List[str] = []
    for instance in metadata or []:
        if not isinstance(instance, dict):
            continue
        source_name = instance.get("name") or ""
        snapshots = instance.get("snapshots") or []
        if not snapshots:
            continue
        row_project = project if project is not None else (instance.get("project") or "").strip()
        if not row_project:
            raise SnapshotProjectUnknown(
                f"instance {source_name!r} carries snapshots but no project"
            )
        holders.append(source_name)
        # recursion=2 already carries each instance's config, so the source's schedule costs
        # nothing extra here. Without it the fleet view cannot tell which rows sit on a source
        # someone else bills, which is the one fact the billing gate acts on.
        source_policy = _policy_from_config(instance.get("config") or {})
        for entry in snapshots:
            row = _normalize(entry, source_name)
            row["project"] = row_project
            row["source_status"] = instance.get("status")
            row["source_externally_managed"] = source_policy["externally_managed"]
            row["source_schedule"] = source_policy["schedule"]
            row["source_expiry"] = source_policy["expiry"]
            rows.append(row)
    return rows, holders


def list_project_snapshots(client, project: str) -> List[Dict[str, Any]]:
    """Every snapshot in a project, in one request.

    `recursion=2` folds each instance's snapshots into the instance listing, so a project of
    forty instances costs one call rather than forty-one. LXD omits the `snapshots` key for an
    instance that has none, so it is read defensively.

    Sizes are not read here: the bulk listing answers -1 for every snapshot, and the per-instance
    size read is a separate bounded phase (snapshot_fleet.measure_holder_sizes), run only over
    the rows the caller may see.
    """
    response = client.api.instances.get(params=_project_params(project, recursion=2))
    rows, _holders = rows_from_instance_listing(_metadata(response), project)
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
    response = client.api.instances[lxd_segment(instance_name, what="Instance name")].snapshots.post(
        json={
            "name": snapshot_name,
            "stateful": bool(stateful),
            **({"expires_at": expires_at} if expires_at else {}),
        },
        params=_project_params(project),
    )
    return _operation_id(response)


def restore_instance_snapshot(client, instance_name: str, project: str, snapshot_name: str):
    response = client.api.instances[lxd_segment(instance_name, what="Instance name")].put(
        json={"restore": snapshot_name}, params=_project_params(project)
    )
    return _operation_id(response)


def delete_instance_snapshot(client, instance_name: str, project: str, snapshot_name: str):
    response = client.api.instances[lxd_segment(instance_name, what="Instance name")].snapshots[
        lxd_segment(snapshot_name, what="Snapshot name")
    ].delete(
        params=_project_params(project)
    )
    return _operation_id(response)


def wait_for_operation(client, operation_id: str, project: str, timeout: int = 20) -> Dict[str, Any]:
    """Block until an LXD operation settles, or the timeout expires.

    Without this a create returns before the snapshot exists, so a 201 would mean "asked for"
    rather than "there". A timeout is not a failure: on a `dir` pool a snapshot is a full copy of
    the instance and can outlast any sensible request budget, so the caller is handed the
    operation to poll.

    The socket read window must outlast the wait asked of LXD. With the pooled client's 8-second
    read timeout, a 20-second wait would die at the socket and report "still running" for an
    operation that had already failed. proxy_operations._client_for follows the same rule.
    """
    endpoint = client.api.operations[safe_segment(operation_id)]
    try:
        response = endpoint.wait.get(
            params=_project_params(project, timeout=timeout),
            timeout=(3, int(timeout) + 10),
        )
    except LXDAPIException:
        # important: LXD answers the wait of a failed operation with an error-typed body, which
        # pylxd raises. Taken as "still running", a restore ZFS refused (a later snapshot exists,
        # which the safety copy guarantees) would be reported and audited as accepted. The
        # operation still reads Failure and why, so it is read once; if that read fails too (a
        # reaped operation's 404), the error propagates.
        response = endpoint.get(params=_project_params(project), timeout=(3, 15))
    metadata = _metadata(response) or {}
    status = metadata.get("status")
    return {
        "status": status,
        "err": metadata.get("err") or None,
        "settled": status in ("Success", "Failure", "Cancelled"),
        # important: Cancelled is a settled non-success. An operation the daemon cancelled (a
        # restart mid-queue, or an operator running `lxc operation delete`) must not be reported
        # as done.
        "failed": status in ("Failure", "Cancelled"),
    }


def _operation_id(response: Any) -> Optional[str]:
    """The LXD operation id of an async (202) response, or None when synchronous.

    The caller reads `None` as "nothing to wait for" and reports the action complete. That is
    right for a synchronous 200 and wrong for a 202 whose body could not be read, where the daemon
    accepted work that is still running. So an accepted response that yields no id raises
    LXDOperationUnreadable.
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


def read_instance_config_and_policy(
    client, instance_name: str, project: str
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """The raw config and the derived snapshot policy of an instance, from one GET.

    The scheduler needs both: `volatile.uuid` tells an instance from a different one wearing the
    same name, and `snapshots.schedule` tells whether the metered product upstream has claimed the
    instance since. One read keeps both answers to the same instant.
    """
    response = client.api.instances[lxd_segment(instance_name, what="Instance name")].get(params=_project_params(project))
    config = (_metadata(response) or {}).get("config") or {}
    return config, _policy_from_config(config)


def read_instance_policy(client, instance_name: str, project: str) -> Dict[str, Any]:
    """The LXD-native snapshot schedule on an instance, for display only.

    A non-empty `snapshots.schedule` means something else is creating snapshots here on a timer,
    in practice OrcaHub's paid product. The operator surface shows that and never writes it.
    """
    return read_instance_config_and_policy(client, instance_name, project)[1]


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
