"""
No key surface returns an exec's environment, only an exec key sees its command, and no write answer
carries a migration source's secrets.

A running exec operation echoes its request: `command` and `environment`, and callers put
credentials in the environment. A read key without `instance.exec` polled another caller's exec
through the raw proxy and got `PGPASSWORD=...` back, with `fds` already stripped. And a write's
answer is the operation it started: `POST /1.0/instances/<n> {"migration": true}` answered with the
control, fs and criu secrets that stream the instance to whoever presents them.

- raw GET (and DELETE) under /1.0/operations, in every form: the environment is gone, and the command
  too unless the key holds `instance.exec`. The A2 secrets stay gone.
- every raw write answer: control, fs and criu are gone, and so are the exec request fields the key
  may not see. An exec's `fds` and an image secret stay, because they are the caller's own.
- the serializing surfaces (/proxy get_operation, GET /integrations/operations/{id}, MCP) return the
  progress allowlist only, so neither field reaches them, whatever the key holds.
- status, err and each operation's other metadata are left as LXD wrote them: the integrator reads
  status, err and metadata.metadata of its own operations.

The raw proxy is driven through its real handler; the session records the request and answers with
a fixed LXD body.
"""

import copy
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
import requests

from app.core.api_key_capabilities import APP_TYPE_CAPABILITIES
from app.mcp import auth as mcp_auth
from app.mcp import tools_inventory
from app.routers import integrations
from app.routers.integrations import ProxyRequest
from app.services.lxd.operation_metadata import (
    EXEC_REQUEST_KEYS,
    MIGRATION_SECRET_KEYS,
    OPERATION_SECRET_KEYS,
    SAFE_OPERATION_METADATA_KEYS,
)
from tests._raw_proxy_fakes import (  # noqa: F401 - fixtures are registered by import
    CLUSTER_ID,
    ORCAHUB_WRITES,
    RecordingSession,
    _clean_breaker,
    call_raw_proxy,
    fence_calls,
)

OP_ID = "5b1e0a4c-0000-4000-8000-000000000002"
ENV_SECRET = "PGPASSWORD=hunter2"

RUNNING_EXEC: Dict[str, Any] = {
    "id": OP_ID, "class": "websocket", "description": "Executing command", "status": "Running",
    "status_code": 103, "resources": {"instances": ["/1.0/instances/web-1"]},
    "metadata": {
        "command": ["psql", "-c", "select 1"],
        "environment": {"PGPASSWORD": "hunter2", "API_TOKEN": "tok-123"},
        "interactive": False,
        "fds": {"0": "fd0-s3cr3t", "control": "c0ntr0l-s3cr3t"},
    },
    "may_cancel": False, "err": "", "location": "node-1",
    "created_at": "2026-09-25T10:00:00Z", "updated_at": "2026-09-25T10:00:01Z",
}

MIGRATION_SOURCE: Dict[str, Any] = {
    "id": OP_ID, "class": "websocket", "description": "Migrating instance", "status": "Running",
    "status_code": 103, "resources": {"instances": ["/1.0/instances/web-1"]},
    "metadata": {"control": "m1gr4t10n-c0ntr0l", "fs": "m1gr4t10n-fs", "criu": "m1gr4t10n-cr1u"},
    "may_cancel": True, "err": "", "location": "node-1",
    "created_at": "2026-09-25T10:00:00Z", "updated_at": "2026-09-25T10:00:01Z",
}

MIGRATION_VALUES = ("m1gr4t10n-c0ntr0l", "m1gr4t10n-fs", "m1gr4t10n-cr1u")


def _key(capabilities: Optional[List[str]], projects: tuple = ("*",)) -> Dict[str, Any]:
    api_key: Dict[str, Any] = {
        "id": "oak_withheld",
        "app_name": "qa-withheld",
        "cluster_permissions": [
            {"cluster_id": CLUSTER_ID, "permissions": ["read", "write", "admin"], "projects": list(projects)}
        ],
    }
    if capabilities is not None:
        api_key["capabilities"] = capabilities
    return api_key


READ = _key(["inventory.read", "lxd.proxy"])
EXEC = _key(["inventory.read", "lxd.proxy", "instance.exec"])
FILES = _key(["inventory.read", "lxd.proxy", "instance.files"])
LEGACY = _key(None)
ORCAHUB = _key(sorted(APP_TYPE_CAPABILITIES["orcahub"]))


class _AnsweringSession(RecordingSession):
    """Records every call and answers with a fixed LXD body, as the real session returns it."""

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


def _async(operation: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "async", "status": "Operation created", "status_code": 100,
            "operation": f"/1.0/operations/{operation['id']}", "error_code": 0, "error": "",
            "metadata": operation}


