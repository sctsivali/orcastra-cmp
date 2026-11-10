"""The steps every credentials request takes before it may touch a guest, in this order.

The caller has already passed `verify_guest_access`, which each handler calls itself so the
cluster-scope ratchet can see it. This adds what the shell's gate adds (break glass, the migration
fence), a per-person budget, this worker's limits, a wall clock, and the facts about the instance
that decide whether anything is sent at all: a held certificate pin, running, not Windows, and for
a virtual machine a reachable agent.

important: no `guard_instance_mutation`, for the reason `instance_files/session.py` gives: its
advisory lock would queue a cluster's mutations behind a stalling guest.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import Counter
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from pylxd.exceptions import NotFound

from app.core.auth import AuthenticatedUser
from app.core.blocking import BlockingPoolFull, BlockingTimeout
from app.core.lxd_path import safe_segment
from app.middleware.security import _shared_allow
from app.services.instance_files.session import http_failures, sensitive_gate
from app.services.lxd.instance_metrics_sections import guest_agent_status
from app.services.migration_fence import MigrationFenceConflict, assert_instance_not_migration_fenced

from . import audit, errors, runtime
from .transport import Guest

REQUEST_SECONDS = 90.0
READS_PER_MINUTE = 30
WRITES_PER_MINUTE = 12
PASSWORDS_PER_TEN_MINUTES = 5
# A person's slots stay below the pool's threads (runtime.POOL_WORKERS, 4 by default), so one
# tenant whose guest stalls every call cannot hold every thread of a worker.
SLOTS = {"instance": 2, "user": 2, "cluster": 8, "worker": 16}

_slots: Dict[Tuple[str, ...], int] = Counter()
_budget: Dict[str, List[float]] = {}
_budget_lock = threading.Lock()
_write_locks: Dict[Tuple[str, str, str], asyncio.Lock] = {}


def _within(key: str, limit: int, window: int) -> bool:
    shared = _shared_allow(f"guest-credentials:{key}", limit, window)
    if shared is not None:
        return shared
    now = time.monotonic()
    with _budget_lock:
        recent = [t for t in _budget.get(key, []) if now - t < window]
        recent.append(now)
        _budget[key] = recent
        if len(_budget) > 10_000:
            _budget.clear()
        return len(recent) <= limit


def spend(user: Optional[AuthenticatedUser], host: str, project: str, instance: str, kind: str) -> None:
    who = user.user_id if user is not None else "anonymous"
    if kind == "read" and not _within(f"read:{who}", READS_PER_MINUTE, 60):
        raise errors.rate_limited(60)
    if kind in ("write", "password") and not _within(f"write:{who}", WRITES_PER_MINUTE, 60):
        raise errors.rate_limited(60)
    if kind == "password" and not _within(f"password:{who}:{host}:{project}:{instance}", PASSWORDS_PER_TEN_MINUTES, 600):
        raise errors.rate_limited(600)


@asynccontextmanager
async def _slot(host: str, project: str, instance: str, who: str) -> AsyncIterator[None]:
    claims = [
        (("instance", host, project, instance), SLOTS["instance"]),
        (("user", who), SLOTS["user"]),
        (("cluster", host), SLOTS["cluster"]),
        (("worker",), SLOTS["worker"]),
    ]
    for key, limit in claims:
        if _slots[key] >= limit:
            raise errors.busy()
    for key, _ in claims:
        _slots[key] += 1
    try:
        yield
    finally:
        for key, _ in claims:
            _slots[key] -= 1
            if _slots[key] <= 0:
                del _slots[key]


def write_lock(host: str, project: str, instance: str) -> asyncio.Lock:
    """Writes to one instance from this worker run one at a time; across workers the key file's
    compare-and-swap is what protects it."""
    key = (host, project, instance)
    lock = _write_locks.get(key)
    if lock is None:
        if len(_write_locks) > 10_000:
            _write_locks.clear()
        lock = _write_locks[key] = asyncio.Lock()
    return lock


def _windows(config: Dict[str, Any]) -> bool:
    return "windows" in str(config.get("image.os") or "").lower()


def _open(host: str, project: str, instance: str) -> Guest:
    """Blocking: a pinned, project-scoped client, the instance and its state."""
    from app.services.lxd_service import get_lxd_service
    from app.services.lxd_ws import connection_pin, lxd_client

    if lxd_client(host) is None:
        raise errors.cluster_unreachable()
    if not connection_pin(host):
        raise errors.cluster_not_pinned()
    scoped = get_lxd_service().connection_manager.get_project_client(
        host, project, timeout=(3, int(runtime.EXEC_TIMEOUT) + 10)
    )
    if scoped is None:
        raise errors.cluster_unreachable()
    try:
        target = scoped.instances.get(safe_segment(instance))
    except NotFound:
        raise errors.refuse("instance_not_found", 404, "This instance no longer exists.") from None
    if int(getattr(target, "status_code", 0) or 0) != 103:
        raise errors.not_running(str(getattr(target, "status", "")))
    config = {**(getattr(target, "config", None) or {}), **(getattr(target, "expanded_config", None) or {})}
    if _windows(config):
        raise errors.unsupported_os()
    response = scoped.api.instances[safe_segment(instance)].state.get(timeout=(3, 15))
    state = (response.json() or {}).get("metadata") or {}
    instance_type = str(getattr(target, "type", "") or "container")
    if guest_agent_status(instance_type, getattr(target, "expanded_config", None), state) == "unreachable":
        raise errors.agent_unavailable()
    return Guest(host, project, instance, target, state, instance_type)


@asynccontextmanager
async def admitted(
    user: Optional[AuthenticatedUser],
    host: str,
    project: str,
    instance: str,
    *,
    kind: str,
    record: Optional[Dict[str, Any]] = None,
    route: str = "",
) -> AsyncIterator[Guest]:
    """Admit, take a slot, open the guest. `kind` is read, write or password (for the budget)."""
    async with http_failures():
        grant_id = await sensitive_gate(user, host)
        if record is not None:
            record["break_glass_grant_id"] = grant_id
        elif grant_id is not None:
            await audit.record_view(user, host, project, instance, grant_id, route)
        try:
            await assert_instance_not_migration_fenced(cluster_id=host, project=project, instance_name=instance)
        except MigrationFenceConflict:
            raise errors.refuse(
                "migration_fenced", 409, "This instance is being migrated. Its logins can be managed when it finishes."
            ) from None
        spend(user, host, project, instance, kind)
        who = user.user_id if user is not None else "anonymous"
        async with asyncio.timeout(REQUEST_SECONDS), _slot(host, project, instance, who):
            try:
                guest = await runtime.blocking(_open, host, project, instance, label="guest_credentials.open", host=host)
            except BlockingPoolFull:
                raise errors.busy() from None
            except BlockingTimeout:
                raise errors.cluster_unreachable() from None
            except errors.CredentialError:
                raise
            except Exception:  # noqa: BLE001 - a cluster that cannot be read is unreachable to the caller
                raise errors.cluster_unreachable() from None
            yield guest
