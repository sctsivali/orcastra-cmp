"""The external integration endpoints read Vault off the event loop.

/validate, /regions, /external/sync-policies and /external/policies each made blocking Vault
calls inline, and all but /regions are reachable without authentication. A degraded Vault then
froze the whole worker, /health included, once per request. Each read now runs on a bounded pool
with a wall-clock cap, and the event loop keeps ticking while it waits.
"""

import asyncio
import hashlib
import hmac
import threading
import time
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.routers import integrations
from app.routers.integrations import APIKeyValidateRequest, SyncPoliciesRequest

SECRET = "oas_unit_test_secret"
METADATA: Dict[str, Any] = {
    "id": "oak_test",
    "app_name": "app",
    "app_type": "orcahub",
    "cluster_permissions": [{"cluster_id": "cluster-a", "projects": ["*"], "permissions": ["read"]}],
}


class SlowRead:
    """Records the thread it runs on and sleeps like a degraded Vault."""

    def __init__(self, delay: float, result: Any) -> None:
        self.delay = delay
        self.result = result
        self.threads: List[int] = []

    def __call__(self, *_a: Any, **_k: Any) -> Any:
        self.threads.append(threading.get_ident())
        time.sleep(self.delay)
        return self.result


async def _with_ticker(coro) -> tuple:
    ticks = 0
    done = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0.02)

    task = asyncio.create_task(ticker())
    started = time.monotonic()
    try:
        result = await coro
    except Exception as exc:  # noqa: BLE001 - the caller asserts on it
        result = exc
    finally:
        done.set()
        await task
    return result, ticks, time.monotonic() - started


@pytest.fixture
def hmac_key(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "unit-test-secret")
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: object())
    return hmac.new(b"unit-test-secret", SECRET.encode(), hashlib.sha256).hexdigest()


async def test_validate_reads_the_key_off_the_loop(hmac_key, monkeypatch):
    read = SlowRead(0.3, (dict(METADATA), hmac_key))
    names = SlowRead(0.0, {"cluster-a": "Cluster A"})
    monkeypatch.setattr(integrations, "get_api_key_from_vault", read)
    monkeypatch.setattr(integrations, "_cluster_display_names_blocking", names)
    loop_thread = threading.get_ident()
    result, ticks, _ = await _with_ticker(
        integrations.validate_api_key_endpoint(APIKeyValidateRequest(api_key_id="oak_test", api_key_secret=SECRET))
    )
    assert result.valid is True
    assert result.cluster_permissions[0].cluster_name == "Cluster A"
    assert read.threads and read.threads[0] != loop_thread
    assert names.threads and names.threads[0] != loop_thread
    assert ticks >= 5, "the event loop must keep running while Vault is slow"


async def test_validate_answers_validation_failed_when_vault_stalls(hmac_key, monkeypatch):
    monkeypatch.setattr(integrations, "VALIDATE_KEY_TIMEOUT", 0.2)
    monkeypatch.setattr(integrations, "get_api_key_from_vault", SlowRead(0.8, (dict(METADATA), hmac_key)))
    result, _, elapsed = await _with_ticker(
        integrations.validate_api_key_endpoint(APIKeyValidateRequest(api_key_id="oak_test", api_key_secret=SECRET))
    )
    assert result.valid is False
    assert result.error == "Validation failed"
    assert elapsed < 0.6


async def test_validate_falls_back_to_ids_when_names_stall(hmac_key, monkeypatch):
    monkeypatch.setattr(integrations, "VALIDATE_KEY_TIMEOUT", 0.2)
    monkeypatch.setattr(integrations, "get_api_key_from_vault", lambda *_a: (dict(METADATA), hmac_key))
    monkeypatch.setattr(integrations, "_cluster_display_names_blocking", SlowRead(0.8, {"cluster-a": "late"}))
    result, _, _ = await _with_ticker(
        integrations.validate_api_key_endpoint(APIKeyValidateRequest(api_key_id="oak_test", api_key_secret=SECRET))
    )
    assert result.valid is True
    assert result.cluster_permissions[0].cluster_name == "cluster-a"


async def test_validate_still_answers_invalid_for_an_unknown_key(hmac_key, monkeypatch):
    def missing(*_a: Any) -> None:
        raise HTTPException(status_code=404, detail="API key not found")

    monkeypatch.setattr(integrations, "get_api_key_from_vault", missing)
    result = await integrations.validate_api_key_endpoint(
        APIKeyValidateRequest(api_key_id="oak_nope", api_key_secret=SECRET)
    )
    assert result.valid is False
    assert result.error == integrations.INVALID_API_KEY_MESSAGE


@pytest.fixture
def regions_env(monkeypatch):
    settings = Settings(VAULT_ENABLED=True, VAULT_TOKEN="t", VAULT_ADDR="https://vault.example.test")
    monkeypatch.setattr(integrations, "get_settings", lambda: settings)

    async def _totals(*_a: Any) -> Dict[str, int]:
        return {"cpu": 0, "memory": 0, "storage": 0, "gpu": 0, "gpu_cards": 0}

    monkeypatch.setattr(integrations, "_integration_hardware_totals", _totals)


