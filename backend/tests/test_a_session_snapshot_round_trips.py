"""A session read from another worker looks identical to one read locally.

Two paths now produce the same API body: the local SessionInfo, and a SessionInfo
rebuilt from a registry snapshot. If they drift, the difference reaches the browser as
a field that is suddenly undefined on some polls and not others - which is the original
bug's signature, so it would be misread as a regression in the fix.

Also pins the deliberate asymmetry: duration is recomputed by the READER, rates are
carried. Snapshotting duration would make the counter stall between heartbeats and then
jump; rates cannot be recomputed remotely because the sliding-window samples live in
the publishing process.
"""

import time

from app.services.session_models import SessionInfo, SessionProtocol


def _live_session():
    s = SessionInfo(
        session_id="op-1",
        instance_name="web-1",
        host="nuc2",
        project="default",
        protocol=SessionProtocol.GRAPHICS,
        user_id="u1",
        user_email="a@example.com",
        user_role="tenant",
        target_organization_id=6,
        target_org_slug="sivali",
        break_glass_grant_id=42,
    )
    s.mark_connected()
    s.add_download_bytes(4096)
    s.add_upload_bytes(128)
    return s


def test_a_remote_session_serializes_to_the_same_fields():
    local = _live_session()
    remote = SessionInfo.from_snapshot(local.to_snapshot("w1"))

    assert set(local.to_dict()) == set(remote.to_dict())


def test_identity_and_accountability_survive_the_round_trip():
    local = _live_session()
    remote = SessionInfo.from_snapshot(local.to_snapshot("w1"))
    a, b = local.to_dict(), remote.to_dict()

    for field in ("session_id", "instance_name", "host", "project", "protocol",
                  "connection_status", "target_org_slug", "is_break_glass"):
        assert a[field] == b[field], field
    assert a["user"] == b["user"]
    assert a["bandwidth"] == b["bandwidth"]


def test_a_break_glass_session_is_marked_as_one():
    # scope=org shows a tenant who is on their clusters. An admin's break-glass session
    # must be distinguishable, or the live view is weaker than the audit trail already is.
    ordinary = _live_session()
    ordinary.break_glass_grant_id = None

    assert _live_session().to_dict()["is_break_glass"] is True
    assert ordinary.to_dict()["is_break_glass"] is False


def test_duration_advances_on_the_readers_clock_not_the_publishers():
    local = _live_session()
    local.started_at = time.time() - 30.0

    snapshot = local.to_snapshot("w1")
    assert "duration_seconds" not in snapshot, (
        "duration must not be carried: a snapshotted value stalls between heartbeats"
    )

    remote = SessionInfo.from_snapshot(snapshot)
    first = remote.to_dict()["duration_seconds"]
    assert first >= 30.0

    # recomputed on every read, so it keeps moving without a new snapshot
    remote.started_at -= 5.0
    assert remote.to_dict()["duration_seconds"] > first


def test_rates_are_carried_because_the_samples_are_not():
    local = _live_session()
    published = local.to_dict()["bandwidth"]["download_rate_bps"]

    remote = SessionInfo.from_snapshot(local.to_snapshot("w1"))

    assert remote.to_dict()["bandwidth"]["download_rate_bps"] == published
    # the sliding-window samples themselves stay behind: they are not serializable and
    # would mean nothing in another process anyway.
    assert len(remote._dl_samples) == 0
    assert len(local._dl_samples) > 0
