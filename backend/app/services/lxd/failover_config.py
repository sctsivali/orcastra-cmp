"""The failover settings of a cluster, as far as an integration may know them.

A marketplace that sells capacity on a cluster has to tell its buyers whether an instance survives
the loss of a host. Most of that is readable through the raw proxy (members, storage pools,
networks), but the one switch that decides automatic recovery, `cluster.healing_threshold`, lives
in the server configuration at the bare `/1.0`.

important: that object is deliberately not proxied. Besides the thresholds it carries the server
certificate, listen addresses and, before LXD 6, `core.trust_password`. This module reduces it to
an allowlist of numbers and one boolean, so a caller learns how the cluster recovers and nothing
about how to reach or join it.
"""

from typing import Any, Dict, Optional

# LXD's own defaults for a key the server configuration does not set. A trusted read that omits
# a key is a positive answer ("left at the default"), not a missing one.
FAILOVER_CONFIG_DEFAULTS: Dict[str, int] = {
    "cluster.healing_threshold": 0,
    "cluster.offline_threshold": 20,
    "cluster.max_voters": 3,
    "cluster.max_standby": 2,
}

_FIELD_NAMES: Dict[str, str] = {
    "cluster.healing_threshold": "healing_threshold_seconds",
    "cluster.offline_threshold": "offline_threshold_seconds",
    "cluster.max_voters": "max_voters",
    "cluster.max_standby": "max_standby",
}

# Bounds a parsed value so a corrupt or hostile string cannot reach a caller as a huge integer.
# Past it the value is unknown. Long thresholds below it are returned as LXD holds them; the
# caller decides whether a slow recovery still counts as automatic.
_MAX_VALUE = 10_000_000


def _as_count(raw: Any) -> Optional[int]:
    """LXD stores these as strings. A value that is not a whole, non-negative number is unknown."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
    else:
        return None
    return value if 0 <= value <= _MAX_VALUE else None


def project_failover_config(metadata: Any) -> Dict[str, Any]:
    """The allowlisted failover fields of a `GET /1.0` metadata block. Pure.

    Every field is None when the read cannot vouch for it: an untrusted answer carries no
    configuration, so its thresholds are unknown rather than LXD's defaults.
    """
    result: Dict[str, Any] = {"clustered": None, **{name: None for name in _FIELD_NAMES.values()}}
    if not isinstance(metadata, dict):
        return result

    environment = metadata.get("environment")
    clustered = environment.get("server_clustered") if isinstance(environment, dict) else None
    result["clustered"] = clustered if isinstance(clustered, bool) else None

    config = metadata.get("config")
    if metadata.get("auth") != "trusted" or not isinstance(config, dict):
        return result

    for key, field in _FIELD_NAMES.items():
        raw = config.get(key)
        result[field] = FAILOVER_CONFIG_DEFAULTS[key] if raw in (None, "") else _as_count(raw)
    return result
