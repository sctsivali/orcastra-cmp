"""A sync never drops a cluster it could not read.

OrcaHub deletes every cluster of a connection that a sync omits, and the service exposures that
hang off it. The cluster-info fan-out returned nothing for a cluster whose Vault read failed, and
skipped any cluster whose LXD breaker was open although its info comes from Vault. One blip
therefore deleted a live cluster downstream while its siblings were reported. Now an unreadable
cluster the registry still lists answers 503, and only a cluster the readable registry no longer
lists is left out, as before.
"""

import threading
import time
from typing import Any, Dict, List, Optional, Set

import pytest
from fastapi import HTTPException

from app.routers import integrations
from app.services.lxd import health
from app.services.lxd_certs.storage import RegistryRead


class FakeCertManager:
    def __init__(self, failing: Set[str], registry: Optional[RegistryRead], registry_raises: bool = False) -> None:
        self.failing = failing
        self.registry = registry
        self.registry_raises = registry_raises
        self.registry_reads: List[Dict[str, Any]] = []

    def get_cluster_info(self, cluster_id: str) -> Dict[str, Any]:
        if cluster_id in self.failing:
            raise RuntimeError(f"Failed to retrieve cluster info for {cluster_id}")
        return {"metadata": {"name": cluster_id, "type": "lxd"}}

    def read_registry(self, use_cache: bool = True) -> RegistryRead:
        self.registry_reads.append({"use_cache": use_cache})
        if self.registry_raises:
            raise RuntimeError("vault sealed")
        return self.registry


def _ids(rows: List[Dict[str, Any]]) -> List[str]:
    return sorted(row["id"] for row in rows)


async def test_an_unreadable_cluster_the_registry_still_lists_is_a_503():
    cm = FakeCertManager({"cluster-b"}, RegistryRead(["cluster-a", "cluster-b"], "vault", True))
    with pytest.raises(HTTPException) as caught:
        await integrations._gather_cluster_info(cm, {"cluster-a", "cluster-b"})
    assert caught.value.status_code == 503
    assert "Nothing was removed" in caught.value.detail


async def test_an_unreadable_cluster_gone_from_a_readable_registry_is_omitted():
    cm = FakeCertManager({"cluster-b"}, RegistryRead(["cluster-a"], "vault", True))
    rows = await integrations._gather_cluster_info(cm, {"cluster-a", "cluster-b"})
    assert _ids(rows) == ["cluster-a"]
    assert cm.registry_reads == [{"use_cache": False}], "the removal decision reads the live registry"


async def test_an_unreadable_registry_is_a_503():
    cm = FakeCertManager({"cluster-b"}, RegistryRead(["cluster-a"], "last_known", False))
    with pytest.raises(HTTPException) as caught:
        await integrations._gather_cluster_info(cm, {"cluster-a", "cluster-b"})
    assert caught.value.status_code == 503


async def test_a_registry_read_that_raises_is_a_503():
    cm = FakeCertManager({"cluster-b"}, None, registry_raises=True)
    with pytest.raises(HTTPException) as caught:
        await integrations._gather_cluster_info(cm, {"cluster-a", "cluster-b"})
    assert caught.value.status_code == 503


async def test_a_tripped_breaker_does_not_hide_a_cluster(monkeypatch):
    monkeypatch.setattr(health, "is_tripped", lambda _c: True)
    cm = FakeCertManager(set(), None)
    rows = await integrations._gather_cluster_info(cm, {"cluster-a", "cluster-b"})
    assert _ids(rows) == ["cluster-a", "cluster-b"]
    assert cm.registry_reads == [], "nothing failed, so the registry is not read"


async def test_a_slow_cluster_is_a_503_not_a_drop(monkeypatch):
    from app.core.blocking import BlockingTimeout

    real = integrations.run_blocking

    async def slow_for_b(fn, *args, **kwargs):
        if kwargs.get("label") == "sync_cluster_info[cluster-b]":
            raise BlockingTimeout(kwargs["label"], 0.1)
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(integrations, "run_blocking", slow_for_b)
    cm = FakeCertManager(set(), RegistryRead(["cluster-a", "cluster-b"], "vault", True))
    with pytest.raises(HTTPException) as caught:
        await integrations._gather_cluster_info(cm, {"cluster-a", "cluster-b"})
    assert caught.value.status_code == 503


async def test_the_sync_handler_answers_503_through_the_route(monkeypatch):
    from app.core.config import Settings

    cm = FakeCertManager({"cluster-b"}, RegistryRead(["cluster-a", "cluster-b"], "vault", True))
    settings = Settings(VAULT_ENABLED=True, VAULT_TOKEN="t", VAULT_ADDR="https://vault.example.test")
    monkeypatch.setattr(integrations, "get_settings", lambda: settings)
    monkeypatch.setattr(integrations, "get_vault_client", lambda *_a: object())
    import app.services.lxd_cert_manager as lcm

    monkeypatch.setattr(lcm, "LXDCertificateManager", lambda **_k: cm)
    key = {"id": "oak_test", "cluster_permissions": [{"cluster_id": "cluster-a"}, {"cluster_id": "cluster-b"}]}
    with pytest.raises(HTTPException) as caught:
        await integrations.sync_data(
            integrations.SyncDataRequest(include_projects=False), api_key=key, lxd_service=None, db=None
        )
    assert caught.value.status_code == 503


class SlowCertManager(FakeCertManager):
    """Every read takes a little while, and the peak number running at once is recorded."""

    def __init__(self) -> None:
        super().__init__(set(), RegistryRead([], "vault", True))
        self._lock = threading.Lock()
        self.running = 0
        self.peak = 0

    def get_cluster_info(self, cluster_id: str) -> Dict[str, Any]:
        with self._lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        time.sleep(0.02)
        with self._lock:
            self.running -= 1
        return super().get_cluster_info(cluster_id)


async def test_a_key_covering_more_clusters_than_the_pool_still_syncs():
    from app.services.lxd.proxy import PROXY_MAX_IN_FLIGHT

    clusters = {f"cluster-{n:02d}" for n in range(PROXY_MAX_IN_FLIGHT + 8)}
    cm = SlowCertManager()
    rows = await integrations._gather_cluster_info(cm, clusters)
    assert _ids(rows) == sorted(clusters)
    assert cm.peak <= integrations.CLUSTER_INFO_CONCURRENCY


async def test_a_saturated_pool_is_a_503_that_says_so(monkeypatch):
    from app.core.blocking import BlockingPoolFull

    real = integrations.run_blocking

    async def shed_b(fn, *args, **kwargs):
        if kwargs.get("label") == "sync_cluster_info[cluster-b]":
            raise BlockingPoolFull(kwargs["label"], 32)
        return await real(fn, *args, **kwargs)

    monkeypatch.setattr(integrations, "run_blocking", shed_b)
    cm = FakeCertManager(set(), RegistryRead(["cluster-a", "cluster-b"], "vault", True))
    with pytest.raises(HTTPException) as caught:
        await integrations._gather_cluster_info(cm, {"cluster-a", "cluster-b"})
    assert caught.value.status_code == 503
    assert caught.value.detail == integrations.CLUSTER_INFO_SATURATED
    assert cm.registry_reads == [], "a shed read is not a Vault failure, so the registry is not asked"
