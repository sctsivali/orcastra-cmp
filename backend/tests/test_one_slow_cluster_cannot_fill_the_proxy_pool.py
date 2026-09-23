"""One slow cluster may hold only its share of the raw-proxy pool, and an operation wait is capped.

A read timeout no longer trips the breaker, so reads to a slow but reachable cluster kept coming
and each held a proxy thread for the whole socket timeout. Nothing bounded how many one cluster
could hold, so a slow cluster could fill the shared pool and every other cluster's calls and every
concurrent /sync answered 503. An operation wait with a long `timeout` held a thread past the read
timeout for a 504.
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


async def test_the_slot_is_given_back_after_the_call() -> None:
    session = _Session(None)
    service = _Service({"cluster-slow": session, "cluster-fast": _Session(None)})
    for _ in range(3):
        assert (await _call(service, "cluster-slow")).status_code == 200
    assert len(session.calls) == 3


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
    slots = integrations._cluster_slots[(id(asyncio.get_running_loop()), "cluster-slow")]
    assert not slots.locked()


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


async def test_a_caller_that_disconnects_while_queued_never_reaches_the_cluster(monkeypatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    tiny = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(integrations, "proxy_executor", tiny)
    ran: List[str] = []
    blocker = threading.Event()
    tiny.submit(blocker.wait, 5)
    task = asyncio.create_task(integrations._run_for_cluster(
        "cluster-slow", lambda: ran.append("sent"), timeout=5, label="probe", executor=tiny, max_in_flight=8))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    blocker.set()
    tiny.shutdown(wait=True)
    assert ran == [], "the client left, and its queued request still went out"
    await asyncio.sleep(0.05)
    assert not integrations._cluster_slots[(id(asyncio.get_running_loop()), "cluster-slow")].locked()


def test_a_cluster_may_use_the_whole_pool_by_default(monkeypatch) -> None:
    """The integrator fans out one read per project, so a half-pool share answered a wide fan-out busy."""
    import importlib

    from app.services.lxd import proxy

    monkeypatch.delenv("ORCASTRA_CLUSTER_PROXY_SLOTS", raising=False)
    fresh = importlib.reload(proxy)
    try:
        assert fresh.CLUSTER_PROXY_SLOTS == fresh.PROXY_POOL_WORKERS
    finally:
        importlib.reload(proxy)


async def test_a_request_takes_one_slot_not_two() -> None:
    """The client lookup no longer queues for a slot, so a request waits once, for its LXD call."""
    gate = threading.Event()
    slow = _Session(gate)
    lookups: List[str] = []
    clients = {c: _Client(s, c) for c, s in (("cluster-slow", slow), ("cluster-fast", _Session(None)))}
    service = type("Svc", (), {"connection_manager": type("CM", (), {
        "get_client": staticmethod(lambda c: lookups.append(c) or clients[c])})()})()
    first = asyncio.create_task(_call(service, "cluster-slow"))
    while not slow.calls:
        await asyncio.sleep(0.01)
    try:
        with pytest.raises(HTTPException) as caught:
            await _call(service, "cluster-slow")
        assert caught.value.status_code == 503 and "busy" in caught.value.detail
        assert lookups == ["cluster-slow", "cluster-slow"], "the second lookup waited on the slot"
    finally:
        gate.set()
        await first
