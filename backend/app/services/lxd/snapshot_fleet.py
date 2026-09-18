"""Reading snapshots across every project on a cluster, in one call where the daemon allows it.

The fleet view used to be scoped to one cluster and one project because the alternative was a
call per project, and nuc2 alone carries 47 of them. LXD answers that directly: since 5.0 the
`instance_all_projects` extension lets a single `GET /1.0/instances?recursion=2&all-projects=true`
return every project's instances. Measured on this fleet, that turns 74 project-scoped calls into
three, at 1.12s, 2.17s and 0.66s per cluster.

Everything here is synchronous and takes one client. The router owns the fan-out, the ACL and the
cache; this module owns the reading. That split is what keeps every tenant decision on the event
loop, where the context variables it reads are actually set.

Sizes are a second phase, not part of the listing. The bulk read answers -1 for every snapshot,
and the only read that obtains a real size is one GET per snapshot-holding instance. Running that
inside the listing would measure instances the caller may not see and would let a slow storage
driver condemn the whole cluster. So `measure_holder_sizes` runs after the router's visibility
filter, over the visible holders only, against a deadline, and a failure there leaves a row
unmeasured and the cluster answered. Nothing in the size phase can mark a cluster unreachable.
"""

import contextvars
import logging
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any, Dict, List, Optional, Tuple

from app.services.lxd.snapshots import (
    SnapshotProjectUnknown,
    _metadata,
    list_instance_snapshots,
    list_project_snapshots,
    rows_from_instance_listing,
)

logger = logging.getLogger(__name__)

# A snapshot-holding instance, as (project, instance name). The host is the cluster being read.
Holder = Tuple[str, str]

# The API extension that makes a one-call read possible. Present on LXD 5.0 and later, and on
# this fleet advertised by all three reachable clusters (5.21.7, 5.21.6 and 6.9).
ALL_PROJECTS_EXTENSION = "instance_all_projects"

# How many projects the fallback path will walk on one cluster before it stops and says so.
# Above nuc2's 47, so the cap is a guard against a fleet that grew rather than a limit anyone
# meets today. Exceeding it marks the cluster partial; it never truncates silently.
MAX_PROJECTS_PER_CLUSTER = 120

# (connect, read). The pooled client's own read timeout is 8s, which a fleet-wide recursion=2 can
# exceed on a large cluster: nuc1 takes 2.17s today and that is with 41 instances. The socket
# timeout is what actually frees the worker thread, so it is passed explicitly at the call site.
# Same rule, and the same reasoning, as wait_for_operation in snapshots.py.
FLEET_READ_TIMEOUT = (3, 25)

# (connect, read) for one per-instance size read. A dir pool size is a directory walk and zfs a
# metadata read; 10s sits above any measured C2 WAN GET and well under the size phase's deadline,
# so an abandoned read frees its thread long before the request budget is spent.
SIZE_READ_TIMEOUT = (3, 10)

# How many size reads are in flight on one cluster at a time.
SIZE_PARALLELISM_PER_CLUSTER = 4

# Live size reads per cluster on one request. nuc1 has 41 instances, so a cluster where every
# instance holds a snapshot is still read whole; beyond it the rest report unmeasured.
MAX_SIZE_HOLDERS_PER_CLUSTER = 60

# important: NOT _fleet_executor. The cluster read that calls measure_holder_sizes already runs
# on a worker of that pool, and a worker of a bounded pool submitting to its own pool and waiting
# is the classic pool deadlock. 16 is four clusters' worth of windows, so one slow cluster cannot
# pin the others.
_SIZES_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="snapshot_sizes")


def supports_all_projects(client) -> bool:
    """Whether this daemon can answer about every project in one request.

    Read off the `/1.0` body pylxd already cached at connect, so this costs nothing. Deliberately
    not `client.has_api_extension`, which raises KeyError when `host_info` carries no
    `api_extensions` key at all, which is exactly the shape a stub or an unusual daemon presents.
    """
    info = getattr(client, "host_info", None) or {}
    return ALL_PROJECTS_EXTENSION in (info.get("api_extensions") or [])


def list_all_projects_snapshots(client) -> List[Dict[str, Any]]:
    """Every snapshot on the cluster, whatever project it lives in, in one request.

    important: the params are built literally and never through `_project_params`, which always
    injects `project`. LXD refuses a request carrying both with
    `Cannot specify a project when requesting all projects`, verified against all three clusters
    on this fleet. Reusing the helper here would 400 every cluster at once.

    Raises SnapshotProjectUnknown when a snapshot-holding instance comes back with no project, so
    the caller can fall back rather than guess a label the tenant ACL is about to be asked about.
    """
    response = client.api.instances.get(
        params={"recursion": 2, "all-projects": "true"}, timeout=FLEET_READ_TIMEOUT
    )
    rows, _holders = rows_from_instance_listing(_metadata(response), None)
    return rows


def list_project_names(client) -> List[str]:
    """Project names on this cluster, without the records. Only the fallback path needs these."""
    response = client.api.projects.get(params={"recursion": 0}, timeout=FLEET_READ_TIMEOUT)
    names: List[str] = []
    for entry in _metadata(response) or []:
        if isinstance(entry, str):
            names.append(entry.rsplit("/", 1)[-1])
        elif isinstance(entry, dict) and entry.get("name"):
            names.append(entry["name"])
    return names


