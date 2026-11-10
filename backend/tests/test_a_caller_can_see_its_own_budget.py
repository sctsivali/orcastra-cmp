"""An integration learns its own rate limit instead of guessing one.

Deployments retune the limit, so the refusal carries the ceiling and the window the store holds
rather than a fixed `Retry-After`. `consume_api_key_budget_state` spends the same request
`consume_api_key_budget` spends and keeps the count Redis returned; the bool-only function stays
for callers that only want a yes or no.
"""

import pytest

from app.middleware import security


class FakeRedis:
    """Counts like Redis does, and reports a window like Redis does."""

    def __init__(self, ttl: int = 42) -> None:
        self.counts: dict = {}
        self._ttl = ttl
        self.expire_calls: list = []

    def incr(self, bucket: str) -> int:
        self.counts[bucket] = self.counts.get(bucket, 0) + 1
        return self.counts[bucket]

    def expire(self, bucket: str, seconds: int) -> None:
        self.expire_calls.append((bucket, seconds))

    def ttl(self, _bucket: str) -> int:
        return self._ttl


@pytest.fixture
def limiter(monkeypatch):
    """A limiter that is on, with a small budget, over a fake shared store."""
    redis = FakeRedis()

    class FakeCacheManager:
        redis_client = redis

    monkeypatch.setattr(security, "_shared_consume", security._shared_consume)
    monkeypatch.setattr(
        "app.core.cache.get_cache_manager", lambda: FakeCacheManager(), raising=False
    )

    from app.core import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True, raising=False)
    monkeypatch.setattr(settings, "rate_limit_api_key_requests", 3, raising=False)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 60, raising=False)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    return redis


def test_the_remaining_budget_falls_with_each_request(limiter) -> None:
    first = security.consume_api_key_budget_state("oak_a")
    second = security.consume_api_key_budget_state("oak_a")

    assert first[0] is True and second[0] is True
    assert first[1]["limit"] == 3
    assert first[1]["remaining"] == 2, "the first request did not report what is left after it"
    assert second[1]["remaining"] == 1, (
        "remaining did not move, so it is a constant rather than a budget"
    )
    assert first[1]["reset"] == 42, "the window is not the one the store actually holds"


def test_the_budget_is_refused_once_it_is_spent_and_reports_zero(limiter) -> None:
    for _ in range(3):
        assert security.consume_api_key_budget_state("oak_b")[0] is True

    allowed, state = security.consume_api_key_budget_state("oak_b")
    assert allowed is False
    assert state["remaining"] == 0, "a refused caller was told it still had budget"


def test_two_keys_do_not_share_a_budget(limiter) -> None:
    """The bucket is per key. One integration exhausting itself must not refuse another."""
    for _ in range(3):
        security.consume_api_key_budget_state("oak_c")

    allowed, state = security.consume_api_key_budget_state("oak_d")
    assert allowed is True
    assert state["remaining"] == 2


def test_a_store_that_cannot_answer_reports_nothing_rather_than_guessing(monkeypatch) -> None:
    """Fails open and reports no budget.

    An integration told it has room it does not have is worse served than one told nothing at all.
    """
    monkeypatch.setattr(security, "_shared_consume", lambda *_a, **_k: None)

    from app.core import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True, raising=False)
    monkeypatch.setattr(settings, "rate_limit_api_key_requests", 3, raising=False)
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    allowed, state = security.consume_api_key_budget_state("oak_e")

    assert allowed is True, "a store outage must not throttle every integration at once"
    assert state is None, "a budget nobody could read was reported as a number anyway"


def test_the_refusal_carries_the_window_rather_than_a_constant() -> None:
    """Retry-After comes from the window the limiter holds, which is configurable."""
    import inspect

    from app.routers.integrations import validate_api_key

    source = inspect.getsource(validate_api_key)
    assert 'retry_after = str(budget["reset"])' in source, (
        "Retry-After no longer comes from the window the limiter actually holds"
    )
    assert "X-RateLimit-Remaining" in source, "the headers that tell a caller where it stands are gone"
