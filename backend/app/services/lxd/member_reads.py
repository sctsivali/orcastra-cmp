"""A cluster's members, read side by side for the capacity aggregation.

Two rounds of parallel reads: every member's resources and pool list, then every pool's space.
The caller folds the answers in the members' order, so totals, GPU card indices and storage come
out as a sequential read would produce them. A member's pools are read only once its resources
answered, and a pool that cannot be read is recorded as degraded rather than skipped.

The members list is read with a bounded timeout, and a failed read is remembered for five
minutes: a member that stops answering makes LXD wait for its own deadline (about 25 seconds)
before failing the list. Meanwhile the caller falls back to the connected member's resources.
"""

import contextvars
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pylxd

from .member_resources import list_member_pools, read_pool_space
from .storage_model import PoolReading

logger = logging.getLogger(__name__)

MEMBER_READ_WORKERS = 12
# (connect, read) seconds for the members list; LXD's own deadline for a silent member is longer.
MEMBERS_READ_TIMEOUT = (3.0, 10.0)
MEMBERS_FAILURE_SECONDS = 300
_executor = ThreadPoolExecutor(max_workers=MEMBER_READ_WORKERS, thread_name_prefix="lxd_member_reads")


@dataclass
class MemberRead:
    name: str
    resources: Dict[str, Any] = field(default_factory=dict)
    read_at: Optional[datetime] = None
    error: Optional[Exception] = None
    pools: List[PoolReading] = field(default_factory=list)
    degraded: List[str] = field(default_factory=list)


def _members_failure_key(node_name: str) -> str:
    from app.core.cache import MEMBER_RESOURCES_CACHE_PREFIX

    # Under the member-resources prefix, so a node refresh and a capacity invalidation clear it
    # together with the member figures.
    return f"{MEMBER_RESOURCES_CACHE_PREFIX}:{node_name}:members_read_failed"


def members_read_failed(cache: Any, node_name: str) -> bool:
    try:
        return cache.get("lxd_resources", _members_failure_key(node_name)) is not None
    except Exception:  # noqa: BLE001 - an unreadable marker means ask the cluster
        return False


def read_members_list(cache: Any, client: pylxd.Client, node_name: str) -> Optional[Any]:
    """The members list body, or None after remembering the failure for five minutes."""
    try:
        response = client.api.cluster.members.get(
            params={"recursion": 1}, timeout=MEMBERS_READ_TIMEOUT
        )
        return response.json()
    except Exception as exc:  # noqa: BLE001 - the caller falls back to one member's resources
        logger.warning(
            "Could not read the members of %s, using the connected member's resources for %ss: %s",
            node_name,
            MEMBERS_FAILURE_SECONDS,
            str(exc)[:200],
        )
        try:
            cache.set(
                "lxd_resources",
                _members_failure_key(node_name),
                {"failed": type(exc).__name__},
                ttl=MEMBERS_FAILURE_SECONDS,
            )
        except Exception:  # noqa: BLE001 - without the marker the next check asks again
            pass
        return None


def _first_round(client: pylxd.Client, member_name: str) -> Tuple[Dict[str, Any], datetime, Any]:
    response = client.api.resources.get(params={"target": member_name})
    read_at = datetime.now(timezone.utc)
    resources = response.json().get("metadata", {})
    try:
        listing: Any = list_member_pools(client, member_name)
    except Exception as err:  # noqa: BLE001 - recorded by the caller as a degraded part
        listing = err
    return resources, read_at, listing


def _pool_entry(entry: Any) -> Optional[Tuple[str, str]]:
    if isinstance(entry, dict):
        name, driver = entry.get("name"), entry.get("driver") or "unknown"
    elif isinstance(entry, str) and entry:
        name, driver = entry.rstrip("/").split("/")[-1], "unknown"
    else:
        return None
    return (str(name), str(driver)) if name else None


def _submit(fn: Any, *args: Any) -> Future:
    # important: one context copy per task. The org scope and the request id live in context
    # variables, and Context.run refuses re-entry, so a shared copy fails concurrent tasks.
    return _executor.submit(contextvars.copy_context().run, fn, *args)


def read_members(client: pylxd.Client, node_name: str, member_names: Sequence[str]) -> List[MemberRead]:
    """One MemberRead per member, in the order given."""
    reads = [MemberRead(name) for name in member_names]
    first = [_submit(_first_round, client, read.name) for read in reads]
    pools: List[Tuple[MemberRead, str, str, Future]] = []
    for read, future in zip(reads, first):
        try:
            read.resources, read.read_at, listing = future.result()
        except Exception as err:  # noqa: BLE001 - the caller reports the member as unread
            read.error = err
            continue
        if isinstance(listing, Exception):
            logger.warning("Could not list pools on %s/%s: %s", node_name, read.name, listing)
            read.degraded.append("storage")
            continue
        for entry in listing:
            parsed = _pool_entry(entry)
            if parsed is not None:
                pools.append((read, *parsed, _submit(read_pool_space, client, read.name, parsed[0])))
    for read, name, driver, future in pools:
        try:
            space = future.result()
        except Exception as err:  # noqa: BLE001 - a pool that cannot be read is recorded
            logger.warning("Could not read pool %s on %s/%s: %s", name, node_name, read.name, err)
            read.degraded.append(f"storage:{name}")
            continue
        read.pools.append(
            PoolReading(
                member=read.name,
                name=name,
                driver=driver,
                space_total=space["total"],
                space_used=space["used"],
            )
        )
    return reads
