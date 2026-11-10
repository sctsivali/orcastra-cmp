"""cloud-init data in an API key's write, and the capability it costs.

LXD hands `cloud-init.user-data`, `cloud-init.vendor-data` and the older `user.user-data` and
`user.vendor-data` to cloud-init, which runs them as root; a changed value gives the guest a new
instance-id (needsNewInstanceID, lxd/instance/drivers/driver_common.go), so the next boot runs it.
`cloud-init.ssh-keys.*` (LXD 5.21+) installs a login for any user and `user.meta-data` is appended
verbatim to the meta-data (lxd/device/disk.go, lxd/devlxd.go). Writing any of these runs a command
in the guest, so it costs `instance.exec` on every key, `*` included. Checked on raw-proxy instance
and profile config writes (`_writes_config`) and on /proxy create_instance and update_instance.

A write is judged by what it changes. LXD merges a PATCH, and a POST that moves an instance, over
the object's config and a PUT replaces it (instance_patch.go, instancePostMigration in
instance_post.go); cloud-init reruns only when an expanded value differs (driver_lxc.go Update).
So a PUT sending back the held values passes; a new, changed or removed value, or any cloud-init
key in a create or a copy, costs exec. Key names compare case-insensitively (after
core/api_key_body.py), so another spelling costs the same although LXD matches exactly. The
host-reaching config rule (raw.*, security.privileged, host devices) is separate, so a GPU recipe
keeps working on a `*` key.
"""

from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Tuple

from app.core.api_key_capabilities import (
    INSTANCE_PATH_FAMILIES,
    assert_explicit_capability,
    has_explicit_capability,
)
from app.core.lxd_segment import lxd_segment
from app.core.raw_proxy_path import canonical_proxy_path

__all__ = [
    "CLOUD_INIT_CAPABILITY",
    "CLOUD_INIT_KEYS",
    "CLOUD_INIT_SSH_KEYS_PREFIX",
    "assert_cloud_init_paid_for",
    "changed_cloud_init_keys",
    "cloud_init_capability_for",
    "cloud_init_compared_at",
    "cloud_init_keys_in",
    "proxy_cloud_init_capability_for",
    "proxy_cloud_init_compared_at",
]

CLOUD_INIT_CAPABILITY = "instance.exec"
CLOUD_INIT_KEYS = frozenset({
    "cloud-init.user-data",
    "cloud-init.vendor-data",
    "user.user-data",
    "user.vendor-data",
    "user.meta-data",
})
CLOUD_INIT_SSH_KEYS_PREFIX = "cloud-init.ssh-keys."
# /proxy operations whose params carry an instance's config to LXD.
_PROXY_CONFIG_WRITES = frozenset({"create_instance", "update_instance"})


def cloud_init_keys_in(config: Any) -> List[str]:
    """The cloud-init keys a config map carries, in the caller's spelling."""
    if not isinstance(config, Mapping):
        return []
    return [
        str(name) for name in config
        if str(name).lower() in CLOUD_INIT_KEYS or str(name).lower().startswith(CLOUD_INIT_SSH_KEYS_PREFIX)
    ]


def _writes_config(segments: Tuple[str, ...], method: str) -> bool:
    """Whether LXD applies the `config` of this raw-proxy write to an instance or a profile."""
    family = segments[:1]
    if family and family[0] in INSTANCE_PATH_FAMILIES:
        return (len(segments) == 1 and method == "POST") or (
            len(segments) == 2 and method in ("POST", "PUT", "PATCH")
        )
    if family == ("profiles",):
        return (len(segments) == 1 and method == "POST") or (len(segments) == 2 and method in ("PUT", "PATCH"))
    return False


def cloud_init_capability_for(path: str, method: str, body: Any) -> Optional[str]:
    """The capability a raw-proxy write costs because it carries cloud-init data, else None."""
    if not isinstance(body, Mapping) or not cloud_init_keys_in(body.get("config")):
        return None
    if _writes_config(canonical_proxy_path(path).lowered, method.upper()):
        return CLOUD_INIT_CAPABILITY
    return None


def cloud_init_compared_at(path: str, method: str) -> Optional[str]:
    """The LXD path of the instance or profile whose own config this raw-proxy write changes.

    None for a create (POST /<family>, POST /profiles), which has no config to compare with.
    """
    parsed = canonical_proxy_path(path)
    if len(parsed.segments) != 2 or not _writes_config(parsed.lowered, method.upper()):
        return None
    family = parsed.lowered[0]
    what = "Profile name" if family == "profiles" else "Instance name"
    return f"/{family}/{lxd_segment(parsed.segments[1], what=what)}"


def assert_cloud_init_paid_for(path: str, method: str, body: Any, api_key: Dict[str, Any]) -> Optional[str]:
    """Raise 403 for a raw-proxy create carrying cloud-init data, unless the key was granted exec.

    For a write to an existing instance or profile the price depends on what the object holds, so
    the path to read it from is returned instead, and the caller refuses a changed value after the
    read (changed_cloud_init_keys). None when nothing is owed, the key holding exec included.
    """
    capability = cloud_init_capability_for(path, method, body)
    if capability is None or has_explicit_capability(api_key, capability):
        return None
    compared_at = cloud_init_compared_at(path, method)
    if compared_at is None:
        assert_explicit_capability(api_key, capability)
    return compared_at


def changed_cloud_init_keys(config: Any, current: Any) -> List[str]:
    """The cloud-init keys `config` sets to a value other than the one `current` holds.

    Compared the way LXD diffs a config, where an absent key reads as "". So null or "" on a
    key the object lacks changes nothing, and "" on a key it holds removes it, which is a change. A
    value LXD would refuse (neither a string nor null) counts as a change rather than a guess.
    """
    held = current if isinstance(current, Mapping) else {}
    changed = []
    for name in cloud_init_keys_in(config):
        value, before = config[name], held.get(name)
        value = "" if value is None else value
        before = "" if before is None else before
        if not isinstance(value, str) or value != before:
            changed.append(name)
    return changed


def proxy_cloud_init_capability_for(operation: str, params: Mapping[str, Any]) -> Optional[str]:
    """The capability a /proxy operation costs because its config carries cloud-init data, else None."""
    if operation in _PROXY_CONFIG_WRITES and cloud_init_keys_in(params.get("config")):
        return CLOUD_INIT_CAPABILITY
    return None


def proxy_cloud_init_compared_at(operation: str, params: Mapping[str, Any]) -> Optional[str]:
    """The LXD path of the instance whose own config a /proxy update merges into, else None.

    update_instance merges `config` into the instance it reads (proxy_operations.py), the way
    LXD merges a PATCH. create_instance, and an update naming no instance, have nothing to compare.
    """
    name = params.get("name")
    if operation != "update_instance" or not isinstance(name, str) or not name:
        return None
    return f"/instances/{lxd_segment(name, what='Instance name')}"
