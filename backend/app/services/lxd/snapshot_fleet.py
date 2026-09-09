"""Reading snapshots across every project on a cluster, in one call where the daemon allows it.

The fleet view used to be scoped to one cluster and one project because the alternative was a
call per project, and nuc2 alone carries 47 of them. LXD answers that directly: since 5.0 the
`instance_all_projects` extension lets a single `GET /1.0/instances?recursion=2&all-projects=true`
return every project's instances. Measured on this fleet, that turns 74 project-scoped calls into
three, at 1.12s, 2.17s and 0.66s per cluster.

Everything here is synchronous and takes one client. The router owns the fan-out, the ACL and the
cache; this module owns the reading. That split is what keeps every tenant decision on the event
loop, where the context variables it reads are actually set.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.services.lxd.snapshots import (
    SnapshotProjectUnknown,
    _metadata,
    list_project_snapshots,
    rows_from_instance_listing,
)

logger = logging.getLogger(__name__)

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


def _walk_projects(client, measure_sizes: bool) -> Tuple[List[Dict[str, Any]], bool]:
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
            rows.extend(list_project_snapshots(client, name, measure_sizes=measure_sizes))
        except Exception as exc:  # noqa: BLE001 - one project is not the cluster
            logger.info("Skipping project %s while reading snapshots: %s", name, exc)
            complete = False
    return rows, complete


def read_cluster_snapshots(
    client,
    host: str,
    *,
    project: Optional[str] = None,
    measure_sizes: bool = False,
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
        rows = list_project_snapshots(client, project, measure_sizes=measure_sizes)
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
            rows, complete = _walk_projects(client, measure_sizes)
    else:
        rows, complete = _walk_projects(client, measure_sizes)

    for row in rows:
        row["host"] = host
    return rows, complete
