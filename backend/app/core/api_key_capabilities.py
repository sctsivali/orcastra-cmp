"""What an external API key is allowed to *do*, as opposed to what it may reach.

`api_key_permissions.py` answers "which cluster and project, and how strongly"; its grant cannot
say whether a key may create organizations at all. A capability names a family of endpoints on
`external_router` (ex; `POST /integrations/sync` creates organizations and pushes roles into
Authentik) and composes with the per-cluster grant: `lxd.proxy` decides whether the proxy is
reachable, and `assert_cluster_permission` still decides which cluster and which verb.

important: an ABSENT `capabilities` field means a legacy key with full access. Such keys are read
straight out of Vault without passing through a Pydantic model, so a restrictive default would
silently revoke running integrations. An explicit empty list is an operator decision and grants
nothing, the same distinction `granted_level` draws between a missing field and an empty one.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Set

from fastapi import HTTPException

from app.core.lxd_names import POLICY_CONFIG_KEYS
from app.core.raw_proxy_path import ALL_PROJECTS_QUERY_KEY, canonical_proxy_path

logger = logging.getLogger(__name__)

# The vocabulary. Values are operator-facing copy: the create dialog and the integrator docs both
# read this table, so a capability cannot be added without a sentence explaining it.
CAPABILITIES: Dict[str, str] = {
    "inventory.read": "Read clusters, projects, instances and region metadata",
    "lxd.proxy": "Send operations to a cluster's LXD API through the proxy",
    "storage.upload": "Upload disk images and ISO volumes",
    "org.sync": "Create and update organizations and their members",
    "tenant.provision": "Create and revoke tenant access policies",
    "instance.exec": "Run commands inside an instance, as root unless a user is named",
    "instance.files": "Read and write files inside an instance",
    "desktop.view": "Capture the screen of an instance's graphical session",
    "desktop.input": "Move the mouse and type into an instance's graphical session",
    "desktop.clipboard": "Read the clipboard of an instance's graphical session",
}

# Reaching INSIDE a guest, rather than managing it from outside.
#
# important: every power that reaches inside a guest must be listed here. `orcahub` is
# `set(CAPABILITIES)` minus this set, and `has_capability` reads a legacy key as holding
# everything, so a guest-reaching entry missing from this set silently joins both. Listed powers
# are checked with `assert_explicit_capability`, which no legacy key satisfies: a power that did
# not exist when a key was issued cannot have been granted to it.
GUEST_ACCESS_CAPABILITIES = frozenset({
    "instance.exec",
    "instance.files",
    # `desktop.input` is a LARGER power than `instance.exec`: exec runs in a fresh non-interactive
    # context, while typing into a logged-in session inherits its authenticated browser, unlocked
    # keyring, live ssh-agent and cached sudo timestamp.
    "desktop.view",
    "desktop.input",
    "desktop.clipboard",
})

# What each application type gets when the caller does not name capabilities itself. These are
# defaults; the operator may tick any capability for any type.
#
# important: `orcahub` gets every capability outside guest access, the same reach a legacy key
# has. Narrowing it changes what the OrcaHub integration may call.
APP_TYPE_CAPABILITIES: Dict[str, Set[str]] = {
    "orcahub": set(CAPABILITIES) - GUEST_ACCESS_CAPABILITIES,
    "mcp": {"inventory.read", "lxd.proxy"},
    "automation": {"inventory.read", "lxd.proxy", "storage.upload"},
    "agent": {"inventory.read", "lxd.proxy"},
    # Empty: "Custom" means the operator chooses. A create request that names no capabilities for
    # it is refused with 422 instead of issuing a key that can do nothing.
    "custom": set(),
}

APP_TYPES = tuple(APP_TYPE_CAPABILITIES)

# Capabilities the create form marks as elevated. `org.sync` creates organizations and sets member
# roles, which propagate to Authentik as global system roles; `tenant.provision` binds clusters to
# organizations and grants tenants operate and console access. The guest-access set crosses from
# managing an instance to reaching inside it: exec runs as root in the guest by default, the file
# API reads any path, and the desktop capabilities act in the user's graphical session.
ELEVATED_CAPABILITIES = frozenset(
    {"org.sync", "tenant.provision"} | GUEST_ACCESS_CAPABILITIES
)

# The operator-facing reason each elevated capability is marked. Only two of them write access
# control.
ELEVATED_REASONS: Dict[str, str] = {
    "org.sync": "Writes access control",
    "tenant.provision": "Writes access control",
    "instance.exec": "Reaches inside the guest",
    "instance.files": "Reaches inside the guest",
    "desktop.view": "Photographs the user's screen",
    # Stronger than "Reaches inside the guest": input acts AS the logged-in user, with everything
    # their session has unlocked, and the create dialog has to say so.
    "desktop.input": "Acts as the logged-in desktop user",
    "desktop.clipboard": "Reads whatever the user last copied",
}


def granted_capabilities(api_key: Dict[str, Any]) -> Optional[Set[str]]:
    """The key's capabilities, or None for a legacy key that predates them.

    None is not "empty": the field is absent or null, and such a key keeps unrestricted access. A
    value that is not a list is corruption, since the one writer always stores a list, so it fails
    closed to the empty set instead of being read as legacy.
    """
    if "capabilities" not in api_key:
        return None
    raw = api_key.get("capabilities")
    if raw is None:
        return None
    if not isinstance(raw, list):
        logger.warning(
            "API key %s has a malformed capabilities field (%s); treating it as granting nothing",
            api_key.get("id", "unknown"), type(raw).__name__,
        )
        return set()
    return {name for name in raw if isinstance(name, str)}


def has_capability(api_key: Dict[str, Any], capability: str) -> bool:
    granted = granted_capabilities(api_key)
    if granted is None:
        return True
    return capability in granted


def has_explicit_capability(api_key: Dict[str, Any], capability: str) -> bool:
    """Like ``has_capability``, but a legacy key does NOT satisfy it.

    For powers that did not exist when legacy keys were issued: they were never granted, so a
    legacy key cannot already hold them.
    """
    granted = granted_capabilities(api_key)
    return granted is not None and capability in granted


def assert_explicit_capability(api_key: Dict[str, Any], capability: str) -> None:
    """Raise 403 unless the key was explicitly issued ``capability``."""
    if has_explicit_capability(api_key, capability):
        return
    legacy = granted_capabilities(api_key) is None
    logger.warning(
        "Refusing %s for API key %s (%s): %s",
        capability, api_key.get("id", "unknown"), api_key.get("app_name", "unknown"),
        "key predates capabilities and this one is never inherited" if legacy
        else "capability not granted",
    )
    raise HTTPException(
        status_code=403,
        detail=(
            f"API key does not have the '{capability}' capability. It reaches inside a guest, so "
            "it is never inherited by a key issued before capabilities existed and is never "
            "seeded by an application type: an operator has to grant it deliberately."
        ),
    )


def assert_capability(api_key: Dict[str, Any], capability: str) -> None:
    """Raise 403 unless the key carries ``capability``.

    The refusal names the capability so an integrator can act on it. It is logged with the key id
    because a denial on this router is the only trace an API-key caller leaves.
    """
    if has_capability(api_key, capability):
        return
    logger.warning(
        "Refusing %s for API key %s (%s): capability not granted",
        capability, api_key.get("id", "unknown"), api_key.get("app_name", "unknown"),
    )
    raise HTTPException(
        status_code=403,
        detail=f"API key does not have the '{capability}' capability",
    )


def default_capabilities_for(app_type: str) -> Set[str]:
    """Capabilities to issue when a create request names a type but no capabilities."""
    return set(APP_TYPE_CAPABILITIES.get(app_type, set()))


# ── the raw LXD proxy's path guard ─────────────────────────────────────────────────────────────

# LXD API paths no integration may reach through the proxy, whatever its cluster grant. A refusal
# list, so unlisted paths keep working. `/certificates` is not project-scoped and lists every
# client certificate the cluster trusts; `/auth` (LXD 5.21+) adds trusted TLS clients and
# server-wide entitlements, a credential that outlives the key. `/cluster/members` and
# `/resources` stay open: they are topology an integration may need.
_REFUSED_PROXY_PREFIXES = ("/certificates", "/auth")

# Resources the per-key project allow-list cannot bound.
#
# important: a project grant binds the request to that project, which bounds instances, images,
# volumes and snapshots but not a resource whose identity IS a project name or one above projects
# (ex; `DELETE /1.0/projects/tenant-b` with `project=tenant-a` passes the project check, and a
# storage pool takes a caller-chosen host path). Mutating these needs a `["*"]` grant; reads stay
# open, since disclosure is a smaller harm than destruction.
_PROJECT_UNBOUNDABLE_PREFIXES = ("/projects", "/storage-pools", "/networks", "/profiles")

# The only resources a project grant bounds, so the only ones a key that is not `*` may change.
#
# important: an allowlist, so the server config, the cluster, network ACLs and zones, warnings and
# any resource LXD adds above projects stay closed to a narrow key. Storage volumes stay refused
# because `/storage-pools` also holds the pools themselves.
# Images are bounded only while the project keeps its own image store (features.images).
_PROJECT_BOUNDED_WRITE_PREFIXES = ("/instances", "/images", "/operations")

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# important: LXD paths that reach INSIDE a guest, and the capability each one costs. Without this,
# any `lxd.proxy` key with `write` could run commands as root through `/exec`, and a `read` key
# could read /etc/shadow through `/files`. `/logs` is priced in `guest_access_capability_for`,
# since exec output lands there as `exec_<uuid>.stdout`.
_GUEST_ACCESS_PREFIXES: Dict[str, str] = {
    "/exec": "instance.exec",
    # important: a POST to `/console` returns the operation's file-descriptor secrets (see
    # `operation_urls` in services/lxd_ws.py, which builds the websocket URL from
    # metadata.metadata.fds), so a caller could attach to the guest's console directly, outside
    # this proxy and every session control. A GET returns the console log buffer (guest tty
    # output).
    "/console": "instance.exec",
    "/files": "instance.files",
    # A backup or a published image is the guest's filesystem in a tarball, which is the same
    # disclosure as reading its files one at a time.
    "/backups": "instance.files",
    # SFTP to the guest filesystem (LXD and Incus), and Incus's dirty bitmaps, which an
    # incremental disk export reads.
    "/sftp": "instance.files",
    "/bitmaps": "instance.files",
    # The metadata area's templates are rendered into the guest as root on create, copy and start,
    # so a template written here is a guest file written by another road.
    "/metadata": "instance.files",
    # important: each of these is root in the guest by another road. `nbd` opens the root and
    # dependent disks, `nvram` (Incus) and `uefi-vars` (LXD) rewrite the boot variables, and
    # `port-forward` (Incus) connects to any TCP port inside the instance past every network rule.
    # `debug` holds Incus's memory dump, which carries every secret the guest has in memory, and
    # its repair action. See tests/test_incus_guest_paths_cost_the_capability_they_reach.py.
    "/nbd": "instance.exec",
    "/nvram": "instance.exec",
    "/uefi-vars": "instance.exec",
    "/port-forward": "instance.exec",
    "/debug": "instance.exec",
}

# Log files that hold the output of a command someone ran. Ordinary instance logs (lxc.log,
# console.log) stay readable at `read`, so a monitoring integration can read a crash log without
# holding root in the guest.
_EXEC_LOG_PREFIX = "exec_"
# important: LXD 5.21 and later record exec output under logs/exec-output/<file>, where
# services/lxd/guest_output.py reads it. LXD itself serves that listing and each file only to a
# can_exec entitlement (instance_logs.go), so the directory and anything below it costs exec here.
_EXEC_OUTPUT_DIR = "exec-output"


# Instance configuration keys and device shapes that are execution by another name.
#
# important: `raw.lxc` reaches the container runtime and `lxc.hook.pre-start` runs as root ON THE
# HOST, more than exec in the guest. `security.privileged` plus a disk device whose source is a
# host path reaches as far. These cost `instance.exec` instead of being refused, because a key
# that may run commands in a guest is not made more dangerous by being able to configure one.
_EXECUTION_EQUIVALENT_CONFIG_PREFIXES = ("raw.", "security.privileged", "security.nesting")


def config_reaches_inside(config: Any, devices: Any) -> Optional[str]:
    """The capability an instance-configuration change costs, or None when it is ordinary.

    Ordinary is the common case and stays at `write`: limits, descriptions, user metadata, a
    profile change. Refusing those would break configuration management for no gain.
    """
    for key in (config or {}) if isinstance(config, dict) else {}:
        lowered = str(key).lower()
        if any(lowered.startswith(prefix) for prefix in _EXECUTION_EQUIVALENT_CONFIG_PREFIXES):
            return "instance.exec"
    for device in ((devices or {}).values() if isinstance(devices, dict) else []):
        if not isinstance(device, dict):
            continue
        source = str(device.get("source") or "")
        if device.get("type") == "disk" and source.startswith("/"):
            # A host path mounted into the guest is the file API with extra steps.
            return "instance.files"
    return None


def policy_keys_in_config(config: Any) -> List[str]:
    """The snapshot-schedule keys an instance-configuration change would set, if any.

    important: this observes and never refuses. The console derives `externally_managed` from
    `snapshots.schedule`, so such a write decides whether an operator's own snapshot actions on
    the instance are gated. The upstream product writes these keys through API keys, and which
    keys do is not known here, so the write is recorded rather than refused on a guess. The
    console's own create paths refuse the keys at the request boundary.
    """
    if not isinstance(config, dict):
        return []
    present = {str(key).lower() for key in config}
    return [key for key in POLICY_CONFIG_KEYS if key in present]


def proxy_path_segments(path: str) -> List[str]:
    """A raw-proxy path as lowercase segments, with the API version and query stripped.

    important: read off canonical_proxy_path, the parser the traversal guard and the sent URL
    read, so a percent-encoded, dotted or fragment spelling cannot get a second answer here.
    """
    return list(canonical_proxy_path(path).lowered)


# Raw-proxy path families that address instances. LXD routes all three to one handler.
INSTANCE_PATH_FAMILIES = frozenset({"instances", "containers", "virtual-machines"})


def policy_keys_in_raw_body(path: str, body: Any) -> List[str]:
    """The same observation for the raw proxy, which passes a body nothing inspects.

    Scoped to the instance path families, the only ones carrying these keys. LXD serves
    `/containers` and `/virtual-machines` as aliases of `/instances` (lxd/instances.go), so a
    write through either is recorded too.
    """
    segments = proxy_path_segments(path)
    if not segments or segments[0] not in INSTANCE_PATH_FAMILIES:
        return []
    if not isinstance(body, dict):
        return []
    return policy_keys_in_config(body.get("config"))


# What a destructive raw-proxy call is called in the audit trail, keyed on the resource it
# addresses. These mirror the dashboard's own names one namespace over, ex; image.delete becomes
# integration.image.delete, so both routes to the same destructive act are alertable and still
# name the principal.
_PROXY_DESTRUCTIVE_RESOURCES: Dict[str, str] = {
    "images": "image",
    "instances": "instance",
    "containers": "instance",
    "virtual-machines": "instance",
    "networks": "network",
    "projects": "project",
    "profiles": "profile",
    "certificates": "certificate",
    "storage-pools": "storage.pool",
}


def proxy_destructive_action_for(method: str, path: str) -> Optional[str]:
    """The audit action name for a raw-proxy call that destroys something, else None.

    A proxy DELETE removes the same resource the dashboard's own endpoint would, so it gets a
    named destructive action instead of the generic `integration.proxy.request`, and alerting on
    destructive actions also covers the API-key route. A bare collection path is not destructive
    (LXD has no `DELETE /1.0/images`), so a malformed path cannot invent an event.
    """
    if (method or "").upper() != "DELETE":
        return None
    segments = proxy_path_segments(path)
    if len(segments) < 2:
        return None
    resource = _PROXY_DESTRUCTIVE_RESOURCES.get(segments[0])
    if resource is None:
        return None
    # storage-pools/<pool>/volumes/<type>/<name> deletes a volume; the pool stays.
    if segments[0] == "storage-pools" and "volumes" in segments:
        return "integration.storage.volume.delete"
    return f"integration.{resource}.delete"


def guest_access_capability_for(path: str) -> Optional[str]:
    """The capability a raw-proxy path costs, or None when it stays outside the guest.

    Matches on the segment after the instance name, so `/1.0/instances/<n>/exec` is caught while
    an instance whose NAME contains "exec" is not. Reads the canonical path, as the traversal
    guard does, so a percent-encoded spelling gets the same answer.
    """
    segments = proxy_path_segments(path)
    # instances/<name>/<action>...
    if len(segments) < 3 or segments[0] not in ("instances", "containers", "virtual-machines"):
        return None
    for prefix, capability in _GUEST_ACCESS_PREFIXES.items():
        if segments[2] == prefix.lstrip("/"):
            return capability

    # `/logs` is split: the directory listing, the exec-output directory and any
    # exec_<uuid>.stdout are exec output and cost the exec capability; an ordinary log does not.
    if segments[2] == "logs":
        if len(segments) > 3 and segments[3] == _EXEC_OUTPUT_DIR:
            return "instance.exec"
        if len(segments) == 3 or segments[3].startswith(_EXEC_LOG_PREFIX):
            return "instance.exec"
    return None


def assert_guest_access_allowed(path: str, api_key: Dict[str, Any]) -> None:
    """Refuse a raw-proxy path that reaches inside a guest without the capability for it."""
    capability = guest_access_capability_for(path)
    if capability is None:
        return
    assert_explicit_capability(api_key, capability)


def project_grant_is_unrestricted(allowed_projects: Optional[List[str]]) -> bool:
    """True when the grant is the `*` wildcard, which is the only project-unlimited shape."""
    return "*" in (allowed_projects or [])


# LXD/Incus project names, with the shape and 63-character ceiling of the other LXD names put in
# a URL (routers/iso_uploads.py, models/schemas.py). A leading digit is legal (`2024-archive`).
#
# important: `\A...\Z` so a trailing newline cannot slip through. The value is compared literally
# against the project LXD reports and put into `?project=`: this is an authorization gate.
PROJECT_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")

# More projects than a cluster can plausibly hold is a paste accident; refuse the grant.
MAX_PROJECTS_PER_GRANT = 64

PROJECT_NAME_MAX_LENGTH = 63


def is_valid_project_name(name: str) -> bool:
    """True when `name` is a project name this platform is willing to put in a grant or a URL."""
    return isinstance(name, str) and bool(PROJECT_NAME_RE.match(name))


def normalise_project_grant(value: List[str]) -> List[str]:
    """Clean a requested project allow-list, or refuse it. Request path only, never stored data.

    important: no case folding and no Unicode normalisation. Every enforcement site compares the
    grant to the project name LXD reports with an exact `in` (routers/integrations.py
    `_granted_projects` and the proxy checks), so folding `Tenant-A` to `tenant-a` would store a
    grant that silently matches nothing. NFKC maps `\uff0a` FULLWIDTH ASTERISK onto `*`, which
    would turn a dead grant into a cluster-wide wildcard; the ASCII charset refuses it instead.
    """
    if not isinstance(value, list):
        raise ValueError("projects must be a list of project names.")

    cleaned: List[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise ValueError("projects must contain only project names.")
        # Measured before the trim so a long name cannot launder its length through whitespace.
        if len(raw) > PROJECT_NAME_MAX_LENGTH:
            raise ValueError(
                f"project name is longer than {PROJECT_NAME_MAX_LENGTH} characters: {raw[:80]!r}"
            )
        trimmed = raw.strip()
        if not trimmed:
            continue
        if trimmed == "*":
            cleaned.append(trimmed)
            continue
        if not PROJECT_NAME_RE.match(trimmed):
            raise ValueError(
                f"{trimmed!r} is not a valid project name. Use letters, digits, dot, hyphen or "
                "underscore, starting with a letter or digit."
            )
        cleaned.append(trimmed)

    deduped = list(dict.fromkeys(cleaned))

    if "*" in deduped and len(deduped) > 1:
        # `_granted_projects` returns the unrestricted wildcard the moment `*` appears, so a
        # stored ["tenant-a", "*"] reads on every screen as scoped to one project while granting
        # the whole cluster. Refuse the mix rather than silently picking one of its two meanings.
        raise ValueError(
            "a projects list may name '*' or specific projects, not both. '*' already covers "
            "every project on the cluster."
        )

    if not deduped:
        # Never fall back to ["default"]. An empty list denies every project at runtime, and
        # quietly widening it to the default project would grant more than the caller asked for.
        raise ValueError(
            "projects names no project, which grants nothing. Name at least one project, or '*' "
            "for every project on the cluster."
        )

    if len(deduped) > MAX_PROJECTS_PER_GRANT:
        raise ValueError(
            f"a grant may name at most {MAX_PROJECTS_PER_GRANT} projects, got {len(deduped)}."
        )

    return deduped


def assert_project_scope_allows_path(
    path: str, method: str, allowed_projects: Optional[List[str]]
) -> None:
    """Refuse a mutation the caller's project grant cannot bound.

    Paired with `assert_proxy_path_allowed`, which refuses paths no key may reach. This one
    refuses what this grant may not reach: an `all-projects` query in any spelling, which reads
    every project on the cluster, and a change to anything outside
    `_PROJECT_BOUNDED_WRITE_PREFIXES`.
    """
    if project_grant_is_unrestricted(allowed_projects):
        return
    granted = sorted(allowed_projects or ['default'])
    canonical = canonical_proxy_path(path)
    if ALL_PROJECTS_QUERY_KEY in canonical.query_names:
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
    if any(canonical.is_under(prefix) for prefix in _PROJECT_BOUNDED_WRITE_PREFIXES):
        return

    prefix = "/" + (canonical.lowered[0] if canonical.segments else "")
    logger.warning("Refusing a %s to %s for a key limited to %s", method.upper(), prefix, granted)
    raise HTTPException(
        status_code=403,
        detail=(
            f"{prefix} is not bounded by a project, so changing it needs a key granted "
            f"all projects on this cluster. This key is limited to "
            f"{sorted(allowed_projects or ['default'])}."
        ),
    )


def assert_proxy_path_allowed(path: str) -> None:
    """Raise 403 for a proxy path no integration may reach, or 400 for a malformed one.

    important: read off canonical_proxy_path, which refuses `.`, `..`, empty segments and a
    backslash with 400 and drops the fragment. requests (urllib3 2.x) removes dot segments and
    never sends a fragment, so a raw-string check would let `/1.0/./certificates` or
    `/1.0/certificates#x` reach the trust store. LXD answers a `..` path with a redirect outside
    `/1.0`, and following it would send the cluster's client certificate there. This refusal and
    `allow_redirects=False` in `services/lxd/proxy.py` are separate layers; keep both.
    """
    canonical = canonical_proxy_path(path)
    for prefix in _REFUSED_PROXY_PREFIXES:
        if canonical.is_under(prefix):
            logger.warning("Refusing proxy request to %s", prefix)
            raise HTTPException(
                status_code=403,
                detail=f"The proxy does not expose {prefix}",
            )