def _walk_projects(client) -> Tuple[List[Dict[str, Any]], bool]:
    """The old shape: enumerate projects, read each one. Returns (rows, complete).

    A single project failing is logged and skipped rather than condemning the cluster: a project
    can be deleted between the enumeration and the read, and one operator's tidy-up must not blank
    the whole fleet view. The cluster is reported partial instead.
    """
    complete = True
    try:
        names = list_project_names(client)
    except Exception as exc:  # noqa: BLE001 - reported as a cluster that could not be read
        logger.warning("Could not enumerate projects: %s", exc)
        raise

    if len(names) > MAX_PROJECTS_PER_CLUSTER:
        logger.warning(
            "Cluster carries %d projects, reading the first %d",
            len(names), MAX_PROJECTS_PER_CLUSTER,
        )
        names = names[:MAX_PROJECTS_PER_CLUSTER]
        complete = False

    rows: List[Dict[str, Any]] = []
    for name in names:
        try:
            rows.extend(list_project_snapshots(client, name))
        except Exception as exc:  # noqa: BLE001 - one project is not the cluster
            logger.info("Skipping project %s while reading snapshots: %s", name, exc)
            complete = False
    return rows, complete


def read_cluster_snapshots(
    client,
    host: str,
    *,
    project: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Snapshots on one cluster, stamped with the host. Returns (rows, complete).

    `project` pins the read to one project. Left None, the whole cluster is read, preferring the
    one-call path and falling back per cluster rather than per fleet, so a single old daemon does
    not slow everyone down.

    important: the host is stamped HERE, not by the caller after filtering. The tenant ACL is
    asked about (host, project, instance), so a row that reaches the filter without a host cannot
    be decided correctly, and a memo keyed on the instance name alone collides across clusters.
    """
    if project is not None:
        rows = list_project_snapshots(client, project)
        complete = True
    elif supports_all_projects(client):
        try:
            rows = list_all_projects_snapshots(client)
            complete = True
        except SnapshotProjectUnknown as exc:
            # The daemon advertises the extension and did not honour it. Falling back costs a
            # call per project on this cluster only, and is the one alternative to attributing a
            # snapshot to a project nobody named.
            logger.warning("%s answered all-projects without labels (%s), re-reading", host, exc)
            rows, complete = _walk_projects(client)
    else:
        rows, complete = _walk_projects(client)

    for row in rows:
        row["host"] = host
    return rows, complete


def _read_holder_sizes(
    client, holder: Holder, timeout: Tuple[int, int]
) -> Dict[str, Optional[int]]:
    project, name = holder
    return {
        snapshot["name"]: snapshot.get("size")
        for snapshot in list_instance_snapshots(client, name, project, timeout=timeout)
    }


def measure_holder_sizes(
    client,
    host: str,
    holders: List[Holder],
    *,
    deadline: float,
    timeout: Tuple[int, int] = SIZE_READ_TIMEOUT,
    parallelism: int = SIZE_PARALLELISM_PER_CLUSTER,
) -> Tuple[Dict[Holder, Dict[str, Optional[int]]], List[Holder]]:
    """Sizes of every snapshot on the given holders, read `parallelism` at a time until `deadline`.

    Returns (sizes_by_holder, unmeasured_holders). A holder is unmeasured when its read raised,
    when the deadline passed before it was read, or when it was still in flight at the deadline.
    `deadline` is a time.monotonic() instant.

    important: an in-flight read is abandoned at the deadline, not joined. The socket timeout
    passed to each read is what frees its thread afterwards, which is why the caller's deadline
    must sit well inside the request budget: the last abandoned read can hold a thread for the
    whole read timeout after this returns.
    """
    sizes: Dict[Holder, Dict[str, Optional[int]]] = {}
    unmeasured: List[Holder] = []
    pending = list(holders)
    in_flight: Dict[Future, Holder] = {}
    ctx = contextvars.copy_context()

    while pending or in_flight:
        while pending and len(in_flight) < parallelism and time.monotonic() < deadline:
            holder = pending.pop(0)
            future = _SIZES_EXECUTOR.submit(ctx.run, _read_holder_sizes, client, holder, timeout)
            in_flight[future] = holder
        remaining = deadline - time.monotonic()
        if not in_flight or remaining <= 0:
            break
        done, _ = wait(in_flight, timeout=remaining, return_when=FIRST_COMPLETED)
        for future in done:
            holder = in_flight.pop(future)
            try:
                sizes[holder] = future.result()
            except Exception as exc:  # noqa: BLE001 - a missing size is not a failed listing
                logger.debug("Could not read snapshot sizes for %s/%s on %s: %s", *holder, host, exc)
                unmeasured.append(holder)

    for future, holder in in_flight.items():
        future.cancel()
        unmeasured.append(holder)
    unmeasured.extend(pending)
    if unmeasured:
        logger.info(
            "Snapshot sizes read for %d of %d instances on %s", len(sizes), len(holders), host
        )
    return sizes, unmeasured
