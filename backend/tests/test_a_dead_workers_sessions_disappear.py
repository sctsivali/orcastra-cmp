"""A worker that dies without cleaning up must not leave sessions listed forever.

The original reaper only ever deleted sessions ALREADY marked disconnected, and a
worker killed by the OOM killer or a SIGKILL never runs its cleanup, so its sessions
stayed "connected" with nothing able to retire them. There was no last_seen and no
heartbeat, so nothing could even tell the difference.

Freshness is reader-side on purpose: a snapshot older than the stale window is gone,
whether or not Redis got round to expiring the field. That means a delete lost during
a Redis blip cannot strand a session either.
"""

import time

from app.services.session_registry import get_session_registry
from app.services.session_tracker import SessionProtocol, get_session_tracker
from tests.session_fakes import install_fake_registry

STALE = 10


def _register(tracker, session_id, instance="web-1"):
    tracker.register(
        session_id=session_id,
        instance_name=instance,
        host="nuc2",
        project="default",
        protocol=SessionProtocol.TERMINAL,
        user_id="u1",
        user_email="a@example.com",
    )


async def test_a_session_that_stops_beating_leaves_the_shared_view():
    install_fake_registry()
    tracker = get_session_tracker()
    registry = get_session_registry()
    _register(tracker, "op-dead")

    live = tracker.live_sessions()

    # a beat from a worker that is still alive
    await registry.publish([s.to_snapshot("w1", time.time()) for s in live], ttl_seconds=STALE)
    snapshots, _ = await registry.fetch(stale_seconds=STALE)
    assert [s["session_id"] for s in snapshots] == ["op-dead"]

    # the worker is killed: its last beat recedes past the stale window and no further
    # beat arrives. The key is still in Redis - nothing deleted it - but it is not live.
    await registry.publish(
        [s.to_snapshot("w1", time.time() - (STALE + 1)) for s in live], ttl_seconds=STALE
    )
    snapshots, degraded = await registry.fetch(stale_seconds=STALE)
    assert snapshots == []
    assert degraded is False
    assert len(registry._client.store["orcastra:sessions:active"]) == 1, (
        "the field must still be present: staleness, not deletion, is what retires it"
    )


async def test_a_transient_redis_error_does_not_stop_the_beat():
    fake = install_fake_registry()
    tracker = get_session_tracker()
    registry = get_session_registry()
    _register(tracker, "op-1")
    live = tracker.live_sessions()

    fake.fail_next = 1
    await registry.publish([s.to_snapshot("w1", time.time()) for s in live], ttl_seconds=STALE)
    assert fake.publishes == 0

    # the breaker holds the next attempt off rather than hammering a sick Redis
    await registry.publish([s.to_snapshot("w1", time.time()) for s in live], ttl_seconds=STALE)
    assert fake.publishes == 0

    # once it closes, publishing resumes; the loop was never killed by the error
    registry._breaker_until = 0.0
    await registry.publish([s.to_snapshot("w1", time.time()) for s in live], ttl_seconds=STALE)
    assert fake.publishes == 1
