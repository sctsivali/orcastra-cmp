"""The operation vocabulary the external ``/integrations/proxy`` endpoint speaks.

This sits beside the proxy runtime rather than in the router because the two halves of a
contract have to stay in step: what an operation costs in permission, and the code that
performs it. They did not. ``update_instance`` and six administrative operations were priced in
the router's permission tables and never implemented, so a caller holding exactly the right
grant passed authorization and then received "Unknown operation" from the dispatcher. That
reads as a broken server rather than a missing feature, and it is invisible to any test that
only checks authorization. ``PROXY_OPERATION_LEVELS`` and ``execute_proxy_operation`` are now
the two halves of one table, and ``tests/test_the_proxy_operation_contract_is_honest.py``
fails if they drift apart again.

important: destruction costs ``admin``, not ``write``. The raw proxy has always required
``admin`` for every DELETE, and the same act arriving through a different door must not cost
less.
"""

import logging
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from fastapi import HTTPException

from .availability import require_client
from .proxy import PROXY_OP_TIMEOUT

logger = logging.getLogger(__name__)

__all__ = [
    "CLUSTER_WIDE_OPERATIONS",
    "status_name",
    "PROXY_ERROR_CODES",
    "assert_required_params",
    "PROXY_OPERATION_LEVELS",
    "PROXY_WAIT_OPERATIONS",
    "execute_proxy_operation",
    "fetch_operation_status",
    "instance_summary",
    "instance_detail",
]


# The complete contract. A name absent from this table is refused with 400 before any work is
# done; a name present here without a dispatch branch below is a promise the server cannot
# keep. The test named in the module docstring asserts the two sets are equal.
PROXY_OPERATION_LEVELS: Dict[str, str] = {
    # Reads. Every write below has a read counterpart, so an integration can discover what it
    # is allowed to create without dropping to the raw proxy to find out.
    "list_instances": "read",
    "get_instance": "read",
    "list_projects": "read",
    "list_images": "read",
    "list_networks": "read",
    "list_storage_pools": "read",
    "list_profiles": "read",
    "list_snapshots": "read",
    "get_operation": "read",
    # Writes: changing an instance that already exists, or creating one.
    "create_instance": "write",
    "update_instance": "write",
    "start_instance": "write",
    "stop_instance": "write",
    "restart_instance": "write",
    "create_snapshot": "write",
    "restore_snapshot": "write",
    # Admin: destruction, and infrastructure that is not scoped to a single instance.
    "delete_instance": "admin",
    "delete_snapshot": "admin",
    "create_network": "admin",
    "delete_network": "admin",
    "create_storage_pool": "admin",
    "delete_storage_pool": "admin",
    "create_project": "admin",
    "delete_project": "admin",
}

# Operations whose target is a resource no project contains, so the per-key project allow-list
# cannot bound them. The router refuses these to a key that is not granted `*` on the cluster.
# See _PROJECT_UNBOUNDABLE_PREFIXES in core/api_key_capabilities.py, which is the same rule for
# the raw proxy.
CLUSTER_WIDE_OPERATIONS: FrozenSet[str] = frozenset({
    "create_project",
    "delete_project",
    "create_storage_pool",
    "delete_storage_pool",
    "create_network",
    "delete_network",
})

# Operations that poll an LXD operation to completion. They need a wider read window than the
# pooled client's default, and they are the ones a caller may run with ``wait=false``.
PROXY_WAIT_OPERATIONS: FrozenSet[str] = frozenset({
    "create_instance",
    "update_instance",
    "delete_instance",
    "start_instance",
    "stop_instance",
    "restart_instance",
    "create_snapshot",
    "restore_snapshot",
    "delete_snapshot",
    "create_network",
    "delete_network",
    "create_storage_pool",
    "delete_storage_pool",
})


# What went wrong, as something a caller can branch on.
#
# important: this endpoint answers 200 with success=false for a failure inside the cluster,
# because that is the contract OrcaHub was built against and changing it would break a shipped
# integration. The consequence is that an unattended caller has to tell "the instance is not
# there" from "the cluster is down" by reading English prose, and those two demand opposite
# responses: give up, or retry later. The code is additive, so nothing that ignores it changes.
PROXY_ERROR_CODES = (
    "not_found",
    "conflict",
    "cluster_unavailable",
    "timeout",
    "invalid_request",
    "overloaded",
    "operation_failed",
)


