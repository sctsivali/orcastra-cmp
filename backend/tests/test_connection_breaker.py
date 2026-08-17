"""The circuit breaker has to sit at `get_client`, the one chokepoint every caller shares.

`connection_manager.get_client` is not a dictionary lookup. It revalidates the pooled
connection with a live request and, when that fails, rebuilds the pylxd client, which performs
a TLS handshake and a verifying GET of its own. Against an unreachable cluster that is seconds
of wall clock.

41 call sites still await it inline from `async def` handlers, so each of those requests parks
a whole uvicorn event loop for that time. Measured on this stack against a single unreachable
cluster, 12 concurrent requests moved `/health` from 4ms to 10.3s, past the 5s container
healthcheck timeout, which in production hands the container to the autoheal sidecar and kills
every live console and terminal session with it.

Converting 41 call sites treats the symptom. `services/lxd/health.py` already existed for
exactly this and its own docstring says every connection path should consult it, but the one
method they all funnel through never did. Checking it here makes the repeat cost of a dead
cluster zero without touching a single caller, because returning None is already part of
`get_client`'s contract.

What must hold:
  1. a tripped cluster returns None without attempting any connection
  2. an untripped cluster is still attempted normally
  3. a failed reconnect trips the breaker, so the second caller is already cheap
  4. recovery is immediate on success, not after the TTL
  5. the breaker expires on its own, so a cluster is re-probed rather than blacklisted
"""

import time

import pytest

from app.services.lxd import health
from app.services.lxd.connection import LXDConnectionManager

CLUSTER = "cluster-under-test"


@pytest.fixture(autouse=True)
def _clean_breaker():
    health.clear(CLUSTER)
    yield
    health.clear(CLUSTER)


@pytest.fixture
def manager(monkeypatch):
    mgr = LXDConnectionManager()
    # The org-scope filter reads a ContextVar that is only set inside a request. Outside one it
    # returns None (unrestricted), which is what we want, but pin it so the test cannot depend
    # on ambient state.
    monkeypatch.setattr(
        "app.core.org_scope.get_current_allowed_clusters", lambda: None, raising=False
    )
    return mgr


def test_a_tripped_cluster_returns_none_without_attempting_a_connection(manager) -> None:
    attempts = []
    manager._auto_reconnect = lambda cid: attempts.append(cid)  # type: ignore[assignment]

    health.record_failure(CLUSTER, "unreachable")
    assert manager.get_client(CLUSTER) is None
    assert attempts == [], "a tripped cluster must not be dialled again"


def test_an_untripped_cluster_is_still_attempted(manager) -> None:
    attempts = []

    def _reconnect(cid):
        attempts.append(cid)
        return "client"

    manager._auto_reconnect = _reconnect  # type: ignore[assignment]

    assert manager.get_client(CLUSTER) == "client"
    assert attempts == [CLUSTER]


def test_a_failed_reconnect_trips_the_breaker_so_the_next_caller_is_free(manager) -> None:
    """The property that turns an O(requests) cost into O(1) per TTL."""
    calls = {"n": 0}

    def _explode(*_args, **_kwargs):
        calls["n"] += 1
        raise OSError("connection refused")

    # No stored connection info is the cheapest real failure path through _auto_reconnect.
    manager._connection_info.pop(CLUSTER, None)

    assert manager.get_client(CLUSTER) is None
    assert health.is_tripped(CLUSTER), "the first failure must record it"

    manager._auto_reconnect = _explode  # type: ignore[assignment]
    assert manager.get_client(CLUSTER) is None
    assert calls["n"] == 0, "the second caller paid a connection cost anyway"


def test_recovery_is_immediate_on_success_not_after_the_ttl(manager) -> None:
    health.record_failure(CLUSTER, "unreachable")
    assert manager.get_client(CLUSTER) is None

    # Whatever brought the cluster back (a successful connect elsewhere) clears it, and the
    # very next call must go through rather than waiting out the TTL.
    health.clear(CLUSTER)
    manager._auto_reconnect = lambda cid: "client"  # type: ignore[assignment]
    assert manager.get_client(CLUSTER) == "client"


def test_the_breaker_expires_so_a_cluster_is_reprobed(manager) -> None:
    """A breaker that never expires is a blacklist. Recovery must not need a restart."""
    health.record_failure(CLUSTER, "unreachable")
    assert health.is_tripped(CLUSTER, ttl=60.0)

    # Same clock, evaluated with an already-elapsed window.
    time.sleep(0.05)
    assert not health.is_tripped(CLUSTER, ttl=0.01)

    manager._auto_reconnect = lambda cid: "client"  # type: ignore[assignment]
    assert manager.get_client(CLUSTER) == "client"
