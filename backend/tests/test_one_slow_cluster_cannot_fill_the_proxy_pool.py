"""One slow cluster may hold only its share of the raw-proxy pool, and an operation wait is capped.

A read timeout no longer trips the breaker, so reads to a slow but reachable cluster kept coming
and each held a proxy thread for the whole socket timeout. Nothing bounded how many one cluster
could hold, so a slow cluster could fill the shared pool and every other cluster's calls and every
concurrent /sync answered 503. An operation wait with a long `timeout` held a thread past the read
timeout for a 504.

A request takes its slot before the client lookup and keeps it through the call, since a lookup to
a slow cluster holds a thread as well. The shipped share leaves two threads of the pool free.
"""

import asyncio
import threading
from typing import Any, List

import pytest
import requests
from fastapi import HTTPException

from app.routers import integrations
from app.routers.integrations import ProxyRequest, RawProxyRequest, proxy_operation, raw_proxy
from app.services.lxd import health

KEY = {
    "app_name": "qa-proxy",
    "cluster_permissions": [
        {"cluster_id": c, "permissions": ["read", "write", "admin"], "projects": ["*"]}
        for c in ("cluster-slow", "cluster-fast")
    ],
}


class _Session:
    def __init__(self, gate: "threading.Event | None") -> None:
        self.gate = gate
        self.calls: List[str] = []

    def _record(self, url: str, **_kwargs: Any) -> requests.Response:
        self.calls.append(url)
        if self.gate is not None:
            self.gate.wait(5)
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"type": "sync", "metadata": {}}'
        return response

    get = post = put = patch = delete = _record


class _Client:
    def __init__(self, session: _Session, cluster: str) -> None:
        self.api = type("Api", (), {"_api_endpoint": f"https://{cluster}.example.test:8443/1.0",
                                     "_timeout": (3, 8), "session": session})()


class _Service:
    def __init__(self, sessions: dict) -> None:
        clients = {c: _Client(s, c) for c, s in sessions.items()}
        self.connection_manager = type("CM", (), {"get_client": staticmethod(lambda c: clients[c])})()


@pytest.fixture(autouse=True)
def one_slot(monkeypatch):
    monkeypatch.setattr(integrations, "CLUSTER_PROXY_SLOTS", 1)
    monkeypatch.setattr(integrations, "CLUSTER_SLOT_WAIT_SECONDS", 0.3)
    integrations._cluster_slots.clear()
    for cluster in ("cluster-slow", "cluster-fast"):
        health.clear(cluster)
    yield
    integrations._cluster_slots.clear()


async def _share_is_whole(cluster: str = "cluster-slow") -> None:
    """Every slot is back and none came back twice: a double release would widen the share for good."""
    key = (id(asyncio.get_running_loop()), cluster)
    for _ in range(100):
        slots = integrations._cluster_slots.get(key)
        if slots is None or slots._value == integrations.CLUSTER_PROXY_SLOTS:
            break
        await asyncio.sleep(0.01)
    assert slots is None or slots._value == integrations.CLUSTER_PROXY_SLOTS, (
        f"share is {slots._value}, expected {integrations.CLUSTER_PROXY_SLOTS}")


def _call(service: _Service, cluster: str, path: str = "/1.0/instances", method: str = "GET") -> Any:
    return raw_proxy(
        request=RawProxyRequest(cluster_id=cluster, method=method, path=path),
        api_key=KEY, lxd_service=service, _=None,
    )


async def test_a_busy_cluster_answers_503_without_touching_the_pool_and_others_go_on() -> None:
    gate = threading.Event()
    slow, fast = _Session(gate), _Session(None)
    service = _Service({"cluster-slow": slow, "cluster-fast": fast})
    first = asyncio.create_task(_call(service, "cluster-slow"))
    while not slow.calls:
        await asyncio.sleep(0.01)
    try:
        with pytest.raises(HTTPException) as caught:
            await _call(service, "cluster-slow")
        assert caught.value.status_code == 503
        assert "busy" in caught.value.detail
        assert caught.value.headers.get("Retry-After") == "2"
        assert len(slow.calls) == 1, "the refused call reached the cluster"
        answer = await _call(service, "cluster-fast")
        assert answer.status_code == 200
    finally:
        gate.set()
        assert (await first).status_code == 200
    assert not health.is_tripped("cluster-slow")
    await _share_is_whole()


