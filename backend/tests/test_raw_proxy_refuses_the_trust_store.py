"""
The raw proxy refuses the trust store and traversal, and never follows a redirect.

`raw_proxy` forwarded any path with the cluster's own client certificate. `/1.0/certificates` is
not project-scoped and a `write` grant can add a certificate to it. A traversal or a redirect
could take the request outside every path check. requests (urllib3 2.x) also drops `.` segments
and the fragment on the client side, so `/1.0/./certificates` and `/1.0/certificates#x` reach the
trust store unless the check decides on the form that is actually sent.
"""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Iterator, List
from urllib.parse import unquote, urlsplit

import pytest
import requests
from fastapi import HTTPException

from app.core.proxy_paths import assert_proxy_path_allowed
from app.routers.integrations import RawProxyRequest, raw_proxy
from app.services.lxd import health

CLUSTER_ID = "cluster-a"
API_KEY = {
    "app_name": "qa-proxy",
    "cluster_permissions": [
        {"cluster_id": CLUSTER_ID, "permissions": ["read", "write", "admin"], "projects": ["*"]}
    ],
}

REFUSED = [
    "/1.0/certificates",
    "/1.0/certificates/",
    "/1.0/CERTIFICATES",
    "/1.0/Certificates?public",
    "/1.0/certificates?project=default",
    "/1.0/certificates/0f3c9a",
    "/1.0/%63ertificates",
    "/1.0/%2563ertificates",
    "/1.0/%63%65%72%74%69%66%69%63%61%74%65%73",
    "/certificates",
    "certificates",
    "/1.0//certificates",
    "//1.0/certificates",
    "/1.0/certificates#fragment",
    "/1.0/certificates%23x",
    "/1.0/certificates%3Fx=1",
]

TRAVERSAL = [
    "/1.0/instances/../certificates",
    "/1.0/instances/%2e%2e/certificates",
    "/1.0/instances/%252e%252e/certificates",
    "/1.0/instances/.%2e/certificates",
    "/1.0/./certificates",
    "/1.0/%2e/certificates",
    "/1.0/instances\\..\\certificates",
    "/1.0/instances%5cweb-1",
    "/1.0/../1.0/certificates",
]

# Every path shape the integrator sends through the raw proxy.
ALLOWED = [
    "/1.0",
    "/1.0/instances",
    "/1.0/instances?recursion=1",
    "/1.0/instances?all-projects=true&recursion=2",
    "/1.0/instances/web-1",
    "/1.0/instances/web-1/state",
    "/1.0/instances/web-1/snapshots",
    "/1.0/instances/web-1/snapshots/snap0",
    "/1.0/projects",
    "/1.0/projects/cust-buyer-0f3c9a",
    "/1.0/resources",
    "/1.0/cluster/members",
    "/1.0/storage-pools",
    "/1.0/storage-pools?all-projects=true",
    "/1.0/storage-pools/default/resources",
    "/1.0/storage-pools/default/volumes",
    "/1.0/storage-pools/default/volumes?all-projects=true",
    "/1.0/storage-pools/default/volumes/custom/data-1",
    "/1.0/images",
    "/1.0/images/0f3c9a1b2c3d",
    "/1.0/images/aliases",
    "/1.0/images/aliases/ubuntu-24.04",
    "/1.0/profiles",
    "/1.0/networks",
    "/1.0/networks/lxdbr0",
    "/1.0/operations/5b1e0a4c-0000-4000-8000-000000000001",
    "/1.0/operations/5b1e0a4c-0000-4000-8000-000000000001/wait",
    "/1.0/operations/5b1e0a4c-0000-4000-8000-000000000001/wait?timeout=30",
    "/1.0/certificates-archive",
]


@pytest.mark.parametrize("path", REFUSED)
def test_every_spelling_of_the_trust_store_is_refused(path: str) -> None:
    with pytest.raises(HTTPException) as caught:
        assert_proxy_path_allowed(path)
    assert caught.value.status_code == 403
    assert caught.value.detail == "The proxy does not expose /certificates"


@pytest.mark.parametrize("path", TRAVERSAL)
def test_traversal_is_refused(path: str) -> None:
    with pytest.raises(HTTPException) as caught:
        assert_proxy_path_allowed(path)
    assert caught.value.status_code == 400


@pytest.mark.parametrize("path", ALLOWED)
def test_the_integrator_paths_are_allowed(path: str) -> None:
    assert_proxy_path_allowed(path)


class RecordingSession:
    def __init__(self) -> None:
        self.calls: List[str] = []

    def _record(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"type": "sync", "metadata": []}'
        return response

    get = post = put = patch = delete = _record


