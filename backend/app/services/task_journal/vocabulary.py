"""Closed vocabularies shared by the follower, the sink, the readers and the panel.

Pure data and pure functions: no I/O, no scope, no settings. The dashboard's audit actions and
LXD's own descriptions meet here, so a task started from the console and the same task started
from an `lxc` shell land under one `action` and one `description`.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple

TERMINAL = frozenset({"success", "failure", "cancelled", "lost"})
OPEN = frozenset({"pending", "running"})

# LXD status codes: https://documentation.ubuntu.com/lxd/latest/rest-api/#status-codes
_PENDING_CODES = {100, 105, 106}
_TERMINAL_CODES = {200: "success", 400: "failure", 401: "cancelled"}
_STATUS_WORDS = {"pending", "running", "success", "failure", "cancelled", "lost"}


def status_from_lxd(status: Optional[str], status_code: Optional[int]) -> str:
    """The journal status for an LXD operation frame; the numeric code wins over the word."""
    if status_code in _TERMINAL_CODES:
        return _TERMINAL_CODES[status_code]
    if status_code in _PENDING_CODES:
        return "pending"
    if isinstance(status_code, int) and status_code > 0:
        return "running"
    word = (status or "").strip().lower()
    return word if word in _STATUS_WORDS else "running"


# LXD's operation descriptions -> the dashboard's action vocabulary. Unknown descriptions keep
# `action=None`; the description itself is still stored and shown.
DESCRIPTION_TO_ACTION: Dict[str, str] = {
    "Creating instance": "instance.create",
    "Starting instance": "instance.start",
    "Stopping instance": "instance.stop",
    "Restarting instance": "instance.restart",
    "Freezing instance": "instance.pause",
    "Unfreezing instance": "instance.resume",
    "Deleting instance": "instance.delete",
    "Updating instance": "instance.update",
    "Renaming instance": "instance.rename",
    "Snapshotting instance": "instance.snapshot.create",
    "Restoring snapshot": "instance.snapshot.restore",
    "Deleting snapshot": "instance.snapshot.delete",
    "Renaming snapshot": "instance.snapshot.rename",
    "Migrating instance": "instance.migrate",
    "Copying instance": "instance.migrate",
    "Rebuilding instance": "instance.rebuild",
    "Downloading image": "image.copy",
    "Importing image": "image.copy",
    "Refreshing image": "image.copy",
    "Deleting image": "image.delete",
    "Executing command": "instance.exec",
    "Showing console": "instance.console",
    "Creating storage volume": "storage.volume.create",
    "Deleting storage volume": "storage.volume.delete",
    "Creating storage pool": "storage.pool.create",
    "Deleting storage pool": "storage.pool.delete",
    "Creating network": "network.create",
    "Deleting network": "network.delete",
    "Deleting project": "project.delete",
}

ACTION_DESCRIPTIONS: Dict[str, str] = {}
for _desc, _action in DESCRIPTION_TO_ACTION.items():
    ACTION_DESCRIPTIONS.setdefault(_action, _desc)
ACTION_DESCRIPTIONS.update(
    {
        "image.upload.import": "Importing uploaded image",
        "vault.unseal.submit": "Submitting a Vault unseal key",
        "vault.unseal.reset": "Resetting the Vault unseal attempt",
        "vault.unsealed": "Unsealing Vault",
        "host_health.transition": "Host health changed",
        "storage.iso.upload.import": "Importing uploaded ISO",
        "network.acl.create": "Creating network ACL",
        "network.acl.delete": "Deleting network ACL",
        "profile.create": "Creating profile",
        "profile.update": "Updating profile",
        "profile.delete": "Deleting profile",
        "project.create": "Creating project",
        "storage.volume.resize": "Resizing storage volume",
        "instance.snapshot.schedule.run": "Running scheduled snapshot",
    }
)

# Audit actions that are tasks: action -> (kind, resource_type). Anything else the activity
# logger emits becomes a log line only.
TASK_ACTIONS: Dict[str, Tuple[str, str]] = {
    "instance.create": ("dashboard_action", "instance"),
    "instance.start": ("dashboard_action", "instance"),
    "instance.stop": ("dashboard_action", "instance"),
    "instance.restart": ("dashboard_action", "instance"),
    "instance.delete": ("dashboard_action", "instance"),
    "instance.migrate": ("dashboard_action", "instance"),
    "instance.rebuild": ("rebuild", "instance"),
    "instance.snapshot.create": ("dashboard_action", "instance"),
    "instance.snapshot.restore": ("dashboard_action", "instance"),
    "instance.snapshot.delete": ("dashboard_action", "instance"),
    "instance.snapshot.schedule.run": ("schedule", "instance"),
    "vm.create": ("dashboard_action", "instance"),
    "vm.start": ("dashboard_action", "instance"),
    "vm.stop": ("dashboard_action", "instance"),
    "vm.restart": ("dashboard_action", "instance"),
    "vm.delete": ("dashboard_action", "instance"),
    "image.delete": ("dashboard_action", "image"),
    "image.copy": ("dashboard_action", "image"),
    "image.upload.import": ("upload", "image"),
    "storage.iso.upload.import": ("upload", "storage_volume"),
    "storage.pool.create": ("dashboard_action", "storage_pool"),
    "storage.pool.delete": ("dashboard_action", "storage_pool"),
    "storage.volume.create": ("dashboard_action", "storage_volume"),
    "storage.volume.delete": ("dashboard_action", "storage_volume"),
    "storage.volume.resize": ("dashboard_action", "storage_volume"),
    "network.create": ("dashboard_action", "network"),
    "network.delete": ("dashboard_action", "network"),
    "network.acl.create": ("dashboard_action", "network_acl"),
    "network.acl.delete": ("dashboard_action", "network_acl"),
    "profile.create": ("dashboard_action", "profile"),
    "profile.update": ("dashboard_action", "profile"),
    "profile.delete": ("dashboard_action", "profile"),
    "project.create": ("dashboard_action", "project"),
    "project.delete": ("dashboard_action", "project"),
    "integration.instance.delete": ("dashboard_action", "instance"),
    "integration.image.delete": ("dashboard_action", "image"),
    "integration.network.delete": ("dashboard_action", "network"),
    "integration.project.delete": ("dashboard_action", "project"),
    "integration.profile.delete": ("dashboard_action", "profile"),
    "integration.storage.pool.delete": ("dashboard_action", "storage_pool"),
    "integration.storage.volume.delete": ("dashboard_action", "storage_volume"),
}

# important: the console's start/stop/restart/create/delete path audits `vm.*`, the
# `/instances` routes audit `instance.*`, and the daemon's frame maps to `instance.*`. The
# task key carries the canonical name so either router's click meets its operation, and a
# console start is listed once; the stdout audit keeps the name the router emitted.
TASK_ACTION_ALIASES: Dict[str, str] = {
    "vm.create": "instance.create",
    "vm.start": "instance.start",
    "vm.stop": "instance.stop",
    "vm.restart": "instance.restart",
    "vm.delete": "instance.delete",
    "integration.instance.delete": "instance.delete",
    "integration.image.delete": "image.delete",
    "integration.network.delete": "network.delete",
    "integration.project.delete": "project.delete",
    "integration.profile.delete": "profile.delete",
    "integration.storage.pool.delete": "storage.pool.delete",
}


def canonical_task_action(action: str) -> str:
    return TASK_ACTION_ALIASES.get(action, action)


# The router action and its service-layer `lxd.*` twin both fire for one click. The twin is
# dropped so a click is one log line and one task; `lxd.connection.*` has no twin and stays.
SUPPRESSED_ACTIONS = frozenset(
    {
        "lxd.instance.create",
        "lxd.instance.start",
        "lxd.instance.stop",
        "lxd.instance.restart",
        "lxd.instance.delete",
        "lxd.project.create",
        "lxd.project.update",
        "lxd.project.delete",
        "audit.view",
        "admin.audit.view",
    }
)

# Machine-driven reads never reach the cluster log, which they would swamp: `vault.secret.read`
# is the cert read behind every dial of a dead cluster and `integration.proxy.request` is the
# hub's polling. They stay on stdout, the audit of record; the log keeps what changed, what was
# denied and who signed in.
LOG_SUPPRESSED_ACTIONS = frozenset(
    {
        "vault.secret.read",
        "vault.kv.list",
        "vault.cert.list",
        "vault.health.check",
        "cluster.auth_check",
        "integration.proxy.request",
        "auth.token.refresh",
        # Opening a folder changes nothing.
        "file.browse",
        # Viewing a guest's accounts changes nothing.
        "guest.account.view",
    }
)


def humanise_action(action: str) -> str:
    """`vault.kv.put` -> `Vault kv put`, for an action the description table does not name."""
    words = [w for w in action.replace("_", " ").replace(".", " ").split(" ") if w]
    if not words:
        return action
    return " ".join([words[0].capitalize(), *words[1:]])

# Audit target types -> the journal's resource vocabulary.
TARGET_TYPE_TO_RESOURCE: Dict[str, str] = {
    "instance": "instance",
    "instance_snapshot": "instance",
    "snapshot": "instance",
    "vm": "instance",
    "container": "instance",
    "virtual_machine": "instance",
    "image": "image",
    "storage_volume": "storage_volume",
    "volume": "storage_volume",
    "storage_pool": "storage_pool",
    "pool": "storage_pool",
    "network": "network",
    "network_acl": "network_acl",
    "acl": "network_acl",
    "profile": "profile",
    "project": "project",
    "cluster": "cluster",
    "node": "cluster",
}

LIFECYCLE_TO_ACTION: Dict[str, str] = {
    "instance-created": "instance.create",
    "instance-started": "instance.start",
    "instance-stopped": "instance.stop",
    "instance-shutdown": "instance.stop",
    "instance-restarted": "instance.restart",
    "instance-paused": "instance.pause",
    "instance-resumed": "instance.resume",
    "instance-deleted": "instance.delete",
    "instance-renamed": "instance.rename",
    "instance-updated": "instance.update",
    "instance-snapshot-created": "instance.snapshot.create",
    "instance-snapshot-restored": "instance.snapshot.restore",
    "instance-snapshot-deleted": "instance.snapshot.delete",
    "image-created": "image.copy",
    "image-deleted": "image.delete",
    "image-refreshed": "image.copy",
    "network-created": "network.create",
    "network-deleted": "network.delete",
    "network-acl-created": "network.acl.create",
    "network-acl-deleted": "network.acl.delete",
    "profile-created": "profile.create",
    "profile-updated": "profile.update",
    "profile-deleted": "profile.delete",
    "project-created": "project.create",
    "project-deleted": "project.delete",
    "storage-volume-created": "storage.volume.create",
    "storage-volume-deleted": "storage.volume.delete",
    "storage-pool-created": "storage.pool.create",
    "storage-pool-deleted": "storage.pool.delete",
}

_LIFECYCLE_PREFIXES = (
    ("instance-snapshot-", "instance"),
    ("instance-", "instance"),
    ("storage-volume-", "storage_volume"),
    ("storage-pool-", "storage_pool"),
    ("network-acl-", "network_acl"),
    ("network-", "network"),
    ("image-", "image"),
    ("profile-", "profile"),
    ("project-", "project"),
    ("cluster-", "cluster"),
    ("warning-", "warning"),
)


def lifecycle_resource_type(action: str) -> str:
    for prefix, kind in _LIFECYCLE_PREFIXES:
        if action.startswith(prefix):
            return kind
    return action.split("-", 1)[0] or "unknown"


def lifecycle_message(action: str, name: Optional[str]) -> str:
    """'Instance web-01 started': the noun from the action, the verb from its tail."""
    noun = lifecycle_resource_type(action).replace("_", " ")
    if action.startswith("instance-snapshot-"):
        noun = "snapshot of instance"
    verb = action[len(_prefix_for(action)):] if _prefix_for(action) else action
    verb = verb.replace("-", " ")
    subject = f"{noun.capitalize()} {name}" if name else noun.capitalize()
    return f"{subject} {verb}".strip()


def _prefix_for(action: str) -> str:
    for prefix, _ in _LIFECYCLE_PREFIXES:
        if action.startswith(prefix):
            return prefix
    return ""


_RESOURCE_PRIORITY = (
    ("instances", "instance"),
    ("containers", "instance"),
    ("images", "image"),
    ("storage_volumes", "storage_volume"),
    ("storage-volumes", "storage_volume"),
    ("storage_pools", "storage_pool"),
    ("storage-pools", "storage_pool"),
    ("networks", "network"),
    ("network_acls", "network_acl"),
    ("profiles", "profile"),
    ("projects", "project"),
)


def resource_from_operation(resources: Optional[Mapping[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """(resource_type, resource_name) from an operation's `resources` block.

    A resource URL outside the default project is `/1.0/instances/<name>?project=<p>`; the query
    is split off before the last path segment is taken, so a name never carries `?project=`.
    """
    if not isinstance(resources, Mapping):
        return None, None
    for key, kind in _RESOURCE_PRIORITY:
        urls = resources.get(key)
        if isinstance(urls, list) and urls:
            path = str(urls[0]).partition("?")[0].rstrip("/")
            name = path.rsplit("/", 1)[-1]
            if name:
                return kind, name
    return None, None


def project_from_operation(resources: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The `?project=` a resource URL carries, when it carries one."""
    if not isinstance(resources, Mapping):
        return None
    for urls in resources.values():
        if not isinstance(urls, list):
            continue
        for u in urls:
            query = str(u).partition("?")[2]
            for pair in query.split("&"):
                key, _, value = pair.partition("=")
                if key == "project" and value:
                    return value
    return None


