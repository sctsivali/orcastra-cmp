"""The key doors (the raw proxy, /proxy and MCP) driven through their real routes, for the guest-gate tests.

A bare FastAPI app mounts the real external router. Only the key lookup (validate_api_key, which
require_capability wraps) and the cluster connection (get_lxd_service) are replaced, so request
validation, the capability dependency and every gate in the handlers run as deployed. AuditTrail
stands in for the activity logger and keeps each record the real _audit_key_action writes. A Cluster
records every client it builds and every request its session receives, which is what the real
session turns into a request on the wire. MCP tools run as their real functions, through
dispatch.call_proxy into the same handlers.
"""

import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import database, org_scope
from app.core.api_key_capabilities import APP_TYPE_CAPABILITIES, CAPABILITIES
from app.mcp import auth as mcp_auth
from app.mcp import dispatch as mcp_dispatch
from app.mcp import tools_inventory
from app.routers import integrations
from tests._cloud_init_fakes import ConfigSession
from tests._limited_write_fakes import FakeClient, ToolRegistry
from tests._raw_proxy_fakes import CLUSTER_ID, ENDPOINT, key

BASE = ["inventory.read", "lxd.proxy"]
NO_GUEST = key(["*"], BASE)
FILES = key(["*"], BASE + ["instance.files"])
EXEC = key(["*"], BASE + ["instance.exec"])
BOTH = key(["*"], BASE + ["instance.exec", "instance.files"])
EVERY = key(["*"], sorted(CAPABILITIES))
ORCAHUB = key(["*"], sorted(APP_TYPE_CAPABILITIES["orcahub"]))
LEGACY = key(["*"])
NARROW = key(["tenant-a"], BASE)
TWO = key(["tenant-a", "tenant-b"], BASE)
ORG = (7, "org-seven")

SPELLINGS: Dict[str, Callable[[str], str]] = {
    "plain": lambda word: word,
    "upper": str.upper,
    "title": str.title,
    "encoded": lambda word: f"%{ord(word[0]):02X}{word[1:]}",
    "double-encoded": lambda word: f"%25{ord(word[0]):02X}{word[1:]}",
}
SPELLING_IDS = [*SPELLINGS, "trailing-slash", "query"]


def spell(template: str, how: str) -> str:
    """`template` with each {word} spelled `how`, or with a trailing slash or a query added."""
    word = SPELLINGS.get(how, SPELLINGS["plain"])
    path = re.sub(r"\{([^}]+)\}", lambda match: word(match.group(1)), template)
    if how == "trailing-slash":
        head, sep, query = path.partition("?")
        return f"{head}/{sep}{query}"
    if how == "query":
        return f"{path}{'&' if '?' in path else '?'}x=1&project=other"
    return path


class AuditTrail:
    """Stands in for the activity logger, keeping each record it is handed."""

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []

    def log_activity(self, **fields: Any) -> None:
        self.records.append(fields)

    @property
    def refusals(self) -> List[Tuple[str, str]]:
        return refusals_in(self.records)


def refusals_in(records: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """(refused, action) for every failure record a gate wrote."""
    return [(record["details"]["refused"], record["action"]) for record in records
            if record.get("result") == "failure" and "refused" in (record.get("details") or {})]


class Cluster:
    """The cluster connection: records each client built and each request its session receives."""

    def __init__(self, session: Optional[ConfigSession] = None, client: Any = None) -> None:
        if client is not None:
            session = client.api.session
        self.session = session if session is not None else ConfigSession()
        self.client = client if client is not None else SimpleNamespace(
            api=SimpleNamespace(_api_endpoint=ENDPOINT, _timeout=(3, 8), session=self.session))
        self.built: List[Any] = []
        self.connection_manager = SimpleNamespace(get_client=self._pooled, get_project_client=self._scoped)

    def _pooled(self, cluster_id: str) -> Any:
        self.built.append(cluster_id)
        return self.client

    def _scoped(self, cluster_id: str, project: str, **_: Any) -> Any:
        self.built.append((cluster_id, project))
        return self.client

    @property
    def writes(self) -> List[Any]:
        """Every write that reached the cluster: raw requests other than GET, and pylxd saves and creates."""
        raw = [(verb, kwargs.get("json")) for verb, _url, kwargs in self.session.calls if verb != "GET"]
        return raw + list(getattr(self.client, "sent", []))

    @property
    def untouched(self) -> bool:
        return self.built == [] and self.session.calls == [] and not getattr(self.client, "sent", [])


def proxy_cluster(held: Optional[Dict[str, Any]] = None, project: str = "default", fail: Any = None) -> Cluster:
    """A /proxy cluster whose instance `web` holds `held` as its own config (absent when None)."""
    records = {} if held is None else {(project, "/instances/web"): {"name": "web", "config": dict(held)}}
    return Cluster(client=FakeClient(ConfigSession(records, fail=fail)))


@dataclass
class Seen:
    """What one request produced: the answer, what reached the cluster, and the audit records."""

    status: int
    text: str
    code: Optional[str]
    cluster: Cluster
    records: List[Dict[str, Any]]

    @property
    def refusals(self) -> List[Tuple[str, str]]:
        return refusals_in(self.records)


_STATE: Dict[str, Any] = {}
_APP: List[TestClient] = []


def _http() -> TestClient:
    if not _APP:
        app = FastAPI()
        app.include_router(integrations.external_router, prefix="/api/v1")
        app.dependency_overrides[integrations.validate_api_key] = lambda: _STATE["key"]
        app.dependency_overrides[integrations.get_lxd_service] = lambda: _STATE["cluster"]
        _APP.append(TestClient(app, raise_server_exceptions=False))
    return _APP[0]


class _NoSession:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: Any) -> None:
        return None