class _Api:
    def __init__(self, session: Any, endpoint: str = "https://cluster-a.example.test:8443/1.0") -> None:
        self._api_endpoint = endpoint
        self._timeout = (3, 8)
        self.session = session


class _Client:
    def __init__(self, session: Any, endpoint: str = "https://cluster-a.example.test:8443/1.0") -> None:
        self.api = _Api(session, endpoint)


class _Manager:
    def __init__(self, client: Any) -> None:
        self._client = client

    def get_client(self, cluster_id: str) -> Any:
        return self._client


class FakeService:
    def __init__(self, client: Any) -> None:
        self.connection_manager = _Manager(client)


@pytest.fixture(autouse=True)
def _reset_breaker() -> Iterator[None]:
    health.clear(CLUSTER_ID)
    yield
    health.clear(CLUSTER_ID)


async def _call(service: FakeService, path: str, method: str = "GET") -> Any:
    return await raw_proxy(
        request=RawProxyRequest(
            cluster_id=CLUSTER_ID,
            method=method,
            path=path,
            body={} if method in ("POST", "PUT", "PATCH") else None,
        ),
        api_key=API_KEY,
        lxd_service=service,
        _=None,
    )


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "path,status",
    [("/1.0/certificates", 403), ("/1.0/certificates#x", 403), ("/1.0/./certificates", 400)],
)
async def test_the_endpoint_never_calls_the_session_for_the_trust_store(
    path: str, status: int, method: str
) -> None:
    session = RecordingSession()
    with pytest.raises(HTTPException) as caught:
        await _call(FakeService(_Client(session)), path, method=method)
    assert caught.value.status_code == status
    assert session.calls == []


async def test_the_endpoint_never_calls_the_session_for_traversal() -> None:
    session = RecordingSession()
    with pytest.raises(HTTPException) as caught:
        await _call(FakeService(_Client(session)), "/1.0/instances/%2e%2e/certificates")
    assert caught.value.status_code == 400
    assert session.calls == []


# A version glued to the next segment. raw_proxy once stripped the string `/1.0` while the guard
# stripped only a whole segment, so the guard judged `1.0certificates` and LXD got /certificates.
GLUED_VERSION = [
    "/1.0certificates",
    "/1.0%63ertificates",
    "/1.0%2563ertificates",
    "/1.0Certificates?public=1",
    "/1.0certificates#x",
]


def _sent_resource(url: str) -> List[str]:
    """The segments under /1.0 that requests puts on the wire for this URL, lowercased."""
    sent = requests.Request("GET", url).prepare().url
    segments = [unquote(segment).lower() for segment in urlsplit(sent).path.split("/") if segment]
    assert segments[0] == "1.0", sent
    return segments[1:]


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("path", GLUED_VERSION)
async def test_a_glued_version_never_reaches_the_trust_store(path: str, method: str) -> None:
    session = RecordingSession()
    try:
        await _call(FakeService(_Client(session)), path, method=method)
    except HTTPException as exc:
        assert exc.status_code in (400, 403)
    for url in session.calls:
        assert _sent_resource(url)[:1] != ["certificates"], url


async def test_a_glued_version_is_sent_as_it_was_judged() -> None:
    session = RecordingSession()
    await _call(FakeService(_Client(session)), "/1.0certificates", method="POST")
    assert session.calls == ["https://cluster-a.example.test:8443/1.0/1.0certificates"]


async def test_an_allowed_path_still_reaches_the_cluster() -> None:
    session = RecordingSession()
    response = await _call(FakeService(_Client(session)), "/1.0/instances?recursion=1")
    assert response.status_code == 200
    assert session.calls == ["https://cluster-a.example.test:8443/1.0/instances?recursion=1"]


class _RedirectingHandler(BaseHTTPRequestHandler):
    hits: List[str] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        type(self).hits.append(self.path)
        if self.path.startswith("/1.0/instances"):
            self.send_response(301)
            self.send_header("Location", "/1.0/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b'{"followed": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture
def redirecting_server() -> Iterator[int]:
    _RedirectingHandler.hits = []
    server = HTTPServer(("127.0.0.1", 0), _RedirectingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


async def test_a_redirect_is_returned_not_followed(redirecting_server: int) -> None:
    client = _Client(requests.Session(), endpoint=f"http://127.0.0.1:{redirecting_server}/1.0")
    response = await _call(FakeService(client), "/1.0/instances")
    assert response.status_code == 301
    assert _RedirectingHandler.hits == ["/1.0/instances"], "the redirect was followed"
