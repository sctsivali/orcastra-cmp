"""
No API-key surface hands out the secrets an LXD operation carries.

An exec or console operation holds `fds`, whose values open the operation websocket with no further
authentication. A token operation holds `secret`, and a migration source holds `control`, `fs` and
`criu`. The dashboard's /operations routes answer through an allowlist, but four surfaces a key
reaches returned LXD's metadata verbatim:

- the `get_operation` proxy operation (POST /integrations/proxy), which costs `read`
- GET /integrations/operations/{id}, which costs `inventory.read`
- MCP `orcastra_get_operation`, which runs that same handler
- the raw proxy's GET /1.0/operations, /1.0/operations/<id> and its /wait

So a read key could collect the websocket secrets of every live console and exec in its projects.
The first three now serialize through the dashboard's allowlist, one function and not a copy. The
raw proxy passes LXD's own JSON through, so it removes the secret fields and leaves the rest as LXD
wrote it, which is what the integrator's operation polls read.

Each surface is driven through its real handler down to a fake cluster that records the lookup the
real client makes.
"""

import json
from types import SimpleNamespace
from typing import Any, Callable, Dict, List

import pytest
import requests

from app.core.raw_proxy_path import canonical_proxy_path
from app.mcp import auth as mcp_auth
from app.mcp import tools_inventory
from app.routers import integrations, lxd_operations
from app.routers.integrations import ProxyRequest
from app.services.lxd import operation_metadata, proxy_operations
from app.services.lxd.raw_proxy_operations import LXD_OPERATION_NOT_FOUND, addressed_operation_id
from app.services.lxd.operation_metadata import (
    EXEC_REQUEST_KEYS,
    OPERATION_SECRET_KEYS,
    redact_operation_response,
    sanitize_operation_metadata,
)
from tests._raw_proxy_fakes import (  # noqa: F401 - the fixture is registered by import
    CLUSTER_ID,
    RecordingSession,
    _clean_breaker,
    call_raw_proxy,
)

OP_ID = "5b1e0a4c-0000-4000-8000-000000000001"

READ_KEY: Dict[str, Any] = {
    "id": "oak_read",
    "app_name": "qa-read",
    "capabilities": ["inventory.read", "lxd.proxy"],
    "cluster_permissions": [
        {"cluster_id": CLUSTER_ID, "permissions": ["read"], "projects": ["default"]}
    ],
}

# note: `all-projects` costs a grant on every project, so the listing forms read with this one.
READ_EVERY_PROJECT: Dict[str, Any] = {
    **READ_KEY,
    "cluster_permissions": [{"cluster_id": CLUSTER_ID, "permissions": ["read"], "projects": ["*"]}],
}

SECRET_VALUES = (
    "fd0-s3cr3t",
    "c0ntr0l-s3cr3t",
    "t0k3n-s3cr3t",
    "m1gr4t10n-c0ntr0l",
    "m1gr4t10n-fs",
    "m1gr4t10n-cr1u",
)

# What LXD answers for each operation class that carries a secret, as GET /1.0/operations/<id>
# returns it (pylxd.Operation sets one attribute per key).
OPERATIONS: Dict[str, Dict[str, Any]] = {
    "exec": {
        "id": OP_ID,
        "class": "websocket",
        "description": "Executing command",
        "created_at": "2026-09-25T10:00:00Z",
        "updated_at": "2026-09-25T10:00:01Z",
        "status": "Running",
        "status_code": 103,
        "resources": {"instances": ["/1.0/instances/web-1"]},
        "metadata": {
            "command": ["bash"],
            "environment": {"TERM": "xterm-256color"},
            "interactive": True,
            "fds": {"0": "fd0-s3cr3t", "control": "c0ntr0l-s3cr3t"},
        },
        "may_cancel": False,
        "err": "",
        "location": "node-1",
    },
    "token": {
        "id": OP_ID,
        "class": "token",
        "description": "Executing operation",
        "created_at": "2026-09-25T10:00:00Z",
        "updated_at": "2026-09-25T10:00:00Z",
        "status": "Running",
        "status_code": 103,
        "resources": {},
        "metadata": {
            "serverName": "node-4",
            "secret": "t0k3n-s3cr3t",
            "fingerprint": "7c2f0a",
            "addresses": ["10.0.0.1:8443"],
            "expiresAt": "2026-09-25T13:00:00Z",
        },
        "may_cancel": True,
        "err": "",
        "location": "node-1",
    },
    "migration": {
        "id": OP_ID,
        "class": "websocket",
        "description": "Migrating instance",
        "created_at": "2026-09-25T10:00:00Z",
        "updated_at": "2026-09-25T10:00:02Z",
        "status": "Running",
        "status_code": 103,
        "resources": {"instances": ["/1.0/instances/web-1"]},
        "metadata": {
            "control": "m1gr4t10n-c0ntr0l",
            "fs": "m1gr4t10n-fs",
            "criu": "m1gr4t10n-cr1u",
            "migration_progress": "rootfs: 12%",
        },
        "may_cancel": True,
        "err": "",
        "location": "node-1",
    },
}

