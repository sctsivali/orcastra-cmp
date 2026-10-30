"""One terminal is one row, even when two workers each think they own it.

register() closes a stale duplicate by scanning its own dict, which only works while
the reconnect lands on the same worker. When it lands elsewhere the old worker keeps
its entry alive and keeps publishing it. Nobody noticed while a read only ever saw one
worker; now that a read sees all of them, the operator would get two rows for one
terminal - a regression caused by fixing the read path, not by the reconnect.

Not theoretical: both LXD sockets run with ping_interval=None, so a closed laptop lid
leaves the old proxy beating until the browser's close finally lands.
"""

from app.services.session_models import SessionInfo, SessionProtocol
from app.services.session_tracker import dedupe_sessions


def _session(session_id, started_at, email="a@example.com", instance="web-1", host="nuc2"):
    s = SessionInfo(
        session_id=session_id,
        instance_name=instance,
        host=host,
        project="default",
        protocol=SessionProtocol.TERMINAL,
        user_id="u1",
        user_email=email,
    )
    s.started_at = started_at
    return s


def test_the_newest_of_two_entries_for_one_connection_wins():
    old = _session("op-old", started_at=100.0)
    new = _session("op-new", started_at=200.0)

    kept = dedupe_sessions([old, new])

    assert [s.session_id for s in kept] == ["op-new"]


def test_the_key_is_the_same_tuple_register_matches_on():
    # differ by instance, by host, and by user: each is a genuinely separate session
    a = _session("op-a", 100.0, instance="web-1")
    b = _session("op-b", 100.0, instance="web-2")
    c = _session("op-c", 100.0, host="pandora")
    d = _session("op-d", 100.0, email="other@example.com")

    kept = dedupe_sessions([a, b, c, d])

    assert len(kept) == 4


def test_sessions_without_an_email_are_never_collapsed():
    # an empty email is not an identity. Collapsing on it would fold every anonymous
    # session on one instance into a single row.
    a = _session("op-a", 100.0, email="")
    b = _session("op-b", 200.0, email="")

    kept = dedupe_sessions([a, b])

    assert {s.session_id for s in kept} == {"op-a", "op-b"}


def test_a_terminal_and_a_console_are_two_sessions():
    # The protocol is in the key, as in register(). A console window keeps its Graphics, Text
    # and Serial tabs connected side by side, so a shell and a display on one instance are two
    # live sessions. Collapsing them hid one and later lost its disconnect audit row.
    term = _session("op-term", 100.0)
    console = _session("op-console", 200.0)
    console.protocol = SessionProtocol.GRAPHICS

    kept = dedupe_sessions([term, console])

    assert {s.session_id for s in kept} == {"op-term", "op-console"}


def test_a_reconnect_of_the_same_kind_still_shows_once():
    old = _session("op-old", 100.0)
    new = _session("op-new", 200.0)

    kept = dedupe_sessions([old, new])

    assert [s.session_id for s in kept] == ["op-new"]
