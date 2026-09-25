"""What a project-limited API key's write applies, judged before the raw proxy or /proxy sends it.

A key granted named projects (not `*`) is held to them by the project its request runs in. The
body can step past that bound in three ways, and each is judged for such a key:

- it names another project, at the top level or in `source` or `target`, which LXD reads for a
  copy, a move and an image built from an instance;
- its own config or devices reach the host, which sits under every project at once;
- it applies profiles, or a source's config and devices, that reach the host. LXD applies profiles
  a body never names: a create naming none gets `default`, a copy its source's, an image create
  its image's, a move to another project that project's, and a restore the snapshot's.

The first two are checks on the body alone. The third needs cluster reads, so `limited_write_plan`
turns the write into the reads it depends on and services/lxd/limited_write_reach.py carries them
out before anything is sent, failing closed when a read cannot answer.

important: every function here reads a body already held to core/api_key_body (lowercase field
names, so no case-folded twin reaches LXD unread) and a path through
core/raw_proxy_path.canonical_proxy_path, the parser the sent URL is built from. A `*` key is
never judged here.

note: the dashboard does not use this module. Its own allowlist (services/instance_security_policy)
judges a Partner's effective payload on a cluster another organization also holds.
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, List, Mapping, Optional, Tuple, Union

from fastapi import HTTPException

from app.core.api_key_body import NESTED_STRUCTS
from app.core.api_key_capabilities import project_grant_is_unrestricted
from app.core.raw_proxy_path import MUTATING_METHODS, canonical_proxy_path

logger = logging.getLogger(__name__)

# A value is judged, not only a key: a switch set off does not reach the host (the dashboard's create
# form sends security.privileged "false" for Deny), and security.idmap.isolated only narrows the map.
_HOST_CONFIG_SET = ("raw.", "security.idmap.base", "security.idmap.size", "linux.kernel_modules")
_HOST_CONFIG_SWITCHES = ("security.privileged", "security.syscalls.intercept.")
_SWITCHED_OFF = frozenset({"", "false", "0", "no", "off"})
# note: a GPU is allowed. It shares the host's cards and reaches neither the host's files nor root.
_HOST_DEVICE_TYPES = frozenset(
    {"unix-char", "unix-block", "unix-hotplug", "usb", "pci", "infiniband", "proxy"}
)
# LXD's own drives for a VM. Any other disk source outside a storage pool is on the host: a path, or
# a ceph:/cephfs: volume mapped with the host's own Ceph credentials.
_BUILTIN_DISK_SOURCES = frozenset({"cloud-init:config", "agent:config"})
_WRITE_VERBS = frozenset({"POST", "PUT", "PATCH"})


def _project_allowed(project: str, allowed_projects: Optional[List[str]]) -> bool:
    return project_grant_is_unrestricted(allowed_projects) or project in (allowed_projects or [])


def _config_reaches_host(key: Any, value: Any) -> bool:
    name = str(key).lower()
    setting = str(value if value is not None else "").strip().lower()
    if name.startswith(_HOST_CONFIG_SWITCHES):
        return setting not in _SWITCHED_OFF
    return name.startswith(_HOST_CONFIG_SET) and setting != ""


def host_reach(body: Mapping[str, Any]) -> Optional[str]:
    """What in an instance or profile body reaches the host, or None."""
    config = body.get("config")
    if isinstance(config, Mapping):
        for key, value in config.items():
            if _config_reaches_host(key, value):
                return f"config {key}"
    devices = body.get("devices")
    if not isinstance(devices, Mapping):
        return None
    for name, device in devices.items():
        if not isinstance(device, Mapping):
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


def projects_named_in(body: Mapping[str, Any]) -> List[str]:
    """The projects a body names for LXD to read from or move to."""
    named = [body.get("project")]
    for key in NESTED_STRUCTS:
        nested = body.get(key)
        if isinstance(nested, Mapping):
            named.append(nested.get("project"))
    return [str(value) for value in named if value not in (None, "")]


def assert_limited_body(body: Mapping[str, Any], allowed_projects: Optional[List[str]], *, instance: bool) -> None:
    """Raise 403 for a body naming a project outside the grant, or, for an instance, reaching the host."""
    granted = sorted(allowed_projects or [])
    for project in projects_named_in(body):
        if not _project_allowed(project, allowed_projects):
            logger.warning("Refusing a body naming project %s for a key limited to %s", project, granted)
            raise HTTPException(
                status_code=403,
                detail=f"The request body names project {project}. This key is limited to {granted}.",
            )
    reach = host_reach(body) if instance else None
    if reach:
        logger.warning("Refusing an instance body with %s for a key limited to %s", reach, granted)
        raise HTTPException(
            status_code=403,
            detail=(
                f"The request sets {reach}, which reaches the host, so it needs a key granted all "
                f"projects on this cluster. This key is limited to {granted}."
            ),
        )


def assert_body_within_grant(
    path: str, method: str, body: Any, allowed_projects: Optional[List[str]]
) -> None:
    """The raw proxy's body rule for a project-limited key. A `*` grant is never touched."""
    if project_grant_is_unrestricted(allowed_projects) or method.upper() not in MUTATING_METHODS:
        return
    if not isinstance(body, Mapping):
        return
    family = canonical_proxy_path(path).lowered[:1]
    assert_limited_body(body, allowed_projects, instance=family == ("instances",))


def limited_key_refusal(reach: str, allowed_projects: Optional[List[str]]) -> str:
    return (
        f"The request applies {reach}, which reaches the host, so it needs a key granted all "
        f"projects on this cluster. This key is limited to {sorted(allowed_projects or [])}."
    )