def _without(operation: Dict[str, Any], withheld: frozenset) -> Dict[str, Any]:
    expected = copy.deepcopy(operation)
    expected["metadata"] = {key: value for key, value in operation["metadata"].items() if key not in withheld}
    return expected


async def _raw(api_key: Dict[str, Any], method: str, path: str, answer: Dict[str, Any],
               body: Optional[Dict[str, Any]] = None) -> Any:
    session = _AnsweringSession(answer)
    outcome, _ = await call_raw_proxy(api_key, method, path, body=body, session=session)
    assert len(session.calls) == 1, outcome
    return outcome.data


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integrations, "_audit_key_action", lambda *args, **kwargs: None)


# ── reads under /1.0/operations ────────────────────────────────────────────────────────────────

ONE = [f"/1.0/operations/{OP_ID}", f"/1.0/operations/{OP_ID}/wait?timeout=30", f"/1.0/%6Fperations/{OP_ID}"]
LISTINGS = ["/1.0/operations?recursion=1", "/1.0/operations?recursion=1&all-projects=true"]


@pytest.mark.parametrize("path", ONE)
@pytest.mark.parametrize(
    "api_key,withheld",
    [(READ, EXEC_REQUEST_KEYS), (LEGACY, EXEC_REQUEST_KEYS), (EXEC, frozenset({"environment"}))],
    ids=["read", "legacy", "exec"],
)
async def test_a_raw_operation_read_withholds_the_exec_request(
    path: str, api_key: Dict[str, Any], withheld: frozenset
) -> None:
    data = await _raw(api_key, "GET", path, _sync(RUNNING_EXEC))

    assert ENV_SECRET.split("=")[1] not in json.dumps(data)
    assert data == _sync(_without(RUNNING_EXEC, OPERATION_SECRET_KEYS | withheld))


@pytest.mark.parametrize("path", LISTINGS)
@pytest.mark.parametrize("api_key", [READ, EXEC], ids=["read", "exec"])
async def test_a_raw_operation_listing_never_returns_an_environment(path: str, api_key: Dict[str, Any]) -> None:
    data = await _raw(api_key, "GET", path, _sync({"running": [RUNNING_EXEC], "success": []}))

    text = json.dumps(data)
    assert "hunter2" not in text and "tok-123" not in text
    command_shown = api_key is EXEC
    assert ("psql" in text) is command_shown
    assert data["metadata"]["running"][0]["metadata"]["interactive"] is False


# ── write answers ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/1.0/instances/web-1", {"migration": True}),
        ("POST", "/1.0/instances/web-1/snapshots/snap0", {"migration": True}),
        ("POST", "/1.0/storage-pools/default/volumes/custom/vol1", {"migration": True}),
        # The target side of a push migration answers with the same three secrets.
        ("POST", "/1.0/instances", {"name": "web-2", "source": {"type": "migration", "mode": "push"}}),
    ],
)
async def test_no_write_answer_carries_a_migration_secret(method: str, path: str, body: Dict[str, Any]) -> None:
    data = await _raw(FILES, method, path, _async(MIGRATION_SOURCE), body=body)

    text = json.dumps(data)
    for value in MIGRATION_VALUES:
        assert value not in text
    assert data == _async(_without(MIGRATION_SOURCE, MIGRATION_SECRET_KEYS))
    assert data["operation"] == f"/1.0/operations/{OP_ID}"


@pytest.mark.usefixtures("fence_calls")
async def test_an_exec_answer_keeps_the_callers_sockets_and_drops_the_environment() -> None:
    body = {"command": ["psql"], "environment": {"PGPASSWORD": "hunter2"}, "wait-for-websocket": True}

    data = await _raw(EXEC, "POST", "/1.0/instances/web-1/exec", _async(RUNNING_EXEC), body=body)

    metadata = data["metadata"]["metadata"]
    assert metadata["fds"] == RUNNING_EXEC["metadata"]["fds"]
    assert metadata["command"] == RUNNING_EXEC["metadata"]["command"]
    assert "environment" not in metadata


async def test_a_read_elsewhere_is_left_as_lxd_wrote_it() -> None:
    answer = _sync({"name": "web-1", "config": {"environment.TZ": "UTC"}, "metadata": {"environment": "kept"}})

    assert await _raw(READ, "GET", "/1.0/instances/web-1", answer) == answer


# The answers the integrator's writes get, as LXD writes them: an operation with no secret, whose
# id (`operation`, or metadata.id) the integrator reads to wait on.
def _integrator_answer(method: str, path: str) -> Dict[str, Any]:
    operation = {
        "id": OP_ID, "class": "task", "description": f"{method} {path}", "status": "Running",
        "status_code": 103, "resources": {}, "metadata": {"download_progress": "rootfs: 3%"} if path == "/1.0/images" else None,
        "may_cancel": False, "err": "", "location": "node-1",
        "created_at": "2026-09-25T10:00:00Z", "updated_at": "2026-09-25T10:00:00Z",
    }
    return _async(operation)


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("method,path", ORCAHUB_WRITES)
async def test_the_integrators_write_answers_are_unchanged(method: str, path: str) -> None:
    answer = _integrator_answer(method, path)
    body = None if method == "DELETE" else {"name": "x"} if method == "POST" else {"config": {}}

    data = await _raw(ORCAHUB, method, path, answer, body=body)

    assert data == answer


