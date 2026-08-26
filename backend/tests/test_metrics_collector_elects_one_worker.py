"""Only one worker may run the expensive collection sweep per cycle.

The collector is started from the FastAPI lifespan, which runs once per worker. With
WEB_CONCURRENCY=4 that is four collectors, each calling the dashboard with fresh=True every
cycle, so the configured interval delivered four full sweeps of every cluster instead of one.
It is visible in production as the startup line appearing four times.

A short Redis lease elects one collector per cycle. The tests below pin the two halves that
matter: exactly one worker wins, and a deployment without Redis keeps collecting rather than
going silent, because a collector that elects nobody is worse than one that over-collects.
"""

from app.services.metrics_collector import MetricsCollector


class _FakeRedis:
    """Models SET NX semantics: the first caller wins until the key is cleared."""

    def __init__(self):
        self.store = {}
        self.calls = []

    def set(self, key, value, nx=False, ex=None):
        self.calls.append((key, nx, ex))
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True


class _Cache:
    def __init__(self, redis_client):
        self.redis_client = redis_client


def _collector(monkeypatch, cache):
    monkeypatch.setattr(
        "app.core.cache.get_cache_manager", lambda: cache, raising=True
    )
    return MetricsCollector(collection_interval=30)


def test_exactly_one_worker_wins_a_cycle(monkeypatch):
    """Four workers racing the same cycle: one sweep, three cache readers."""
    redis = _FakeRedis()
    collector = _collector(monkeypatch, _Cache(redis))

    verdicts = [collector._claim_collection_lease() for _ in range(4)]

    assert verdicts.count(True) == 1, f"expected one leader, got {verdicts}"
    assert verdicts.count(False) == 3


def test_the_lease_expires_with_the_cycle(monkeypatch):
    """A leader that dies mid-sweep must cost one cycle, not the loop."""
    redis = _FakeRedis()
    collector = _collector(monkeypatch, _Cache(redis))

    collector._claim_collection_lease()

    key, nx, ex = redis.calls[0]
    assert key == MetricsCollector.COLLECTION_LEASE_KEY
    assert nx is True, "without NX every worker would think it leads"
    assert ex == 30, "the lease must expire with the cycle so the next one re-elects"

    # Simulating expiry: the next cycle elects again rather than deadlocking on a stale lease.
    redis.store.clear()
    assert collector._claim_collection_lease() is True


def test_no_redis_means_collect_locally(monkeypatch):
    """Single-worker and Redis-down deployments must not stop collecting."""
    collector = _collector(monkeypatch, _Cache(None))
    assert collector._claim_collection_lease() is True


def test_a_failing_redis_means_collect_locally(monkeypatch):
    """A lease we cannot take is not a reason to skip the work."""

    class _Broken:
        def set(self, *args, **kwargs):
            raise RuntimeError("connection reset")

    collector = _collector(monkeypatch, _Cache(_Broken()))
    assert collector._claim_collection_lease() is True
