"""What an external API key is allowed to *do*, as opposed to what it may reach.

`api_key_permissions.py` answers "which cluster and project, and how strongly". It cannot answer
"may this key create organizations at all", because the grant it reads has no room for the
question. That gap was the whole security model for `external_router`: every valid key reached
every endpoint there, so a key issued for a read-only integration could also call
`POST /integrations/sync` and mint an organization, add members, and push a `partner` role into
Authentik.

A capability names a family of endpoints. It composes with the per-cluster grant rather than
replacing it: `lxd.proxy` decides whether the proxy is reachable, and `assert_cluster_permission`
still decides which cluster and which verb once it is.

important: an ABSENT `capabilities` field means legacy, and legacy means full access. Keys issued
before this existed are read straight out of Vault without passing through any Pydantic model, so
inventing a restrictive default would silently revoke access from running integrations. An
explicit empty list is the opposite: it is a decision the operator expressed, and it grants
nothing. That is the same distinction `granted_level` already draws between a missing field and
an empty one.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Set
from urllib.parse import unquote

from fastapi import HTTPException

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
# important: these two are excluded from every preset and from the legacy grandfather rule below,
# and both exclusions are load-bearing rather than tidy. `orcahub` is `set(CAPABILITIES)`, so
# without the first a new vocabulary entry silently widens the one integration in production. And
# `granted_capabilities` returns None for a key issued before capabilities existed, which
# `has_capability` reads as everything; 17 of the 22 keys in this deployment are in that state, so
# without the second, shipping this grants root-in-guest to most of the fleet with nobody deciding
# it. A power that did not exist when a key was issued cannot already have been granted to it.
GUEST_ACCESS_CAPABILITIES = frozenset({
    "instance.exec",
    "instance.files",
    # The desktop family belongs here for the same reason and one stronger. `desktop.input` is not
    # merely another way to reach inside a guest, it is a LARGER power than `instance.exec`:
    # exec runs in a fresh non-interactive context, while typing into a logged-in session inherits
    # that session's authenticated browser, unlocked keyring, live ssh-agent and already-validated
    # sudo timestamp. Pricing it as an equal of exec would understate it.
    "desktop.view",
    "desktop.input",
    "desktop.clipboard",
})

# What each application type gets when the caller does not name capabilities itself. These are
# defaults, not a ceiling: the operator may tick any capability for any type.
#
# important: `orcahub` is every capability on purpose. It is the only integration in production,
# and this is the value that keeps its behaviour identical to before capabilities existed.
APP_TYPE_CAPABILITIES: Dict[str, Set[str]] = {
    "orcahub": set(CAPABILITIES) - GUEST_ACCESS_CAPABILITIES,
    "mcp": {"inventory.read", "lxd.proxy"},
    "automation": {"inventory.read", "lxd.proxy", "storage.upload"},
    "agent": {"inventory.read", "lxd.proxy"},
    # Deliberately empty. "Custom" means the operator chooses, so there is nothing to guess, and
    # a request that names neither a type default nor its own capabilities is refused at the
    # boundary rather than issued as a key that can do nothing.
    "custom": set(),
}

APP_TYPES = tuple(APP_TYPE_CAPABILITIES)

# Capabilities that write authorization data rather than reading or driving infrastructure.
# `org.sync` creates organizations and sets member roles, which are propagated to Authentik as
# global system roles; `tenant.provision` binds clusters to organizations and grants tenants
# operate and console access. Granting either to an unattended caller is a real decision, so the
# create form marks them instead of listing all five as equivalent ticks.
# `instance.exec` and `instance.files` are elevated for a different reason: they cross from
# managing an instance to reaching inside it. Exec runs as root in the guest by default, and the
# file API reads any path, so either one turns a key into a credential for the guest's contents
# rather than for its lifecycle.
ELEVATED_CAPABILITIES = frozenset(
    {"org.sync", "tenant.provision"} | GUEST_ACCESS_CAPABILITIES
)

# The operator-facing reason a capability is marked, so the create form stops claiming that every
# elevated capability "writes access control", which is true of two of them and false of two.
ELEVATED_REASONS: Dict[str, str] = {
    "org.sync": "Writes access control",
    "tenant.provision": "Writes access control",
    "instance.exec": "Reaches inside the guest",
    "instance.files": "Reaches inside the guest",
    "desktop.view": "Photographs the user's screen",
    # important: not "Reaches inside the guest". That sentence is true of exec and files and
    # undersells this one, which acts AS the logged-in user with everything their session already
    # has unlocked. An operator reading the create dialog has to be told the difference.
    "desktop.input": "Acts as the logged-in desktop user",
    "desktop.clipboard": "Reads whatever the user last copied",
}


def granted_capabilities(api_key: Dict[str, Any]) -> Optional[Set[str]]:
    """The key's capabilities, or None for a legacy key that predates them.

    None is not "empty". It means the field was never written, which only happens for keys issued
    before this module existed, and those keep the unrestricted access they were issued with.

    A value that is not a list is corruption rather than a decision, and there is exactly one
    writer, so it fails closed to the empty set instead of being read as legacy.
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

    important: the only difference is the grandfather rule, and that difference is the whole
    point. ``has_capability`` answers "was this key issued before we started restricting", which
    is the right question for a power that existed then. It is the wrong question for a power
    that did not: nobody chose to grant it, so nobody can be said to already hold it.
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