async def test_regions_read_off_the_loop(regions_env, monkeypatch):
    rows = SlowRead(0.3, {"r1": {"region_id": "r1", "node_count": 1}})
    monkeypatch.setattr(integrations, "_region_rows_blocking", rows)
    loop_thread = threading.get_ident()
    result, ticks, _ = await _with_ticker(
        integrations.get_integration_regions(api_key=dict(METADATA), lxd_service=None)
    )
    assert result["total_nodes"] == 1
    assert rows.threads and rows.threads[0] != loop_thread
    assert ticks >= 5


async def test_regions_answer_503_when_vault_stalls(regions_env, monkeypatch):
    monkeypatch.setattr(integrations, "SYNC_CLUSTER_TIMEOUT", 0.2)
    monkeypatch.setattr(integrations, "_region_rows_blocking", SlowRead(0.8, {}))
    result, _, elapsed = await _with_ticker(
        integrations.get_integration_regions(api_key=dict(METADATA), lxd_service=None)
    )
    assert isinstance(result, HTTPException) and result.status_code == 503
    assert elapsed < 0.6


class _Request:
    headers = {"X-API-Key-ID": "oak_test", "X-API-Key-Secret": "s3cret"}


class _EmptySession:
    async def execute(self, *_a: Any, **_k: Any) -> Any:
        class _R:
            def scalars(self):
                return self

            def unique(self):
                return self

            def all(self):
                return []

        return _R()

    async def commit(self) -> None:
        return None


class _Cache:
    def invalidate_resource_cache(self, *_a: Any) -> None:
        return None


@pytest.mark.parametrize("endpoint", ["sync", "read"])
async def test_the_policy_endpoints_read_the_key_off_the_loop(endpoint, monkeypatch):
    legacy = hashlib.sha256(b"s3cret").hexdigest()
    read = SlowRead(0.3, ({"id": "oak_test"}, legacy))
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: object())
    monkeypatch.setattr(integrations, "get_api_key_from_vault", read)
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: _Cache())
    call = (
        integrations.sync_policies_from_external(SyncPoliciesRequest(policies=[]), _Request(), db=_EmptySession())
        if endpoint == "sync"
        else integrations.get_policies_for_external(None, _Request(), db=_EmptySession())
    )
    loop_thread = threading.get_ident()
    result, ticks, _ = await _with_ticker(call)
    assert not isinstance(result, Exception), result
    assert read.threads and read.threads[0] != loop_thread
    assert ticks >= 5


@pytest.mark.parametrize("endpoint", ["sync", "read"])
async def test_the_policy_endpoints_answer_503_when_vault_stalls(endpoint, monkeypatch):
    monkeypatch.setattr(integrations, "VALIDATE_KEY_TIMEOUT", 0.2)
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: object())
    monkeypatch.setattr(integrations, "get_api_key_from_vault", SlowRead(0.8, ({}, "")))
    call = (
        integrations.sync_policies_from_external(SyncPoliciesRequest(policies=[]), _Request(), db=_EmptySession())
        if endpoint == "sync"
        else integrations.get_policies_for_external(None, _Request(), db=_EmptySession())
    )
    result, _, elapsed = await _with_ticker(call)
    assert isinstance(result, HTTPException) and result.status_code == 503
    assert elapsed < 0.6


class _FakeVault:
    def __init__(self, metadata: Dict[str, Any]) -> None:
        self.metadata = metadata
        self.paths: List[str] = []

    def vault_read(self, path: str) -> Any:
        self.paths.append(path)
        cluster_id = path.split("/")[-2]
        if cluster_id not in self.metadata:
            raise RuntimeError("not found")
        return self.metadata[cluster_id]


def test_display_names_read_only_cluster_metadata(regions_env, monkeypatch):
    vault = _FakeVault({"cluster-a": {"display_name": "Cluster A"}, "cluster-b": {"name": "b-name"}})
    monkeypatch.setattr(integrations, "get_vault_client", lambda *_a: vault)
    names = integrations._cluster_display_names_blocking(["cluster-a", "cluster-b", "cluster-c"])
    assert names == {"cluster-a": "Cluster A", "cluster-b": "b-name", "cluster-c": "cluster-c"}
    assert all(p.endswith("/metadata") for p in vault.paths)


def test_region_rows_keep_their_shape(regions_env, monkeypatch):
    class _CM:
        def __init__(self, **_k: Any) -> None:
            pass

        def get_cluster_info(self, cluster_id: str) -> Dict[str, Any]:
            if cluster_id == "cluster-b":
                raise RuntimeError("unreadable")
            return {"metadata": {"location": {"region_id": "jkt", "latitude": 1.0, "longitude": 2.0, "city": "C"}}}

    import app.services.lxd_cert_manager as lcm

    monkeypatch.setattr(lcm, "LXDCertificateManager", _CM)
    monkeypatch.setattr(integrations, "get_vault_client", lambda *_a: object())
    rows = integrations._region_rows_blocking({"cluster-a", "cluster-b"})
    assert list(rows) == ["jkt"]
    assert rows["jkt"]["node_count"] == 1
    assert rows["jkt"]["city"] == "C"
    assert rows["jkt"]["country_code"] == "XX"