_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_lxd_time(value: Any) -> Optional[datetime]:
    """LXD emits nine fractional digits; Python parses six. Naive values are read as UTC."""
    if not value or not isinstance(value, str):
        return None
    text = _FRACTION.sub(r"\1", value.strip()).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def requestor_kind(
    requestor: Optional[Mapping[str, Any]], dashboard_identities: frozenset
) -> Tuple[str, str]:
    """(actor_kind, source) for an LXD `requestor` block.

    A unix-socket caller is someone at an `lxc` shell on the host. A TLS caller whose username
    (the certificate fingerprint) is one of this dashboard's own identities is the dashboard
    itself, and the audit record supplies the actor. Any other identity is an external client.
    """
    if not isinstance(requestor, Mapping):
        return "system", "events"
    protocol = str(requestor.get("protocol") or "").lower()
    username = str(requestor.get("username") or "")
    if protocol == "unix":
        return "external", "cli"
    if protocol == "tls" and username and username.lower() in dashboard_identities:
        return "system", "events"
    return "external", "events"


# Keys an LXD operation's metadata may contribute to the journal. Everything else, and in
# particular `fds`, `command`, `environment` and any credential-shaped key, is never stored.
METADATA_ALLOWLIST = frozenset({"location", "resources", "requestor", "job_id", "session_id", "schedule_id", "snapshot", "audit_error", "layer"})
REQUESTOR_KEYS = ("protocol", "username", "address")
METADATA_LIMIT = 8192


