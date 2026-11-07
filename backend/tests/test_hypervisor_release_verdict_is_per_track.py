"""What a Cluster card is told about its hypervisor version (services/hypervisor_releases/verdict.py).

The owner's screenshot of 2026-11-07 showed LXD 5.21.5 marked "Latest" while 5.21/stable was at
5.21.9: the console compared against a table typed in a year earlier. The verdict now compares
against the newest release on the Cluster's own track, names a newer track separately, and says
"not checked" rather than guess when it cannot place a version or the feed is missing or old.
"""

from __future__ import annotations

import pytest

from app.services.hypervisor_releases import feed
from app.services.hypervisor_releases.verdict import GOOD_FOR_SECONDS, INCUS, LXD, engine_of, evaluate
from tests._hypervisor_feeds import incus_body, lxd_body

NOW = 1_800_000_000.0
LXD_TRACKS = feed.parse_lxd(lxd_body())
INCUS_TRACKS = feed.parse_incus(incus_body())
FRESH = {"last_success_at": NOW - 3600, "last_attempt_at": NOW - 3600}


def verdict(engine, version, lts=None, *, tracks=None, state=FRESH):
    return evaluate(
        engine=engine,
        server_version=version,
        server_lts=lts,
        engine_state=state,
        tracks=(LXD_TRACKS if engine == LXD else INCUS_TRACKS) if tracks is None else tracks,
        now=NOW,
    )


@pytest.mark.parametrize("version, lts, status, latest, channel", [
    # The screenshot: Hetzner on 5.21.5 and Console Lab on 5.21.9.
    ("5.21.5", True, "update_available", "5.21.9", "5.21/stable"),
    ("5.21.9", True, "latest", "5.21.9", "5.21/stable"),
    ("5.21.10", True, "ahead", "5.21.9", "5.21/stable"),
    ("5.0.2", True, "update_available", "5.0.11", "5.0/stable"),
    ("4.0.15", True, "latest", "4.0.15", "4.0/stable"),
    ("6.9", False, "update_available", "6.10.1", "6/stable"),
    ("6.10.1", False, "latest", "6.10.1", "6/stable"),
    # Without server_lts the track is read off the version: 5.21 exists, 6.10 does not.
    ("5.21.5", None, "update_available", "5.21.9", "5.21/stable"),
    ("6.10.1", None, "latest", "6.10.1", "6/stable"),
])
def test_lxd_is_compared_with_its_own_track(version, lts, status, latest, channel) -> None:
    out = verdict(LXD, version, lts)
    assert (out["status"], out["latest_version"], out["channel"], out["reason"]) == (status, latest, channel, None)
    assert out["engine"] == "LXD"


def test_an_lts_cluster_is_told_about_the_feature_track_without_being_told_to_move() -> None:
    out = verdict(LXD, "5.21.9", True)
    assert (out["status"], out["track"], out["lts"]) == ("latest", "5.21 LTS", True)
    assert out["newer_track"] == {"track": "6.x", "version": "6.10.1", "channel": "6/stable", "lts": False}
    assert out["released_at"].startswith("2026-11-01T05:10:05")


def test_an_older_lts_is_pointed_at_the_newest_lts_first() -> None:
    out = verdict(LXD, "4.0.15", True)
    assert out["newer_track"]["track"] == "5.21 LTS"
    assert verdict(LXD, "6.10.1", False)["newer_track"] is None


def test_a_retired_feature_release_is_moved_to_the_next_lts() -> None:
    out = verdict(LXD, "5.20", False)
    assert (out["status"], out["track_retired"], out["latest_version"], out["channel"]) == (
        "update_available", True, "5.21.9", "5.21/stable",
    )


@pytest.mark.parametrize("version, lts, status, latest, channel, track", [
    ("6.0.4", None, "update_available", "6.0.6", "lts-6.0", "6.0 LTS"),
    ("7.0.2", True, "latest", "7.0.2", "lts-7.0", "7.0 LTS"),
    ("7.5", None, "update_available", "7.6.0", "stable", "stable"),
    ("7.6", False, "latest", "7.6.0", "stable", "stable"),
    ("6.23", None, "update_available", "7.6.0", "stable", "stable"),
    ("7.7", None, "ahead", "7.6.0", "stable", "stable"),
])
def test_incus_lts_and_feature_series(version, lts, status, latest, channel, track) -> None:
    out = verdict(INCUS, version, lts)
    assert (out["status"], out["latest_version"], out["channel"], out["track"]) == (status, latest, channel, track)
    assert out["engine"] == "Incus"


def test_incus_lts_names_the_newer_lts() -> None:
    assert verdict(INCUS, "6.0.6", True)["newer_track"]["track"] == "7.0 LTS"
    assert verdict(INCUS, "7.0.2", True)["newer_track"] == {
        "track": "stable", "version": "7.6.0", "channel": "stable", "lts": False,
    }


@pytest.mark.parametrize("version", ["", None, "qa", "five", "5", "5.21.x", "1" * 40])
def test_a_version_this_cannot_read_is_not_checked(version) -> None:
    out = verdict(LXD, version)
    assert (out["status"], out["reason"], out["latest_version"]) == ("not_checked", "unknown_version", None)


def test_an_unknown_engine_is_not_checked() -> None:
    out = evaluate(engine=None, server_version="5.21.5", server_lts=True, engine_state=FRESH, tracks=LXD_TRACKS, now=NOW)
    assert (out["status"], out["reason"]) == ("not_checked", "unknown_version")