# The integrator's polls of its own operations (image publish and pull, a create): it reads status,
# err and metadata.metadata, none of which is withheld.
INTEGRATOR_OPERATIONS = [
    {"id": OP_ID, "class": "task", "description": "Creating image", "status": "Success", "status_code": 200,
     "resources": {"images": ["/1.0/images/0f3c9a"]}, "metadata": {"fingerprint": "0f3c9a", "size": 123},
     "may_cancel": False, "err": "", "location": "node-1"},
    {"id": OP_ID, "class": "task", "description": "Downloading image", "status": "Running", "status_code": 103,
     "resources": {}, "metadata": {"download_progress": "rootfs: 48%"}, "may_cancel": False, "err": "",
     "location": "node-1"},
    {"id": OP_ID, "class": "task", "description": "Creating instance", "status": "Failure", "status_code": 400,
     "resources": {}, "metadata": {"create_instance_from_image_unpack_progress": "Unpack: 40%",
                                   "progress": {"stage": "create_instance_from_image_unpack", "percent": "40"}},
     "may_cancel": False, "err": "Failed creating instance", "location": "node-1"},
]


@pytest.mark.parametrize("operation", INTEGRATOR_OPERATIONS, ids=["publish", "pull", "create"])
@pytest.mark.parametrize("path", [f"/1.0/operations/{OP_ID}", f"/1.0/operations/{OP_ID}/wait?timeout=55"])
async def test_the_integrators_operation_polls_read_what_lxd_wrote(path: str, operation: Dict[str, Any]) -> None:
    data = await _raw(ORCAHUB, "GET", path, _sync(operation))

    assert data == _sync(operation)
    assert (data["metadata"]["status"], data["metadata"]["err"], data["metadata"]["metadata"]) == (
        operation["status"], operation["err"], operation["metadata"]
    )


# ── the serializing surfaces ───────────────────────────────────────────────────────────────────


def _service(operation: Dict[str, Any]) -> SimpleNamespace:
    """pylxd's operation manager, one attribute per LXD field, as the real lookup returns it."""
    def get(operation_id: str) -> Any:
        found = SimpleNamespace()
        for key, value in copy.deepcopy(operation).items():
            setattr(found, key, value)
        return found

    client = SimpleNamespace(operations=SimpleNamespace(get=get))
    return SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda cluster_id: client))


class _Registry:
    def __init__(self) -> None:
        self.tools: Dict[str, Any] = {}

    def tool(self, name: str, **_: Any) -> Any:
        def register(function: Any) -> Any:
            self.tools[name] = function
            return function

        return register


async def _serialized(surface: str, api_key: Dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Any:
    service = _service(RUNNING_EXEC)
    if surface == "proxy":
        response = await integrations.proxy_operation(
            request=ProxyRequest(cluster_id=CLUSTER_ID, operation="get_operation", params={"operation_id": OP_ID}),
            api_key=api_key, lxd_service=service, _=None,
        )
        return response.data
    if surface == "route":
        return await integrations.get_integration_operation(
            operation_id=OP_ID, cluster_id=CLUSTER_ID, project="default", api_key=api_key, lxd_service=service,
        )
    registry = _Registry()
    tools_inventory.register(registry)
    monkeypatch.setattr(tools_inventory, "get_lxd_service", lambda: service)
    token = mcp_auth._CURRENT_KEY.set(api_key)
    try:
        result = await registry.tools["orcastra_get_operation"](operation_id=OP_ID, cluster_id=CLUSTER_ID)
    finally:
        mcp_auth._CURRENT_KEY.reset(token)
    return [json.loads(block.text) for block in result.content] + [result.structured_content]


@pytest.mark.parametrize("surface", ["proxy", "route", "mcp"])
@pytest.mark.parametrize("api_key", [READ, EXEC], ids=["read", "exec"])
async def test_a_serializing_surface_returns_neither_field(
    surface: str, api_key: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = await _serialized(surface, api_key, monkeypatch)

    text = json.dumps(payload, default=str)
    assert "hunter2" not in text and "tok-123" not in text and "psql" not in text


def test_the_allowlist_names_neither_field() -> None:
    assert not EXEC_REQUEST_KEYS & SAFE_OPERATION_METADATA_KEYS
    assert MIGRATION_SECRET_KEYS <= OPERATION_SECRET_KEYS
