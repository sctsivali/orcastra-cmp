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
from typing import Any, Dict, FrozenSet, Tuple

from fastapi import HTTPException

from .availability import require_client
from .guest_exec import exec_instance
from .guest_files import get_file, put_file
from .instance_view import instance_detail, instance_summary, status_name
from ..desktop.entry import desktop_action, desktop_probe
from ..desktop.runtime import (
    DESKTOP_ACTION_TIMEOUT,
    DESKTOP_MAX_IN_FLIGHT,
    DESKTOP_PROBE_TIMEOUT,
    DESKTOP_SCREENSHOT_TIMEOUT,
    desktop_executor,
)
from . import snapshots as snapshots_service
from .proxy import (
    EXEC_MAX_IN_FLIGHT,
    EXEC_TIMEOUT,
    PROXY_MAX_IN_FLIGHT,
    PROXY_OP_TIMEOUT,
    exec_executor,
    proxy_executor,
)
from app.core.lxd_path import safe_segment

logger = logging.getLogger(__name__)

__all__ = [
    "CLUSTER_WIDE_OPERATIONS",
    "operation_pool",
    "operation_timeout",
    "GUEST_ACCESS_OPERATIONS",
    "PROXY_ERROR_CODES",
    "PROXY_OPERATION_LEVELS",
    "PROXY_WAIT_OPERATIONS",
    "assert_required_params",
    "execute_proxy_operation",
    "SNAPSHOT_MUTATIONS",
    "fetch_operation_status",
    # Re-exported so the router keeps one import site for the whole proxy vocabulary.
    "instance_detail",
    "instance_summary",
    "status_name",
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
    # Reading a path inside a guest. Priced `read` because that is what it does to the cluster,
    # and because the contract test refuses to let anything named `get_` cost more. The level is
    # not what bounds it: instance.files is, and no preset seeds that.
    "get_file": "read",
    # Seeing and driving a guest's graphical session. Four operations rather than one, because
    # GUEST_ACCESS_OPERATIONS below maps an operation to exactly ONE capability: a single
    # `desktop_action` whose real cost varied with its arguments would be gated at whatever that
    # map named, and a caller granted only desktop.view could ask it to type.
    "desktop_probe": "read",
    "desktop_view": "read",
    "desktop_clipboard": "read",
    # Reaching inside a guest. Priced `write` because it changes the instance's state, but the
    # level is not what bounds it: GUEST_ACCESS_OPERATIONS below demands an explicit capability
    # on top, which no application-type preset seeds and no legacy key inherits.
    "exec_instance": "write",
    "put_file": "write",
    "desktop_input": "write",
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

# Operations that reach inside a guest, and the capability each costs on top of `lxd.proxy` and
# the per-cluster level. Kept beside the price list because it is the same kind of fact: what an
# operation costs before it may run.
GUEST_ACCESS_OPERATIONS: Dict[str, str] = {
    "exec_instance": "instance.exec",
    # Separate from instance.exec on purpose, and the vocabulary already says why: holding one
    # does not imply the other. A health check that runs a command has no reason to read files,
    # and a backup that reads files has no reason to run anything.
    "get_file": "instance.files",
    "put_file": "instance.files",
    # The desktop family. `desktop.input` is a LARGER power than instance.exec rather than a
    # sibling of it: exec runs in a fresh non-interactive context, while typing into a logged-in
    # session inherits that session's authenticated browser, unlocked keyring, live ssh-agent and
    # already-validated sudo timestamp.
    "desktop_probe": "desktop.view",
    "desktop_view": "desktop.view",
    "desktop_input": "desktop.input",
    "desktop_clipboard": "desktop.clipboard",
}

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


# Which pool and which budget an operation runs under.
#
# important: three tiers, not two. Selecting on GUEST_ACCESS_OPERATIONS alone put every desktop
# action on `exec_executor`, which has four workers, and `services/desktop/runtime.py` argues at
# length that this must not happen: "a screenshot loop at even a modest rate would fill all four
# and take the ordinary exec_instance of every other integration down with it". The desktop pool
# was built and never used, so that starvation was reachable. It is now the pool desktop runs on.
#
# The budgets differ within the desktop tier too. A click that takes fifteen seconds means the
# guest is wedged; a capture legitimately needs longer because it encodes and ships megabytes.
_DESKTOP_BUDGETS: Dict[str, float] = {
    "desktop_probe": DESKTOP_PROBE_TIMEOUT,
    "desktop_view": DESKTOP_SCREENSHOT_TIMEOUT,
    "desktop_input": DESKTOP_ACTION_TIMEOUT,
    "desktop_clipboard": DESKTOP_ACTION_TIMEOUT,
}


def operation_timeout(operation: str) -> float:
    """The response budget for one operation, whichever tier it belongs to."""
    if operation in _DESKTOP_BUDGETS:
        return _DESKTOP_BUDGETS[operation]
    if operation in GUEST_ACCESS_OPERATIONS:
        return EXEC_TIMEOUT
    return PROXY_OP_TIMEOUT


def operation_pool(operation: str) -> Tuple[Any, int]:
    """The executor and in-flight cap for one operation."""
    if operation in _DESKTOP_BUDGETS:
        return desktop_executor, DESKTOP_MAX_IN_FLIGHT
    if operation in GUEST_ACCESS_OPERATIONS:
        return exec_executor, EXEC_MAX_IN_FLIGHT
    return proxy_executor, PROXY_MAX_IN_FLIGHT


def _client_for(lxd_service: Any, cluster_id: str, project: str, operation: str) -> Any:
    """The pooled client, replaced by a project-scoped one where the work needs it.

    The pooled client comes first regardless: it is what populates the cached connection info
    and temp certificate paths that ``get_project_client`` reuses, and it is what the
    reconnect logic maintains.
    """
    client = require_client(
        lxd_service.connection_manager.get_client(cluster_id), cluster_id
    )
    # important: exec belongs here too, and leaving it out was a real defect rather than a
    # tidiness one. It is not in PROXY_WAIT_OPERATIONS, so it took the pooled client's 8s read
    # window while being budgeted at EXEC_TIMEOUT. Measured live: `sleep 10` failed after 10.5s
    # with a message claiming it had exceeded a 30s budget it never reached. Any legitimate
    # command slower than the socket window was refused, and told the wrong reason.
    needs_scope = (
        (project and project != "default")
        or operation in PROXY_WAIT_OPERATIONS
        or operation in GUEST_ACCESS_OPERATIONS
    )
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
    # The window is derived from whichever budget bounds this call, so the budget is always the
    # binding limit and the timeout arrives on the path that can explain it.
    # Derived from the same selector the router spends, so the window cannot be sized for a
    # budget the call is not actually given.
    budget = operation_timeout(operation)
    scoped = lxd_service.connection_manager.get_project_client(
        cluster_id, project or "default", timeout=(3, int(budget) + 5)
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
    "exec_instance": (("name", "Instance name"), ("command", "Command")),
    # note: the branches validate `path` themselves in guest_files, which does more than a
    # presence check. The rows are still here because this table is the up-front gate: without
    # them a request missing a path pays for a connection before being told about its own body.
    "get_file": (("name", "Instance name"), ("path", "File path")),
    "put_file": (("name", "Instance name"), ("path", "File path")),
    "desktop_probe": (("name", "Instance name"),),
    "desktop_view": (("name", "Instance name"), ("action", "Desktop action")),
    "desktop_input": (("name", "Instance name"), ("action", "Desktop action")),
    "desktop_clipboard": (("name", "Instance name"), ("action", "Desktop action")),
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


# Operations that change what the fleet snapshot listing would return. Named here, beside the rest
# of the operation vocabulary, and acted on by the router: this module runs on a worker and the
# cache invalidation belongs on the loop with every other one.
SNAPSHOT_MUTATIONS = frozenset(
    {"create_snapshot", "restore_snapshot", "delete_snapshot", "delete_instance"}
)


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
        return client.instances.get(safe_segment(_required(params, "name", "Instance name")))

    def instance_api() -> Any:
        return client.api.instances[safe_segment(_required(params, "name", "Instance name"))]

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
            # recursion=1: every field read above is lazily synced otherwise.
            for i in client.instances.all(recursion=1)
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
            client.operations.get(safe_segment(_required(params, "operation_id", "Operation id")))
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

    if operation == "exec_instance":
        return exec_instance(instance(), params)

    if operation == "get_file":
        return get_file(instance(), params)

    if operation == "put_file":
        return put_file(instance(), params)

    # note: the operation names are spelled out in the tests below rather than checked against a
    # module-level set. `test_the_price_list_and_the_dispatcher_name_the_same_operations` reads
    # them off the string constants in each branch's condition, so a set membership test is
    # invisible to it and the operation reads as priced-but-unimplemented.
    #
    # The target triple is not caller-supplied on this path: cluster and project come from the
    # request the router already authorized, so a params field cannot redirect the action at a
    # different guest than the one the permission check was made against.
    if operation == "desktop_probe":
        return desktop_probe(instance(), {**params, "cluster_id": cluster_id, "project": project})

    if operation in ("desktop_view", "desktop_input", "desktop_clipboard"):
        return desktop_action(
            instance(), {**params, "cluster_id": cluster_id, "project": project}, operation
        )

    if operation == "create_snapshot":
        # note: validate before the lookup. Reversed, a request missing snapshot_name spent a
        # round trip to the cluster and came back reporting the instance, which points a caller
        # at the wrong thing to fix.
        snapshot_name = _required(params, "snapshot_name", "Snapshot name")
        stateful = bool(params.get("stateful", False))
        inst = instance()
        if not wait:
            return _accepted(
                snapshots_service.create_instance_snapshot_request(
                    instance_api(), snapshot_name, stateful=stateful
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
        client.networks.get(safe_segment(name)).delete()
        return {"deleted": True, "name": name}

    if operation == "create_storage_pool":
        definition = params.get("definition") or params
        _required(definition, "name", "Storage pool name")
        _required(definition, "driver", "Storage pool driver")
        client.storage_pools.create(definition, wait=True)
        return {"name": definition["name"], "created": True}

    if operation == "delete_storage_pool":
        name = _required(params, "name", "Storage pool name")
        client.storage_pools.get(safe_segment(name)).delete()
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
        client.projects.get(safe_segment(name)).delete()
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
    return _serialize_operation(client.operations.get(safe_segment(operation_id)))