def classify_proxy_error(error: Exception) -> str:
    """Map a failure from the cluster onto PROXY_ERROR_CODES."""
    status = getattr(error, "status_code", None) or getattr(
        getattr(error, "response", None), "status_code", None
    )
    if status in (404,):
        return "not_found"
    if status in (409,):
        return "conflict"
    if status in (503, 502, 504):
        return "cluster_unavailable"
    if status in (400, 422):
        return "invalid_request"

    text = str(error).lower()
    if "not found" in text or "no such" in text:
        return "not_found"
    if "already exists" in text or "conflict" in text or "in use" in text:
        return "conflict"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if any(hint in text for hint in ("connect", "unreachable", "certificate", "refused")):
        return "cluster_unavailable"
    return "operation_failed"


# LXD's own status codes, which are lossless where the dashboard's VMStatus enum is not.
#
# important: `parse_vm_status` (services/lxd/utils.py) maps four names and collapses everything
# else to UNKNOWN, which is right for a dashboard badge and wrong for a caller polling a
# transition. An agent that starts an instance and waits for it to leave "Starting" would see
# "Unknown" and could not tell it from a real fault. The code survives the enum, so read it.
LXD_STATUS_NAMES: Dict[int, str] = {
    100: "Operation created", 101: "Started", 102: "Stopped", 103: "Running",
    104: "Cancelling", 105: "Pending", 106: "Starting", 107: "Stopping",
    108: "Aborting", 109: "Freezing", 110: "Frozen", 111: "Thawed", 112: "Error",
    200: "Success", 400: "Failure", 401: "Cancelled",
}


def status_name(status_code: Optional[int], fallback: Any) -> Any:
    """The status LXD reported, preferring its code over a lossy enum round trip."""
    if status_code in LXD_STATUS_NAMES:
        return LXD_STATUS_NAMES[status_code]
    return getattr(fallback, "value", fallback)


# ── serialising an instance ────────────────────────────────────────────────────────────────


def _resources_from(
    config: Optional[Dict[str, Any]], expanded_devices: Optional[Dict[str, Any]]
) -> Dict[str, Optional[str]]:
    """CPU, memory and root disk as LXD records them.

    important: the effective root disk lives in ``expanded_devices``, not ``devices``. An
    instance that overrides nothing still has a root disk, inherited from its profile, and
    reading ``devices`` alone reports None for the common case.
    """
    config = config or {}
    root = (expanded_devices or {}).get("root") or {}
    return {
        "cpu": config.get("limits.cpu"),
        "memory": config.get("limits.memory"),
        "disk": root.get("size") or root.get("size.state"),
    }