# The top-level fields every key surface returns, all of them non-secret.
_SERIALIZED = ("id", "status", "status_code", "description", "may_cancel", "created_at", "updated_at", "resources")


def _keys(value: Any) -> List[str]:
    if isinstance(value, dict):
        return [str(key) for key in value] + [key for item in value.values() for key in _keys(item)]
    if isinstance(value, list):
        return [key for item in value for key in _keys(item)]
    return []


def _no_secret_in(payload: Any) -> None:
    text = json.dumps(payload, default=str)
    for value in SECRET_VALUES:
        assert value not in text, value
    assert not {key.lower() for key in _keys(payload)} & OPERATION_SECRET_KEYS, text


class _Operations:
    """pylxd's operation manager. Records every id looked up, as the real GET does."""

    def __init__(self, operation: Dict[str, Any]) -> None:
        self.operation = operation
        self.lookups: List[str] = []

    def get(self, operation_id: str) -> Any:
        self.lookups.append(operation_id)
        found = SimpleNamespace()
        for key, value in json.loads(json.dumps(self.operation)).items():
            setattr(found, key, value)
        return found


def _service(operation: Dict[str, Any]) -> SimpleNamespace:
    operations = _Operations(operation)
    client = SimpleNamespace(operations=operations)
    manager = SimpleNamespace(get_client=lambda cluster_id: client)
    return SimpleNamespace(connection_manager=manager, operations=operations)