async def test_the_slot_is_given_back_after_the_call() -> None:
    session = _Session(None)
    service = _Service({"cluster-slow": session, "cluster-fast": _Session(None)})
    for _ in range(3):
        assert (await _call(service, "cluster-slow")).status_code == 200
    assert len(session.calls) == 3
    await _share_is_whole()


@pytest.mark.parametrize(
    "path,sent",
    [
        ("/1.0/operations/abc/wait?timeout=55", "timeout=5"),
        ("/1.0/operations/abc/wait", "timeout=5"),
        ("/1.0/operations/abc/wait?timeout=-1", "timeout=5"),
        ("/1.0/operations/abc/wait?timeout=2", "timeout=2"),
        ("/1.0/operations/abc/wait?timeout=junk", "timeout=5"),
    ],
)
async def test_an_operation_wait_is_capped_below_the_read_timeout(path: str, sent: str) -> None:
    session = _Session(None)
    await _call(_Service({"cluster-slow": session, "cluster-fast": _Session(None)}), "cluster-slow", path)
    assert session.calls[0].endswith("/operations/abc/wait?" + sent)


async def test_other_paths_and_verbs_keep_their_timeout() -> None:
    session = _Session(None)
    service = _Service({"cluster-slow": session, "cluster-fast": _Session(None)})
    await _call(service, "cluster-slow", "/1.0/instances?timeout=55")
    await _call(service, "cluster-slow", "/1.0/operations/abc/wait?timeout=55", method="DELETE")
    assert session.calls[0].endswith("/instances?timeout=55")
    assert session.calls[1].endswith("/operations/abc/wait?timeout=55")


async def test_a_healthy_burst_waits_its_turn_instead_of_failing() -> None:
    session = _Session(None)
    service = _Service({"cluster-slow": session, "cluster-fast": _Session(None)})
    answers = await asyncio.gather(*[_call(service, "cluster-slow") for _ in range(6)])
    assert [a.status_code for a in answers] == [200] * 6
    assert len(session.calls) == 6


async def test_the_slot_stays_taken_until_the_thread_finishes(monkeypatch) -> None:
    monkeypatch.setattr(integrations, "RAW_PROXY_TIMEOUT", 0.2)
    gate = threading.Event()
    slow = _Session(gate)
    service = _Service({"cluster-slow": slow, "cluster-fast": _Session(None)})
    first = await _call(service, "cluster-slow")
    assert first.status_code == 504, "the await gave up while the thread still reads"
    with pytest.raises(HTTPException) as caught:
        await _call(service, "cluster-slow")
    assert caught.value.status_code == 503
    gate.set()
    for _ in range(50):
        if not integrations._cluster_slots[(id(asyncio.get_running_loop()), "cluster-slow")].locked():
            break
        await asyncio.sleep(0.02)
    slow.gate = None
    assert (await _call(service, "cluster-slow")).status_code == 200
    await _share_is_whole()


async def test_an_infinite_wait_timeout_is_capped_not_a_500() -> None:
    session = _Session(None)
    service = _Service({"cluster-slow": session, "cluster-fast": _Session(None)})
    for value in ("inf", "1e309", "nan"):
        answer = await _call(service, "cluster-slow", f"/1.0/operations/abc/wait?timeout={value}")
        assert answer.status_code == 200
    assert all(url.endswith("wait?timeout=5") for url in session.calls)



