"""A cluster that accepts TCP and then goes silent cannot park the API through raw_proxy.

A `session.get(full_url, verify=False)` with no timeout, awaited inline on the loop, parks the
worker's event-loop thread for as long as the peer stays silent, `/health` included. Four
things have to hold for that to be impossible, and this module asserts all four:

  1. the endpoint returns within its wall-clock cap
  2. the event loop keeps running while the blocked call is outstanding
  3. the blocking call does not execute on the loop thread
  4. the call carries a socket timeout, which is the only thing that ever frees the worker
     thread (cancelling the await does not stop it)
"""

import asyncio
import threading
import time
from typing import Any, Optional

import pytest
import requests

from app.routers.integrations import RawProxyRequest, raw_proxy

# The wall-clock cap the test drives the endpoint against. Kept small so the suite stays fast.
TEST_CAP_SECONDS = 1.0

# Releases a call that carries no timeout, so a missing timeout fails the suite instead of
# hanging it.
SAFETY_RELEASE_SECONDS = 3.0


class BlackholeSession:
    """A requests.Session that accepts and never answers.

    Mirrors the contract that matters: with no timeout the call blocks indefinitely, with
    timeout=(connect, read) it raises ReadTimeout after `read`.
    """

    def __init__(self, safety_release: float = SAFETY_RELEASE_SECONDS) -> None:
        self.calls: list[dict[str, Any]] = []
        self._release = threading.Event()
        threading.Timer(safety_release, self._release.set).start()

    def _block(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append({**kwargs, "_url": url, "_thread": threading.get_ident()})
        timeout = kwargs.get("timeout")
        read = timeout[1] if isinstance(timeout, (tuple, list)) else timeout
        if read is None:
            # No timeout: the calling thread is parked until something else gives up.
            self._release.wait()
            raise requests.exceptions.ReadTimeout("safety release (no timeout was passed)")
        if not self._release.wait(read):
            raise requests.exceptions.ReadTimeout(f"read timed out after {read}s")
        raise requests.exceptions.ReadTimeout("safety release")

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._block(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._block(url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> requests.Response:
        return self._block(url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> requests.Response:
        return self._block(url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> requests.Response:
        return self._block(url, **kwargs)


class _FakeApi:
    def __init__(self, session: BlackholeSession) -> None:
        self._api_endpoint = "https://blackhole.invalid:8443/1.0"
        self._timeout = (3, 8)
        self.session = session


class _FakeClient:
    def __init__(self, session: BlackholeSession) -> None:
        self.api = _FakeApi(session)


class _FakeConnectionManager:
    def __init__(self, client: _FakeClient) -> None:
        self._client = client

    def get_client(self, cluster_id: str) -> _FakeClient:
        return self._client


class FakeLXDService:
    def __init__(self, session: BlackholeSession) -> None:
        self.connection_manager = _FakeConnectionManager(_FakeClient(session))


CLUSTER_ID = "bh"

API_KEY = {
    "app_name": "qa-blackhole",
    "cluster_permissions": [
        {
            "cluster_id": CLUSTER_ID,
            "permissions": ["read", "write", "admin"],
            "projects": ["*"],
        }
    ],
}


@pytest.fixture(autouse=True)
def _reset_circuit_breaker():
    """The breaker is process-global module state.

    Every test here drives the same cluster id into failure, so without this reset the first
    test would trip the breaker and every later test would be short-circuited before the
    session was ever called.
    """
    from app.services.lxd import health

    health.clear(CLUSTER_ID)
    yield
    health.clear(CLUSTER_ID)


def _apply_test_cap(monkeypatch: pytest.MonkeyPatch) -> Optional[float]:
    """Shrink the production wall-clock cap so the suite stays fast.

    Patched on the router module rather than app.services.lxd.proxy. The router
    imports the constants by value, so patching the definition site has no effect on the name
    the endpoint reads.

    Returns the cap in force, or None when the router has no RAW_PROXY_TIMEOUT, so the test
    still runs and fails on its assertions rather than on a missing attribute.
    """
    from app.routers import integrations as router_module

    if not hasattr(router_module, "RAW_PROXY_TIMEOUT"):
        return None
    monkeypatch.setattr(router_module, "RAW_PROXY_TIMEOUT", TEST_CAP_SECONDS)
    monkeypatch.setattr(
        router_module, "PROXY_CONNECT_TIMEOUT", TEST_CAP_SECONDS, raising=False
    )
    return TEST_CAP_SECONDS


async def _heartbeat(stop: asyncio.Event, ticks: list[float], interval: float = 0.01) -> None:
    """A pure-asyncio ticker. It stops ticking exactly when the loop is parked by blocking I/O.

    This is the in-process equivalent of probing /health from outside.
    """
    while not stop.is_set():
        ticks.append(time.monotonic())
        await asyncio.sleep(interval)


async def _drive(session: BlackholeSession, method: str = "GET") -> tuple[Any, float, int]:
    """Run raw_proxy against the blackhole while a heartbeat measures loop liveness."""
    stop, ticks = asyncio.Event(), []
    beat = asyncio.create_task(_heartbeat(stop, ticks))
    await asyncio.sleep(0.05)
    before = len(ticks)

    started = time.monotonic()
    response = await raw_proxy(
        request=RawProxyRequest(
            cluster_id="bh",
            method=method,
            path="/1.0/networks",
            body={} if method in ("POST", "PUT", "PATCH") else None,
        ),
        api_key=API_KEY,
        lxd_service=FakeLXDService(session),
        _=None,
    )
    elapsed = time.monotonic() - started

    stop.set()
    await beat
    return response, elapsed, len(ticks) - before


async def test_blackholed_cluster_does_not_park_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cap = _apply_test_cap(monkeypatch) or TEST_CAP_SECONDS
    session = BlackholeSession()
    loop_thread = threading.get_ident()

    response, elapsed, ticked = await _drive(session)

    assert elapsed < cap + 1.5, (
        f"raw_proxy took {elapsed:.2f}s against a {cap}s cap: the call is not bounded"
    )
    # A nominal loop ticks ~100x/s. Requiring only 10x/s leaves headroom for a busy CI box
    # while still failing hard when the loop is parked (then it is 0).
    assert ticked >= elapsed * 10, (
        f"event loop starved: only {ticked} ticks in {elapsed:.2f}s"
    )
    assert session.calls, "the session was never called"
    assert session.calls[0]["_thread"] != loop_thread, (
        "the blocking call ran on the event loop thread"
    )
    assert session.calls[0].get("timeout") is not None, (
        "the session call carried no timeout, so the worker thread can never be freed"
    )
    assert response.status_code in (502, 503, 504), (
        f"expected a bounded upstream error, got {response.status_code}"
    )


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
async def test_every_verb_passes_a_socket_timeout(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """The call site of every verb carries a socket timeout."""
    _apply_test_cap(monkeypatch)
    session = BlackholeSession()

    await _drive(session, method=method)

    assert session.calls, f"{method}: the session was never called"
    assert session.calls[0].get("timeout") is not None, (
        f"{method}: the session call carried no timeout"
    )
