"""A migration's cache clearance is only delivered once the shared cache really cleared.

Projects, project usage and tags were cleared best effort, so during a Redis outage the row was
marked delivered and those views kept showing the instance on its old cluster for a full TTL.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.core.cache import CacheInvalidationError
from app.services.migrations import completion


class _Cache:
    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        self.calls: list[tuple[str, Any]] = []

    def invalidate_instance_cache(self) -> None:
        self.calls.append(("instances", None))

    def invalidate_resource_cache(self, *kinds: str, require_shared: bool = False) -> None:
        self.calls.append(("resources", require_shared))
        if self.fail and require_shared:
            raise CacheInvalidationError("redis is down")

    def invalidate_tag_cache(self, *, require_shared: bool = False) -> None:
        self.calls.append(("tags", require_shared))


ROW = SimpleNamespace(channel="cache", event_type="migration.cache.invalidate", payload={})


@pytest.mark.asyncio
async def test_every_family_requires_the_shared_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _Cache(fail=False)
    monkeypatch.setattr(completion, "get_cache_manager", lambda: cache)

    assert await completion.deliver_completion_event(ROW) is True
    assert ("resources", True) in cache.calls and ("tags", True) in cache.calls


@pytest.mark.asyncio
async def test_a_redis_failure_leaves_the_row_for_a_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(completion, "get_cache_manager", lambda: _Cache(fail=True))

    with pytest.raises(CacheInvalidationError):
        await completion.deliver_completion_event(ROW)
