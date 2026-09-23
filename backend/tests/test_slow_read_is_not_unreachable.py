"""
A slow read through the raw proxy is not an unreachable cluster.

A recursive list on a host with many interfaces outlived the pooled client's read timeout. The
timeout tripped the per-cluster breaker for 60s, so every raw-proxy call to that cluster,
provisioning included, answered 503 "recently unreachable". A read timeout now answers an inner
504 and leaves the breaker alone. A connect failure still trips it, and so does a write that
outlives the wall-clock cap.

The pooled session mounts Retry(read=0), so a GET read timeout reaches the handler as
ConnectionError(MaxRetryError(ReadTimeoutError)) and a stalled body as
ConnectionError(ReadTimeoutError). An isinstance(ReadTimeout) test sees neither, which is why the
real shapes and a real socket are both exercised here.
"""

import socket
import threading
from typing import Any, Iterator, Optional

import pytest
import requests
from fastapi import HTTPException
from urllib3.exceptions import MaxRetryError, NewConnectionError, ReadTimeoutError

from app.routers import integrations as router_module
from app.routers.integrations import RawProxyRequest, raw_proxy
from app.services.lxd import health

CLUSTER_ID = "cluster-a"
API_KEY = {
    "app_name": "qa-slow",
    "cluster_permissions": [
        {"cluster_id": CLUSTER_ID, "permissions": ["read", "write", "admin"], "projects": ["*"]}
    ],
}


