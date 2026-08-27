"""
Shared doubles for the session-registry tests.

CI runs the suite against a closed Redis port on purpose, so nothing here may open a
socket. The fake is deliberately thin: it models the handful of hash operations the
registry actually uses, and nothing else.
"""

import asyncio
from typing import Any, Dict, List, Optional

from app.services import session_registry as registry_module
from app.services import session_tracker as tracker_module


class FakePipeline:
    """Collects the calls the registry pipelines, then applies them on execute()."""

    def __init__(self, redis: "FakeAsyncRedis") -> None:
        self._redis = redis
        self._ops: List[tuple] = []

    def hset(self, key: str, mapping: Dict[str, str]) -> None:
        self._ops.append(("hset", key, mapping))

    def hexpire(self, key: str, seconds: int, *fields: str) -> None:
        self._ops.append(("hexpire", key, seconds, fields))

    def expire(self, key: str, seconds: int) -> None:
        self._ops.append(("expire", key, seconds))

    async def execute(self) -> None:
        self._redis.raise_if_armed()
        for op in self._ops:
            if op[0] == "hset":
                self._redis.store.setdefault(op[1], {}).update(op[2])
            elif op[0] == "hexpire":
                self._redis.field_ttls[op[1]] = {f: op[2] for f in op[3]}
            elif op[0] == "expire":
                self._redis.key_ttl[op[1]] = op[2]
        self._redis.publishes += 1
        self._ops = []


class FakeAsyncRedis:
    """Enough of redis.asyncio to exercise the shared session registry."""

    def __init__(self) -> None:
        self.store: Dict[str, Dict[str, str]] = {}
        self.field_ttls: Dict[str, Dict[str, int]] = {}
        self.key_ttl: Dict[str, int] = {}
        self.fail_next = 0          # number of upcoming calls that should raise
        self.publishes = 0          # round trips actually made, for idle assertions

    def raise_if_armed(self) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ConnectionError("fake redis is unreachable")

    async def ping(self) -> bool:
        self.raise_if_armed()
        return True

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)

    async def hgetall(self, key: str) -> Dict[str, str]:
        self.raise_if_armed()
        return dict(self.store.get(key, {}))

    async def hdel(self, key: str, *fields: str) -> int:
        self.raise_if_armed()
        bucket = self.store.get(key, {})
        removed = 0
        for f in fields:
            if bucket.pop(f, None) is not None:
                removed += 1
        return removed

    async def aclose(self) -> None:
        return None


def install_fake_registry() -> FakeAsyncRedis:
    """Point the process-wide registry at a fake client and hand it back."""
    fake = FakeAsyncRedis()
    registry = registry_module.get_session_registry()
    registry._client = fake
    registry._breaker_until = 0.0
    registry._fault_logged = False
    return fake


def reset_session_singletons() -> None:
    """
    Clear both module globals between tests.

    Both the tracker and the registry are process-wide singletons. Without this a
    session registered by one test is still present in the next one, so results
    depend on collection order and a passing suite proves nothing.
    """
    registry_module._registry = None
    tracker_module._tracker = None