async def test_a_call_that_gave_up_while_queued_never_reaches_the_cluster(monkeypatch) -> None:
    """The client lookup runs at once, then other work takes the only thread, so the request's own
    job waits in the queue past its cap. When the thread frees up, that job must not run."""
    from concurrent.futures import ThreadPoolExecutor

    tiny = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(integrations, "proxy_executor", tiny)
    monkeypatch.setattr(integrations, "RAW_PROXY_TIMEOUT", 0.2)
    blocker = threading.Event()
    queued = _Session(None)
    client = _Client(queued, "cluster-slow")

    def get_client(_cluster: str) -> Any:
        tiny.submit(blocker.wait, 5)  # queued ahead of the request's job
        return client

    service = type("Svc", (), {"connection_manager": type("CM", (), {"get_client": staticmethod(get_client)})()})()
    try:
        with pytest.raises(HTTPException) as caught:
            await _call(service, "cluster-slow", "/1.0/instances", method="POST")
        assert caught.value.status_code == 503
        assert "not sent" in caught.value.detail, "the caller must learn the request never left"
        assert caught.value.headers.get("Retry-After") == "2"
    finally:
        blocker.set()
    tiny.shutdown(wait=True)
    assert queued.calls == [], "a request whose caller already gave up reached the cluster"
    assert not health.is_tripped("cluster-slow"), "a cluster that was never contacted cannot be unreachable"
    await asyncio.sleep(0.05)  # the release is scheduled onto the loop
    await _share_is_whole()


async def test_a_proxy_operation_that_gave_up_while_queued_is_never_sent(monkeypatch) -> None:
    """The operation route shares the pool. A start queued past its cap must not run later."""
    from concurrent.futures import ThreadPoolExecutor

    tiny = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(integrations, "proxy_executor", tiny)
    monkeypatch.setattr(integrations, "PROXY_OP_TIMEOUT", 0.2)
    ran: List[str] = []
    monkeypatch.setattr(integrations, "_execute_proxy_operation", lambda *a, **_k: ran.append(a[2]) or {"ok": True})
    blocker = threading.Event()
    tiny.submit(blocker.wait, 5)
    try:
        answer = await proxy_operation(
            request=ProxyRequest(cluster_id="cluster-slow", operation="start_instance", params={"name": "vm"}),
            api_key=KEY, lxd_service=_Service({"cluster-slow": _Session(None), "cluster-fast": _Session(None)}), _=None,
        )
        assert answer.success is False and "not sent" in answer.error
    finally:
        blocker.set()
    tiny.shutdown(wait=True)
    assert ran == [], "an operation whose caller already gave up reached the cluster"
    assert not health.is_tripped("cluster-slow")


async def test_a_proxy_operation_that_started_still_runs_to_its_answer(monkeypatch) -> None:
    ran: List[str] = []
    monkeypatch.setattr(integrations, "_execute_proxy_operation", lambda *a, **_k: ran.append(a[2]) or {"ok": True})
    answer = await proxy_operation(
        request=ProxyRequest(cluster_id="cluster-slow", operation="start_instance", params={"name": "vm"}),
        api_key=KEY, lxd_service=_Service({"cluster-slow": _Session(None), "cluster-fast": _Session(None)}), _=None,
    )
    assert answer.success is True and ran == ["start_instance"]


async def test_a_client_lookup_that_gave_up_while_queued_is_not_a_cluster_failure(monkeypatch) -> None:
    """The get_client stage shares the pool. Dropped there, it is 'not sent', never 'not reachable'."""
    from concurrent.futures import ThreadPoolExecutor

    tiny = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(integrations, "proxy_executor", tiny)
    monkeypatch.setattr(integrations, "PROXY_CONNECT_TIMEOUT", 0.2)
    looked_up: List[str] = []
    service = type("Svc", (), {"connection_manager": type("CM", (), {
        "get_client": staticmethod(lambda c: looked_up.append(c) or _Client(_Session(None), c))})()})()
    blocker = threading.Event()
    tiny.submit(blocker.wait, 5)
    try:
        with pytest.raises(HTTPException) as caught:
            await _call(service, "cluster-slow")
        assert caught.value.status_code == 503 and "not sent" in caught.value.detail
    finally:
        blocker.set()
    tiny.shutdown(wait=True)
    assert looked_up == [], "a lookup whose caller already gave up still ran"
    assert not health.is_tripped("cluster-slow")
    await _share_is_whole()


