"""Reading LXD resources across every project, and deciding which project owns what.

The dashboard's pooled pylxd client is built without a `project=` kwarg, which binds it to
LXD's `default` project for the life of the pool. Every read of networks, network ACLs and
profiles therefore answered about one project out of however many the cluster carries, and
nothing in the tree consulted the project's own `features.*` flags to know whether that was
even the right project to ask about. This module supplies both halves.

LXD answers the first half directly. Since the `networks_all_projects`,
`network_acls_all_projects` and `profiles_all_projects` extensions, one
`?recursion=1&all-projects=true` request returns every project's records, each stamped with
the project it belongs to. That is one request per cluster instead of one per project, and
cheaper than what it replaces: pylxd's collection managers list at recursion 0 and then sync
each item, so reading 91 networks cost 92 round trips.

The second half is `features.*`. A project with `features.networks=true` owns its own
networks; with the flag false or absent it uses the `default` project's. The same rule governs
network ACLs, which LXD does not give a key of their own, and `features.profiles` governs
profiles. Resolving that is what `effective_project` is for, and it applies to writes as much
as to reads: creating a network in a project that does not own networks has to land in
`default`, because that is where LXD will look for it afterwards.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_PROJECT = "default"

# LXD API extensions that make a one-request read possible, per collection.
ALL_PROJECTS_EXTENSION: Dict[str, str] = {
    "networks": "networks_all_projects",
    "network-acls": "network_acls_all_projects",
    "profiles": "profiles_all_projects",
    "instances": "instance_all_projects",
}

# Which project feature flag decides whether a collection is isolated per project.
# note: LXD has no `features.network-acls`; ACLs follow `features.networks`.
FEATURE_FOR_COLLECTION: Dict[str, str] = {
    "networks": "features.networks",
    "network-acls": "features.networks",
    "profiles": "features.profiles",
}

# How many projects the fallback path will walk on one cluster before it stops and says so.
# Above the 47 that the largest cluster on this fleet carries, so it guards against a fleet
# that grew rather than limiting anyone today. Exceeding it marks the read partial; it never
# truncates silently.
MAX_PROJECTS_PER_CLUSTER = 120

# (connect, read). The pooled client's own read budget is 8s, which a fleet-wide recursion=1
# across every project can exceed on a large cluster. The socket timeout is what actually
# frees the worker thread, so it is passed explicitly at the call site.
ALL_PROJECTS_READ_TIMEOUT = (3, 25)


def _metadata(response: Any) -> Any:
    """The `metadata` body of an LXD response, or None."""
    try:
        return response.json().get("metadata")
    except Exception:  # noqa: BLE001 - a non-JSON body is a failed read, reported by the caller
        return None


def supports_all_projects(client: Any, collection: str) -> bool:
    """Whether this daemon can answer about every project in one request.

    Read off the `/1.0` body pylxd already holds, so this costs nothing. Deliberately not
    `client.has_api_extension`, which raises KeyError when `host_info` carries no
    `api_extensions` key at all, which is exactly the shape a stub or an unusual daemon
    presents.
    """
    extension = ALL_PROJECTS_EXTENSION.get(collection)
    if not extension:
        return False
    info = getattr(client, "host_info", None) or {}
    return extension in (info.get("api_extensions") or [])


def _assert_pooled(client: Any, collection: str) -> None:
    """Refuse an all-projects read on a project-bound client.

    important: pylxd's _APINode injects `params["project"] = self._project` into every verb
    whenever the client carries a project, and `get_project_client` always passes one (it
    defaults to "default" rather than None). LXD refuses a request carrying both project and
    all-projects with "Cannot specify a project when requesting all projects", so a
    project-bound client here would 400 the whole cluster rather than reading it.
    """
    bound = getattr(client, "project", None)
    if bound is not None:
        raise ValueError(
            f"all-projects read of {collection} needs the pooled client; "
            f"this one is bound to project '{bound}'"
        )


def list_project_configs(client: Any) -> Dict[str, Dict[str, str]]:
    """Every project on the cluster mapped to its config, in one request.

    The `features.*` flags live in that config. Read at recursion 1 on purpose: pylxd's
    ProjectManager lists at recursion 0 and then syncs each project on first attribute access,
    which is one extra round trip per project.
    """
    response = client.api.projects.get(
        params={"recursion": 1}, timeout=ALL_PROJECTS_READ_TIMEOUT
    )
    configs: Dict[str, Dict[str, str]] = {}
    for entry in _metadata(response) or []:
        if isinstance(entry, dict) and entry.get("name"):
            configs[entry["name"]] = entry.get("config") or {}
    return configs


def list_project_names(client: Any) -> List[str]:
    """Project names without the records. Only the fallback path needs these."""
    response = client.api.projects.get(
        params={"recursion": 0}, timeout=ALL_PROJECTS_READ_TIMEOUT
    )
    names: List[str] = []
    for entry in _metadata(response) or []:
        if isinstance(entry, str):
            names.append(entry.rstrip("/").rsplit("/", 1)[-1].split("?")[0])
        elif isinstance(entry, dict) and entry.get("name"):
            names.append(entry["name"])
    return names


def effective_project(
    collection: str,
    project: Optional[str],
    project_configs: Optional[Dict[str, Dict[str, str]]],
) -> str:
    """The project LXD will actually store this collection in.

    A project only holds its own networks, ACLs or profiles when the governing `features.*`
    flag is true. Unset means false: LXD's documented default for every `features.*` option is
    false, and several projects on this fleet omit the key entirely rather than setting it.
    Anything else resolves to `default`, which is where the records really live.
    """
    if not project or project == DEFAULT_PROJECT:
        return DEFAULT_PROJECT
    feature = FEATURE_FOR_COLLECTION.get(collection)
    if feature is None:
        return project
    config = (project_configs or {}).get(project)
    if config is None:
        # An unknown project is not a licence to guess. Ask about it as named and let LXD
        # answer; a project that does not exist is a 404 the caller should see.
        return project
    return project if str(config.get(feature, "false")).lower() == "true" else DEFAULT_PROJECT


def resolve_owner(client: Any, collection: str, project: Optional[str]) -> str:
    """The project that really holds `collection` for a caller asking about `project`.

    Short-circuits before reading the project list when the answer cannot be anything but
    `default`, so the common case costs no extra round trip.
    """
    if not project or project == DEFAULT_PROJECT:
        return DEFAULT_PROJECT
    return effective_project(collection, project, list_project_configs(client))


def project_params(project: Optional[str], **extra: Any) -> Dict[str, Any]:
    """Query parameters naming a project, for a raw call on the pooled client.

    note: the pooled client carries no project of its own, so the project has to be named on
    every request that is not about `default`. Naming it explicitly is also what keeps a write
    from silently landing in `default`.
    """
    params: Dict[str, Any] = dict(extra)
    params["project"] = project or DEFAULT_PROJECT
    return params


def list_in_project(
    client: Any, collection: str, project: str, recursion: int = 1
) -> List[Dict[str, Any]]:
    """One project's records, each stamped with the project it was read from.

    LXD stamps `project` itself on an all-projects listing but not always on a single-project
    one, and the callers key on that field either way.
    """
    response = client.api[collection].get(
        params={"recursion": recursion, "project": project},
        timeout=ALL_PROJECTS_READ_TIMEOUT,
    )
    rows = []
    for row in _metadata(response) or []:
        if isinstance(row, dict):
            row.setdefault("project", project)
            rows.append(row)
    return rows


def list_across_projects(
    client: Any, collection: str, recursion: int = 1
) -> Tuple[List[Dict[str, Any]], bool]:
    """Every record of `collection` on the cluster, whatever project holds it.

    Returns (rows, complete). `complete` is False when the fallback could not read every
    project, so the caller can say the list is partial rather than presenting a short list as
    the whole truth.
    """
    _assert_pooled(client, collection)

    if supports_all_projects(client, collection):
        response = client.api[collection].get(
            params={"recursion": recursion, "all-projects": "true"},
            timeout=ALL_PROJECTS_READ_TIMEOUT,
        )
        rows = [row for row in (_metadata(response) or []) if isinstance(row, dict)]
        unstamped = [r.get("name") for r in rows if not r.get("project")]
        if not unstamped:
            return rows, True
        # A daemon that accepted the parameter but answered without labels cannot be used:
        # every row would have to be attributed by guesswork. Fall through and walk instead.
        logger.warning(
            "all-projects listing of %s returned %d row(s) with no project label, re-reading "
            "project by project",
            collection,
            len(unstamped),
        )

    return _walk_projects(client, collection, recursion)


def _walk_projects(
    client: Any, collection: str, recursion: int
) -> Tuple[List[Dict[str, Any]], bool]:
    """The fallback: enumerate projects and read each one.

    important: with the governing feature flag false, a project does not hold its own records,
    it sees `default`'s. Reading such a project returns `default`'s rows again, so a cluster
    with 47 projects would return the same bridge 47 times, each stamped differently, and the
    total would be a lie. Records are therefore keyed on the project that really holds them.
    """
    complete = True
    names = list_project_names(client)
    if len(names) > MAX_PROJECTS_PER_CLUSTER:
        logger.warning(
            "Cluster carries %d projects, reading the first %d",
            len(names),
            MAX_PROJECTS_PER_CLUSTER,
        )
        names = names[:MAX_PROJECTS_PER_CLUSTER]
        complete = False

    configs = list_project_configs(client)

    # Resolve every project to the one that really holds its records, then read each of those
    # once. On a fleet where most projects inherit, this is the difference between 47 requests
    # and two, and it is also what stops the same bridge appearing 47 times.
    owners: List[str] = []
    for name in names:
        owner = effective_project(collection, name, configs)
        if owner not in owners:
            owners.append(owner)

    rows: List[Dict[str, Any]] = []
    for owner in owners:
        try:
            for row in list_in_project(client, collection, owner, recursion):
                row["project"] = owner
                rows.append(row)
        except Exception as exc:  # noqa: BLE001 - reported as a partial read, not an outage
            # A project can be deleted between the enumeration and the read. One operator's
            # tidy-up must not blank the whole list, so the read is marked partial instead.
            logger.warning("Could not read %s in project %s: %s", collection, owner, exc)
            complete = False

    return rows, complete


class InstanceRecord:
    """One instance from an all-projects listing, exposing what the readers duck-type.

    The member, node and region readers all count instances by walking objects and asking for
    `status`, `location`, `expanded_devices` and `config`. Handing them raw dicts would make
    every one of those getattr calls miss and report zero, which reads as free capacity rather
    than as a failure, so the rows are wrapped instead of the readers being rewritten.
    """

    __slots__ = (
        "name",
        "project",
        "location",
        "status",
        "config",
        "devices",
        "expanded_config",
        "expanded_devices",
        "type",
    )

    def __init__(self, row: Dict[str, Any]):
        self.name = row.get("name") or ""
        self.project = row.get("project") or DEFAULT_PROJECT
        self.location = row.get("location") or ""
        self.status = row.get("status") or ""
        self.type = row.get("type") or ""
        self.config = row.get("config") or {}
        self.devices = row.get("devices") or {}
        self.expanded_config = row.get("expanded_config") or self.config
        self.expanded_devices = row.get("expanded_devices") or self.devices


def list_instance_records(client: Any) -> Tuple[List[InstanceRecord], bool]:
    """Every instance on the cluster, in every project, with its config expanded.

    recursion=2 because the GPU reader needs `expanded_devices`, which a recursion=1 listing
    does not carry. Returns (records, complete).
    """
    rows, complete = list_across_projects(client, "instances", recursion=2)
    return [InstanceRecord(row) for row in rows], complete