def _without_nul(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, Mapping):
        return {_without_nul(key): _without_nul(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_without_nul(item) for item in value]
    return value


def bounded_json(obj: Any, limit: int = METADATA_LIMIT) -> Dict[str, Any]:
    """A JSON object no larger than `limit` bytes; oversize values are replaced, never truncated
    mid-token, so the stored document always parses.

    important: NUL is stripped everywhere. Postgres refuses \\u0000 in jsonb, and the writer
    inserts in batches, so one such string would lose every record beside it.
    """
    if not isinstance(obj, Mapping):
        return {}
    obj = _without_nul(obj)
    out: Dict[str, Any] = {}
    for key, value in obj.items():
        candidate = dict(out)
        candidate[str(key)] = value
        if len(json.dumps(candidate, default=str)) > limit:
            out[str(key)] = "[omitted: too large]"
        else:
            out = candidate
    return out


def allowlisted_metadata(source: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not isinstance(source, Mapping):
        return {}
    out: Dict[str, Any] = {}
    for key in METADATA_ALLOWLIST:
        if key in source and source[key] is not None:
            value = source[key]
            if key == "requestor" and isinstance(value, Mapping):
                value = {k: value.get(k) for k in REQUESTOR_KEYS if value.get(k) is not None}
            out[key] = value
    return bounded_json(out)
