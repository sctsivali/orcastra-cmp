"""A key's bound stamp read while Redis is down counts as unreadable.

The stamp tells every worker that a binding, a project or an owner set changed on a cluster, so a
cached unbounded verdict must not outlive that change. While a configured Redis is failing, the
cache answers from this worker's own memory, which another worker's change never reached. That
answer cannot prove "nothing changed": a cached unbounded verdict is read again from the database
(fail closed), while a change this worker published itself still counts. With no Redis configured
at all, memory is the store and its answer is authoritative.
"""

import time

from app.core.cache import CacheManager
from app.services import key_party_bound as kpb

CLUSTER = "cluster-a"


def _down(monkeypatch) -> CacheManager:
    manager = CacheManager(redis_url=None)
    manager._redis_configured = True  # configured but not answering: memory is a fallback only
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: manager)
    return manager


def test_a_missing_stamp_from_the_fallback_cannot_clear_an_unbounded_verdict(monkeypatch):
    _down(monkeypatch)
    assert kpb._changed_since(CLUSTER, 0.0, unreadable_is_change=True) is True
    # A held verdict is the narrow one and is served on.
    assert kpb._changed_since(CLUSTER, 0.0) is False


def test_a_change_this_worker_published_counts_even_from_the_fallback(monkeypatch):
    manager = _down(monkeypatch)
    manager.set(kpb._CHANGED_NS, CLUSTER, time.time(), ttl=60)
    assert kpb._changed_since(CLUSTER, 0.0) is True
    assert kpb._changed_since(CLUSTER, 0.0, unreadable_is_change=True) is True


def test_without_redis_configured_memory_is_the_store(monkeypatch):
    manager = CacheManager(redis_url=None)
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: manager)
    assert kpb._changed_since(CLUSTER, 0.0, unreadable_is_change=True) is False
    manager.set(kpb._CHANGED_NS, CLUSTER, time.time(), ttl=60)
    assert kpb._changed_since(CLUSTER, 0.0) is True
    assert kpb._changed_since(CLUSTER, time.time() + 60) is False
