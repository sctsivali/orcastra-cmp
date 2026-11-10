"""A shared rate counter whose expiry was lost gets its window back.

The counter is an INCR, and the expiry is set by a second call when the count is 1. A blip between the
two leaves a counter with no expiry, so an enrollment budget or a lockout spent once stays spent until
Redis is cleaned by hand.
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


# The yes-or-no form (_shared_allow) needs the same repair: a counter past its limit with no expiry
# would refuse its subject for good (console actions, guest credentials, desktop, the forced-refresh
# budget). The Redis guard makes a lost expiry likelier, since it can open between the two calls.


def test_a_refused_counter_without_an_expiry_gets_its_window_back(redis: Dict[str, Any]) -> None:
    redis["client"] = _Redis(count=11, ttl=-1)
    assert security._shared_allow("fresh:subject", 10, 60) is False
    assert redis["client"].expires == [("orcastra:ratelimit:fresh:subject", 60)]


def test_an_allowed_hit_costs_no_ttl_read(redis: Dict[str, Any]) -> None:
    client = _Redis(count=5, ttl=-1)
    client.ttl = None  # type: ignore[assignment]  # calling it would raise
    redis["client"] = client
    assert security._shared_allow("fresh:subject", 10, 60) is True
    assert client.expires == []


def test_a_repair_that_fails_does_not_change_the_answer(redis: Dict[str, Any]) -> None:
    class _NoTtl(_Redis):
        def ttl(self, _bucket: str) -> Optional[int]:
            raise RuntimeError("ttl unavailable")

    redis["client"] = _NoTtl(count=11, ttl=None)
    assert security._shared_allow("fresh:subject", 10, 60) is False
