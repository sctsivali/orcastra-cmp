"""Which LXD API paths the external raw proxy may forward.

The raw proxy hands an integration's path to LXD with the cluster's own client certificate, so a
path is an authorization input, not a string to pass along. Every comparison here runs on the
form requests and LXD will act on: percent-decoding repeated, dot segments refused, empty segments
collapsed, and the query and fragment dropped.

important: requests (urllib3 2.x) removes `.` and `..` segments on the client side and never sends
the fragment. `/1.0/./certificates` and `/1.0/certificates#x` both reach `/1.0/certificates`, so a
check that only looks for `..` or compares the raw string lets both through.

important: split_proxy_path is the one split both the guards and raw_proxy read. The guards once
normalised the path their own way, so `/1.0certificates` was judged as a harmless segment while
raw_proxy sent it to `/1.0/certificates`.
"""

import logging
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, unquote_plus

from fastapi import HTTPException

logger = logging.getLogger(__name__)

# LXD API paths no integration may reach through the proxy, whatever its cluster grant.
#
# important: a refusal list, not an allowlist, and a deliberate trade. An allowlist would break
# the integrator the first time it used a path nobody had enumerated. Both entries are the trust
# store: `/certificates` lists and adds trusted client certificates, and on LXD 5.21 and later
# `/auth` adds a TLS identity (`/auth/identities/tls`) and gives it server-wide entitlements
# (`/auth/groups`). Either one lets a `write` grant mint a credential that outlives the key.
_REFUSED_PROXY_PREFIXES = ("/certificates", "/auth")

# Server-wide state no integration may change, whatever its grant. Reads stay open: the
# integrator reads GET /1.0/cluster/members for a node count and never writes either one.
#
# important: a write to the bare /1.0 sets server configuration, which includes the OIDC issuer
# and, before LXD 6, core.trust_password, and either one hands out trust like the trust store
# does. `/cluster` holds the cluster certificate, join tokens (POST /1.0/cluster/members admits a
# new trusted member), member state and member removal.
_SERVER_WRITE_PREFIXES = ("/cluster",)

# The only resources a project grant bounds, so the only ones a key that is not `*` may change.
#
# important: a grant of `projects: ["tenant-a"]` is enforced by binding the request to that
# project, which is a real boundary for instances (and their snapshots), images and operations.
# It is not one for the server itself, the cluster, projects, profiles, networks or storage
# pools, all of which live above a project or are shared with `default`. An allowlist, not a
# refusal list, because LXD keeps adding resources above projects and a narrow key must not
# gain each new one by default. Reads are left alone: refusing `GET /1.0/networks` would break
# integrations that enumerate topology, and disclosure is a smaller harm than destruction.
# note: images are bounded only while the project keeps its own image store (features.images,
# which services/lxd/projects.py turns on for a project created without a config). A project
# without one shares the images of `default`, and telling the two apart needs a cluster read,
# so that case is a known limit here.
# note: storage volumes stay refused although they can be project-scoped, because
# `/storage-pools` also holds the pools themselves, and this release does not widen a grant.
_PROJECT_BOUNDED_WRITE_PREFIXES = ("/instances", "/images", "/operations")
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ALL_PROJECTS_KEY = "all-projects"
_API_VERSION = "/1.0"

_MAX_DECODE_ROUNDS = 5
_QUERY_OR_FRAGMENT = re.compile(r"[?#]")


def fully_decoded(value: str) -> str:
    """Percent-decode until the value stops changing.

    note: raw_proxy decodes a query key once and re-emits it, and requests then unescapes
    unreserved characters, so a double-encoded spelling reaches LXD decoded. Deciding on the fully
    decoded form means no spelling gets a second answer.
    """
    current = value or ""
    for _ in range(_MAX_DECODE_ROUNDS):
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return current


def _path_part(decoded: str) -> str:
    return _QUERY_OR_FRAGMENT.split(decoded, maxsplit=1)[0]


