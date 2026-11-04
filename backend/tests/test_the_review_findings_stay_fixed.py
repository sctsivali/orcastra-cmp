"""Findings of the 2026-11-04 security review of the cold-miss round, pinned.

F1: run_blocking's max_in_flight refuses at once, so a gather over more clusters than the cap
dropped every cluster past it (OrcaHub totals past 4 clusters, regions past 10). Slots are now
waited for. F2: a late read whose memo entry was evicted must not schedule a rebuild. F3: a TLS
failure in the connectivity probe, a pin mismatch included, is a certificate problem, not an
offline host. And a forced refresh never answers a cluster from a late read's stand-in.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import requests

from app.core import swr
from app.routers import integrations
from app.services.lxd import fanout
from app.services.lxd.fanout import LateRead, NodeRead, gather_clusters, run_in_slot
from app.services.lxd_certs import connectivity

POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="test_review")
RESOURCES = SimpleNamespace(cpu_total=1, memory_total=1, storage_total=1, gpu_total=0, gpu_cards_total=0)


def test_slots_queue_instead_of_refusing():
    peak = {"now": 0, "max": 0}
    lock = threading.Lock()

    def work(i):
        with lock:
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
        time.sleep(0.05)
        with lock:
            peak["now"] -= 1
        return i

    async def scenario():
        return await asyncio.gather(
            *(run_in_slot("test", 2, work, i, timeout=5, label=f"t{i}", executor=POOL) for i in range(6))
        )

    assert asyncio.run(scenario()) == list(range(6))
    assert peak["max"] == 2


def test_a_key_granting_more_clusters_than_the_pool_counts_them_all():
    service = SimpleNamespace(check_node_status=lambda cid: SimpleNamespace(connected=True, resources=RESOURCES))
    clusters = {f"c{i}" for i in range(integrations._TOTALS_WORKERS + 3)}
    totals = asyncio.run(integrations._integration_hardware_totals(service, clusters))
    assert totals["cpu"] == len(clusters)


def test_an_evicted_late_read_schedules_no_rebuild(monkeypatch):
    scheduled = []
    monkeypatch.setattr(swr, "capture_refresh", lambda: (lambda: scheduled.append(1)))

    def slow(node):
        time.sleep(0.2)
        return NodeRead(rows=[node])

    async def scenario():
        await gather_clusters(["slow"], slow, executor=POOL, label="t", memo_key="t", wall=0.05)
        fanout._late.clear()  # evicted before it finished
        await asyncio.sleep(0.35)

    asyncio.run(scenario())
    assert scheduled == []


def test_a_forced_refresh_never_answers_from_a_stand_in():
    calls = []

    def slow(node):
        calls.append(node)
        time.sleep(0.15 if len(calls) == 1 else 0)
        return NodeRead(rows=[len(calls)])

    async def scenario():
        first = await gather_clusters(["slow"], slow, executor=POOL, label="t", memo_key="t", wall=0.05)
        assert isinstance(first[0], LateRead)
        await asyncio.sleep(0.3)
        token = swr._forced.set(True)
        try:
            forced = await gather_clusters(["slow"], slow, executor=POOL, label="t", memo_key="t", wall=1)
        finally:
            swr._forced.reset(token)
        assert forced[0].rows == [2]

    asyncio.run(scenario())
    assert calls == ["slow", "slow"]


def test_a_tls_failure_in_the_probe_is_a_certificate_error_not_an_outage(monkeypatch):
    def handshake_refused(url, **kwargs):
        raise requests.exceptions.SSLError("certificate verify failed: fingerprint mismatch")

    monkeypatch.setattr(connectivity.requests, "get", handshake_refused)
    monkeypatch.setattr(connectivity.PinState, "from_metadata", staticmethod(lambda metadata: None))
    from app.services.lxd_cert_manager import LXDCertificateManager

    manager = LXDCertificateManager.__new__(LXDCertificateManager)
    manager.clusters_path = "secret/data/clusters"
    manager.vault = type("V", (), {"vault_read": lambda self, path: {"endpoint": "https://nuc2.invalid:8443"}})()
    manager.get_cluster_certificate = lambda cluster_id: {"client.crt": "c", "client.key": "k"}
    recorded = []
    with patch("app.services.lxd_certs.connectivity.cert_files.materialize", return_value=("/c", "/k")), \
         patch("app.services.lxd.health.is_tripped", return_value=False), \
         patch("app.services.lxd.health.record_failure", side_effect=lambda *a, **k: recorded.append(a)):
        probe = manager.probe_connection("nuc2")
    assert probe.reachable is True and probe.trusted is False
    assert "Certificate error" in probe.message
    assert recorded == [], "a refused handshake tripped the breaker as offline"