def instance_summary(
    *,
    name: str,
    cluster_id: str,
    project: Optional[str],
    status: Optional[str],
    instance_type: Optional[str],
    architecture: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    expanded_devices: Optional[Dict[str, Any]] = None,
    status_code: Optional[int] = None,
    ip_addresses: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Enough for a caller to pick a target without a second round trip."""
    return {
        "name": name,
        "cluster_id": cluster_id,
        "project": project or "default",
        "status": status,
        "status_code": status_code,
        "type": instance_type,
        "architecture": architecture,
        "ip_addresses": list(ip_addresses or []),
        **_resources_from(config, expanded_devices),
    }


def instance_detail(
    *,
    devices: Optional[Dict[str, Any]] = None,
    expanded_devices: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    profiles: Optional[List[str]] = None,
    location: Optional[str] = None,
    description: Optional[str] = None,
    created_at: Any = None,
    last_used_at: Any = None,
    **summary_fields: Any,
) -> Dict[str, Any]:
    """The full specification, for a caller that has already chosen its target."""
    detail = instance_summary(
        config=config, expanded_devices=expanded_devices, **summary_fields
    )
    detail.update(
        {
            "config": dict(config or {}),
            "devices": dict(devices or {}),
            "expanded_devices": dict(expanded_devices or {}),
            "profiles": list(profiles or []),
            "location": location,
            "description": description,
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
            "last_used_at": (
                last_used_at.isoformat() if hasattr(last_used_at, "isoformat") else last_used_at
            ),
        }
    )
    return detail


# ── the dispatcher ─────────────────────────────────────────────────────────────────────────


def _accepted(response: Any) -> Dict[str, Any]:
    """Turn LXD's asynchronous response into something a caller can poll.

    important: pylxd's high-level methods discard the operation entirely when ``wait`` is
    false. ``Instance._set_state`` and ``Model.delete`` return None, and ``Instance.create``
    returns an object built from the request rather than the operation. So the non-waiting
    path goes through ``client.api`` directly, which is the only place the operation id is
    still visible.
    """
    body = response.json()
    operation_url = body.get("operation") or ""
    operation_id = operation_url.rsplit("/", 1)[-1] if operation_url else None
    if not operation_id:
        # note: LXD answers some of these synchronously (pylxd's _APINode accepts 200 as
        # success), and there is then no operation to poll. Reporting accepted with a null id
        # sent callers to /integrations/operations/None. Say it is already done instead.
        return {
            "accepted": False,
            "operation_id": None,
            "status": status_name(body.get("status_code"), body.get("status") or "Success"),
        }
    return {"accepted": True, "operation_id": operation_id, "status": "Running"}


# Reads that are a plain projection of a pylxd manager. Kept as data so adding one is a table
# entry rather than another near-identical comprehension.
_SIMPLE_LISTS: Dict[str, Any] = {
    "list_networks": ("networks", ("name", "type", "managed", "description", "used_by")),
    "list_storage_pools": ("storage_pools", ("name", "driver", "description", "status", "used_by")),
    "list_profiles": ("profiles", ("name", "description", "config", "devices")),
}

_OPERATION_FIELDS = (
    "id", "status", "status_code", "description", "may_cancel",
    "created_at", "updated_at", "resources", "metadata",
)


def _attr(item: Any, name: str) -> Any:
    value = getattr(item, name, None)
    return dict(value) if isinstance(value, dict) else list(value) if isinstance(value, list) else value


def _serialize_operation(operation: Any) -> Dict[str, Any]:
    payload = {name: _attr(operation, name) for name in _OPERATION_FIELDS}
    payload["error"] = getattr(operation, "err", None) or None
    return payload


def _client_for(lxd_service: Any, cluster_id: str, project: str, operation: str) -> Any:
    """The pooled client, replaced by a project-scoped one where the work needs it.

    The pooled client comes first regardless: it is what populates the cached connection info
    and temp certificate paths that ``get_project_client`` reuses, and it is what the
    reconnect logic maintains.
    """
    client = require_client(
        lxd_service.connection_manager.get_client(cluster_id), cluster_id
    )
    needs_scope = (project and project != "default") or operation in PROXY_WAIT_OPERATIONS
    if not needs_scope:
        return client
    # important: the socket read window has to outlast the response budget, not undercut it.
    # At 30s a create that pulls an image hit the socket timeout while LXD kept building, so
    # the caller was told "timed out" by a path that does not know the work survived, and the
    # instance was left running. Observed in QA: a failed create left a real instance behind.
    # Letting PROXY_OP_TIMEOUT be the binding limit puts the timeout on the path that says so.
    #
    # note: the cost is worker occupancy, and it is worth stating because it is not obvious.
    # BlockingTimeout frees the CALLER, not the thread (core/blocking.py), so a worker is held
    # until its own socket timeout fires. A waiting create is three sequential requests (POST,
    # operation GET, /wait), each getting a fresh window, so the worst case moved from roughly
    # 90s to roughly 195s on one of PROXY_POOL_WORKERS threads. Raise
    # ORCASTRA_PROXY_POOL_WORKERS if a fleet does enough slow creates to shed reads, or push
    # integrations towards `"wait": false`, which returns in about a second and holds nothing.
    scoped = lxd_service.connection_manager.get_project_client(
        cluster_id, project or "default", timeout=(3, int(PROXY_OP_TIMEOUT) + 5)
    )
    if scoped:
        return scoped
    if project and project != "default":
        raise HTTPException(
            status_code=503, detail=f"Cannot scope to project {project} on {cluster_id}"
        )
    return client


# What each operation cannot run without. Checked before a client is built, because
# `_client_for` revalidates the pooled connection and may rebuild it, so a request missing a
# name used to pay a round trip to the cluster only to be told about its own body. The branches
# still call `_required` themselves; this table is the gate, and the contract test asserts the
# two agree.
_REQUIRED_PARAMS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "get_instance": (("name", "Instance name"),),
    "update_instance": (("name", "Instance name"),),
    "start_instance": (("name", "Instance name"),),
    "stop_instance": (("name", "Instance name"),),
    "restart_instance": (("name", "Instance name"),),
    "delete_instance": (("name", "Instance name"),),
    "list_snapshots": (("name", "Instance name"),),
    "create_snapshot": (("name", "Instance name"), ("snapshot_name", "Snapshot name")),
    "restore_snapshot": (("name", "Instance name"), ("snapshot_name", "Snapshot name")),
    "delete_snapshot": (("name", "Instance name"), ("snapshot_name", "Snapshot name")),
    "get_operation": (("operation_id", "Operation id"),),
    "create_network": (("name", "Network name"),),
    "delete_network": (("name", "Network name"),),
    "delete_storage_pool": (("name", "Storage pool name"),),
    "create_project": (("name", "Project name"),),
    "delete_project": (("name", "Project name"),),
}


def _required(params: Dict[str, Any], field: str, what: str) -> Any:
    value = params.get(field)
    if not value:
        raise HTTPException(status_code=400, detail=f"{what} is required in params.{field}")
    return value


def assert_required_params(operation: str, params: Dict[str, Any]) -> None:
    """Refuse a malformed request before it costs a connection."""
    for field, what in _REQUIRED_PARAMS.get(operation, ()):
        _required(params, field, what)


def execute_proxy_operation(
    lxd_service: Any,
    cluster_id: str,
    operation: str,
    params: Dict[str, Any],
    project: str,
    wait: bool = True,
) -> Any:
    """Blocking half of ``proxy_operation``. Only ever runs on ``proxy_executor``.

    important: pylxd binds the project at the CLIENT level. ``Instance.all/get/create`` and
    ``Project.all``/``Image.all`` take no ``project=`` keyword, so a per-call ``project=``
    raised TypeError before any network I/O and every instance branch returned success=False.
    Project scoping goes through ``get_project_client``, which is what that method exists for.
    """
    assert_required_params(operation, params)
    client = _client_for(lxd_service, cluster_id, project, operation)

    def instance() -> Any:
        return client.instances.get(_required(params, "name", "Instance name"))

    def instance_api() -> Any:
        return client.api.instances[_required(params, "name", "Instance name")]

    # ── reads ──────────────────────────────────────────────────────────────────────────────

    if operation == "list_instances":
        return [
            instance_summary(
                name=i.name,
                cluster_id=cluster_id,
                project=project or "default",
                status=i.status,
                status_code=getattr(i, "status_code", None),
                instance_type=i.type,
                architecture=i.architecture,
                config=dict(i.config or {}),
                expanded_devices=dict(getattr(i, "expanded_devices", {}) or {}),
            )
            for i in client.instances.all()
        ]

    if operation == "get_instance":
        inst = instance()
        return instance_detail(
            name=inst.name,
            cluster_id=cluster_id,
            project=project or "default",
            status=status_name(getattr(inst, "status_code", None), inst.status),
            status_code=getattr(inst, "status_code", None),
            instance_type=inst.type,
            architecture=inst.architecture,
            config=dict(inst.config or {}),
            devices=dict(getattr(inst, "devices", {}) or {}),
            expanded_devices=dict(getattr(inst, "expanded_devices", {}) or {}),
            profiles=list(getattr(inst, "profiles", []) or []),
            location=getattr(inst, "location", None),
            description=getattr(inst, "description", None),
            created_at=getattr(inst, "created_at", None),
            last_used_at=getattr(inst, "last_used_at", None),
        )

    if operation == "list_projects":
        return [{"name": p.name, "description": p.description} for p in client.projects.all()]

    if operation == "list_images":
        return [
            {
                "fingerprint": img.fingerprint[:12],
                "description": img.properties.get("description", ""),
                "size": img.size,
            }
            for img in client.images.all()
        ]

    if operation in _SIMPLE_LISTS:
        manager, fields = _SIMPLE_LISTS[operation]
        return [
            {name: _attr(item, name) for name in fields}
            for item in getattr(client, manager).all()
        ]

    if operation == "list_snapshots":
        return [
            {
                "name": s.name,
                "created_at": getattr(s, "created_at", None),
                "stateful": getattr(s, "stateful", None),
            }
            for s in instance().snapshots.all()
        ]

    if operation == "get_operation":
        return _serialize_operation(
            client.operations.get(_required(params, "operation_id", "Operation id"))
        )

    # ── writes ─────────────────────────────────────────────────────────────────────────────

    if operation == "create_instance":
        if not wait:
            return _accepted(client.api.instances.post(json=params))
        created = client.instances.create(params, wait=True)
        return {"name": created.name, "status": created.status}

    if operation == "update_instance":
        inst = instance()
        if "config" in params:
            inst.config.update(params["config"] or {})
        if "devices" in params:
            inst.devices.update(params["devices"] or {})
        if "description" in params:
            inst.description = params["description"]
        if "profiles" in params:
            inst.profiles = list(params["profiles"] or [])
        if not wait:
            return _accepted(instance_api().put(json=inst.marshall()))
        inst.save(wait=True)
        return {"name": inst.name, "updated": True}

    if operation in ("start_instance", "stop_instance", "restart_instance"):
        action = operation.split("_", 1)[0]
        force = bool(params.get("force", False))
        if not wait:
            return _accepted(
                instance_api().state.put(
                    json={"action": action, "timeout": 30, "force": force}
                )
            )
        target = instance()
        if action == "start":
            target.start(wait=True)
        elif action == "stop":
            target.stop(force=force, wait=True)
        else:
            target.restart(force=force, wait=True)
        return {"name": target.name, "status": "Stopped" if action == "stop" else "Running"}

    if operation == "create_snapshot":
        # note: validate before the lookup. Reversed, a request missing snapshot_name spent a
        # round trip to the cluster and came back reporting the instance, which points a caller
        # at the wrong thing to fix.
        snapshot_name = _required(params, "snapshot_name", "Snapshot name")
        stateful = bool(params.get("stateful", False))
        inst = instance()
        if not wait:
            return _accepted(
                instance_api().snapshots.post(
                    json={"name": snapshot_name, "stateful": stateful}
                )
            )
        inst.snapshots.create(snapshot_name, stateful=stateful, wait=True)
        return {"name": inst.name, "snapshot": snapshot_name, "created": True}

    if operation == "restore_snapshot":
        snapshot_name = _required(params, "snapshot_name", "Snapshot name")
        inst = instance()
        if not wait:
            return _accepted(instance_api().put(json={"restore": snapshot_name}))
        inst.restore_snapshot(snapshot_name, wait=True)
        return {"name": inst.name, "restored": snapshot_name}

    # ── admin ──────────────────────────────────────────────────────────────────────────────

    if operation == "delete_instance":
        if not wait:
            return _accepted(instance_api().delete())
        instance().delete(wait=True)
        return {"deleted": True}

    if operation == "delete_snapshot":
        snapshot_name = _required(params, "snapshot_name", "Snapshot name")
        if not wait:
            return _accepted(instance_api().snapshots[snapshot_name].delete())
        instance().snapshots.get(snapshot_name).delete(wait=True)
        return {"deleted": True, "snapshot": snapshot_name}

    if operation == "create_network":
        name = _required(params, "name", "Network name")
        if not wait:
            return _accepted(
                client.api.networks.post(
                    json={
                        "name": name,
                        "description": params.get("description", ""),
                        "type": params.get("type"),
                        "config": params.get("config") or {},
                    }
                )
            )
        client.networks.create(
            name,
            description=params.get("description"),
            type=params.get("type"),
            config=params.get("config") or {},
            wait=True,
        )
        return {"name": name, "created": True}

    if operation == "delete_network":
        name = _required(params, "name", "Network name")
        client.networks.get(name).delete()
        return {"deleted": True, "name": name}

    if operation == "create_storage_pool":
        definition = params.get("definition") or params
        _required(definition, "name", "Storage pool name")
        _required(definition, "driver", "Storage pool driver")
        client.storage_pools.create(definition, wait=True)
        return {"name": definition["name"], "created": True}

    if operation == "delete_storage_pool":
        name = _required(params, "name", "Storage pool name")
        client.storage_pools.get(name).delete()
        return {"deleted": True, "name": name}

    if operation == "create_project":
        name = _required(params, "name", "Project name")
        client.projects.create(
            name,
            description=params.get("description"),
            config=params.get("config") or {},
        )
        return {"name": name, "created": True}

    if operation == "delete_project":
        name = _required(params, "name", "Project name")
        client.projects.get(name).delete()
        return {"deleted": True, "name": name}

    # Unreachable while the contract test passes: an unknown name is refused by the router
    # before any client is built. Kept as the backstop rather than falling off the end.
    raise HTTPException(status_code=400, detail=f"Unknown operation: {operation}")


def fetch_operation_status(
    lxd_service: Any, cluster_id: str, operation_id: str, project: str = "default"
) -> Dict[str, Any]:
    """Blocking half of the operations endpoint. Only ever runs on ``proxy_executor``."""
    # important: reuse _client_for rather than falling back to the pooled client when scoping
    # fails. The fallback answered from the DEFAULT project for a request authorised against a
    # different one, so an operation id from `default` could be read by a key granted only
    # `tenant-a`, including the resource URLs it names. _client_for raises 503 instead.
    client = _client_for(lxd_service, cluster_id, project, "get_operation")
    return _serialize_operation(client.operations.get(operation_id))