async def test_a_caller_that_disconnects_while_queued_never_reaches_the_cluster(monkeypatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    tiny = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(integrations, "proxy_executor", tiny)
    session = _Session(None)
    looked_up: List[str] = []
    service = type("Svc", (), {"connection_manager": type("CM", (), {
        "get_client": staticmethod(lambda c: looked_up.append(c) or _Client(session, c))})()})()
    blocker = threading.Event()
    tiny.submit(blocker.wait, 5)
    task = asyncio.create_task(_call(service, "cluster-slow", method="POST"))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    blocker.set()
    tiny.shutdown(wait=True)
    assert looked_up == [] and session.calls == [], "the client left, and its queued request still went out"
    await _share_is_whole()


def test_a_cluster_leaves_two_threads_for_the_others_by_default(monkeypatch) -> None:
    """The integrator fans out one read per project, so the share stays wide, but never the whole pool."""
    import importlib

    from app.services.lxd import proxy

    monkeypatch.delenv("ORCASTRA_CLUSTER_PROXY_SLOTS", raising=False)
    fresh = importlib.reload(proxy)
    try:
        assert fresh.CLUSTER_PROXY_SLOTS == max(1, fresh.PROXY_POOL_WORKERS - 2)
    finally:
        importlib.reload(proxy)


async def test_a_request_takes_its_slot_before_its_lookup() -> None:
    """A lookup to a slow cluster holds a thread, so it counts against the share, and a request waits once."""
    gate = threading.Event()
    lookups: List[str] = []

    def get_client(cluster: str) -> Any:
        lookups.append(cluster)
        gate.wait(5)
        return _Client(_Session(None), cluster)

    service = type("Svc", (), {"connection_manager": type("CM", (), {"get_client": staticmethod(get_client)})()})()
    first = asyncio.create_task(_call(service, "cluster-slow"))
    while not lookups:
        await asyncio.sleep(0.01)
    try:
        with pytest.raises(HTTPException) as caught:
            await _call(service, "cluster-slow")
        assert caught.value.status_code == 503 and "busy" in caught.value.detail
        assert lookups == ["cluster-slow"], "the second lookup ran past a full share"
    finally:
        gate.set()
        assert (await first).status_code == 200
    await _share_is_whole()


async def test_at_the_shipped_share_a_stalled_cluster_leaves_threads_for_the_others(monkeypatch) -> None:
    """The shipped pool and share, not one slot: a stalled fan-out holds its share and no more."""
    from concurrent.futures import ThreadPoolExecutor

    from app.services.lxd import proxy

    pool = ThreadPoolExecutor(max_workers=proxy.PROXY_POOL_WORKERS)
    monkeypatch.setattr(integrations, "proxy_executor", pool)
    monkeypatch.setattr(integrations, "CLUSTER_PROXY_SLOTS", proxy.CLUSTER_PROXY_SLOTS)
    gate = threading.Event()
    slow, fast = _Session(gate), _Session(None)
    service = _Service({"cluster-slow": slow, "cluster-fast": fast})
    wide = proxy.PROXY_POOL_WORKERS + 2
    stalled = [asyncio.create_task(_call(service, "cluster-slow")) for _ in range(wide)]
    try:
        while len(slow.calls) < proxy.CLUSTER_PROXY_SLOTS:
            await asyncio.sleep(0.01)
        answer = await asyncio.wait_for(_call(service, "cluster-fast"), 2)
        assert answer.status_code == 200, "another cluster got no thread while one cluster stalled"
        await asyncio.sleep(integrations.CLUSTER_SLOT_WAIT_SECONDS + 0.2)  # the rest give up waiting
        assert len(slow.calls) == proxy.CLUSTER_PROXY_SLOTS
    finally:
        gate.set()
        outcomes = await asyncio.gather(*stalled, return_exceptions=True)
        pool.shutdown(wait=True)
    busy = [o for o in outcomes if isinstance(o, HTTPException)]
    assert busy and all(o.status_code == 503 and "busy" in o.detail for o in busy)
    assert len(busy) + len(slow.calls) == wide
    await _share_is_whole()