def split_proxy_path(path: str) -> Tuple[str, Dict[str, str]]:
    """Split a raw proxy path into what raw_proxy sends: the path under /1.0 and the query.

    important: the fragment is dropped before anything else. requests never sends it, and
    raw_proxy appends the enforced `project` after the caller's query, so a raw `#` anywhere
    would otherwise turn that `project` into a fragment and land the request in `default`.
    `/1.0` is stripped only as a whole segment: `/1.0certificates` stays `/1.0certificates` and
    reaches LXD as `/1.0/1.0certificates`. Query names and values are decoded once, the way LXD's
    HTTP layer decodes them, and raw_proxy encodes them again, so a decoded `&`, `#` or `=`
    stays data instead of adding a key. A pair without `=` is dropped, as raw_proxy always did.
    """
    base, _, query = (path or "").split("#", 1)[0].partition("?")
    if base == _API_VERSION or base.startswith(_API_VERSION + "/"):
        base = base[len(_API_VERSION):]
    if not base.startswith("/"):
        base = "/" + base
    params: Dict[str, str] = {}
    for pair in query.split("&"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            params[unquote_plus(key)] = unquote_plus(value)
    return base, params


def resource_segments(path: str) -> List[str]:
    """The resource a proxy path reaches under /1.0, as lowercase segments.

    note: read off split_proxy_path, then fully decoded. A leading `1.0` segment that survives the
    split (`//1.0/certificates`, sent as `/1.0//1.0/...`) is dropped as well, which can only
    refuse more.
    """
    relative, _ = split_proxy_path(path)
    segments = [segment for segment in _path_part(fully_decoded(relative)).split("/") if segment]
    if segments and segments[0] == "1.0":
        segments = segments[1:]
    return [segment.lower() for segment in segments]


def _is_under(segments: List[str], prefix: str) -> bool:
    wanted = [part for part in prefix.strip("/").split("/") if part]
    return segments[: len(wanted)] == wanted


def assert_proxy_path_allowed(path: str) -> None:
    """Raise 400 for a traversal or backslash, or 403 for a path no integration may reach.

    Traversal is refused rather than normalised. Redirects are also no longer followed
    (services/lxd/proxy.py), because either guard alone leaves the other as the only thing
    standing.
    """
    decoded = fully_decoded(path)
    raw_segments = _path_part(decoded).split("/")
    if ".." in decoded or "\\" in decoded or "." in raw_segments:
        raise HTTPException(status_code=400, detail="Path may not contain traversal segments")

    segments = resource_segments(path)
    for prefix in _REFUSED_PROXY_PREFIXES:
        if _is_under(segments, prefix):
            logger.warning("Refusing proxy request to %s", prefix)
            raise HTTPException(status_code=403, detail=f"The proxy does not expose {prefix}")


def assert_server_state_unchanged(path: str, method: str) -> None:
    """Raise 403 for a write to the server configuration or the cluster, for every grant.

    Paired with assert_proxy_path_allowed, which refuses the trust store for every method. These
    two resources are refused only for writes, because the integrator reads them.
    """
    if method.upper() not in _MUTATING_METHODS:
        return
    segments = resource_segments(path)
    if segments and not any(_is_under(segments, prefix) for prefix in _SERVER_WRITE_PREFIXES):
        return
    resource = "/" + segments[0] if segments else "the server configuration"
    logger.warning("Refusing a %s to %s through the proxy", method.upper(), resource)
    raise HTTPException(status_code=403, detail=f"The proxy does not change {resource}")


# A project grant is enforced by binding the request to that project, which the body can undo.
# LXD reads a project from the body of a copy, a move and an image-from-instance, and an
# instance's own config and devices can reach the host, which belongs to every project at once.
# note: refused only for a key that is not `*`. The integrator's keys are `*` and unaffected.
# A value is judged, not only a key: the dashboard's create form sends security.privileged "false"
# for Deny, and security.idmap.isolated only ever narrows the map, so it is left out.
_HOST_CONFIG_SET = ("raw.", "security.idmap.base", "security.idmap.size", "linux.kernel_modules")
_HOST_CONFIG_SWITCHES = ("security.privileged", "security.syscalls.intercept.")
_SWITCHED_OFF = frozenset({"", "false", "0", "no", "off"})
# note: a GPU is allowed. It shares the host's cards, and does not reach the host's files or root.
_HOST_DEVICE_TYPES = frozenset(
    {"unix-char", "unix-block", "unix-hotplug", "usb", "pci", "infiniband", "proxy"}
)
# LXD's own drives for a VM. Any other disk source outside a storage pool is on the host: a path,
# or a ceph:/cephfs: volume mapped with the host's own Ceph credentials.
_BUILTIN_DISK_SOURCES = frozenset({"cloud-init:config", "agent:config"})


def _projects_named_in(body: Dict[str, Any]) -> List[str]:
    named = [body.get("project")]
    for key in ("source", "target"):
        nested = body.get(key)
        if isinstance(nested, dict):
            named.append(nested.get("project"))
    return [str(value) for value in named if value not in (None, "")]


def _config_reaches_host(key: Any, value: Any) -> bool:
    name, setting = str(key).lower(), str(value if value is not None else "").strip().lower()
    if name.startswith(_HOST_CONFIG_SWITCHES):
        return setting not in _SWITCHED_OFF
    return name.startswith(_HOST_CONFIG_SET) and setting != ""


def _host_reach(body: Dict[str, Any]) -> Optional[str]:
    """What in an instance or profile body reaches the host, or None."""
    config = body.get("config")
    if isinstance(config, dict):
        for key, value in config.items():
            if _config_reaches_host(key, value):
                return f"config {key}"
    devices = body.get("devices")
    if isinstance(devices, dict):
        for name, device in devices.items():
            if not isinstance(device, dict):
                continue
            kind = str(device.get("type") or "").lower()
            if kind in _HOST_DEVICE_TYPES:
                return f"device {name} of type {kind}"
            if kind == "disk":
                source = str(device.get("source") or "")
                if source.startswith("/"):
                    return f"device {name}, a disk from a host path"
                if source and not device.get("pool") and source.lower() not in _BUILTIN_DISK_SOURCES:
                    return f"device {name}, a disk from outside a storage pool"
            if kind == "nic" and str(device.get("nictype") or "").lower() == "physical":
                return f"device {name}, a physical host interface"
    return None


# important: LXD decodes a body with Go's encoding/json, which matches a struct field whatever its case
# (and with Unicode case folding), so "Profiles" or "Project" is read as profiles or project. Every
# body rule here reads the plain lowercase spelling, so a limited key's body must use it for the fields
# of the body and of its source and target. Map keys (config, devices, properties) match exactly.
_FIELD_NAME = re.compile(r"^[a-z0-9_-]+$")
_NESTED_STRUCTS = ("source", "target")


def _odd_field_name(body: Dict[str, Any]) -> Optional[str]:
    scopes = [body] + [body[key] for key in _NESTED_STRUCTS if isinstance(body.get(key), dict)]
    for scope in scopes:
        for key in scope:
            if not isinstance(key, str) or not _FIELD_NAME.fullmatch(key):
                return str(key)
    return None


def assert_body_within_grant(
    path: str, method: str, body: Optional[Dict[str, Any]], allowed_projects: Optional[List[str]]
) -> None:
    """Refuse a write body that leaves a project-limited grant. A `*` grant is never touched."""
    if project_grant_is_unrestricted(allowed_projects) or method.upper() not in _MUTATING_METHODS:
        return
    if not isinstance(body, dict):
        return
    granted = sorted(allowed_projects or [])
    odd = _odd_field_name(body)
    if odd is not None:
        logger.warning("Refusing a body field spelled %r for a key limited to %s", odd, granted)
        raise HTTPException(
            status_code=400,
            detail=f"The request body field {odd!r} must be spelled in lowercase ASCII for a key limited to {granted}.",
        )
    for project in _projects_named_in(body):
        if not project_allowed_by_grant(project, allowed_projects):
            logger.warning("Refusing a body naming project %s for a key limited to %s", project, granted)
            raise HTTPException(
                status_code=403,
                detail=f"The request body names project {project}. This key is limited to {granted}.",
            )
    segments = resource_segments(path)
    if segments[:1] == ["instances"]:
        reach = _host_reach(body)
        if reach:
            logger.warning("Refusing an instance body with %s for a key limited to %s", reach, granted)
            raise HTTPException(
                status_code=403,
                detail=(
                    f"The request sets {reach}, which reaches the host, so it needs a key granted "
                    f"all projects on this cluster. This key is limited to {granted}."
                ),
            )


def query_key_name(key: str) -> str:
    """A query name compared the strict way: fully decoded, trimmed and lowercased."""
    return fully_decoded(key).strip().lower()


def project_grant_is_unrestricted(allowed_projects: Optional[List[str]]) -> bool:
    """True for the `*` grant, the only project-unlimited shape."""
    return "*" in (allowed_projects or [])


def project_allowed_by_grant(project: str, allowed_projects: Optional[List[str]]) -> bool:
    """Whether a key's project grant on a cluster covers this project. A missing grant is none."""
    return project_grant_is_unrestricted(allowed_projects) or project in (allowed_projects or [])


def assert_project_scope_allows_path(
    path: str, method: str, allowed_projects: Optional[List[str]]
) -> None:
    """Refuse what a project-limited grant cannot bound. A `*` grant is never touched.

    Paired with assert_proxy_path_allowed, which refuses paths nobody may reach. This one refuses
    paths this grant may not reach: `all-projects` in any spelling, which reads every project on
    the cluster, and a change to anything outside _PROJECT_BOUNDED_WRITE_PREFIXES.
    """
    if project_grant_is_unrestricted(allowed_projects):
        return
    granted = sorted(allowed_projects or [])
    _, params = split_proxy_path(path)
    if _ALL_PROJECTS_KEY in {query_key_name(name) for name in params}:
        logger.warning("Refusing all-projects for a key limited to %s", granted)
        raise HTTPException(
            status_code=403,
            detail=(
                "all-projects reads every project on the cluster, so it needs a key granted all "
                f"projects. This key is limited to {granted}."
            ),
        )
    if method.upper() not in _MUTATING_METHODS:
        return
    segments = resource_segments(path)
    if any(_is_under(segments, prefix) for prefix in _PROJECT_BOUNDED_WRITE_PREFIXES):
        return
    resource = "/" + (segments[0] if segments else "")
    logger.warning("Refusing a %s to %s for a key limited to %s", method.upper(), resource, granted)
    raise HTTPException(
        status_code=403,
        detail=(
            f"{resource} is not bounded by a project, so changing it needs a key granted "
            f"all projects on this cluster. This key is limited to {granted}."
        ),
    )


def host_reach(body: Dict[str, Any]) -> Optional[str]:
    """Public name for the host-reach rule, so the dashboard routes and the proxy share one list."""
    return _host_reach(body)


def limited_write_plan(path: str, method: str, body: Any, project: str) -> List[Tuple[Any, ...]]:
    """What a project-limited key's write applies without saying so, as reads for raw_proxy to judge.

    Each step is one of:
      ("names", names, in_project): profiles the body names, resolved in in_project
      ("carried", instance, snapshot, read_project, judge_project, with_config, with_profiles, set_here):
          an instance or a snapshot whose config and devices (with_config, less the config keys and
          device names in set_here, which the body sets itself) and whose profile names
          (with_profiles, judged in judge_project) the write carries
      ("image", source, in_project, kind): the image a create of type kind applies the profiles of

    important: LXD applies profiles a body does not name. A create names none and gets default, a
    copy takes its source's profile names and an image create its image's, all resolved in the
    target project. A move to another project takes the target's profiles of the names the
    instance carries, and a restore re-applies a snapshot's profiles, config and devices. The
    instance name is decoded once, as LXD decodes it.
    """
    verb = method.upper()
    if not isinstance(body, dict) or verb not in ("POST", "PUT", "PATCH"):
        return []
    relative, _ = split_proxy_path(path)
    parts = [unquote(part) for part in _path_part(relative).split("/") if part]
    if parts[:1] == ["1.0"]:
        parts = parts[1:]
    kind = [part.lower() for part in parts[:1]]
    named = [str(name) for name in body["profiles"]] if isinstance(body.get("profiles"), list) else None
    here = project or "default"
    if kind == ["images"]:
        return [("names", named, here)] if named is not None else []
    if kind != ["instances"]:
        return []
    if verb == "POST" and len(parts) == 1:
        steps: List[Tuple[Any, ...]] = [("names", named, here)] if named is not None else []
        source = body.get("source") if isinstance(body.get("source"), dict) else {}
        kind_of_source = str(source.get("type") or "").lower()
        if kind_of_source == "copy":
            # note: a copy carries its source's config and devices except the keys and devices the
            # body sets (LXD's createFromCopy skips those), and its profile names only when the body
            # names none
            instance, _, snapshot = str(source.get("source") or "").partition("/")
            config = body.get("config") if isinstance(body.get("config"), dict) else {}
            devices = body.get("devices") if isinstance(body.get("devices"), dict) else {}
            return steps + [("carried", instance, snapshot or None, str(source.get("project") or here), here,
                             True, named is None, (tuple(sorted(config)), tuple(sorted(devices))))]
        if named is not None:
            return steps
        if kind_of_source == "image":
            return [("image", source, here, str(body.get("type") or "container"))]
        return [("names", ["default"], here)]
    if len(parts) != 2:
        return []
    instance = parts[1]
    if verb == "POST":
        target = body.get("project")
        if target not in (None, "") and str(target) != here:
            if named is not None:
                return [("names", named, str(target))]
            return [("carried", instance, None, here, str(target), False, True, ((), ()))]
        return [("names", named, here)] if named is not None else []
    steps = [("names", named, here)] if named is not None else []
    if body.get("restore"):
        steps.append(("carried", instance, str(body["restore"]), here, here, True, True, ((), ())))
    return steps


def profile_reach(read: Callable[[str], Optional[Dict[str, Any]]], names: Iterable[str]) -> Optional[str]:
    """The first of `names` whose config or devices reach the host, with what it sets, or None.

    important: a profile's config and devices land on the instance as its own do, so the host-reach
    rule holds for the profiles a write applies as well as for its body. `read` answers None for a
    profile that does not exist, which LXD refuses on its own.
    """
    for name in names:
        profile = read(name)
        reach = _host_reach(profile) if isinstance(profile, dict) else None
        if reach:
            return f"profile {name} with {reach}"
    return None
