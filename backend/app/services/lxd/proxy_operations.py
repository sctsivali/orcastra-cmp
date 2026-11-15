"""The operation vocabulary the external ``/integrations/proxy`` endpoint speaks.

It sits beside the proxy runtime rather than in the router so the two halves of the contract
stay in step: what an operation costs in permission (``PROXY_OPERATION_LEVELS``) and the code
that performs it (``execute_proxy_operation``). An operation priced but never dispatched would
pass authorization and then answer "Unknown operation";
``tests/test_the_proxy_operation_contract_is_honest.py`` fails if the two drift apart.

important: destruction costs ``admin``. The raw proxy requires ``admin`` for every DELETE, and the
same act arriving through a different door must not cost less.
"""

import logging
from typing import Any, Dict, FrozenSet, Optional, Tuple

from fastapi import HTTPException

from .availability import require_client
from .proxy_path_params import assert_path_params
from .guest_exec import exec_instance
from .guest_files import get_file, put_file
from .instance_view import instance_detail, instance_summary, status_name
from .operation_metadata import sanitize_operation_metadata
from .operation_ownership import visible_to
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
from app.core.error_sanitizer import sanitize_error
from app.core.lxd_segment import lxd_segment
from app.core.tenant_visibility import references_in_projects

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
    "LIST_MUTATIONS",
    "OperationNotVisible",
    "fetch_operation_status",
    # Re-exported so the router keeps one import site for the whole proxy vocabulary.
    "instance_detail",
    "instance_summary",
    "status_name",
]


