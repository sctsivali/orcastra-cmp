"""get_client returns the client it validated, even if another thread evicted it meanwhile.

It validated `self._clients[cluster_id]` and then read `self._clients[cluster_id]` again to return
it. Validation is a live GET /1.0 (about a second on central-1), and the pool is shared by every
thread in the worker: a concurrent reconnect, the healer or the dead-cluster probe can call
cleanup_connection in between. The second read then raised KeyError, and on dev (2026-11-04) the
system warnings feed reported central-1 as failed for that read. The pooled client had just
answered, so the right answer is that client.
"""

from __future__ import annotations

import pytest

from app.services.lxd import health
from app.services.lxd.connection import LXDConnectionManager

CLUSTER = "central-1"


@pytest.fixture
def manager(monkeypatch):
    health.clear(CLUSTER)
    manager = LXDConnectionManager()
    client = object()
    manager._clients[CLUSTER] = client

    def validate_while_another_thread_evicts(validated, cluster_id):
        assert validated is client
        manager._clients.pop(cluster_id, None)
        return True

    monkeypatch.setattr(manager, "_validate_connection", validate_while_another_thread_evicts)
    return manager, client


def test_the_validated_client_is_returned(manager):
    manager, client = manager
    assert manager.get_client(CLUSTER) is client


def test_a_pin_check_reads_the_same_client(manager, monkeypatch):
    from app.services.lxd import connection

    manager, client = manager
    seen = []
    monkeypatch.setattr(connection, "_client_adapter_fingerprint", lambda c: seen.append(c) or "aa")
    monkeypatch.setattr(connection, "normalize_server_fingerprint", lambda f: "aa")
    monkeypatch.setattr(connection, "require_client_server_fingerprint", lambda c, f: seen.append(c))
    assert manager.get_client(CLUSTER, expected_server_fingerprint="aa") is client
    assert seen == [client, client]
