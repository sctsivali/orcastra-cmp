"""A worker that has never talked to a cluster must connect instead of answering 503.

`connection_manager.get_client` is memory-only. With several workers only the one that
served a registration holds a client, so without a connect the same GET would answer 200 or
503 depending on which worker took it. `connection.py` does not trip the breaker on that
branch, because it is no evidence about the cluster, and names ensure_connection as the
repair.

A cluster the breaker has tripped must still cost nothing, so the tripped case asserts that no
Vault read happened at all rather than only that the answer was None.
"""

import asyncio
import unittest
from typing import Any, List, Optional

from app.services.lxd import health
from app.services.lxd.lazy_client import client_for, connect_or_get_client


CLUSTER = "qa-cold-worker"


class _Conn:
    """A connection manager for a worker that has never connected to this cluster."""

    def __init__(self, connect_succeeds: bool = True) -> None:
        self.clients: dict = {}
        self.connect_succeeds = connect_succeeds
        self.calls: List[str] = []

    def get_client(self, cluster_id: str, *a: Any, **kw: Any) -> Optional[object]:
        self.calls.append("get_client")
        return self.clients.get(cluster_id)

    def connect_with_vault_cert(self, cluster_id: str, **kw: Any) -> Optional[object]:
        self.calls.append("connect")
        if not self.connect_succeeds:
            return None
        client = object()
        self.clients[cluster_id] = client
        return client


class _Vault:
    def __init__(self) -> None:
        self.reads: List[str] = []

    def vault_read(self, path: str, use_cache: bool = True) -> dict:
        self.reads.append(path)
        return {"endpoint": "https://10.9.9.9:8443"}


class _Certs:
    def __init__(self) -> None:
        self.vault = _Vault()
        self.cert_reads = 0

    def get_cluster_certificate(self, cluster_id: str) -> dict:
        self.cert_reads += 1
        return {"client.crt": "crt", "client.key": "key"}


def _service(connect_succeeds: bool = True):
    from app.services.lxd.lxd_service import LXDService

    svc = LXDService.__new__(LXDService)
    svc.connection_manager = _Conn(connect_succeeds)
    svc.cert_manager = _Certs()
    return svc


class ColdWorkerConnects(unittest.TestCase):
    def setUp(self) -> None:
        health.clear(CLUSTER)

    tearDown = setUp

    def test_a_cold_worker_gets_a_client(self):
        svc = _service()
        client = connect_or_get_client(svc, CLUSTER)
        self.assertIsNotNone(client, "the whole defect: three workers out of four said 503")
        self.assertIn("connect", svc.connection_manager.calls)

    def test_a_warm_worker_does_not_pay_vault(self):
        svc = _service()
        svc.connection_manager.clients[CLUSTER] = object()
        client = connect_or_get_client(svc, CLUSTER)
        self.assertIsNotNone(client)
        self.assertEqual([], svc.cert_manager.vault.reads)
        self.assertEqual(0, svc.cert_manager.cert_reads)

    def test_a_tripped_cluster_costs_nothing(self):
        health.record_failure(CLUSTER, "connection refused")
        svc = _service()
        self.assertIsNone(connect_or_get_client(svc, CLUSTER))
        self.assertEqual(
            [],
            svc.cert_manager.vault.reads,
            "the breaker must be consulted BEFORE Vault, not after",
        )
        self.assertEqual(0, svc.cert_manager.cert_reads)
        self.assertNotIn("connect", svc.connection_manager.calls)

    def test_a_refused_connect_answers_none_rather_than_a_broken_client(self):
        svc = _service(connect_succeeds=False)
        self.assertIsNone(connect_or_get_client(svc, CLUSTER))

    def test_an_out_of_scope_cluster_is_refused_without_touching_vault(self):
        """The org-scope guard inside ensure_connection holds because run_blocking carries
        the context into the worker thread."""
        from app.core import org_scope

        svc = _service()
        token = org_scope._allowed_clusters_var.set({"some-other-cluster"})
        try:
            self.assertIsNone(connect_or_get_client(svc, CLUSTER))
        finally:
            org_scope._allowed_clusters_var.reset(token)
        self.assertEqual([], svc.cert_manager.vault.reads)

    def test_the_async_wrapper_returns_the_same_client(self):
        svc = _service()
        client = asyncio.run(client_for(svc, CLUSTER))
        self.assertIsNotNone(client)

    def test_the_async_wrapper_carries_the_callers_scope_into_the_thread(self):
        """An out-of-scope cluster is refused although the check runs in a worker thread."""
        from app.core import org_scope

        svc = _service()

        async def go():
            org_scope._allowed_clusters_var.set({"some-other-cluster"})
            return await client_for(svc, CLUSTER)

        self.assertIsNone(asyncio.run(go()))
        self.assertEqual([], svc.cert_manager.vault.reads)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