# The complete contract. A name absent from this table is refused with 400 before any work is
# done. The contract test named in the module docstring asserts that this table and the dispatch
# branches below name the same operations.
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
    # Reading a path inside a guest. Priced `read`, which is what it does to the cluster (the
    # contract test also refuses a `get_` name that costs more). The instance.files capability is
    # what bounds it, and no preset seeds that.
    "get_file": "read",
    # Seeing and driving a guest's graphical session. Four operations, because
    # GUEST_ACCESS_OPERATIONS maps each operation to exactly one capability: a single
    # `desktop_action` priced by its arguments would be gated at one capability, and a caller
    # granted only desktop.view could ask it to type.
    "desktop_probe": "read",
    "desktop_view": "read",
    "desktop_clipboard": "read",
    # Reaching inside a guest. Priced `write` because it changes the instance's state; what bounds
    # it is the explicit capability GUEST_ACCESS_OPERATIONS demands on top, which no
    # application-type preset seeds and no legacy key inherits.
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
# the per-cluster level.
GUEST_ACCESS_OPERATIONS: Dict[str, str] = {
    "exec_instance": "instance.exec",
    # Separate from instance.exec: a health check that runs a command has no reason to read
    # files, and a backup that reads files has no reason to run anything.
    "get_file": "instance.files",
    "put_file": "instance.files",
    # `desktop.input` is a larger power than instance.exec: exec runs in a fresh non-interactive
    # context, while typing into a logged-in session inherits its authenticated browser, unlocked
    # keyring, live ssh-agent and validated sudo timestamp.
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
# important: this endpoint answers 200 with success=false for a failure inside the cluster, the
# contract existing OrcaHub integrations depend on. The code lets an unattended caller tell "not
# found" (give up) from "cluster down" (retry later) without parsing prose; a caller that ignores
# it sees no change.
PROXY_ERROR_CODES = (
    "not_found",
    "conflict",
    "forbidden",
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


def classify_lxd_status(status_code: int) -> str:
    """The PROXY_ERROR_CODES name for a status LXD itself answered (raw proxy)."""
    if status_code == 404:
        return "not_found"
    if status_code == 409:
        return "conflict"
    if status_code in (401, 403):
        return "forbidden"
    if status_code in (400, 422):
        return "invalid_request"
    if status_code in (502, 503, 504):
        return "cluster_unavailable"
    return "operation_failed"


# ── the dispatcher ─────────────────────────────────────────────────────────────────────────


def _accepted(response: Any) -> Dict[str, Any]:
    """Turn LXD's asynchronous response into something a caller can poll.

    pylxd's high-level methods discard the operation when ``wait`` is false:
    ``Instance._set_state`` and ``Model.delete`` return None, and ``Instance.create`` returns an
    object built from the request. The non-waiting path therefore goes through ``client.api``,
    where the operation id is still visible.
    """
    body = response.json()
    operation_url = body.get("operation") or ""
    operation_id = operation_url.rsplit("/", 1)[-1] if operation_url else None
    if not operation_id:
        # LXD answers some of these synchronously (pylxd's _APINode accepts 200 as success),
        # with no operation to poll. Report the work as done rather than accepted with a null id.
        return {
            "accepted": False,
            "operation_id": None,
            "status": status_name(body.get("status_code"), body.get("status") or "Success"),
        }
    return {"accepted": True, "operation_id": operation_id, "status": "Running"}


# Reads that are a plain projection of a pylxd manager: (manager attribute, fields).
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


class OperationNotVisible(LookupError):
    """An operation this caller may not see, answered exactly like one LXD has already reaped.

    `status_code` makes `classify_proxy_error` read it as `not_found`, and the operations route
    turns that into the same 404 it gives a reaped operation. An id must not be an ownership
    oracle.
    """

    status_code = 404

    def __init__(self) -> None:
        super().__init__("Operation not found")


def _serialize_operation(
    operation: Any, visible_projects: Optional[FrozenSet[str]] = frozenset()
) -> Dict[str, Any]:
    """One operation as a key may see it.

    `visible_projects` is None for a key granted every project (`*`), otherwise the projects it
    holds on this cluster. The default sees nothing, so a caller that forgets it fails closed.

    An operation id is cluster-wide: LXD answers GET /1.0/operations/{id} whatever ?project=
    says, so without this check a key granted only `default` could read another tenant's create,
    start, snapshot and delete operations and the resource URLs they name. Ownership is read off
    the resources, the rule the dashboard's own operations routes apply; anything else is not found.
    """
    if not visible_to(operation, visible_projects):
        raise OperationNotVisible()
    payload = {name: _attr(operation, name) for name in _OPERATION_FIELDS}
    # important: the dashboard's own allowlist. An exec or console operation holds its websocket
    # secrets in `metadata.fds`, and a read key can poll any operation id in its projects; raw
    # metadata would let it attach to another caller's session.
    payload["metadata"] = sanitize_operation_metadata(getattr(operation, "metadata", None))
    # Daemon text, which can name host paths and image-server URLs. Same treatment as the
    # dashboard's operation answers.
    description = getattr(operation, "description", None)
    payload["description"] = sanitize_error(str(description), limit=500) if description else description
    error = getattr(operation, "err", None)
    payload["error"] = sanitize_error(str(error), limit=500) if error else None
    return payload


# Which pool and which budget an operation runs under: desktop, other guest access, the rest.
#
# important: desktop actions must not run on `exec_executor`. It has four workers, and a
# screenshot loop would fill them and take every integration's exec_instance down with it
# (services/desktop/runtime.py).
#
# Budgets differ within the desktop tier too: a click that takes fifteen seconds means the guest
# is wedged, while a capture legitimately takes longer because it encodes and ships megabytes.
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
    # important: guest access needs a scoped client too. Exec is not in PROXY_WAIT_OPERATIONS, and
    # on the pooled client's 8s read window a command slower than that would fail with a
    # misleading timeout well inside its EXEC_TIMEOUT budget.
    needs_scope = (
        (project and project != "default")
        or operation in PROXY_WAIT_OPERATIONS
        or operation in GUEST_ACCESS_OPERATIONS
    )
    if not needs_scope:
        return client
    # important: the socket read window has to outlast the response budget. With a shorter
    # window, a create that pulls an image hits the socket timeout while LXD keeps building: the
    # caller is told "timed out" by a path that cannot know the work survived, and the instance
    # is left behind. The window comes from operation_timeout, the selector the router spends,
    # so the budget is always the binding limit and the timeout arrives on the path that says so.
    #
    # The cost is worker occupancy. BlockingTimeout frees the caller, not the thread
    # (core/blocking.py), and a waiting create is three sequential requests (POST, operation GET,
    # /wait), each with a fresh window: about 195s worst case on one of PROXY_POOL_WORKERS threads.
    # Raise ORCASTRA_PROXY_POOL_WORKERS if slow creates start shedding reads, or have
    # integrations send `"wait": false`, which returns in about a second and holds nothing.
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


# What each operation cannot run without. Checked before a client is built: `_client_for`
# revalidates the pooled connection and may rebuild it, which a malformed request should not
# pay for. The branches still call `_required`; the contract test asserts the two agree.
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
    # guest_files validates `path` fully; these rows are the up-front presence check, so a
    # request missing a path is refused before it costs a connection.
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


# Operations that change what a dashboard list would return: every write and admin operation
# except guest access. The router acts on it, because cache invalidation belongs on the event
# loop and this module runs on a worker. Derived, so every new write is included.
LIST_MUTATIONS: FrozenSet[str] = frozenset(
    operation
    for operation, level in PROXY_OPERATION_LEVELS.items()
    if level != "read" and operation not in GUEST_ACCESS_OPERATIONS
)


def execute_proxy_operation(
    lxd_service: Any,
    cluster_id: str,
    operation: str,
    params: Dict[str, Any],
    project: str,
    wait: bool = True,
    *,
    visible_projects: Optional[FrozenSet[str]] = frozenset(),
) -> Any:
    """Blocking half of ``proxy_operation``. Runs on the pool ``operation_pool`` picks.

    pylxd binds the project at the client level: ``Instance.all/get/create``, ``Project.all``
    and ``Image.all`` take no ``project=`` keyword and raise TypeError on one. Project scoping
    goes through ``get_project_client``, via ``_client_for``.
    """
    assert_required_params(operation, params)
    assert_path_params(operation, params)
    client = _client_for(lxd_service, cluster_id, project, operation)

    def instance() -> Any:
        return client.instances.get(lxd_segment(_required(params, "name", "Instance name"), what="Instance name"))

    def instance_api() -> Any:
        return client.api.instances[lxd_segment(_required(params, "name", "Instance name"), what="Instance name")]

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
        rows = [
            {name: _attr(item, name) for name in fields}
            for item in getattr(client, manager).all()
        ]
        if visible_projects is not None:
            # important: a network's or pool's used_by is cluster-wide; unfiltered, a key granted
            # some projects would see every other project's instances and volumes on a shared
            # cluster. Keep only the references inside its grant, as operation reads do.
            for row in rows:
                if "used_by" in row:
                    row["used_by"] = references_in_projects(row["used_by"] or [], visible_projects)
        return rows

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
            client.operations.get(
                lxd_segment(_required(params, "operation_id", "Operation id"), what="Operation id")
            ),
            visible_projects,
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

    # Operation names are spelled out as string constants in each branch condition, not checked
    # against a module-level set: the contract test
    # `test_the_price_list_and_the_dispatcher_name_the_same_operations` reads them off the
    # conditions and cannot see a set membership test.
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
        # Validated before the instance lookup, so a request missing snapshot_name is told
        # about snapshot_name rather than whatever the lookup reports.
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
        # important: pylxd puts a name into the URL as it is: `x?project=other&` would delete a
        # snapshot in a project this key was never granted, and requests folds a `..` name out of
        # the path, leaving the instance's own URL. LXD accepts `?`, `&` and `#` in a snapshot
        # name, so create_snapshot can make one. Both wait modes send the one encoded segment;
        # pylxd's Snapshot model would rebuild the URL from the raw name LXD returns.
        snapshot_segment = lxd_segment(snapshot_name, what="Snapshot name")
        response = instance_api().snapshots[snapshot_segment].delete()
        if not wait:
            return _accepted(response)
        if response.json().get("type") == "async":
            client.operations.wait_for_operation(response.json()["operation"])
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
        client.networks.get(lxd_segment(name, what="Network name")).delete()
        return {"deleted": True, "name": name}

    if operation == "create_storage_pool":
        definition = params.get("definition") or params
        _required(definition, "name", "Storage pool name")
        _required(definition, "driver", "Storage pool driver")
        client.storage_pools.create(definition, wait=True)
        return {"name": definition["name"], "created": True}

    if operation == "delete_storage_pool":
        name = _required(params, "name", "Storage pool name")
        client.storage_pools.get(lxd_segment(name, what="Storage pool name")).delete()
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
        # The /proxy route sends delete_project to services/project_deletion (cluster lock,
        # migration fence, grant revoke, bounded outcome). This branch is the parameter contract
        # and the backstop for any other caller of the dispatcher.
        name = _required(params, "name", "Project name")
        client.projects.get(lxd_segment(name, what="Project name")).delete()
        return {"deleted": True, "name": name}

    # Unreachable while the contract test passes: an unknown name is refused by the router
    # before any client is built. Kept as the backstop rather than falling off the end.
    raise HTTPException(status_code=400, detail=f"Unknown operation: {operation}")


def fetch_operation_status(
    lxd_service: Any,
    cluster_id: str,
    operation_id: str,
    project: str = "default",
    *,
    visible_projects: Optional[FrozenSet[str]] = frozenset(),
) -> Dict[str, Any]:
    """Blocking half of the operations endpoint. Only ever runs on ``proxy_executor``."""
    # important: _client_for raises 503 when scoping fails. Falling back to the pooled client
    # would answer from the `default` project for a request authorised against another, so a key
    # granted only `tenant-a` could read a `default` operation and the resource URLs it names.
    assert_path_params("get_operation", {"operation_id": operation_id})
    client = _client_for(lxd_service, cluster_id, project, "get_operation")
    return _serialize_operation(
        client.operations.get(lxd_segment(operation_id, what="Operation id")), visible_projects
    )
