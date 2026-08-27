"""A session that ended stays ended, however many heartbeats follow.

unregister() only MARKS a session disconnected. The object stays in the tracker's dict
for CLEANUP_TTL (five minutes) so the reaper can retire it later. A publisher iterating
that dict directly would keep re-writing the entry it had just deleted from Redis, once
per heartbeat, for five minutes: the row would leave the operator's list and come back,
which reads as exactly the flapping this change removes, now with the opposite cause.

Pins the publisher to live_sessions() rather than to the raw dict.
"""

import time

from app.services.session_registry import get_session_registry
from app.services.session_tracker import SessionProtocol, get_session_tracker
from tests.session_fakes import install_fake_registry


def _register(tracker, session_id="op-1"):
    tracker.register(
        session_id=session_id,
        instance_name="web-1",
        host="nuc2",
        project="default",
        protocol=SessionProtocol.TERMINAL,
        user_id="u1",
        user_email="a@example.com",
    )


async def test_live_sessions_excludes_a_disconnected_session():
    tracker = get_session_tracker()
    _register(tracker)
    assert [s.session_id for s in tracker.live_sessions()] == ["op-1"]

    tracker.unregister("op-1")

    # still in the dict, awaiting the reaper, but no longer live
    assert tracker.get("op-1") is not None
    assert tracker.live_sessions() == []


async def test_a_heartbeat_after_disconnect_does_not_restore_the_row():
    fake = install_fake_registry()
    tracker = get_session_tracker()
    registry = get_session_registry()
    _register(tracker)

    # first beat shares it
    await registry.publish(
        [s.to_snapshot("w1", time.time()) for s in tracker.live_sessions()],
        ttl_seconds=10,
    )
    assert len(fake.store["orcastra:sessions:active"]) == 1

    tracker.unregister("op-1")
    await registry.remove("op-1")

    # the beat that follows must have nothing to say about it
    await registry.publish(
        [s.to_snapshot("w1", time.time()) for s in tracker.live_sessions()],
        ttl_seconds=10,
    )
    snapshots, degraded = await registry.fetch(stale_seconds=10)
    assert snapshots == []
    assert degraded is False
