"""Shared fakes for the limited-key body and write-plan tests: a cluster that answers per project.

The session records every call (verb, URL, project) the way the real session turns it into a
request on the wire, and answers the reads a write plan makes from per-project tables. The client
records what the /proxy operations would send (a create, an instance save, a restore), which is
the side effect the real pylxd calls turn into a write.
"""

from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import unquote

import pytest
import requests
from fastapi import HTTPException

from app.mcp import auth as mcp_auth
from app.mcp import dispatch as mcp_dispatch
from app.routers.integrations import ProxyRequest, RawProxyRequest, proxy_operation, raw_proxy
from app.services.webhooks import emitter
from tests._raw_proxy_fakes import CLUSTER_ID, key

PRIVILEGED = {"config": {"security.privileged": "true"}, "devices": {}}
PASSTHROUGH = {"config": {}, "devices": {"d": {"type": "unix-block", "source": "/dev/sda"}}}
PLAIN = {"config": {"limits.cpu": "2"}, "devices": {"root": {"type": "disk", "path": "/", "pool": "default"}}}
FLEET = {"default": PLAIN, "web": PLAIN, "priv": PRIVILEGED, "disk": PASSTHROUGH}

NARROW = key(["tenant-a"])
WILDCARD = key(["*"])
TWO = key(["tenant-a", "tenant-b"])
SERVER = "https://images.example.test"


def _response(status: int, body: Any) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = requests.compat.json.dumps(body).encode()
    return response


