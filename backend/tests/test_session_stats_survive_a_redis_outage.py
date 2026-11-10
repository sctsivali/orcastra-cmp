"""Redis going away degrades the session list; it must not break it or hide it.

Terminals keep working when Redis is down, since the socket never touches it. What is lost
is the shared view, so each worker falls back to its own sessions. A short list looks like
a correct one, so the fallback must not be silent: the caller gets a degraded flag.

Mirrors the invariant in `CacheManager.get_entry` (app/core/cache.py): Redis answering at
all is authoritative, and the local view is a Redis-is-down fallback, never a miss path.
"""

import time

from app.services.session_registry import get_session_registry
from app.services.session_tracker import SessionProtocol, SessionTracker, get_session_tracker
from tests.session_fakes import install_fake_registry


def _register(tracker, session_id, email="a@example.com"):
    tracker.register(
        session_id=session_id,
        instance_name="web-1",
        host="nuc2",
        project="default",
        protocol=SessionProtocol.TERMINAL,
        user_id="u1",
        user_email=email,
    )


async def test_the_owning_worker_still_answers_when_redis_is_down():
    fake = install_fake_registry()
    tracker = get_session_tracker()
    _register(tracker, "op-mine")

    fake.fail_next = 1
    sessions, degraded = await tracker.list_active(user_email="a@example.com")

    assert [s.session_id for s in sessions] == ["op-mine"]
    assert degraded is True, "the caller must be told the view is partial"


async def test_a_miss_is_authoritative_and_is_not_a_degraded_answer():
    install_fake_registry()
    tracker = get_session_tracker()

    sessions, degraded = await tracker.list_active(user_email="a@example.com")

    assert sessions == []
    assert degraded is False, "Redis answering 'nothing' is an answer, not an outage"


async def test_the_breaker_closes_again_and_the_shared_view_returns():
    fake = install_fake_registry()
    tracker = get_session_tracker()
    registry = get_session_registry()

    # A second tracker stands in for another process: its session reaches this tracker
    # only through the shared hash, never through its own dict.
    other = SessionTracker()
    other.register(
        session_id="op-theirs",
        instance_name="db-1",
        host="nuc2",
        project="default",
        protocol=SessionProtocol.TERMINAL,
        user_id="u1",
        user_email="a@example.com",
    )
    await registry.publish(
        [s.to_snapshot("w2", time.time()) for s in other.live_sessions()], ttl_seconds=10
    )

    _register(tracker, "op-mine")

    fake.fail_next = 1
    sessions, degraded = await tracker.list_active(user_email="a@example.com")
    assert degraded is True
    assert [s.session_id for s in sessions] == ["op-mine"]

    registry._breaker_until = 0.0
    sessions, degraded = await tracker.list_active(user_email="a@example.com")
    assert degraded is False
    assert {s.session_id for s in sessions} == {"op-mine", "op-theirs"}
