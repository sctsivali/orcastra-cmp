"""Read the config a key's cloud-init write would change, before the write is sent.

core/cloud_init_config.py prices a write to an existing instance or profile by what it changes, so
the object's own config is read first, as a job of its own (limited_write_reach.run_checks), and
only for a body carrying a cloud-init key from a key without instance.exec.

important: fails closed. A read that errors, times out, meets a 5xx or finds no such object sends
nothing (ChecksNotFinished), because what the write changes cannot be judged. The cluster's breaker
is left alone for it, as for the limited-key plan reads.

note: the read and the write are two requests, so the value can change in between. What passes is
only a value the object held, which someone allowed to write it put there, never the caller's own.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Mapping, Optional, Tuple

import requests

from app.core.api_key_capabilities import assert_explicit_capability, has_explicit_capability
from app.core.cloud_init_config import (
    CLOUD_INIT_CAPABILITY,
    changed_cloud_init_keys,
    proxy_cloud_init_capability_for,
    proxy_cloud_init_compared_at,
)
from app.services.lxd.exceptions import LXDConnectionError
from app.services.lxd.limited_write_reach import ChecksNotFinished, ReadRefused, reader, run_checks
from app.services.lxd.proxy import socket_timeout_for

logger = logging.getLogger(__name__)

__all__ = [
    "cloud_init_changes",
    "proxy_cloud_init_changes",
    "proxy_cloud_init_owed",
    "refuse_changed_cloud_init",
]


def _not_sent(status_code: int, error_code: str, why: str) -> ChecksNotFinished:
    return ChecksNotFinished(
        status_code, error_code, f"The write was not sent: {why}, so the cloud-init data it carries could not be compared"
    )


def cloud_init_changes(
    session: Any, endpoint: str, path: str, project: str, config: Any, socket_timeout: Tuple[int, int]
) -> List[str]:
    """Blocking: the cloud-init keys `config` changes on the instance or profile at `path`."""
    try:
        held = reader(session, endpoint, socket_timeout)(path, project)
    except requests.exceptions.Timeout:
        raise
    except requests.exceptions.RequestException as unreachable:
        # note: the requests message names the LXD host's address, which the caller has no use for
        raise ReadRefused("the cluster did not answer a read the write depends on") from unreachable
    if held is None:
        raise _not_sent(404, "not_found", f"{path} does not exist in project {project}")
    current = (held.get("config") or {}) if isinstance(held, Mapping) else None
    if not isinstance(current, Mapping):
        raise _not_sent(502, "cluster_unavailable", f"LXD answered the read of {path} without a config")
    return changed_cloud_init_keys(config, current)


def proxy_cloud_init_changes(lxd_service: Any, cluster_id: str, path: str, project: str, config: Any) -> List[str]:
    """Blocking: cloud_init_changes for a /proxy operation, on the cluster's pooled client."""
    client = lxd_service.connection_manager.get_client(cluster_id)
    if client is None:
        raise LXDConnectionError(f"Could not connect to {cluster_id}")
    return cloud_init_changes(
        client.api.session, client.api._api_endpoint, path, project, config, socket_timeout_for(client)
    )


async def refuse_changed_cloud_init(
    read_fn: Any,
    *args: Any,
    api_key: Dict[str, Any],
    timeout: float,
    label: str,
    executor: ThreadPoolExecutor,
    max_in_flight: Optional[int],
) -> None:
    """Read what a raw-proxy write changes, then raise 403 for a changed value unless the key holds exec."""
    changed = await run_checks(
        read_fn, *args, timeout=timeout, label=label, executor=executor, max_in_flight=max_in_flight
    )
    if changed:
        logger.warning("Key %s writes changed cloud-init data %s", api_key.get("id", "unknown"), changed)
        assert_explicit_capability(api_key, CLOUD_INIT_CAPABILITY)


async def proxy_cloud_init_owed(
    lxd_service: Any,
    cluster_id: str,
    operation: str,
    params: Mapping[str, Any],
    project: str,
    api_key: Dict[str, Any],
    *,
    timeout: float,
    executor: ThreadPoolExecutor,
    max_in_flight: Optional[int],
) -> Optional[str]:
    """The capability a /proxy operation owes for the cloud-init data it carries, else None.

    An update from a key without exec is read first and owes nothing when every value it carries is
    already the instance's own. A create, and a key holding exec, are priced as before.
    """
    owed = proxy_cloud_init_capability_for(operation, params)
    if owed is None or has_explicit_capability(api_key, owed):
        return owed
    compared_at = proxy_cloud_init_compared_at(operation, params)
    if compared_at is None:
        return owed
    try:
        changed = await run_checks(
            proxy_cloud_init_changes, lxd_service, cluster_id, compared_at, project, params.get("config"),
            timeout=timeout, label=f"proxy_operation[cloud-init {cluster_id}]", executor=executor,
            max_in_flight=max_in_flight,
        )
    except LXDConnectionError as unreachable:
        raise _not_sent(503, "cluster_unavailable", f"cluster {cluster_id} is not connected") from unreachable
    return owed if changed else None