def test_a_track_the_feed_does_not_list_is_not_checked() -> None:
    # An LTS the store does not carry is not quietly compared with the feature track.
    assert verdict(LXD, "7.0.1", True)["reason"] == "unknown_track"
    assert verdict(INCUS, "8.0.0", True)["reason"] == "unknown_track"


def test_a_missing_feed_says_so() -> None:
    out = verdict(LXD, "5.21.5", True, tracks={}, state={"last_attempt_at": NOW, "error": "upstream returned 503"})
    assert (out["status"], out["reason"]) == ("not_checked", "feed_unreachable")
    assert out["attempted_at"] is not None and out["checked_at"] is None


def test_a_feed_older_than_a_week_stops_speaking() -> None:
    old = {"last_success_at": NOW - GOOD_FOR_SECONDS - 1, "last_attempt_at": NOW}
    out = verdict(LXD, "5.21.5", True, state=old)
    assert (out["status"], out["reason"], out["latest_version"]) == ("not_checked", "stale_feed", None)
    assert verdict(LXD, "5.21.5", True, state={"last_success_at": NOW - GOOD_FOR_SECONDS})["status"] == "update_available"


@pytest.mark.parametrize("node_type, environment, engine", [
    ("LXD", None, LXD),
    ("MicroCloud", None, LXD),
    ("Incus", None, INCUS),
    (None, {"server": "incus"}, INCUS),
    (None, {"server": "lxd"}, LXD),
    (None, {"server": "something"}, None),
    (None, None, None),
])
def test_the_engine_comes_from_the_type_then_the_server_name(node_type, environment, engine) -> None:
    assert engine_of(node_type, environment) == engine


def test_a_rolling_upgrade_is_judged_by_its_oldest_member() -> None:
    # LXD keeps upgraded members blocked until the last one matches, so the cluster is only as
    # current as its oldest member, whatever the endpoint member answers.
    out = evaluate(
        engine=LXD, server_version="5.21.9", server_lts=True, engine_state=FRESH, tracks=LXD_TRACKS, now=NOW,
        member_versions=[("node-a", "5.21.9"), ("node-c", "5.21.5"), ("node-b", "5.21.9-2313ff2")],
    )
    assert (out["status"], out["latest_version"], out["mixed_versions"]) == ("update_available", "5.21.9", True)
    assert out["members"] == [
        {"member": "node-c", "version": "5.21.5"},
        {"member": "node-a", "version": "5.21.9"},
        {"member": "node-b", "version": "5.21.9"},
    ]


def test_members_on_one_version_are_not_listed_and_unreadable_ones_are_ignored() -> None:
    out = evaluate(
        engine=LXD, server_version="5.21.9", server_lts=True, engine_state=FRESH, tracks=LXD_TRACKS, now=NOW,
        member_versions=[("node-a", "5.21.9"), ("node-b", "5.21.9"), ("node-c", "garbage"), (7, "5.21.1")],
    )
    assert (out["status"], out["mixed_versions"], out["members"]) == ("latest", False, None)
    many = [(f"m{i}", "5.21.5" if i % 2 else "5.21.9") for i in range(500)]
    capped = evaluate(engine=LXD, server_version="5.21.9", server_lts=True, engine_state=FRESH,
                      tracks=LXD_TRACKS, now=NOW, member_versions=many)
    assert len(capped["members"]) == 64


@pytest.mark.parametrize("version, lts, security, fixed_in", [
    # The 2026-10-29 Incus advisories are fixed in 7.6.0; the LTS backports land after the newest
    # LTS tags, so 7.0.2 and 6.0.7 still read as needing a security update (core/engine_baseline.py).
    ("7.6", False, False, None),
    ("7.5", None, True, "7.6.0"),
    ("7.0.2", True, True, "7.0.3"),
    ("6.0.7", None, True, "6.0.8"),
    ("7.0.3", True, False, None),
])
def test_an_incus_build_below_the_security_baseline_is_flagged(version, lts, security, fixed_in) -> None:
    out = verdict(INCUS, version, lts)
    assert (out["security_update"], out["security_fixed_in"]) == (security, fixed_in)


def test_the_security_flag_does_not_wait_for_the_feed() -> None:
    """The baseline is a fact about the build, not about the feed: an unreachable feed still flags it."""
    out = verdict(INCUS, "7.0.2", True, tracks={})
    assert out["status"] == "not_checked"
    assert (out["security_update"], out["security_fixed_in"]) == (True, "7.0.3")


def test_an_engine_without_a_baseline_is_not_judged() -> None:
    """LXD has no security baseline wired, so the card must not read 'no known issue' either."""
    out = verdict(LXD, "5.21.5", True)
    assert (out["security_update"], out["security_fixed_in"]) == (None, None)


def test_a_mixed_incus_cluster_is_flagged_by_its_oldest_member() -> None:
    out = evaluate(
        engine=INCUS,
        server_version="7.6",
        server_lts=False,
        engine_state=FRESH,
        tracks=INCUS_TRACKS,
        now=NOW,
        member_versions=[("a1", "7.6"), ("a2", "7.5"), ("a3", "7.6")],
    )
    assert (out["security_update"], out["security_fixed_in"]) == (True, "7.6.0")
