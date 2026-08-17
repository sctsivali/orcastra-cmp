"""
Security properties of the process-local API key validation cache.

`validate_api_key` used to perform 6 to 7 blocking Vault round trips inline on the event
loop for every external request. It now offloads them once and caches the validated
metadata in process memory for a short TTL.

Caching an authentication decision is only safe if all of these hold, so each is asserted:

  - the cache key covers the secret as well as the id, so a wrong secret can never be served
    from a cached entry
  - revocation is immediate, not TTL-delayed, because the eviction hook is wired into the
    revoke, delete and permission-update paths
  - revoked, inactive and expired states are re-checked on every request, including cache
    hits, so a key cannot outlive its own metadata
  - a missing key reports 401, never 404, so the endpoint does not leak which ids exist
  - a degraded Vault produces a bounded 503 rather than a parked worker
"""

import time
from typing import Any, Dict

import pytest
from fastapi import HTTPException

from app.routers import integrations
from app.routers.integrations import evict_api_key_cache, validate_api_key

KEY_ID = "oak_test"
KEY_SECRET = "s3cret"


def _metadata(**overrides: Any) -> Dict[str, Any]:
    base = {
        "id": KEY_ID,
        "app_name": "qa",
        "is_active": True,
        "is_revoked": False,
        "expires_at": None,
        "cluster_permissions": [],
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _clean_cache():
    evict_api_key_cache(KEY_ID)
    yield
    evict_api_key_cache(KEY_ID)


class _Recorder:
    """Stands in for the blocking Vault path and counts how often it is reached."""

    def __init__(self, metadata: Dict[str, Any], raises: Exception = None) -> None:
        self.metadata = metadata
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    def __call__(self, key_id: str, key_secret: str) -> Dict[str, Any]:
        self.calls.append((key_id, key_secret))
        if self.raises:
            raise self.raises
        if key_secret != KEY_SECRET:
            raise HTTPException(status_code=401, detail="Invalid API key secret")
        return self.metadata


async def test_cache_hit_avoids_a_second_vault_round_trip(monkeypatch) -> None:
    recorder = _Recorder(_metadata())
    monkeypatch.setattr(integrations, "_validate_api_key_blocking", recorder)

    first = await validate_api_key(KEY_ID, KEY_SECRET)
    second = await validate_api_key(KEY_ID, KEY_SECRET)

    assert first == second
    assert len(recorder.calls) == 1, "the second request should have been served from cache"


async def test_wrong_secret_never_hits_a_cached_entry(monkeypatch) -> None:
    """The digest covers id AND secret, so a warmed cache cannot be used as an oracle."""
    recorder = _Recorder(_metadata())
    monkeypatch.setattr(integrations, "_validate_api_key_blocking", recorder)

    await validate_api_key(KEY_ID, KEY_SECRET)

    with pytest.raises(HTTPException) as caught:
        await validate_api_key(KEY_ID, "wrong-secret")
    assert caught.value.status_code == 401
    assert recorder.calls[-1] == (KEY_ID, "wrong-secret"), (
        "a wrong secret must be re-validated against Vault, not answered from cache"
    )


async def test_revocation_is_immediate_not_ttl_delayed(monkeypatch) -> None:
    recorder = _Recorder(_metadata())
    monkeypatch.setattr(integrations, "_validate_api_key_blocking", recorder)

    await validate_api_key(KEY_ID, KEY_SECRET)
    assert len(recorder.calls) == 1

    # What revoke_api_key / delete_api_key / update_api_key_permissions call.
    evict_api_key_cache(KEY_ID)
    recorder.metadata = _metadata(is_revoked=True, is_active=False)

    with pytest.raises(HTTPException) as caught:
        await validate_api_key(KEY_ID, KEY_SECRET)
    assert caught.value.status_code == 401
    assert len(recorder.calls) == 2, "eviction must force a fresh Vault read"


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"is_revoked": True}, "revoked"),
        ({"is_active": False}, "inactive"),
        ({"expires_at": "2000-01-01T00:00:00"}, "expired"),
    ],
)
async def test_state_is_rechecked_on_cache_hits(monkeypatch, overrides, reason) -> None:
    """State checks must not be skipped just because metadata came from cache."""
    metadata = _metadata(**overrides)
    recorder = _Recorder(metadata)
    monkeypatch.setattr(integrations, "_validate_api_key_blocking", recorder)

    for attempt in range(2):
        with pytest.raises(HTTPException) as caught:
            await validate_api_key(KEY_ID, KEY_SECRET)
        assert caught.value.status_code == 401, f"{reason} key accepted on attempt {attempt}"


async def test_missing_key_reports_401_not_404(monkeypatch) -> None:
    """404 would leak whether an id exists. The Vault layer says 404, the boundary says 401."""
    recorder = _Recorder({}, raises=HTTPException(status_code=404, detail="API key not found"))
    monkeypatch.setattr(integrations, "_validate_api_key_blocking", recorder)

    with pytest.raises(HTTPException) as caught:
        await validate_api_key("oak_does_not_exist", KEY_SECRET)
    assert caught.value.status_code == 401


async def test_degraded_vault_yields_a_bounded_503(monkeypatch) -> None:
    """A stalled Vault must not park the worker. Bounded 503 beats an unbounded wait."""
    monkeypatch.setattr(integrations, "VALIDATE_KEY_TIMEOUT", 0.5)

    def _stalls(key_id: str, key_secret: str) -> Dict[str, Any]:
        time.sleep(5)
        return _metadata()

    monkeypatch.setattr(integrations, "_validate_api_key_blocking", _stalls)

    started = time.monotonic()
    with pytest.raises(HTTPException) as caught:
        await validate_api_key(KEY_ID, KEY_SECRET)
    elapsed = time.monotonic() - started

    assert caught.value.status_code == 503
    assert elapsed < 2.0, f"validation waited {elapsed:.2f}s against a 0.5s cap"
