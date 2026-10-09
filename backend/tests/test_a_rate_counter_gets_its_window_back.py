"""A shared rate counter whose expiry was lost gets its window back.

The counter is an INCR, and the expiry is set by a second call when the count is 1. A blip between the
two left a counter with no expiry, so an enrollment budget or a lockout spent once stayed spent until
someone cleaned Redis by hand.
"""

from typing import Any, Dict, List, Optional

import pytest

from app.middleware import security


class _Redis:
    def __init__(self, count: int, ttl: Optional[int]) -> None:
        self.count, self._ttl = count - 1, ttl
        self.expires: List[tuple] = []

    def incr(self, _bucket: str) -> int:
        self.count += 1
        return self.count

    def expire(self, bucket: str, seconds: int) -> None:
        self.expires.append((bucket, seconds))

    def ttl(self, _bucket: str) -> Optional[int]:
        return self._ttl


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    holder: Dict[str, Any] = {}

    class _Cache:
        @property
        def redis_client(self) -> Any:
            return holder["client"]

    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: _Cache())
    return holder


def test_a_counter_without_an_expiry_gets_its_window_back(redis: Dict[str, Any]) -> None:
    redis["client"] = _Redis(count=4, ttl=-1)
    assert security._shared_consume("owner-enroll:key:k", 10, 60) == (4, 60)
    assert redis["client"].expires == [("orcastra:ratelimit:owner-enroll:key:k", 60)]


def test_a_counter_with_its_window_is_left_alone(redis: Dict[str, Any]) -> None:
    redis["client"] = _Redis(count=4, ttl=37)
    assert security._shared_consume("owner-enroll:key:k", 10, 60) == (4, 37)
    assert redis["client"].expires == []


def test_the_first_hit_sets_the_window(redis: Dict[str, Any]) -> None:
    redis["client"] = _Redis(count=1, ttl=60)
    assert security._shared_consume("owner-enroll:key:k", 10, 60) == (1, 60)
    assert redis["client"].expires == [("orcastra:ratelimit:owner-enroll:key:k", 60)]
