"""A slow read through the raw proxy is not an unreachable cluster.

On a MicroCloud host with about a hundred network interfaces, `GET /1.0/networks?recursion=1`
outlived the pooled client's 8 s read timeout. The timeout tripped the per-cluster breaker for 60 s,
so every raw-proxy call to that cluster, buyers' provisioning included, answered 503 "recently
unreachable" because an integrator had opened a form. A read timeout now answers 504 and leaves the
breaker alone, and so does a GET that outlives the wall-clock cap; a connect failure still trips it.
The pooled session never retries a connect (`Retry(connect=0)`), so a dead host still fails fast as
a connect error and is still recorded.
"""

import socket
import threading
import time
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
import requests
from fastapi import HTTPException
from urllib3.exceptions import MaxRetryError, ReadTimeoutError

from app.routers import integrations
from app.routers.integrations import RawProxyRequest, raw_proxy
from app.services.lxd import health
from app.services.lxd.connection import LXDConnectionManager
from tests._raw_proxy_fakes import CLUSTER_ID, WILDCARD, _clean_breaker, call_raw_proxy, fence_calls  # noqa: F401

NETWORKS = "/1.0/networks?recursion=1"


class RaisingSession:
    """Raises `error` on every verb, the way the real session surfaces a transport failure."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def _raise(self, url: str, **kwargs: Any) -> requests.Response:
        raise self.error

    get = post = put = patch = delete = _raise


class SlowSession:
    """Answers only after `seconds`, longer than the wall-clock cap the test sets."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def _slow(self, url: str, **kwargs: Any) -> requests.Response:
        time.sleep(self.seconds)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"type": "sync", "metadata": []}'
        return response

    get = post = put = patch = delete = _slow


def _pooled_read_timeout() -> requests.exceptions.ConnectionError:
    """The shape a GET read timeout takes on the pooled session, which mounts Retry(read=0)."""
    reason = ReadTimeoutError(None, "/1.0/networks", "Read timed out. (read timeout=8)")
    return requests.exceptions.ConnectionError(MaxRetryError(None, "/1.0/networks", reason=reason))


def _stalled_body() -> requests.exceptions.ConnectionError:
    """The shape a read timeout takes while the body is streaming, after the headers arrived."""
    return requests.exceptions.ConnectionError(ReadTimeoutError(None, "/1.0/networks", "Read timed out."))


async def test_a_read_timeout_answers_504_and_leaves_the_cluster_reachable() -> None:
    response, _ = await call_raw_proxy(
        WILDCARD, "GET", NETWORKS,
        session=RaisingSession(requests.exceptions.ReadTimeout("Read timed out. (read timeout=8)")),
    )
    assert response.status_code == 504
    assert "Read timed out" in (response.error or "")
    assert not health.is_tripped(CLUSTER_ID)


@pytest.mark.parametrize("error", [_pooled_read_timeout(), _stalled_body()], ids=["pooled-get", "stalled-body"])
async def test_every_read_timeout_shape_answers_504_and_leaves_the_cluster_reachable(error: Exception) -> None:
    response, _ = await call_raw_proxy(WILDCARD, "GET", NETWORKS, session=RaisingSession(error))
    assert response.status_code == 504
    assert not health.is_tripped(CLUSTER_ID)


async def test_a_write_whose_answer_timed_out_answers_504_too(fence_calls) -> None:
    response, _ = await call_raw_proxy(WILDCARD, "POST", "/1.0/networks", session=RaisingSession(_pooled_read_timeout()))
    assert response.status_code == 504
    assert not health.is_tripped(CLUSTER_ID)


async def test_a_read_past_the_wall_clock_cap_answers_504_and_leaves_the_cluster_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(integrations, "RAW_PROXY_TIMEOUT", 0.2)
    response, _ = await call_raw_proxy(WILDCARD, "GET", NETWORKS, session=SlowSession(1.0))
    assert response.status_code == 504
    assert not health.is_tripped(CLUSTER_ID)


async def test_a_write_past_the_wall_clock_cap_still_rests_the_cluster(
    fence_calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unchanged on purpose: a write that outlived the cap has an unknown outcome, so callers back off."""
    monkeypatch.setattr(integrations, "RAW_PROXY_TIMEOUT", 0.2)
    response, _ = await call_raw_proxy(WILDCARD, "POST", "/1.0/networks", session=SlowSession(1.0))
    assert response.status_code == 504
    assert health.is_tripped(CLUSTER_ID)


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
    client = SimpleNamespace(
        api=SimpleNamespace(
            _api_endpoint=f"http://127.0.0.1:{silent_server}/1.0", _timeout=(1, 1), session=requests.Session()
        )
    )
    LXDConnectionManager._configure_client_session(LXDConnectionManager.__new__(LXDConnectionManager), client)
    monkeypatch.setattr(integrations, "RAW_PROXY_TIMEOUT", 5.0)
    service = SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda cluster_id: client))

    response = await raw_proxy(
        request=RawProxyRequest(cluster_id=CLUSTER_ID, method="GET", path=NETWORKS),
        api_key=WILDCARD,
        lxd_service=service,
        _=None,
    )
    assert response.status_code == 504
    assert not health.is_tripped(CLUSTER_ID)


async def test_a_connect_failure_still_trips_the_breaker() -> None:
    response, _ = await call_raw_proxy(
        WILDCARD, "GET", NETWORKS, session=RaisingSession(requests.exceptions.ConnectionError("Connection refused")),
    )
    assert response.status_code == 500
    assert health.is_tripped(CLUSTER_ID)


async def test_a_connect_timeout_still_trips_the_breaker() -> None:
    await call_raw_proxy(
        WILDCARD, "GET", NETWORKS, session=RaisingSession(requests.exceptions.ConnectTimeout("connect timeout=3")),
    )
    assert health.is_tripped(CLUSTER_ID)


async def _call_without_a_client() -> HTTPException:
    service = SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda cluster_id: None))
    with pytest.raises(HTTPException) as caught:
        await raw_proxy(
            request=RawProxyRequest(cluster_id=CLUSTER_ID, method="GET", path=NETWORKS),
            api_key=WILDCARD,
            lxd_service=service,
            _=None,
        )
    return caught.value


async def test_a_cluster_that_did_not_answer_names_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "get_reason", lambda cluster_id: "certificate not trusted")
    refusal = await _call_without_a_client()
    assert refusal.status_code == 503
    assert "certificate not trusted" in str(refusal.detail)


async def test_a_cluster_that_did_not_answer_without_a_reason_says_so_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "get_reason", lambda cluster_id: None)
    refusal = await _call_without_a_client()
    assert refusal.status_code == 503
    assert refusal.detail == f"Could not connect to {CLUSTER_ID}"