# -- the reads a write depends on ---------------------------------------------------------------


@dataclass(frozen=True)
class NamedProfiles:
    """Profiles a body names, read in the project LXD resolves them in."""

    names: Tuple[str, ...]
    project: str


@dataclass(frozen=True)
class CarriedFrom:
    """An instance or snapshot whose config and devices (with_config, less what the body sets
    itself) or whose profile names (with_profiles, read in judge_project) the write carries."""

    instance: str
    snapshot: Optional[str]
    read_project: str
    judge_project: str
    with_config: bool
    with_profiles: bool
    set_config: Tuple[str, ...] = ()
    set_devices: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ImageApplied:
    """The image an instance create of type `kind` applies the profiles of, read in `project`.

    important: a local image create naming `source.project` is resolved in the target project by
    LXD 5.21 and in `source.project` by later LXD, and either version then reads the profiles in the
    target project. Which one a cluster runs is not known here, so `source_project` is read as well
    and the profiles of every record that exists are judged.
    """

    project: str
    kind: str
    fingerprint: Optional[str] = None
    alias: Optional[str] = None
    server: Optional[str] = None
    source_project: Optional[str] = None

    @property
    def lookup_projects(self) -> Tuple[str, ...]:
        """The projects LXD may resolve a local image in: the target, then any source.project."""
        if self.server or not self.source_project:
            return (self.project,)
        return tuple(dict.fromkeys((self.project, self.source_project)))


PlanStep = Union[NamedProfiles, CarriedFrom, ImageApplied]
InstanceWrite = Tuple[str, Optional[str], Mapping[str, Any]]


def _text(value: Any) -> Optional[str]:
    return None if value in (None, "") else str(value)


def _named_profiles(body: Mapping[str, Any]) -> Optional[Tuple[str, ...]]:
    profiles = body.get("profiles")
    return tuple(str(name) for name in profiles) if isinstance(profiles, list) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def instance_write_plan(
    verb: str, instance: Optional[str], body: Mapping[str, Any], project: str
) -> List[PlanStep]:
    """The reads an instance write depends on. `instance` None is the collection (a create).

    important: a copy carries its source's config and devices except the keys and devices the body
    sets (LXD's createFromCopy skips those), and its profile names only when the body names none.
    A move to another project takes that project's profiles of the names the instance carries.
    """
    here = project or "default"
    named = _named_profiles(body)
    steps: List[PlanStep] = [NamedProfiles(named, here)] if named is not None else []
    if instance is None:
        if verb != "POST":
            return []
        source = _mapping(body.get("source"))
        source_type = str(source.get("type") or "").lower()
        if source_type == "copy":
            origin, _, snapshot = str(source.get("source") or "").partition("/")
            return steps + [CarriedFrom(
                origin, snapshot or None, _text(source.get("project")) or here, here, True, named is None,
                tuple(sorted(_mapping(body.get("config")))), tuple(sorted(_mapping(body.get("devices")))),
            )]
        if named is not None:
            return steps
        if source_type == "image":
            return [ImageApplied(
                here, str(body.get("type") or "container"),
                _text(source.get("fingerprint")), _text(source.get("alias")), _text(source.get("server")),
                _text(source.get("project")),
            )]
        return [NamedProfiles(("default",), here)]
    if verb == "POST":
        target = _text(body.get("project"))
        if target is not None and target != here:
            if named is not None:
                return [NamedProfiles(named, target)]
            return [CarriedFrom(instance, None, here, target, False, True)]
        return steps
    restore = _text(body.get("restore"))
    if restore is not None:
        steps.append(CarriedFrom(instance, restore, here, here, True, True))
    return steps


def limited_write_plan(path: str, method: str, body: Any, project: str) -> List[PlanStep]:
    """The reads a project-limited key's raw-proxy write depends on, empty when it applies nothing."""
    verb = method.upper()
    if not isinstance(body, Mapping) or verb not in _WRITE_VERBS:
        return []
    segments = canonical_proxy_path(path).segments
    family = segments[0].lower() if segments else ""
    if family == "images":
        named = _named_profiles(body)
        return [NamedProfiles(named, project or "default")] if named is not None else []
    if family != "instances" or len(segments) > 2:
        return []
    return instance_write_plan(verb, segments[1] if len(segments) == 2 else None, body, project)


def proxy_instance_write(operation: str, params: Mapping[str, Any]) -> Optional[InstanceWrite]:
    """The instance write a /proxy operation sends, as (verb, instance, body), or None.

    note: mirrors services/lxd/proxy_operations.execute_proxy_operation. create_instance sends
    params as the body, update_instance merges config and devices into the instance and replaces
    its profiles, and restore_snapshot re-applies the snapshot's profiles, config and devices.
    """
    if operation == "create_instance":
        return ("POST", None, params)
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return None
    if operation == "update_instance":
        body = {field: params[field] or {} for field in ("config", "devices") if field in params}
        if "profiles" in params:
            body["profiles"] = list(params["profiles"] or [])
        return ("PATCH", name, body)
    snapshot = params.get("snapshot_name")
    if operation == "restore_snapshot" and isinstance(snapshot, str) and snapshot:
        return ("PUT", name, {"restore": snapshot})
    return None


def profile_reach(read: Callable[[str], Optional[Mapping[str, Any]]], names: Iterable[str]) -> Optional[str]:
    """The first of `names` whose config or devices reach the host, with what it sets, or None.

    note: `read` answers None for a profile that does not exist, which LXD refuses on its own.
    """
    for name in names:
        profile = read(name)
        reach = host_reach(profile) if isinstance(profile, Mapping) else None
        if reach:
            return f"profile {name} with {reach}"
    return None