async def _via_proxy_operation(service: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    response = await integrations.proxy_operation(
        request=ProxyRequest(
            cluster_id=CLUSTER_ID, operation="get_operation", params={"operation_id": OP_ID}
        ),
        api_key=READ_KEY,
        lxd_service=service,
        _=None,
    )
    assert response.success is True, response
    return response.data


async def _via_operations_route(service: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    return await integrations.get_integration_operation(
        operation_id=OP_ID,
        cluster_id=CLUSTER_ID,
        project="default",
        api_key=READ_KEY,
        lxd_service=service,
    )


class _Registry:
    """The part of MCPServer that tools_inventory.register uses."""

    def __init__(self) -> None:
        self.tools: Dict[str, Callable[..., Any]] = {}

    def tool(self, name: str, **_: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(function: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = function
            return function

        return register


async def _via_mcp(service: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    registry = _Registry()
    tools_inventory.register(registry)
    monkeypatch.setattr(tools_inventory, "get_lxd_service", lambda: service)
    token = mcp_auth._CURRENT_KEY.set(READ_KEY)
    try:
        result = await registry.tools["orcastra_get_operation"](operation_id=OP_ID, cluster_id=CLUSTER_ID)
    finally:
        mcp_auth._CURRENT_KEY.reset(token)
    assert result.is_error is False, result.content
    _no_secret_in([json.loads(block.text) for block in result.content])
    return result.structured_content


async def _via_mcp_proxy_tool(service: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """`lxd_get_operation`, the poll a key without inventory.read uses, through call_proxy."""
    from app.mcp import dispatch as mcp_dispatch
    from app.mcp import tools_read

    registry = _Registry()
    tools_read.register(registry)
    monkeypatch.setattr(mcp_dispatch, "get_lxd_service", lambda: service)
    token = mcp_auth._CURRENT_KEY.set(READ_KEY)
    try:
        result = await registry.tools["lxd_get_operation"](cluster_id=CLUSTER_ID, operation_id=OP_ID)
    finally:
        mcp_auth._CURRENT_KEY.reset(token)
    assert result.is_error is False, result.content
    _no_secret_in([json.loads(block.text) for block in result.content])
    return result.structured_content["data"]


SERIALIZING_SURFACES = {
    "proxy_get_operation": _via_proxy_operation,
    "integrations_operations_route": _via_operations_route,
    "mcp_orcastra_get_operation": _via_mcp,
    "mcp_lxd_get_operation": _via_mcp_proxy_tool,
}


# Operations whose resources name one project. The token operation names none, so no key surface
# may hand it out at all (see the ownership tests below).
OWNED = sorted(kind for kind, operation in OPERATIONS.items() if operation["resources"])


@pytest.mark.parametrize("kind", OWNED)
@pytest.mark.parametrize("surface", sorted(SERIALIZING_SURFACES))
async def test_a_serializing_key_surface_returns_no_secret(
    surface: str, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = OPERATIONS[kind]
    service = _service(operation)

    payload = await SERIALIZING_SURFACES[surface](service, monkeypatch)

    assert service.operations.lookups == [OP_ID]
    _no_secret_in(payload)
    for field in _SERIALIZED:
        assert payload[field] == operation[field], field
    assert payload["error"] is None
    assert payload["metadata"] == sanitize_operation_metadata(operation["metadata"])


async def test_the_allowlisted_progress_is_returned_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """The allowlist keeps what a poller of a key's own `wait=false` create reads beside status."""
    operation = dict(OPERATIONS["migration"])
    service = _service(operation)

    payload = await _via_operations_route(service, monkeypatch)

    assert payload["metadata"] == {"migration_progress": "rootfs: 12%"}


# ── ownership: an operation id is cluster-wide ─────────────────────────────────────────────────
#
# LXD answers GET /1.0/operations/{id} whatever ?project= says. Observed on nuc2 on 2026-10-14: a
# key granted only `default` read the create, start, snapshot and delete operations of another
# project, with resource URLs naming that project's instances.


async def _refused_everywhere(operation: Dict[str, Any], api_key: Dict[str, Any],
                              monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from app.mcp import dispatch as mcp_dispatch
    from app.mcp import tools_read

    service = _service(operation)
    response = await integrations.proxy_operation(
        request=ProxyRequest(cluster_id=CLUSTER_ID, operation="get_operation", params={"operation_id": OP_ID}),
        api_key=api_key, lxd_service=service, _=None,
    )
    assert (response.success, response.error_code) == (False, "not_found"), response
    with pytest.raises(HTTPException) as refused:
        await integrations.get_integration_operation(
            operation_id=OP_ID, cluster_id=CLUSTER_ID, project="default", api_key=api_key, lxd_service=service,
        )
    assert refused.value.status_code == 404 and str(refused.value.detail).startswith(f"No operation {OP_ID}")

    registry = _Registry()
    tools_inventory.register(registry)
    tools_read.register(registry)
    monkeypatch.setattr(tools_inventory, "get_lxd_service", lambda: service)
    monkeypatch.setattr(mcp_dispatch, "get_lxd_service", lambda: service)
    token = mcp_auth._CURRENT_KEY.set(api_key)
    try:
        for tool in ("orcastra_get_operation", "lxd_get_operation"):
            result = await registry.tools[tool](operation_id=OP_ID, cluster_id=CLUSTER_ID)
            text = " ".join(block.text for block in result.content)
            assert result.is_error and text.startswith("[not_found]"), (tool, text)
            assert "web-1" not in text and "tenant-b" not in text
    finally:
        mcp_auth._CURRENT_KEY.reset(token)


async def test_an_ownerless_operation_is_not_found_on_every_key_surface(monkeypatch) -> None:
    await _refused_everywhere(OPERATIONS["token"], READ_KEY, monkeypatch)


async def test_another_projects_operation_reads_as_a_reaped_one(monkeypatch) -> None:
    elsewhere = {**OPERATIONS["exec"], "resources": {"instances": ["/1.0/instances/web-1?project=tenant-b"]}}
    await _refused_everywhere(elsewhere, READ_KEY, monkeypatch)


async def test_a_wildcard_key_sees_any_owned_operation_but_not_an_ownerless_one(monkeypatch) -> None:
    elsewhere = {**OPERATIONS["exec"], "resources": {"instances": ["/1.0/instances/web-1?project=tenant-b"]}}
    payload = await integrations.get_integration_operation(
        operation_id=OP_ID, cluster_id=CLUSTER_ID, project="default",
        api_key=READ_EVERY_PROJECT, lxd_service=_service(elsewhere),
    )
    assert payload["resources"] == elsewhere["resources"]
    await _refused_everywhere(OPERATIONS["token"], READ_EVERY_PROJECT, monkeypatch)


async def test_a_snapshot_operation_is_owned_as_lxd_names_it(monkeypatch) -> None:
    """LXD 5.21 writes `instances_snapshots`; the table only knew `instance-snapshots`."""
    snapshot = {**OPERATIONS["exec"], "description": "Snapshotting instance", "resources": {
        "instances": ["/1.0/instances/web-1"],
        "instances_snapshots": ["/1.0/instances/web-1/snapshots/s1"],
    }}
    payload = await integrations.get_integration_operation(
        operation_id=OP_ID, cluster_id=CLUSTER_ID, project="default", api_key=READ_KEY,
        lxd_service=_service(snapshot),
    )
    assert payload["description"] == "Snapshotting instance"


async def test_description_and_error_are_sanitized(monkeypatch) -> None:
    failed = {**OPERATIONS["exec"], "status": "Failure", "description": "Creating instance",
              "err": "Failed getting image from https://images.internal.example/streams: open "
                     "/var/snap/lxd/common/lxd/images/abc: no such file"}
    payload = await integrations.get_integration_operation(
        operation_id=OP_ID, cluster_id=CLUSTER_ID, project="default", api_key=READ_KEY,
        lxd_service=_service(failed),
    )
    assert payload["error"] and "images.internal.example" not in payload["error"]
    assert "/var/snap" not in payload["error"]


def test_every_surface_reads_one_ownership_function() -> None:
    from app.services.lxd import operation_ownership

    assert lxd_operations._operation_ownership is operation_ownership.operation_ownership
    assert proxy_operations.visible_to is operation_ownership.visible_to


def test_every_serializing_surface_reads_the_dashboards_allowlist() -> None:
    """One function, not a copy: the dashboard route and the key surfaces cannot drift apart."""
    assert lxd_operations._sanitize_operation_metadata is sanitize_operation_metadata
    assert proxy_operations.sanitize_operation_metadata is sanitize_operation_metadata
    assert operation_metadata.SAFE_OPERATION_METADATA_KEYS.isdisjoint(OPERATION_SECRET_KEYS)


# ── the raw proxy ──────────────────────────────────────────────────────────────────────────────


class _AnsweringSession(RecordingSession):
    """Records every call and answers with a fixed LXD body."""

    def __init__(self, body: Dict[str, Any], status: int = 200) -> None:
        super().__init__()
        self.body = body
        self.status = status

    def _answer(self, verb: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append((verb, url, kwargs))
        response = requests.Response()
        response.status_code = self.status
        response._content = json.dumps(self.body).encode()
        return response


def _sync(metadata: Any) -> Dict[str, Any]:
    return {"type": "sync", "status": "Success", "status_code": 200, "operation": "", "error_code": 0,
            "error": "", "metadata": metadata}


def _without_secrets(operation: Dict[str, Any]) -> Dict[str, Any]:
    """What a read key without instance.exec is shown: no secret and no exec request field."""
    expected = dict(operation)
    expected["metadata"] = {
        key: value
        for key, value in operation["metadata"].items()
        if key not in OPERATION_SECRET_KEYS | EXEC_REQUEST_KEYS
    }
    return expected


async def _raw_get(path: str, body: Dict[str, Any], api_key: Dict[str, Any] = READ_KEY) -> Any:
    session = _AnsweringSession(body)
    outcome, _ = await call_raw_proxy(api_key, "GET", path, session=session)
    # note: a project-limited key's call to one operation is preceded by the ownership read
    # (test_a_raw_operation_read_is_bounded_by_its_owner.py). Everything else is one call.
    limited = all("*" not in grant["projects"] for grant in api_key["cluster_permissions"])
    one_operation = addressed_operation_id(canonical_proxy_path(path)) is not None
    assert len(session.calls) == (2 if limited and one_operation else 1), outcome
    return outcome.data


ONE_OPERATION_PATHS = [
    f"/1.0/operations/{OP_ID}",
    f"/1.0/operations/{OP_ID}/wait",
    f"/1.0/operations/{OP_ID}/wait?timeout=30",
    f"/1.0/%6Fperations/{OP_ID}",
    f"/1.0/OPERATIONS/{OP_ID}/",
]


@pytest.mark.parametrize("kind", OWNED)
@pytest.mark.parametrize("path", ONE_OPERATION_PATHS)
async def test_a_raw_operation_read_returns_no_secret(path: str, kind: str) -> None:
    operation = OPERATIONS[kind]

    data = await _raw_get(path, _sync(operation))

    _no_secret_in(data)
    assert data == _sync(_without_secrets(operation))


@pytest.mark.parametrize("path", ONE_OPERATION_PATHS)
async def test_a_raw_read_of_an_ownerless_operation_is_not_found(path: str) -> None:
    """The token operation names no resource, so no project owns it and a limited key gets 404."""
    session = _AnsweringSession(_sync(OPERATIONS["token"]))
    outcome, _ = await call_raw_proxy(READ_KEY, "GET", path, session=session)
    assert outcome.status_code == 404 and outcome.data == LXD_OPERATION_NOT_FOUND
    assert len(session.calls) == 1
    _no_secret_in(outcome.data)


@pytest.mark.parametrize(
    "path",
    [
        "/1.0/operations?recursion=1",
        "/1.0/operations?recursion=1&all-projects=true",
        "/1.0/operations?all-projects=true&recursion=1",
        "/1.0/operations/?recursion=1",
    ],
)
async def test_a_raw_operation_listing_returns_no_secret(path: str) -> None:
    listing = {
        "running": [OPERATIONS["exec"], OPERATIONS["migration"]],
        "success": [OPERATIONS["token"]],
    }

    data = await _raw_get(path, _sync(listing), READ_EVERY_PROJECT)

    _no_secret_in(data)
    assert data == _sync({
        "running": [_without_secrets(OPERATIONS["exec"]), _without_secrets(OPERATIONS["migration"])],
        "success": [_without_secrets(OPERATIONS["token"])],
    })


async def test_a_listing_by_url_is_returned_as_lxd_wrote_it() -> None:
    listing = {"running": [f"/1.0/operations/{OP_ID}"], "success": []}

    assert await _raw_get("/1.0/operations", _sync(listing)) == _sync(listing)


# The operation polls the integrator makes, as its client source reads them: status, status_code,
# err and, for an image pull, metadata.download_progress. None carries a secret, so the filter
# must hand each back exactly as LXD wrote it.
INTEGRATOR_POLLS = [
    {
        "id": OP_ID, "class": "task", "description": "Downloading image", "status": "Running",
        "status_code": 103, "resources": {"images": ["/1.0/images/0f3c9a"]},
        "metadata": {"download_progress": "rootfs: 48% (12.50MB/s)"}, "may_cancel": False,
        "err": "", "location": "node-1", "created_at": "2026-09-25T10:00:00Z",
        "updated_at": "2026-09-25T10:00:03Z",
    },
    {
        "id": OP_ID, "class": "task", "description": "Creating instance", "status": "Failure",
        "status_code": 400, "resources": {"instances": ["/1.0/instances/web-1"]},
        "metadata": None, "may_cancel": False, "err": "Failed creating instance: secureboot",
        "location": "node-1", "created_at": "2026-09-25T10:00:00Z",
        "updated_at": "2026-09-25T10:00:03Z",
    },
    {
        "id": OP_ID, "class": "task", "description": "Executing command", "status": "Success",
        "status_code": 200, "resources": {"instances": ["/1.0/instances/web-1"]},
        "metadata": {"return": 0, "output": {"1": "/1.0/instances/web-1/logs/exec_1.stdout"}},
        "may_cancel": False, "err": "", "location": "node-1",
        "created_at": "2026-09-25T10:00:00Z", "updated_at": "2026-09-25T10:00:03Z",
    },
]


@pytest.mark.parametrize("operation", INTEGRATOR_POLLS, ids=["image-pull", "failed-create", "exec-result"])
@pytest.mark.parametrize(
    "path", [f"/1.0/operations/{OP_ID}", f"/1.0/operations/{OP_ID}/wait?timeout=55"]
)
async def test_an_integrator_poll_is_answered_as_lxd_wrote_it(path: str, operation: Dict[str, Any]) -> None:
    body = _sync(operation)

    data = await _raw_get(path, body)

    assert data == body
    assert data["metadata"]["status"] == operation["status"]
    assert data["metadata"]["err"] == operation["err"]


async def test_a_raw_read_of_anything_else_is_untouched() -> None:
    body = _sync({"name": "web-1", "status": "Running", "metadata": {"secret": "kept-as-sent"}})

    assert await _raw_get("/1.0/instances/web-1", body) == body


def test_the_filter_is_scoped_to_the_operations_path() -> None:
    body = _sync(OPERATIONS["exec"])

    assert redact_operation_response(canonical_proxy_path("/1.0/instances/web-1"), body) is body
    assert redact_operation_response(canonical_proxy_path(f"/1.0/operations/{OP_ID}"), body) != body
