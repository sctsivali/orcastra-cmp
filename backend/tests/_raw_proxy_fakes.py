"""Shared fakes for the raw-proxy path tests: a recording session, a cluster, and the integrator paths.

The session records what raw_request hands it (verb, URL and keyword arguments), which is the side
effect the real session turns into a request on the wire. `lxd_reads` parses a sent URL the way LXD
does after requests has prepared it: the path decoded once and split, the query decoded once.
"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest
import requests
from fastapi import HTTPException

from app.routers import integrations
from app.routers.integrations import RawProxyRequest, raw_proxy
from app.services.lxd import health

CLUSTER_ID = "cluster-a"
ENDPOINT = "https://cluster-a.example.test:8443/1.0"


def key(projects: List[str], capabilities: Optional[List[str]] = None) -> Dict[str, Any]:
    """A key with read, write and admin on CLUSTER_ID. No `capabilities` field means legacy."""
    api_key: Dict[str, Any] = {
        "id": "oak_test",
        "app_name": "qa-proxy",
        "cluster_permissions": [
            {"cluster_id": CLUSTER_ID, "permissions": ["read", "write", "admin"], "projects": projects}
        ],
    }
    if capabilities is not None:
        api_key["capabilities"] = capabilities
    return api_key


WILDCARD = key(["*"])
NARROW = key(["tenant-a"])


class RecordingSession:
    """Records every call raw_request makes and answers 200 with an empty sync body."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []

    @property
    def urls(self) -> List[str]:
        return [url for _, url, _ in self.calls]

    def _answer(self, verb: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append((verb, url, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"type": "sync", "metadata": []}'
        return response

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


def fake_service(session: Any, endpoint: str = ENDPOINT) -> SimpleNamespace:
    client = SimpleNamespace(api=SimpleNamespace(_api_endpoint=endpoint, _timeout=(3, 8), session=session))
    manager = SimpleNamespace(get_client=lambda cluster_id: client)
    return SimpleNamespace(connection_manager=manager)


async def call_raw_proxy(
    api_key: Dict[str, Any],
    method: str,
    path: str,
    project: Optional[str] = None,
    body: Optional[Dict[str, Any]] = None,
    session: Optional[Any] = None,
    endpoint: str = ENDPOINT,
) -> Tuple[Any, Any]:
    """Drive the real raw_proxy handler. Returns (response or HTTPException, session)."""
    session = session if session is not None else RecordingSession()
    if body is None and method in ("POST", "PUT", "PATCH"):
        body = {}
    try:
        with _without_rule_bookkeeping():
            response = await raw_proxy(
                request=RawProxyRequest(cluster_id=CLUSTER_ID, method=method, path=path, project=project, body=body),
                api_key=api_key,
                lxd_service=fake_service(session, endpoint),
                _=None,
            )
    except HTTPException as refusal:
        return refusal, session
    return response, session


@contextmanager
def _without_rule_bookkeeping() -> Iterator[None]:
    """Run the raw proxy's request with the access-rule bookkeeping around it taken out.

    A project DELETE runs inside the deletion core and a stranding write captures rules first
    (services/raw_project_delete.py, services/stranded_rules.py); both need the database. These
    tests judge what is SENT, so the core runs the sender alone and nothing is captured. The
    bookkeeping itself is covered by test_a_raw_project_delete_forgets_its_access_rules.py and
    test_a_deleted_instance_takes_its_named_rules_with_it.py.
    """
    from app.services import project_deletion, stranded_rules
    from app.services.lxd.exceptions import LXDProjectNotFoundError

    async def sender_only(_lxd_service, _host, _name, *, requester, scope_project, send=None):
        try:
            await send()
        except LXDProjectNotFoundError:
            return project_deletion.ProjectDeleted(already_gone=True, revoke_note="")
        return project_deletion.ProjectDeleted(already_gone=False, revoke_note="")

    async def nothing_captured(_cluster_id, _target):
        return None

    with patch.object(project_deletion, "delete_for_integration", sender_only), patch.object(
        stranded_rules, "capture", nothing_captured
    ):
        yield


def lxd_reads(url: str, method: str = "GET") -> Tuple[List[str], List[Tuple[str, str]]]:
    """The resource segments under /1.0 and the query LXD parses from what requests sends."""
    prepared = requests.Request(method, url).prepare().url
    parts = urlsplit(prepared)
    segments = [unquote(segment) for segment in parts.path.split("/")[1:]]
    assert segments[:1] == ["1.0"], prepared
    return segments[1:], parse_qsl(parts.query, keep_blank_values=True)


@pytest.fixture
def fence_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[List[Tuple[str, Dict[str, Any]]]]:
    """Record which migration fence raw_proxy asks, and for what, without a database."""
    calls: List[Tuple[str, Dict[str, Any]]] = []

    def recorder(name: str) -> Any:
        async def _record(**kwargs: Any) -> None:
            calls.append((name, kwargs))

        return _record

    for name in (
        "_assert_proxy_write_is_not_fenced",
        "_assert_proxy_project_write_is_not_fenced",
        "_assert_proxy_cluster_write_is_not_fenced",
    ):
        monkeypatch.setattr(integrations, name, recorder(name))
    yield calls


@pytest.fixture(autouse=True)
def _clean_breaker() -> Iterator[None]:
    health.clear(CLUSTER_ID)
    yield
    health.clear(CLUSTER_ID)


# Every raw-proxy path shape the integrator (OrcaHub) sends, read from its client source, with the
# names it percent-encodes (`encodeURIComponent`) filled in.
ORCAHUB_READS = [
    "/1.0",
    "/1.0/instances",
    "/1.0/instances?recursion=2",
    "/1.0/instances?recursion=2&all-projects=true",
    "/1.0/instances/web-1",
    "/1.0/instances/web-1/state",
    "/1.0/instances/web-1/snapshots",
    "/1.0/instances/web-1/snapshots?recursion=1",
    "/1.0/instances/web-1/snapshots/snap0",
    "/1.0/instances/ATLAS-Semanan/snapshots/snap-%25Y%25m0-%25H%25M",
    "/1.0/projects",
    "/1.0/projects?recursion=1",
    "/1.0/projects/cust-buyer-0f3c9a",
    "/1.0/resources",
    "/1.0/cluster/members",
    "/1.0/metadata/configuration",
    "/1.0/storage-pools",
    "/1.0/storage-pools?recursion=1",
    "/1.0/storage-pools?all-projects=true",
    "/1.0/storage-pools/default/resources",
    "/1.0/storage-pools/default/volumes",
    "/1.0/storage-pools/default/volumes?recursion=1&all-projects=true",
    "/1.0/storage-pools/default/volumes/custom?recursion=1",
    "/1.0/storage-pools/default/volumes/custom/data-1",
    "/1.0/storage-pools/default/volumes/custom/data-1/snapshots?recursion=1",
    "/1.0/images",
    "/1.0/images?recursion=1",
    "/1.0/images/0f3c9a1b2c3d",
    "/1.0/images/aliases",
    "/1.0/images/aliases/ubuntu-24.04",
    "/1.0/profiles?recursion=1",
    "/1.0/networks",
    "/1.0/networks?recursion=1",
    "/1.0/networks/lxdbr0",
    "/1.0/operations/5b1e0a4c-0000-4000-8000-000000000001",
    "/1.0/operations/5b1e0a4c-0000-4000-8000-000000000001/wait",
    "/1.0/operations/5b1e0a4c-0000-4000-8000-000000000001/wait?timeout=30",
]

ORCAHUB_WRITES = [
    ("POST", "/1.0/images"),
    ("DELETE", "/1.0/images/0f3c9a1b2c3d"),
    ("POST", "/1.0/images/aliases"),
    ("DELETE", "/1.0/images/aliases/ubuntu-24.04"),
    ("POST", "/1.0/instances"),
    ("PUT", "/1.0/instances/web-1"),
    ("PATCH", "/1.0/instances/web-1"),
    ("DELETE", "/1.0/instances/web-1"),
    ("PUT", "/1.0/instances/web-1/state"),
    ("POST", "/1.0/instances/web-1/snapshots"),
    ("DELETE", "/1.0/instances/web-1/snapshots/snap0"),
    ("DELETE", "/1.0/operations/5b1e0a4c-0000-4000-8000-000000000001"),
    ("POST", "/1.0/projects"),
    ("PATCH", "/1.0/projects/cust-buyer-0f3c9a"),
    ("DELETE", "/1.0/projects/cust-buyer-0f3c9a"),
    ("POST", "/1.0/storage-pools/default/volumes/custom"),
    ("PUT", "/1.0/storage-pools/default/volumes/custom/data-1"),
    ("PATCH", "/1.0/storage-pools/default/volumes/custom/data-1"),
    ("PUT", "/1.0/storage-pools/default/volumes/custom/data-1/state"),
    ("DELETE", "/1.0/storage-pools/default/volumes/custom/data-1"),
    ("POST", "/1.0/storage-pools/default/volumes/custom/data-1/snapshots"),
    ("DELETE", "/1.0/storage-pools/default/volumes/custom/data-1/snapshots/snap0"),
]
