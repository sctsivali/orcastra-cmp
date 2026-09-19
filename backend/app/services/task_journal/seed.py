"""What the event stream cannot tell the journal: the operations a cluster holds right now, and
its warnings.

The hub only reports what happens while it is connected. On every connect, after a database
outage and every two minutes while connected, the follower reads `/1.0/operations` in every
bucket and upserts each one, then marks open rows the daemon no longer knows as lost. Every
read goes through `client_for` and `run_blocking` on this module's own bounded pool, with the
socket timeout inside the pylxd call and the wall clock outside it.
"""

from __future__ import annotations

import functools
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.blocking import run_blocking
from app.services.lxd.availability import unreachable_detail
from app.services.lxd.events_hub import LXDEventsHub
from app.services.lxd.lazy_client import client_for
from app.services.task_journal import repository
from app.services.task_journal.follower_frames import operation_frame

logger = logging.getLogger(__name__)

_JOURNAL_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="task_journal")
_MAX_IN_FLIGHT = 4
# (connect, read) inside pylxd; the wall clock outside is what the caller waits.
READ_TIMEOUT = (5, 30)
WALL_TIMEOUT_S = 35.0
BUCKETS = ("running", "pending", "success", "failure", "cancelled")
LOST_AFTER_S = 120
MAX_PROJECTS = 200

SEVERITY_FROM_LXD = {"low": "info", "moderate": "warning", "high": "error"}
_ENTITY_TYPES = {
    "instances": "instance",
    "containers": "instance",
    "virtual-machines": "instance",
    "images": "image",
    "storage-pools": "storage_pool",
    "volumes": "storage_volume",
    "networks": "network",
    "network-acls": "network_acl",
    "profiles": "profile",
    "projects": "project",
    "cluster": "cluster",
}
_SLUG = re.compile(r"[^a-z0-9]+")


