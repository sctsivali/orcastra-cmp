"""cloud-init user-data and vendor-data in a key's write, and what it costs.

LXD hands `cloud-init.user-data` and `cloud-init.vendor-data` (and the older `user.user-data` and
`user.vendor-data`) to cloud-init inside the guest, which runs what they say as root. When one of
them changes in an instance's expanded config, LXD gives the guest a new cloud-init instance-id
(needsNewInstanceID in lxd/instance/drivers/driver_common.go, called from each driver's Update), so
cloud-init treats the next boot as a first boot and runs the new data even on an instance that has
booted before. A profile carrying them reaches every instance that uses it. Writing one is running a
command in the guest, so it costs `instance.exec` for every key, `*` included.

Read wherever a key writes instance or profile config:

- the raw proxy: POST /instances in every source form, PUT and PATCH /instances/<name>, POST
  /instances/<name> when its body carries config, on /instances, /containers and /virtual-machines,
  and POST /profiles, PUT and PATCH /profiles/<name>;
- /proxy create_instance and update_instance, which MCP calls.

important: only these four keys are new for every key. The host-reaching config rule (raw.*,
security.privileged, host devices) keeps the reach it has on each surface today, so the integrator's
GPU recipe keeps working on a `*` key.

note: key names are compared without regard to case, after the body rule (core/api_key_body.py).
LXD matches a config key exactly, so another spelling is refused by LXD or never read by cloud-init,
and it is priced rather than guessed at.
"""

from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Tuple

from app.core.api_key_capabilities import INSTANCE_PATH_FAMILIES, assert_explicit_capability
from app.core.raw_proxy_path import canonical_proxy_path

__all__ = [
    "CLOUD_INIT_CAPABILITY",
    "CLOUD_INIT_KEYS",
    "assert_cloud_init_paid_for",
    "cloud_init_capability_for",
    "cloud_init_keys_in",
    "proxy_cloud_init_capability_for",
]

CLOUD_INIT_CAPABILITY = "instance.exec"
CLOUD_INIT_KEYS = frozenset({
    "cloud-init.user-data",
    "cloud-init.vendor-data",
    "user.user-data",
    "user.vendor-data",
})
# /proxy operations whose params carry an instance's config to LXD.
_PROXY_CONFIG_WRITES = frozenset({"create_instance", "update_instance"})


def cloud_init_keys_in(config: Any) -> List[str]:
    """The cloud-init keys a config map carries, in the caller's spelling."""
    if not isinstance(config, Mapping):
        return []
    return [str(name) for name in config if str(name).lower() in CLOUD_INIT_KEYS]


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


def assert_cloud_init_paid_for(path: str, method: str, body: Any, api_key: Dict[str, Any]) -> None:
    """Raise 403 for a raw-proxy write carrying cloud-init data, unless the key was granted exec."""
    capability = cloud_init_capability_for(path, method, body)
    if capability is not None:
        assert_explicit_capability(api_key, capability)


def proxy_cloud_init_capability_for(operation: str, params: Mapping[str, Any]) -> Optional[str]:
    """The capability a /proxy operation costs because its config carries cloud-init data, else None."""
    if operation in _PROXY_CONFIG_WRITES and cloud_init_keys_in(params.get("config")):
        return CLOUD_INIT_CAPABILITY
    return None
