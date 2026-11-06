"""A project read for a host no cluster is registered under answers 404, not 503.

The cold-miss path cannot tell an unknown name from a worker that has not connected yet, so
GET /projects/{host} for a typo answered 503 "temporarily unavailable" and invited a retry that
could never succeed (RC5 acceptance F-06, 2026-11-05). The registry answers that question: an id
it does not hold is 404 `cluster_not_registered`; a registered but unreachable cluster stays 503,
and so does every host while the registry cannot be read.
"""

from typing import Optional

import pytest

from app.services.lxd import health
from app.services.lxd import registry as cluster_registry
from tests.test_unreachable_cluster_is_503 import HOST, _app


@pytest.fixture(autouse=True)
def _trip_breaker():
    health.record_failure(HOST, "unreachable")
    try:
        yield
    finally:
        health.clear(HOST)


def _registry(monkeypatch, value: Optional[frozenset]) -> None:
    async def _registered(fresh: bool = False):
        return value

    monkeypatch.setattr(cluster_registry, "registered_cluster_ids", _registered)


@pytest.mark.parametrize("path", [f"/projects/{HOST}", f"/projects/{HOST}/default/usage"])
def test_a_host_the_registry_does_not_hold_is_404(monkeypatch, path: str) -> None:
    from app.routers import projects

    _registry(monkeypatch, frozenset({"another-cluster"}))
    response = _app(projects).get(path)
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "cluster_not_registered"


@pytest.mark.parametrize("path", [f"/projects/{HOST}", f"/projects/{HOST}/default/usage"])
def test_a_registered_cluster_that_does_not_answer_stays_503(monkeypatch, path: str) -> None:
    from app.routers import projects

    _registry(monkeypatch, frozenset({HOST}))
    assert _app(projects).get(path).status_code == 503


def test_an_unreadable_registry_keeps_the_503(monkeypatch) -> None:
    from app.routers import projects

    _registry(monkeypatch, None)
    assert _app(projects).get(f"/projects/{HOST}").status_code == 503