def _is_http_400(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 400


def _metadata(response: Any) -> Any:
    body = response.json() if hasattr(response, "json") else response
    return (body or {}).get("metadata") if isinstance(body, dict) else None


async def _read(host: str, node: Any, params: Dict[str, str], label: str) -> Any:
    return await run_blocking(
        functools.partial(node.get, params=params, timeout=READ_TIMEOUT),
        timeout=WALL_TIMEOUT_S,
        label=f"task_journal.{label}[{host}]",
        executor=_JOURNAL_EXECUTOR,
        max_in_flight=_MAX_IN_FLIGHT,
    )


async def _project_names(client: Any, host: str) -> Tuple[List[str], bool]:
    """(names, complete): complete is False when the walk was cut at MAX_PROJECTS."""
    response = await _read(host, client.api.projects, {"recursion": "0"}, "projects")
    names: List[str] = []
    for entry in _metadata(response) or []:
        if isinstance(entry, str):
            names.append(entry.rsplit("/", 1)[-1])
        elif isinstance(entry, dict) and entry.get("name"):
            names.append(str(entry["name"]))
    if len(names) > MAX_PROJECTS:
        logger.warning("%s carries %d projects; reading the first %d", host, len(names), MAX_PROJECTS)
        return names[:MAX_PROJECTS], False
    return names, True


async def read_operations(client: Any, host: str) -> Tuple[Dict[str, List[Dict[str, Any]]], bool]:
    """Every bucket of `/1.0/operations` across projects, falling back to a per-project walk
    when the daemon refuses `all-projects` with a 400.

    The second value says whether the read saw every project. A partial read (a project that
    refused, projects past MAX_PROJECTS) must not feed `mark_lost`, or every live operation in
    the part it did not see is flipped to lost.
    """
    try:
        response = await _read(
            host, client.api.operations,
            {"recursion": "1", "all-projects": "true"}, "operations",
        )
    except Exception as exc:  # noqa: BLE001 - only a 400 is a fallback; the rest propagate
        if not _is_http_400(exc):
            raise
        logger.info("%s refused all-projects on /1.0/operations; walking projects", host)
        return await _read_operations_per_project(client, host)
    return _buckets(_metadata(response)), True


async def _read_operations_per_project(
    client: Any, host: str
) -> Tuple[Dict[str, List[Dict[str, Any]]], bool]:
    merged: Dict[str, List[Dict[str, Any]]] = {bucket: [] for bucket in BUCKETS}
    names, complete = await _project_names(client, host)
    for name in names:
        try:
            response = await _read(
                host, client.api.operations,
                {"recursion": "1", "project": name}, "operations",
            )
        except Exception as exc:  # noqa: BLE001 - one project is not the cluster
            logger.info("Skipping project %s on %s while reading operations: %s", name, host, exc)
            complete = False
            continue
        for bucket, ops in _buckets(_metadata(response)).items():
            merged.setdefault(bucket, []).extend(ops)
    return merged, complete


def _buckets(metadata: Any) -> Dict[str, List[Dict[str, Any]]]:
    if not isinstance(metadata, dict):
        return {}
    return {
        str(bucket): [op for op in ops if isinstance(op, dict)]
        for bucket, ops in metadata.items()
        if isinstance(ops, list)
    }


def frames_from_buckets(
    host: str, buckets: Dict[str, List[Dict[str, Any]]], identities: frozenset = frozenset()
) -> List[Any]:
    """Repository frames for every operation in every bucket, through the hub's own parser so a
    seeded operation and a streamed one are read identically (and the inner metadata is never
    copied)."""
    frames: List[Any] = []
    for bucket in BUCKETS:
        for op in buckets.get(bucket, []):
            raw = json.dumps({"type": "operation", "metadata": op}, default=str)
            payload = LXDEventsHub._parse_event(host, raw)
            mapped = operation_frame(payload, identities) if payload else None
            if mapped is not None:
                frames.append(mapped)
    return frames


async def reconcile_cluster(
    lxd_service: Any,
    host: str,
    session_maker: Callable[[], Any],
    identities: Optional[Any] = None,
) -> Optional[int]:
    """Upsert every operation the daemon holds and mark vanished open rows lost.

    Returns how many operations were read, or None when the cluster did not answer (its state
    row is set to disconnected with the breaker's reason).
    """
    client = await client_for(lxd_service, host, label="task_journal.seed")
    if client is None:
        async with session_maker() as db:
            await repository.set_cluster_state(
                db, host, "disconnected", error=unreachable_detail(host)
            )
            await db.commit()
        return None
    buckets, complete = await read_operations(client, host)
    known = await identities.for_host(host) if identities is not None else frozenset()
    frames = frames_from_buckets(host, buckets, known)
    lost = 0
    async with session_maker() as db:
        for frame in frames:
            await repository.upsert_operation(db, frame)
        if complete:
            lost = await repository.mark_lost(
                db,
                cluster_id=host,
                seen_ids=[frame.operation_id for frame in frames],
                older_than_s=LOST_AFTER_S,
            )
        else:
            logger.info("%s: partial operations read, nothing marked lost this pass", host)
        # The REST path answered: the state row says so, whatever an earlier timed-out read
        # wrote, so the panel does not call a cluster stale while its stream is flowing.
        await repository.set_cluster_state(db, host, "connected")
        await db.commit()
    if lost:
        logger.info("%s: %d operations the daemon no longer reports marked lost", host, lost)
    return len(frames)


async def seed_cluster(
    lxd_service: Any,
    host: str,
    session_maker: Callable[[], Any],
    identities: Optional[Any] = None,
) -> Optional[int]:
    """`reconcile_cluster` plus the seeded stamp on the cluster's state row."""
    count = await reconcile_cluster(lxd_service, host, session_maker, identities)
    if count is None:
        return None
    async with session_maker() as db:
        await repository.set_cluster_state(db, host, "connected", seeded=True)
        await db.commit()
    return count


def _entity(entity_url: Any) -> tuple:
    """(resource_type, resource_name, project) from a warning's entity URL."""
    if not isinstance(entity_url, str) or not entity_url:
        return None, None, None
    path, _, query = entity_url.partition("?")
    project = None
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        if key == "project" and value:
            project = value
    parts = [p for p in path.split("/") if p and p != "1.0"]
    if not parts:
        return None, None, project
    resource_type = _ENTITY_TYPES.get(parts[0], parts[0].replace("-", "_"))
    name = parts[-1] if len(parts) > 1 else None
    return resource_type, name, project


def _action_for(warning_type: Any) -> str:
    slug = _SLUG.sub("-", str(warning_type or "unknown").lower()).strip("-")[:80] or "unknown"
    return f"warning.{slug}"


def warning_row(host: str, warning: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    uuid = warning.get("uuid")
    if not uuid:
        return None
    resource_type, resource_name, url_project = _entity(warning.get("entity_url"))
    return {
        "ts": warning.get("last_seen_at"),
        "kind": "warning",
        "severity": SEVERITY_FROM_LXD.get(str(warning.get("severity") or "").lower(), "warning"),
        "cluster_id": host,
        "project": warning.get("project") or url_project,
        "resource_type": resource_type,
        "resource_name": resource_name,
        "action": _action_for(warning.get("type")),
        "message": str(warning.get("last_message") or warning.get("type") or "Warning"),
        "actor_email": None,
        "actor_role": None,
        "actor_kind": "system",
        "api_key_id": None,
        "result": None,
        "request_id": None,
        "event_id": f"warning:{uuid}:{warning.get('last_seen_at') or ''}",
        "task_id": None,
        "target_organization_id": None,
        "details": {
            "uuid": uuid,
            "type": warning.get("type"),
            "status": warning.get("status"),
            "count": warning.get("count"),
            "location": warning.get("location"),
            "first_seen_at": warning.get("first_seen_at"),
        },
    }


async def sweep_warnings(
    lxd_service: Any, host: str, session_maker: Callable[[], Any]
) -> int:
    """One cluster log row per (warning, last seen); repeats are no-ops on the event id."""
    client = await client_for(lxd_service, host, label="task_journal.warnings")
    if client is None:
        return 0
    try:
        response = await _read(
            host, client.api.warnings,
            {"recursion": "1", "all-projects": "true"}, "warnings",
        )
    except Exception as exc:  # noqa: BLE001 - only a 400 is a fallback; the rest propagate
        if not _is_http_400(exc):
            raise
        response = await _read(host, client.api.warnings, {"recursion": "1"}, "warnings")
    rows = [
        row
        for row in (warning_row(host, w) for w in _metadata(response) or [] if isinstance(w, dict))
        if row is not None
    ]
    if not rows:
        return 0
    async with session_maker() as db:
        inserted = await repository.insert_log_rows(db, rows)
        await db.commit()
    return inserted
