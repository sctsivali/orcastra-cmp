"""
Security properties of the process-local API key validation cache.

`validate_api_key` used to perform 6 to 7 blocking Vault round trips inline on the event
loop for every external request. It now offloads them once and caches the validated
metadata in process memory for a short TTL.

Caching an authentication decision is only safe if all of these hold, so each is asserted:

  - the cache key covers the secret as well as the id, so a wrong secret can never be served
    from a cached entry
  - revocation is immediate, not TTL-delayed, because the eviction hook is wired into the
    revoke, delete and permission-update paths, and because it publishes a shared tombstone
    the other uvicorn workers honour (prod runs four of them, each with its own cache)
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
from app.core.cache import get_cache_manager
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
    """Reset local state directly.

    Calling evict_api_key_cache here would publish a revocation tombstone into the shared cache
    that outlives the test and makes every later one see the key as revoked.
    """

    def _reset() -> None:
        with integrations._key_cache_lock:
            integrations._key_cache.clear()
            integrations._last_used_written.clear()
        try:
            get_cache_manager().delete(integrations._REVOKED_NS, KEY_ID)
        except Exception:
            pass

    _reset()
    yield
    _reset()


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
            raise HTTPException(status_code=401, detail=integrations.INVALID_API_KEY_MESSAGE)
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
        ({"expires_at": ""}, "empty expiry"),
        ({"expires_at": "not-a-date"}, "unreadable expiry"),
        ({"expires_at": "2000-01-01T00:00:00Z"}, "expired with an offset"),
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


def test_revocation_reaches_the_other_workers_not_just_the_one_that_served_it(monkeypatch) -> None:
    """Regression: the metadata cache is per-process, so a local-only eviction left a revoked
    key usable on every worker that did not serve the revoke, for the whole TTL. With
    WEB_CONCURRENCY=4 that is three of four requests continuing to authenticate a key the
    operator had just killed.
    """
    digest = integrations._api_key_digest(KEY_ID, KEY_SECRET)
    integrations._store_key_metadata(digest, KEY_ID, {"id": KEY_ID, "is_active": True})
    assert integrations._cached_key_metadata(digest) is not None, "precondition: a live cache hit"

    # Stand in for the shared cache as seen by a worker that never handled the revocation.
    published: Dict[str, Any] = {}

    class _SharedCache:
        def set(self, namespace: str, key: str, value: Any, ttl: int | None = None) -> None:
            published[(namespace, key)] = value

        def get(self, namespace: str, key: str) -> Any:
            return published.get((namespace, key))

    monkeypatch.setattr(integrations, "get_cache_manager", lambda: _SharedCache())

    # A different worker revokes the key: it only publishes, it cannot touch our local dict.
    published[(integrations._REVOKED_NS, KEY_ID)] = True

    assert integrations._cached_key_metadata(digest) is None, (
        "a key revoked on another worker must stop authenticating here immediately"
    )
    assert digest not in integrations._key_cache, "the stale entry must be dropped, not just skipped"


def test_a_cache_outage_cannot_break_authentication(monkeypatch) -> None:
    """The tombstone lookup is a safety net, not a dependency: if the shared cache is down the
    key must still validate off the local entry rather than every external request failing.
    """
    digest = integrations._api_key_digest(KEY_ID, KEY_SECRET)
    integrations._store_key_metadata(digest, KEY_ID, {"id": KEY_ID, "is_active": True})

    class _Broken:
        def get(self, *_a, **_k):
            raise RuntimeError("redis is down")

        def set(self, *_a, **_k):
            raise RuntimeError("redis is down")

    monkeypatch.setattr(integrations, "get_cache_manager", lambda: _Broken())

    assert integrations._cached_key_metadata(digest) is not None
    evict_api_key_cache(KEY_ID)  # must not raise


async def test_a_future_expiry_with_an_offset_is_honoured(monkeypatch) -> None:
    """An offset-bearing value compared naive against aware raised TypeError, a 500."""
    metadata = _metadata(expires_at="2099-01-01T00:00:00+00:00")
    monkeypatch.setattr(integrations, "_validate_api_key_blocking", _Recorder(metadata))

    assert await validate_api_key(KEY_ID, KEY_SECRET) == metadata


@pytest.mark.parametrize(
    "expires_at,expired",
    [
        (None, False),
        ("2099-01-01T00:00:00", False),
        ("2000-01-01T00:00:00", True),
        ("2099-01-01T00:00:00+07:00", False),
        ("2000-01-01T00:00:00-05:00", True),
        ("", True),
        ("   ", True),
        ("garbage", True),
        (0, True),
    ],
)
def test_the_expiry_reading(expires_at, expired) -> None:
    assert integrations._api_key_has_expired(expires_at) is expired


def test_an_offset_is_converted_to_utc_before_comparing() -> None:
    from datetime import datetime

    now = datetime(2030, 1, 1, 12, 0, 0)
    # 18:00 at +07:00 is 11:00 UTC, one hour before `now`.
    assert integrations._api_key_has_expired("2030-01-01T18:00:00+07:00", now=now) is True
    # 18:00 at -01:00 is 19:00 UTC, still ahead of `now`.
    assert integrations._api_key_has_expired("2030-01-01T18:00:00-01:00", now=now) is False


@pytest.mark.parametrize(
    "expires_at,valid",
    [("", False), ("not-a-date", False), ("2099-01-01T00:00:00+00:00", True)],
)
async def test_validate_endpoint_reports_an_unreadable_expiry_as_expired(
    monkeypatch, expires_at, valid
) -> None:
    from types import SimpleNamespace

    from app.routers.integrations import APIKeyValidateRequest, validate_api_key_endpoint

    metadata = _metadata(expires_at=expires_at)
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: object())
    monkeypatch.setattr(integrations, "get_api_key_from_vault", lambda _client, _kid: (metadata, "h"))
    monkeypatch.setattr(integrations, "verify_api_key_secret", lambda _secret, _hash: True)
    monkeypatch.setattr(
        integrations, "get_settings", lambda: SimpleNamespace(vault_enabled=False, vault_token=None)
    )

    response = await validate_api_key_endpoint(
        APIKeyValidateRequest(api_key_id=KEY_ID, api_key_secret=KEY_SECRET)
    )
    assert response.valid is valid
    if not valid:
        assert response.error == "API key expired"