class RaisingSession:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def _raise(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls += 1
        raise self.error

    get = post = put = patch = delete = _raise


class StallingSession:
    """Holds the worker thread past the wall-clock cap, the shape a BlockingTimeout comes from."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def _stall(self, url: str, **kwargs: Any) -> requests.Response:
        self.release.wait(3)
        raise requests.exceptions.ReadTimeout("released")

    get = post = put = patch = delete = _stall


class _Api:
    def __init__(self, session: Any) -> None:
        self._api_endpoint = "https://cluster-a.example.test:8443/1.0"
        self._timeout = (3, 8)
        self.session = session


class _Client:
    def __init__(self, session: Any) -> None:
        self.api = _Api(session)


class _Manager:
    def __init__(self, client: Any, reason_on_miss: Optional[str] = None) -> None:
        self._client = client
        self._reason_on_miss = reason_on_miss

    def get_client(self, cluster_id: str) -> Any:
        # note: the real get_client records the reason when a reconnect fails and returns None.
        if self._client is None and self._reason_on_miss:
            health.record_failure(cluster_id, self._reason_on_miss)
        return self._client


class FakeService:
    def __init__(self, client: Any, reason_on_miss: Optional[str] = None) -> None:
        self.connection_manager = _Manager(client, reason_on_miss)


@pytest.fixture(autouse=True)
def _reset_breaker() -> Iterator[None]:
    health.clear(CLUSTER_ID)
    yield
    health.clear(CLUSTER_ID)


async def _call(service: FakeService, method: str = "GET") -> Any:
    return await raw_proxy(
        request=RawProxyRequest(
            cluster_id=CLUSTER_ID,
            method=method,
            path="/1.0/networks?recursion=1",
            body={} if method in ("POST", "PUT", "PATCH") else None,
        ),
        api_key=API_KEY,
        lxd_service=service,
        _=None,
    )


def _pooled_read_timeout() -> requests.exceptions.ConnectionError:
    reason = ReadTimeoutError(None, "/1.0/networks", "Read timed out. (read timeout=8)")
    return requests.exceptions.ConnectionError(MaxRetryError(None, "/1.0/networks", reason=reason))


def _stalled_body() -> requests.exceptions.ConnectionError:
    return requests.exceptions.ConnectionError(ReadTimeoutError(None, "/1.0/networks", "Read timed out."))


def _refused() -> requests.exceptions.ConnectionError:
    reason = NewConnectionError(None, "Failed to establish a new connection: [Errno 111] refused")
    return requests.exceptions.ConnectionError(MaxRetryError(None, "/1.0/networks", reason=reason))


@pytest.mark.parametrize(
    "error",
    [
        _pooled_read_timeout(),
        _stalled_body(),
        requests.exceptions.ReadTimeout("Read timed out. (read timeout=8)"),
    ],
    ids=["pooled-get", "stalled-body", "plain-read-timeout"],
)
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_a_read_timeout_answers_504_and_leaves_the_cluster_reachable(
    error: Exception, method: str
) -> None:
    session = RaisingSession(error)
    response = await _call(FakeService(_Client(session)), method=method)
    assert response.status_code == 504
    assert response.data is None
    assert not health.is_tripped(CLUSTER_ID)


async def test_a_second_call_still_reaches_the_cluster() -> None:
    session = RaisingSession(_pooled_read_timeout())
    service = FakeService(_Client(session))
    await _call(service)
    await _call(service)
    assert session.calls == 2, "the first slow read must not short-circuit the second call"


@pytest.mark.parametrize(
    "error",
    [_refused(), requests.exceptions.ConnectTimeout("connect timed out")],
    ids=["refused", "connect-timeout"],
)
async def test_a_connect_failure_still_trips_the_breaker(error: Exception) -> None:
    response = await _call(FakeService(_Client(RaisingSession(error))))
    assert response.status_code == 500
    assert health.is_tripped(CLUSTER_ID)


async def test_a_get_that_outlives_the_cap_does_not_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_module, "RAW_PROXY_TIMEOUT", 0.2)
    session = StallingSession()
    try:
        response = await _call(FakeService(_Client(session)), method="GET")
    finally:
        session.release.set()
    assert response.status_code == 504
    assert not health.is_tripped(CLUSTER_ID)


async def test_a_write_that_outlives_the_cap_still_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_module, "RAW_PROXY_TIMEOUT", 0.2)
    session = StallingSession()
    try:
        response = await _call(FakeService(_Client(session)), method="POST")
    finally:
        session.release.set()
    assert response.status_code == 504
    assert health.is_tripped(CLUSTER_ID)


async def test_not_connected_names_the_recorded_reason() -> None:
    with pytest.raises(HTTPException) as caught:
        await _call(FakeService(None, reason_on_miss="certificate not trusted by the cluster"))
    assert caught.value.status_code == 503
    assert caught.value.detail == (
        f"Cluster {CLUSTER_ID} is not connected: certificate not trusted by the cluster"
    )


async def test_not_connected_without_a_reason_says_so() -> None:
    with pytest.raises(HTTPException) as caught:
        await _call(FakeService(None))
    assert caught.value.status_code == 503
    assert caught.value.detail == (
        f"Cluster {CLUSTER_ID} is not connected (the connection could not be re-established)"
    )


@pytest.fixture
def silent_server() -> Iterator[int]:
    """A loopback peer that accepts, reads the request and never answers."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    held: list = []
    stop = threading.Event()

    def accept() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            try:
                conn.settimeout(0.5)
                conn.recv(65536)
            except OSError:
                pass
            held.append(conn)

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield listener.getsockname()[1]
    stop.set()
    thread.join(timeout=2)
    for conn in held:
        conn.close()
    listener.close()


async def test_a_real_pooled_session_read_timeout_does_not_trip_the_breaker(
    silent_server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The effect through the session configuration the app really mounts, not a fake raise."""
    from app.services.lxd.connection import LXDConnectionManager

    monkeypatch.delenv("ORCASTRA_LXD_RETRY_READ", raising=False)
    monkeypatch.setattr(router_module, "RAW_PROXY_TIMEOUT", 5.0)
    client = _Client(requests.Session())
    client.api._api_endpoint = f"http://127.0.0.1:{silent_server}/1.0"
    client.api._timeout = (1, 1)
    LXDConnectionManager._configure_client_session(
        LXDConnectionManager.__new__(LXDConnectionManager), client
    )

    response = await _call(FakeService(client))
    assert response.status_code == 504
    assert not health.is_tripped(CLUSTER_ID)