# LXD API paths an integration must never reach through the proxy, whatever its cluster grant.
#
# important: a refusal list, not an allowlist, and that is a deliberate trade. An allowlist would
# be safer and would also break OrcaHub the first time it used a path nobody had enumerated, so
# the choice was to close what is provably dangerous and leave the rest working.
#
# `/certificates` is the sharp one. It is not project-scoped, so the per-key project allow-list
# does not touch it, and it returns every client certificate trusted by that cluster. A key with
# nothing but `read` on one cluster could enumerate the trust store of the whole control plane.
# `/cluster/members` and `/resources` are omitted deliberately: they are topology, an integration
# has legitimate reasons to read them, and the audit rated them lower than the certificate store.
_REFUSED_PROXY_PREFIXES = ("/certificates",)

# Resources the per-key project allow-list cannot bound.
#
# important: a grant of `projects: ["tenant-a"]` is enforced by binding the request to that
# project, which is a real boundary for instances, images, volumes and snapshots. It is not a
# boundary for a resource whose identity IS a project name, nor for one that lives above projects
# entirely. `DELETE /1.0/projects/tenant-b` carrying `project=tenant-a` passes the project check
# and destroys another tenant's project, and a storage pool is cluster-wide with a caller-chosen
# source path on the LXD host.
#
# So mutating these needs a grant that is not project-limited. A key holding `["*"]` on the
# cluster already reaches all of it and loses nothing; a key scoped to one project never had a
# defensible claim to them. Reads are left alone: refusing `GET /1.0/networks` would break
# integrations that legitimately enumerate topology, and disclosure is a smaller harm than
# destruction.
_PROJECT_UNBOUNDABLE_PREFIXES = ("/projects", "/storage-pools", "/networks", "/profiles")

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# LXD paths that reach INSIDE a guest rather than managing it from outside, and the capability
# each one costs.
#
# important: this closes a hole rather than adding a rule. `_REFUSED_PROXY_PREFIXES` holds exactly
# one entry, so `POST /1.0/instances/<n>/exec` was never refused: any key with `lxd.proxy` and
# `write` ran arbitrary commands as root in any guest in an allowed project, and a GET of
# `/files` needed only `read`, which made a read-only monitoring key an exfiltration tool.
# Demonstrated against this deployment before the fix: `uid=0(root)` from a `write` key, and
# /etc/shadow read with a `read` key. The integrator document actively recommended the exec path.
#
# `/logs` is here because exec output lands there as `exec_<uuid>.stdout`, and listing the
# directory exposes another caller's. A key using the first-class `exec_instance` operation gets
# its output in the response and never needs it.
_GUEST_ACCESS_PREFIXES: Dict[str, str] = {
    "/exec": "instance.exec",
    # important: `/console` hands back the same thing the endpoint removed from terminal.py did.
    # A POST returns the operation's file-descriptor secrets (see services/console_service.py,
    # which reads metadata.metadata.fds to build its wss URL), so a caller can attach to the
    # guest's console directly, outside this proxy and outside every session control. A GET
    # returns the console log buffer, which is guest tty output.
    "/console": "instance.exec",
    "/files": "instance.files",
    # A backup or a published image is the guest's filesystem in a tarball, which is the same
    # disclosure as reading its files one at a time.
    "/backups": "instance.files",
}

