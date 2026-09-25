"""cloud-init user-data and vendor-data in a key's write, and what it costs.

LXD hands `cloud-init.user-data` and `cloud-init.vendor-data` (and the older `user.user-data` and
`user.vendor-data`) to cloud-init inside the guest, which runs what they say as root. When one of
them changes in an instance's expanded config, LXD gives the guest a new cloud-init instance-id
(needsNewInstanceID in lxd/instance/drivers/driver_common.go, called from each driver's Update), so
cloud-init treats the next boot as a first boot and runs the new data even on an instance that has
booted before. A profile carrying them reaches every instance that uses it. Writing one is running a
command in the guest, so it costs `instance.exec` for every key, `*` included. So does
`cloud-init.ssh-keys.<name>` (LXD's cloud_init_ssh_keys extension, 5.21 and later), whose `<user>:<key>`
value cloud-init installs as a login for that user, root included, on instances and profiles alike.

Read wherever a key writes instance or profile config:

- the raw proxy: POST /instances in every source form, PUT and PATCH /instances/<name>, POST
  /instances/<name> when its body carries config, on /instances, /containers and /virtual-machines,
  and POST /profiles, PUT and PATCH /profiles/<name>;
- /proxy create_instance and update_instance, which MCP calls.

A write is judged by what it changes. LXD merges a PATCH, and a POST /<family>/<name> that moves an
instance, over the object's own config and a PUT replaces it (instance_patch.go,
instancePostMigration in instance_post.go), then reruns cloud-init only when an expanded value
differs (driver_lxc.go Update diffs the expanded config). So a write to an existing instance or
profile whose cloud-init values the object's own config already holds changes nothing cloud-init
reads, whatever its profiles say, and it passes. That is the integrator's read-modify-write PUT,
which sends back the whole config it just read. A new, changed or removed value costs exec, and so
does any cloud-init key in a create, which has no config to compare with. A copy is a create: LXD
copies the source's config anyway, so carrying the source's value adds nothing a caller needs.

important: only these keys are new for every key. The host-reaching config rule (raw.*,
security.privileged, host devices) keeps the reach it has on each surface today, so the integrator's
GPU recipe keeps working on a `*` key.

note: key names are compared without regard to case, after the body rule (core/api_key_body.py).
LXD matches a config key exactly, so another spelling is refused by LXD or never read by cloud-init,
and it is priced rather than guessed at.
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

    note: compared the way LXD diffs a config, where an absent key reads as "". So null or "" on a
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

    note: update_instance merges `config` into the instance it reads (proxy_operations.py), the way
    LXD merges a PATCH. create_instance, and an update naming no instance, have nothing to compare.
    """
    name = params.get("name")
    if operation != "update_instance" or not isinstance(name, str) or not name:
        return None
    return f"/instances/{lxd_segment(name, what='Instance name')}"
