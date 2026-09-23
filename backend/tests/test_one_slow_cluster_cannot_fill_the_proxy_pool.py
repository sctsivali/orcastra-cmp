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
from app.routers.integrations import RawProxyRequest, raw_proxy
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