# Log files whose contents are the output of a command someone ran. Ordinary instance logs
# (lxc.log, console.log) stay readable at `read`, because a monitoring integration has a real
# reason to want them and forcing it to hold root-in-guest to read a crash log is worse than the
# disclosure it prevents.
_EXEC_LOG_PREFIX = "exec_"


# Instance configuration keys and device shapes that are execution by another name.
#
# important: `raw.lxc` reaches the container runtime and `lxc.hook.pre-start` runs as root ON THE
# HOST, which is more than exec-in-guest, not less. `security.privileged` plus a disk device whose
# source is a host path is the same reach by a slower road. All of it was available to any key
# with `lxd.proxy` and `write`, which would have made the new capability a door beside an open
# window. Gated at the same capability rather than refused outright, because a key that may run
# commands in a guest is not made more dangerous by being able to configure one.
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


def proxy_path_segments(path: str) -> list:
    """A raw-proxy path as lowercase segments, with the API version and query stripped.

    important: double-unquoted, because the traversal guard beside this is, and a classifier
    that decodes once while its neighbour decodes twice gives a percent-encoded spelling a
    second answer. One parser, so the two cannot drift apart.
    """
    normalised = unquote(unquote(path or "")).split("?", 1)[0]
    if normalised.startswith("/1.0"):
        normalised = normalised[4:]
    if not normalised.startswith("/"):
        normalised = "/" + normalised
    return [segment for segment in normalised.lower().split("/") if segment]


