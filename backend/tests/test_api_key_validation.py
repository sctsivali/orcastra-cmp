"""
Security properties of the process-local API key validation cache.

`validate_api_key` runs its blocking Vault round trips off the event loop and caches the
validated metadata in process memory for a short TTL. That is only safe if all of these hold:

  - the cache key covers the secret as well as the id, so a wrong secret is never served
  - revocation is immediate: eviction runs on revoke, delete and permission update, and
    publishes a tombstone the other uvicorn workers, each with its own cache, honour
  - revoked, inactive and expired states are re-checked on every request, cache hits included
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
        # Every notation must answer 401: a Z suffix or an offset must not raise TypeError on the
        # comparison, a malformed value must not raise ValueError, and an empty string must not
        # skip the check.
        ({"expires_at": "2000-01-01T00:00:00Z"}, "expired, Z suffix"),
        ({"expires_at": "2000-01-01T00:00:00+00:00"}, "expired, explicit offset"),
        ({"expires_at": "2000-01-01T07:00:00+07:00"}, "expired, non-UTC offset"),
        ({"expires_at": "not-a-date"}, "unreadable expiry"),
        ({"expires_at": ""}, "empty expiry"),
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
    """A stalled Vault must not park the worker: validation answers 503 at its timeout."""
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
    """The metadata cache is per-process, so a local-only eviction would leave a revoked key
    usable for the whole TTL on every worker that did not serve the revoke.
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

    # A different worker revokes the key: it only publishes, it cannot touch this local dict.
    published[(integrations._REVOKED_NS, KEY_ID)] = True

    assert integrations._cached_key_metadata(digest) is None, (
        "a key revoked on another worker must stop authenticating here immediately"
    )
    assert digest not in integrations._key_cache, "the stale entry must be dropped, not just skipped"


def test_a_cache_outage_cannot_break_authentication(monkeypatch) -> None:
    """If the shared cache is down, the key must still validate off the local entry instead of
    every external request failing on the tombstone lookup.
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