class ProfileSession:
    """Answers GET /profiles/<name> from FLEET in every project (404 when absent), records every call."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, str]] = []

    def _answer(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        project = (kwargs.get("params") or {}).get("project") or "default"
        self.calls.append((method, url, project))
        name = unquote(url.rsplit("/profiles/", 1)[-1]) if "/profiles/" in url else None
        if method == "GET" and name is not None:
            return _response(200, {"metadata": FLEET[name]}) if name in FLEET else _response(404, {})
        return _response(200, {"type": "sync", "metadata": {}})

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("DELETE", url, **kwargs)

    @property
    def sent(self) -> bool:
        """Whether anything but a read reached the cluster."""
        return any(method != "GET" for method, *_ in self.calls)


class ProjectSession(ProfileSession):
    """Profiles, instances, snapshots and images per project.

    'priv' is plain in tenant-a and privileged in tenant-b. app's snapshot s0 was taken while an
    administrator had it privileged.
    """

    PROFILES = {"tenant-a": {"priv": PLAIN, "default": PLAIN}, "tenant-b": {"priv": PRIVILEGED, "default": PLAIN}}
    INSTANCES = {"tenant-a": {"web": {"name": "web", "profiles": ["priv"]},
                              "app": {"name": "app", "profiles": ["default"]},
                              "root": {"name": "root", "profiles": ["default"],
                                       "config": {"security.privileged": "true"}, "devices": {}}}}
    SNAPSHOTS = {"tenant-a": {("app", "s0"): {"profiles": ["default"], "config": {"security.privileged": "true"},
                                             "devices": {}}}}
    IMAGES = {"tenant-b": {"abc": {"profiles": ["priv"]}, "plain": {"profiles": ["default"]},
                           "cached": {"profiles": ["priv"], "update_source": {"server": SERVER, "alias": "jammy"}},
                           "cached-ct": {"profiles": ["web"], "type": "container", "update_source": {
                               "server": SERVER, "alias": "focal", "protocol": "simplestreams"}},
                           "cached-vm": {"profiles": ["priv"], "type": "virtual-machine", "update_source": {
                               "server": SERVER, "alias": "focal", "protocol": "simplestreams"}},
                           "vm-only": {"profiles": ["priv"], "type": "virtual-machine", "update_source": {
                               "server": SERVER, "alias": "vmonly"}},
                           # f00d was downloaded here by fingerprint, and tenant-a cached it under an alias
                           "f00d": {"profiles": ["priv"]}},
              "tenant-a": {"f00d": {"profiles": ["default"], "type": "container", "update_source": {
                               "server": SERVER, "alias": "ubuntu/24.04"}}}}
    ALIASES = {"tenant-b": {"ubuntu": "abc", "clean": "plain"}}

    def _answer(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        query = kwargs.get("params") or {}
        project = query.get("project") or "default"
        if method != "GET":
            self.calls.append((method, url, project))
            return _response(200, {"type": "sync", "metadata": {}})
        self.calls.append((method, url, project))
        tail = unquote(url.rsplit("/", 1)[-1])
        if "/profiles/" in url:
            return self._found(self.PROFILES.get(project, {}).get(tail))
        if "/snapshots/" in url:
            instance, snapshot = [unquote(part) for part in url.split("/instances/", 1)[1].split("/snapshots/")]
            return self._found(self.SNAPSHOTS.get(project, {}).get((instance, snapshot)))
        if "/instances/" in url:
            # LXD decodes the path segment once before looking the name up
            return self._found(self.INSTANCES.get(project, {}).get(tail))
        if "/images/aliases/" in url:
            target = self.ALIASES.get(project, {}).get(tail)
            return self._found({"target": target} if target else None)
        if "/images/" in url:
            return self._found(self.IMAGES.get(project, {}).get(tail))
        if url.endswith("/images") and query.get("all-projects"):
            return _response(200, {"metadata": [dict(image, fingerprint=fingerprint, project=owner)
                                                 for owner, images in self.IMAGES.items()
                                                 for fingerprint, image in images.items()]})
        return _response(200, {"type": "sync", "metadata": {}})

    @staticmethod
    def _found(metadata: Optional[Dict[str, Any]]) -> requests.Response:
        return _response(200, {"metadata": metadata}) if metadata is not None else _response(404, {})


class FakeInstance:
    """What execute_proxy_operation drives for update_instance and restore_snapshot."""

    def __init__(self, client: "FakeClient", name: str) -> None:
        self._client, self.name, self.status = client, name, "Stopped"
        self.config: Dict[str, Any] = {}
        self.devices: Dict[str, Any] = {}
        self.profiles: List[str] = ["default"]
        self.description = ""

    def save(self, wait: bool = True) -> None:
        self._client.sent.append(("save", self.name, {"config": dict(self.config), "devices": dict(self.devices),
                                                      "profiles": list(self.profiles)}))

    def restore_snapshot(self, snapshot: str, wait: bool = True) -> None:
        self._client.sent.append(("restore", self.name, snapshot))


class FakeClient:
    def __init__(self, session: ProfileSession) -> None:
        self.sent: List[Tuple[str, str, Any]] = []
        self.api = SimpleNamespace(_api_endpoint="https://cluster-a.example.test:8443/1.0", _timeout=(3, 8),
                                   session=session)
        self.instances = SimpleNamespace(create=self._create, get=lambda name: FakeInstance(self, unquote(name)))

    @property
    def created(self) -> List[Any]:
        return [body for verb, _name, body in self.sent if verb == "create"]

    def _create(self, params: Dict[str, Any], wait: bool = True) -> Any:
        self.sent.append(("create", str(params.get("name")), params))
        return SimpleNamespace(name=params.get("name"), status="Stopped")


def fake_service(client: FakeClient) -> SimpleNamespace:
    manager = SimpleNamespace(get_client=lambda _cluster: client,
                              get_project_client=lambda *_a, **_k: client)
    return SimpleNamespace(connection_manager=manager)


@pytest.fixture(autouse=True)
def _no_webhook_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def _no_event(**_kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(emitter, "emit_resource_event", _no_event)
    yield


async def raw_write(
    method: str, path: str, project: str, body: Optional[Dict[str, Any]], api_key: Dict[str, Any] = TWO,
    session: Optional[ProfileSession] = None,
) -> Tuple[Any, ProfileSession]:
    """Drive the real raw_proxy handler. Returns (response or HTTPException, session)."""
    session = session if session is not None else ProjectSession()
    try:
        answer = await raw_proxy(
            request=RawProxyRequest(cluster_id=CLUSTER_ID, method=method, path=path, project=project, body=body),
            api_key=api_key, lxd_service=fake_service(FakeClient(session)), _=None,
        )
    except HTTPException as refusal:
        return refusal, session
    return answer, session


async def proxy_call(
    operation: str, project: str, params: Dict[str, Any], api_key: Dict[str, Any] = TWO,
    client: Optional[FakeClient] = None,
) -> Tuple[Any, FakeClient]:
    """Drive the real proxy_operation handler. Returns (response or HTTPException, client)."""
    client = client if client is not None else FakeClient(ProjectSession())
    try:
        answer = await proxy_operation(
            request=ProxyRequest(cluster_id=CLUSTER_ID, operation=operation, project=project, params=params),
            api_key=api_key, lxd_service=fake_service(client), _=None,
        )
    except HTTPException as refusal:
        return refusal, client
    return answer, client


class ToolRegistry:
    """Stands in for the MCP server: collects the tool functions a module registers."""

    def __init__(self) -> None:
        self.tools: Dict[str, Any] = {}

    def tool(self, name: str, **_: Any) -> Any:
        def register(function: Any) -> Any:
            self.tools[name] = function
            return function

        return register


async def mcp_call(
    module: Any, tool: str, client: FakeClient, api_key: Dict[str, Any], monkeypatch: pytest.MonkeyPatch,
    **arguments: Any,
) -> Any:
    """Run one MCP tool as `api_key`, through dispatch.call_proxy into the real proxy_operation."""
    registry = ToolRegistry()
    module.register(registry)
    monkeypatch.setattr(mcp_dispatch, "get_lxd_service", lambda: fake_service(client))
    token = mcp_auth._CURRENT_KEY.set(api_key)
    try:
        return await registry.tools[tool](**arguments)
    finally:
        mcp_auth._CURRENT_KEY.reset(token)