class Door:
    """Sends one request through a real route as `api_key` and reports what it produced."""

    def __init__(self, trail: AuditTrail, monkeypatch: pytest.MonkeyPatch) -> None:
        self.trail = trail
        self._monkeypatch = monkeypatch

    def _seen(self, response: Any, cluster: Cluster, start: int, envelope: bool) -> Seen:
        body = response.json() if response.content else {}
        records = self.trail.records[start:]
        if response.status_code != 200 or not isinstance(body, dict):
            detail = body.get("detail", body) if isinstance(body, dict) else body
            return Seen(response.status_code, str(detail), None, cluster, records)
        if envelope:
            return Seen(body["status_code"], body.get("error") or "", None, cluster, records)
        if body.get("success") is False:
            return Seen(200, body.get("error") or "", body.get("error_code"), cluster, records)
        return Seen(200, str(body.get("data", body)), None, cluster, records)

    def _send(self, api_key: Dict[str, Any], cluster: Optional[Cluster], verb: str, url: str, **kw: Any) -> Seen:
        cluster = cluster if cluster is not None else Cluster()
        _STATE.update(key=api_key, cluster=cluster)
        start = len(self.trail.records)
        return self._seen(_http().request(verb, url, **kw), cluster, start, url.endswith("/raw-proxy"))

    def raw(self, api_key: Dict[str, Any], method: str, path: str, body: Any = None, project: Optional[str] = None,
            cluster: Optional[Cluster] = None) -> Seen:
        payload: Dict[str, Any] = {"cluster_id": CLUSTER_ID, "method": method, "path": path}
        if project is not None:
            payload["project"] = project
        if body is not None:
            payload["body"] = body
        return self._send(api_key, cluster, "POST", "/api/v1/integrations/raw-proxy", json=payload)

    def proxy(self, api_key: Dict[str, Any], operation: str, params: Dict[str, Any], project: str = "default",
              cluster: Optional[Cluster] = None) -> Seen:
        payload = {"cluster_id": CLUSTER_ID, "operation": operation, "project": project, "params": params}
        cluster = cluster if cluster is not None else Cluster(client=FakeClient(ConfigSession()))
        return self._send(api_key, cluster, "POST", "/api/v1/integrations/proxy", json=payload)

    def get(self, api_key: Dict[str, Any], url: str, cluster: Optional[Cluster] = None, **params: Any) -> Seen:
        return self._send(api_key, cluster, "GET", url, params={"cluster_id": CLUSTER_ID, **params})

    async def mcp(self, module: Any, tool: str, api_key: Dict[str, Any], cluster: Optional[Cluster] = None,
                  **arguments: Any) -> Seen:
        cluster = cluster if cluster is not None else Cluster(client=FakeClient(ConfigSession()))
        registry = ToolRegistry()
        module.register(registry)
        self._monkeypatch.setattr(mcp_dispatch, "get_lxd_service", lambda: cluster)
        self._monkeypatch.setattr(tools_inventory, "get_lxd_service", lambda: cluster)
        token = mcp_auth._CURRENT_KEY.set(api_key)
        start = len(self.trail.records)
        try:
            result = await registry.tools[tool](**arguments)
        finally:
            mcp_auth._CURRENT_KEY.reset(token)
        text = result.content[0].text if result.content else ""
        code = re.match(r"\[([a-z_]+)\] ", text)
        return Seen(0 if result.is_error else 200, text, code.group(1) if code else None, cluster,
                    self.trail.records[start:])


@pytest.fixture(name="door")
def door_fixture(monkeypatch: pytest.MonkeyPatch, fence_calls: Any) -> Iterator[Door]:
    """A Door with the audit trail captured, the owning organization known and desktop consent given."""
    trail = AuditTrail()
    monkeypatch.setattr(integrations, "activity_logger", trail)
    monkeypatch.setattr(mcp_dispatch, "get_activity_logger", lambda: trail)

    async def _owner(*_args: Any, **_kwargs: Any) -> Tuple[int, str]:
        return ORG

    async def _consented(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(org_scope, "resolve_cluster_org", _owner)
    monkeypatch.setattr(database, "async_session_maker", _NoSession)
    monkeypatch.setattr(integrations, "_assert_desktop_allowed", _consented)
    yield Door(trail, monkeypatch)
    _STATE.clear()
