"""The LXD and Incus release feeds are read from fixed hosts and treated as untrusted input
(services/hypervisor_releases/feed.py and service.py).

Nothing identifying the deployment or its Clusters is sent. A body that does not parse fails its
own engine only, the last good copy is kept through failures, reads back off, a store that was
tampered with is re-validated, and a check turned off reads and reports nothing.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.core import config
from app.core.safe_http import SafeHTTPError
from app.services.hypervisor_releases import feed, service
from app.services.release_check.store import ReleaseCheckStore
from tests._hypervisor_feeds import channel, incus_body, lxd_body

NOW = 1_800_000_000.0


@pytest.fixture
def store(monkeypatch):
    local = ReleaseCheckStore(redis=lambda: None, state_key=service.STATE_KEY)
    monkeypatch.setattr(service, "get_store", lambda: local)
    return local


@pytest.fixture
def settings(monkeypatch):
    def apply(**values):
        patched = config.get_settings().model_copy(update=values)
        monkeypatch.setattr(service, "get_settings", lambda: patched)
        return patched
    apply(hypervisor_release_check_enabled=True, hypervisor_release_check_interval_hours=6)
    return apply


@pytest.fixture
def feeds(monkeypatch):
    calls = []
    answers = {service.LXD_URL: lxd_body(), service.INCUS_URL: incus_body()}

    async def fake(url, **kwargs):
        calls.append((url, kwargs))
        answer = answers.get(url)
        if isinstance(answer, Exception):
            raise answer
        return "application/json", answer

    monkeypatch.setattr(service, "fetch_guarded", fake)
    return calls, answers


def test_the_snap_channel_map_gives_each_stable_track_without_the_build_hash() -> None:
    tracks = feed.parse_lxd(lxd_body())
    assert {name: t.version for name, t in tracks.items()} == {
        "5.21": "5.21.9", "6": "6.10.1", "5.0": "5.0.11", "4.0": "4.0.15",
    }
    assert "latest" not in tracks  # an alias of "6", not a track of its own
    assert tracks["5.21"].lts and not tracks["6"].lts
    assert tracks["5.21"].channel == "5.21/stable"


def test_an_arm64_only_track_is_still_known_and_amd64_wins_when_both_exist() -> None:
    body = lxd_body({"5.21": ("5.21.9-2313ff2", None)}, extra=[
        channel("5.0", "5.0.11-e42d773", None, arch="arm64"),
        channel("5.21", "5.21.99-ffffff", None, arch="riscv64"),
    ])
    tracks = feed.parse_lxd(body)
    assert tracks["5.0"].version == "5.0.11"
    assert tracks["5.21"].version == "5.21.9"


@pytest.mark.parametrize("body", [
    b"",
    b"<html>Service Unavailable</html>",
    b"[]",
    json.dumps({"channel-map": "lots"}).encode(),
    json.dumps({"channel-map": []}).encode(),
    json.dumps({"channel-map": [{"channel": "5.21/stable", "version": "5.21.9"}]}).encode(),
])
def test_a_snap_answer_that_is_not_a_channel_map_is_refused(body) -> None:
    with pytest.raises(feed.FeedInvalid) as raised:
        feed.parse_lxd(body)
    assert "Service" not in str(raised.value)  # the message never quotes the body


def test_forged_entries_are_skipped_not_trusted() -> None:
    body = lxd_body({"5.21": ("5.21.9-2313ff2", "2026-11-01T05:10:05+00:00")}, extra=[
        channel("5.21", "99999.1.1", None),          # a part longer than four digits
        channel("<script>", "5.21.20", None),          # a track name that is not plain
        channel("5.21", "5.21.30; rm -rf /", None),    # trailing text after the version
        channel("6", {"v": 6}, None),                  # wrong type
        channel("6", "6.10.1", "yesterday"),           # an unreadable stamp is dropped, not kept
    ])
    tracks = feed.parse_lxd(body)
    assert tracks["5.21"].version == "5.21.9"
    assert set(tracks) == {"5.21", "6"}
    assert tracks["6"].released_at is None


def test_incus_tags_give_the_feature_series_and_each_lts() -> None:
    tracks = feed.parse_incus(incus_body())
    assert {name: t.version for name, t in tracks.items()} == {
        "stable": "7.6.0", "lts-6.0": "6.0.6", "lts-7.0": "7.0.2",
    }
    assert tracks["lts-7.0"].lts and not tracks["stable"].lts


def test_incus_tags_that_are_not_releases_are_skipped() -> None:
    tracks = feed.parse_incus(incus_body(["v7.6.0", "v7.7.0-rc1", "v8.0.0~beta", "latest", "v9999999.0.0"]))
    assert tracks["stable"].version == "7.6.0"
    with pytest.raises(feed.FeedInvalid):
        feed.parse_incus(json.dumps({"message": "API rate limit exceeded"}).encode())
    with pytest.raises(feed.FeedInvalid):
        feed.parse_incus(incus_body(["nightly"]))


def test_a_tampered_store_is_validated_again_on_the_way_out() -> None:
    stored = {
        "5.21": {"version": "5.21.9", "lts": True, "channel": "5.21/stable", "released_at": None},
        "6": {"version": "rm -rf", "lts": False, "channel": "6/stable"},
        "x" * 80: {"version": "1.0.0", "lts": True, "channel": "x/stable"},
        "5.0": {"version": "5.0.11", "lts": True, "channel": "<b>5.0</b>"},
    }
    assert set(feed.tracks_from_dict(stored)) == {"5.21"}
    assert feed.tracks_from_dict(["not", "a", "dict"]) == {}


def test_both_feeds_are_read_from_their_hosts_with_nothing_identifying(store, settings, feeds) -> None:
    calls, _answers = feeds
    state = asyncio.run(service.check(now=NOW))

    assert state["lxd"]["error"] is None and state["incus"]["error"] is None
    assert state["lxd"]["tracks"]["5.21"]["version"] == "5.21.9"
    by_url = {url: kwargs for url, kwargs in calls}
    lxd, incus = by_url[service.LXD_URL], by_url[service.INCUS_URL]
    assert lxd["allowed_hosts"] == frozenset({"api.snapcraft.io"})
    assert incus["allowed_hosts"] == frozenset({"api.github.com"})
    for kwargs in (lxd, incus):
        assert kwargs["require_https"] is True
        assert kwargs["headers"]["User-Agent"] == "Orcastra-CMP-hypervisor-release-check/1"
        assert set(kwargs["headers"]) <= {"User-Agent", "Accept", "Snap-Device-Series", "X-GitHub-Api-Version"}
    assert lxd["headers"]["Snap-Device-Series"] == "16"
    assert lxd["max_bytes"] == feed.MAX_LXD_BYTES


def test_one_feed_failing_keeps_the_other_and_its_own_last_good_copy(store, settings, feeds) -> None:
    _calls, answers = feeds
    asyncio.run(service.check(now=NOW))
    answers[service.INCUS_URL] = SafeHTTPError("upstream returned 403", status_code=403)
    answers[service.LXD_URL] = b"<html>maintenance</html>"

    state = asyncio.run(service.check(now=NOW + 7 * 3600))

    assert state["incus"]["error"] == "upstream returned 403"
    assert state["incus"]["failures"] == 1
    assert state["incus"]["tracks"]["stable"]["version"] == "7.6.0"  # kept
    assert state["incus"]["last_success_at"] == NOW
    assert state["lxd"]["error"] == "the feed is not JSON"
    assert state["lxd"]["tracks"]["5.21"]["version"] == "5.21.9"


def test_reads_back_off_after_failures_and_otherwise_follow_the_interval(store, settings, feeds) -> None:
    _calls, answers = feeds
    state = asyncio.run(service.check(now=NOW))
    assert not service.due(state, NOW + 5 * 3600)
    assert service.due(state, NOW + 6 * 3600)

    answers[service.LXD_URL] = SafeHTTPError("could not reach the URL")
    answers[service.INCUS_URL] = SafeHTTPError("could not reach the URL")
    for failures, wait in ((1, 3600), (2, 2 * 3600), (3, 4 * 3600), (4, 12 * 3600), (5, 12 * 3600)):
        state = asyncio.run(service.check(now=NOW))
        assert state["lxd"]["failures"] == failures
        assert not service.due(state, NOW + wait - 1)
        assert service.due(state, NOW + wait)

    # A stamp from the future does not postpone the next read.
    state["lxd"]["last_attempt_at"] = NOW + 86400
    assert service.due(state, NOW)
    assert service.due({}, NOW) and service.due(["junk"], NOW)


def test_the_check_reads_nothing_once_it_is_turned_off(store, settings, feeds, monkeypatch) -> None:
    calls, _answers = feeds
    settings(hypervisor_release_check_enabled=False)

    claims = []

    class Lease:
        def __init__(self, *args):
            pass

        def claim(self):
            claims.append(1)
            return True

        def release(self):
            pass

    monkeypatch.setattr(service, "CollectorLease", Lease)

    async def one_pass():
        monkeypatch.setattr(service.random, "uniform", lambda a, b: 0.0)
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) > 2:
                raise asyncio.CancelledError

        monkeypatch.setattr(service.asyncio, "sleep", fake_sleep)
        with pytest.raises(asyncio.CancelledError):
            await service._loop()

    asyncio.run(one_pass())
    assert calls == [] and claims == []


def test_a_trailing_newline_is_not_a_plain_name() -> None:
    # Python's $ matches before a final newline; a channel ending in one would split the copied
    # upgrade command into two lines.
    stored = {"5.21": {"version": "5.21.9", "lts": True, "channel": "5.21/stable\n"}}
    assert feed.tracks_from_dict(stored) == {}
    assert feed.parse_version("5.21.9\n") == (5, 21, 9)  # stripped first, then matched whole
    assert feed.parse_version("5.21.9\nrm") is None
    tracks = feed.parse_incus(incus_body(["v7.6.0"]) [:-1] + b',{"ref": "refs/tags/v9.0.0\\n"}]')
    assert tracks["stable"].version == "7.6.0"


def test_the_track_count_is_capped_whatever_the_feed_lists() -> None:
    many = feed.parse_incus(incus_body([f"v{major}.0.0" for major in range(1, 5001)]))
    assert len(many) == feed.MAX_TRACKS
    assert many["stable"].version == "5000.0.0"
    assert "lts-5000.0" in many and "lts-1.0" not in many  # the newest majors are kept
    lxd = feed.parse_lxd(lxd_body({str(n): (f"{n}.1", None) for n in range(1, 500)}))
    assert len(lxd) == feed.MAX_TRACKS
    stored = {str(n): {"version": f"{n}.1", "lts": False, "channel": f"{n}/stable"} for n in range(1, 500)}
    assert len(feed.tracks_from_dict(stored)) == feed.MAX_TRACKS