# What a destructive raw-proxy call is called in the audit trail, keyed on the resource it
# addresses. These mirror the dashboard's own names one namespace over, ex; image.delete becomes
# integration.image.delete, so the two routes to the same destructive act are both alertable and
# still tell you which principal did it.
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

    A DELETE through the proxy removes the same resource the dashboard's own endpoint would,
    but it used to be recorded as the generic `integration.proxy.request`, which is not a
    high-severity action. Anything alerting on `image.delete` therefore never saw the API-key
    route at all. This names it for what it is.

    A bare collection path is not destructive: `DELETE /1.0/images` is not a thing LXD does, and
    refusing to name it stops a malformed path inventing an event.
    """
    if (method or "").upper() != "DELETE":
        return None
    segments = proxy_path_segments(path)
    if len(segments) < 2:
        return None
    resource = _PROXY_DESTRUCTIVE_RESOURCES.get(segments[0])
    if resource is None:
        return None
    # storage-pools/<pool>/volumes/<type>/<name> is a volume, not the pool itself.
    if segments[0] == "storage-pools" and "volumes" in segments:
        return "integration.storage.volume.delete"
    return f"integration.{resource}.delete"


def guest_access_capability_for(path: str) -> Optional[str]:
    """The capability a raw-proxy path costs, or None when it stays outside the guest.

    Matches on the segment after the instance name, so `/1.0/instances/<n>/exec` is caught while
    an instance whose NAME contains "exec" is not. Decodes first, the way the traversal guard
    beside it does, so a percent-encoded spelling is not a second answer.
    """
    segments = proxy_path_segments(path)
    # instances/<name>/<action>...
    if len(segments) < 3 or segments[0] not in ("instances", "containers", "virtual-machines"):
        return None
    for prefix, capability in _GUEST_ACCESS_PREFIXES.items():
        if segments[2] == prefix.lstrip("/"):
            return capability

    # `/logs` is split: the directory listing and any exec_<uuid>.stdout is exec output and costs
    # the exec capability; a named ordinary log does not.
    if segments[2] == "logs":
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


# LXD/Incus project names, same shape and same 63-character ceiling this repo already applies to
# the other LXD object names it puts in a URL (`_VOLUME_NAME_RE`/`_POOL_NAME_RE` in
# routers/iso_uploads.py, `ProjectCreateRequest.name` max_length in models/schemas.py). Leading
# alphanumeric rather than leading alpha: LXD's letter-first rule is specific to networks, and a
# project named `2024-archive` is legal.
#
# important: `\A...\Z`, not `^...$`, so a trailing newline cannot slip through. This value is
# compared literally against the project LXD reports and is interpolated into `?project=`, so it
# is an authorization gate, not a tidiness rule.
PROJECT_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")

# A grant naming more projects than a cluster can plausibly hold is a paste accident, not a scope.
MAX_PROJECTS_PER_GRANT = 64

PROJECT_NAME_MAX_LENGTH = 63


def is_valid_project_name(name: str) -> bool:
    """True when `name` is a project name this platform is willing to put in a grant or a URL."""
    return isinstance(name, str) and bool(PROJECT_NAME_RE.match(name))


def normalise_project_grant(value: List[str]) -> List[str]:
    """Clean a requested project allow-list, or refuse it. Request path only, never stored data.

    important: no case folding and no unicode normalisation, both deliberate.

    Every enforcement site compares the grant to the project name LXD reports with an exact `in`
    (routers/integrations.py `_granted_projects` and the proxy checks), so folding `Tenant-A` to
    `tenant-a` would produce a grant that matches nothing. That silent dead grant is the defect
    this validation exists to prevent, so manufacturing it here would be self-defeating.

    NFKC is worse than useless: it maps `\uff0a` FULLWIDTH ASTERISK onto `*`, which would turn a
    harmless dead grant into a real cluster-wide wildcard. The ASCII charset refuses it instead.
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

    Paired with `assert_proxy_path_allowed`, which refuses paths nobody may reach. This one
    refuses paths this particular grant may not reach, which is why it needs the grant.
    """
    if project_grant_is_unrestricted(allowed_projects):
        return
    if method.upper() not in _MUTATING_METHODS:
        return

    normalised = unquote(unquote(path or "")).split("?", 1)[0]
    if normalised.startswith("/1.0"):
        normalised = normalised[4:]
    if not normalised.startswith("/"):
        normalised = "/" + normalised
    lowered = normalised.lower().rstrip("/")

    for prefix in _PROJECT_UNBOUNDABLE_PREFIXES:
        if lowered == prefix or lowered.startswith(prefix + "/"):
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

    Traversal is refused rather than normalised. `requests` does not remove dot segments, so a
    `..` reached LXD, whose Go mux cleaned the path and answered a redirect to somewhere outside
    `/1.0`; following it carried the cluster's client certificate along. Redirects are no longer
    followed (`services/lxd/proxy.py`), and this refuses the input that produced them, because
    one of those two alone leaves the other as the only thing standing.
    """
    raw = path or ""
    # Compare on the decoded form: `%2e%2e` and `..` must not be two different answers, which is
    # the same lesson the project-smuggling filter in raw_proxy already learned.
    decoded = unquote(unquote(raw))
    if ".." in decoded or "\\" in decoded:
        raise HTTPException(
            status_code=400,
            detail="Path may not contain traversal segments",
        )

    normalised = decoded.split("?", 1)[0]
    if normalised.startswith("/1.0"):
        normalised = normalised[4:]
    if not normalised.startswith("/"):
        normalised = "/" + normalised

    lowered = normalised.lower().rstrip("/")
    for prefix in _REFUSED_PROXY_PREFIXES:
        if lowered == prefix or lowered.startswith(prefix + "/"):
            logger.warning("Refusing proxy request to %s", prefix)
            raise HTTPException(
                status_code=403,
                detail=f"The proxy does not expose {prefix}",
            )
