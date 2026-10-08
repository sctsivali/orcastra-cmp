"""One /1.0/metrics read per member, shared by the dashboard sweep and every instance dialog.

A scrape without `project` covers every project on the member, so the sweep can read honest
memory for all of a member's instances at once. The dialog reads the same cached entry, which
keeps every scrape of a member on one cache key: LXD caches its metrics for 8 seconds and ours
lasts 10, so a scrape on that one key always lands after LXD's cache expired and its time is the
sample time the disk I/O rate divides by. A project's own read is the fallback only when the
member-wide read is refused or too large.
"""

import time

import pytest
from pylxd.exceptions import LXDAPIException

from app.services.lxd import metrics_scrape
from app.services.lxd.metrics_scrape import (
    MEMBER_CACHE_NAMESPACE,
    MemberScrape,
    instance_scrape,
    member_metrics,
)
from app.services.lxd.metrics_text import (
    InstanceSeries,
    honest_memory_for,
    parse_instance_series,
    parse_member_series,
)
from tests.test_lxd_metrics_scrape_cache import _Cache, _client, _Metrics

GIB = 1024**3
BODY = (
    'lxd_memory_MemTotal_bytes{name="web",project="shop",type="container"} 100\n'
    'lxd_memory_MemFree_bytes{name="web",project="shop",type="container"} 20\n'
    'lxd_memory_Cached_bytes{name="web",project="shop",type="container"} 50\n'
    'lxd_memory_MemTotal_bytes{name="web",project="blog",type="container"} 200\n'
    'lxd_memory_MemTotal_bytes{name="db",type="virtual-machine"} 300\n'
)


@pytest.fixture
def cache(monkeypatch):
    fake = _Cache()
    monkeypatch.setattr(metrics_scrape, "_cache", lambda: fake)
    return fake


def _deadline():
    return time.monotonic() + 10


def test_a_member_body_is_keyed_by_project_and_name():
    series = parse_member_series(BODY)

    assert set(series) == {("shop", "web"), ("blog", "web"), ("default", "db")}
    assert series[("shop", "web")].mem_total == 100.0 and series[("blog", "web")].mem_total == 200.0
    assert series[("default", "db")].instance_type == "virtual-machine", "no project label is default"


def test_one_project_still_reads_only_its_own_instances():
    """A project's own read keeps its lines; an unlabelled line in it belongs to that project."""
    assert set(parse_instance_series(BODY, project="shop")) == {"web", "db"}
    assert parse_instance_series(BODY, project="blog")["web"].mem_total == 200.0
    assert "web" not in parse_instance_series(BODY, project="other")


def test_unaddressable_names_and_runaway_project_labels_are_skipped():
    body = (
        'lxd_memory_MemTotal_bytes{name="../etc",project="p",type="container"} 1\n'
        f'lxd_memory_MemTotal_bytes{{name="ok",project="{"p" * 300}",type="container"}} 1\n'
        'lxd_memory_MemTotal_bytes{name="ok",project="",type="container"} 1\n'
    )

    assert parse_member_series(body) == {}


@pytest.mark.parametrize(
    "agent, kind, expected",
    [
        ("not_applicable", "container", True),
        ("connected", "virtual-machine", True),
        ("unreachable", "virtual-machine", False),
        ("disabled", "virtual-machine", False),
        ("", "container", False),
    ],
)
def test_the_memory_gate_is_the_one_the_dialog_uses(agent, kind, expected):
    series = InstanceSeries(
        instance_type=kind, mem_total=4 * GIB, mem_available=3 * GIB, mem_free=GIB, mem_cached=GIB
    )

    assert (honest_memory_for(series, agent) is not None) is expected
    assert honest_memory_for(None, "connected") is None


def test_a_member_read_asks_for_every_project_and_targets_the_member(cache):
    metrics = _Metrics(BODY)

    standalone = member_metrics(_client(metrics), "c", "none", deadline=_deadline())
    member_metrics(_client(metrics), "c", "member-2", deadline=_deadline())

    assert metrics.calls[0]["params"] == {}, "no project: the member's every project"
    assert metrics.calls[1]["params"] == {"target": "member-2"}
    assert isinstance(standalone, MemberScrape) and ("shop", "web") in standalone.series
    assert cache.ttls[(MEMBER_CACHE_NAMESPACE, "c:none")] == metrics_scrape.CACHE_TTL_SECONDS


def test_a_cached_member_read_is_reused_and_survives_the_round_trip(cache):
    metrics = _Metrics(BODY)
    first = member_metrics(_client(metrics), "c", "none", deadline=_deadline())

    second = member_metrics(_client(metrics), "c", "none", deadline=_deadline())

    assert len(metrics.calls) == 1
    assert second.series == first.series and second.collected_at == first.collected_at


def test_a_member_read_failure_is_cached_by_kind(cache):
    error = LXDAPIException(type("R", (), {"status_code": 403, "json": lambda self: {}})())
    metrics = _Metrics(error=error)

    assert member_metrics(_client(metrics), "c", "none", deadline=_deadline()) is None
    assert member_metrics(_client(metrics), "c", "none", deadline=_deadline()) is None

    assert len(metrics.calls) == 1
    assert cache.entries[(MEMBER_CACHE_NAMESPACE, "c:none")] == {"failed": "refused"}


def test_the_dialog_takes_its_project_from_the_member_read(cache):
    metrics = _Metrics(BODY)

    scrape = instance_scrape(_client(metrics), "c", "none", "shop", deadline=_deadline())

    assert set(scrape.series) == {"web"} and scrape.series["web"].mem_total == 100.0
    assert [call["params"] for call in metrics.calls] == [{}], "one member-wide read, no project read"


@pytest.mark.parametrize("kind, falls_back", [("refused", True), ("too_large", True), ("timeout", False)])
def test_the_dialog_reads_its_project_only_when_the_member_read_is_refused_or_too_large(
    cache, kind, falls_back
):
    cache.set(MEMBER_CACHE_NAMESPACE, "c:none", {"failed": kind}, 600)
    metrics = _Metrics(BODY.replace('project="shop"', 'project="p"'))

    scrape = instance_scrape(_client(metrics), "c", "none", "p", deadline=_deadline())

    if falls_back:
        assert [call["params"] for call in metrics.calls] == [{"project": "p"}]
        assert scrape.series["web"].mem_total == 100.0
    else:
        assert metrics.calls == [] and scrape is None


def test_an_unrecognisable_member_is_never_read(cache):
    metrics = _Metrics(BODY)

    assert member_metrics(_client(metrics), "c", "../etc", deadline=_deadline()) is None
    assert metrics.calls == [] and cache.entries == {}


def test_a_member_that_times_out_is_not_asked_again_for_two_sweeps(cache):
    from requests import exceptions as requests_exc

    metrics = _Metrics(error=requests_exc.ReadTimeout("read timed out"))

    assert member_metrics(_client(metrics), "c", "none", deadline=_deadline()) is None
    assert member_metrics(_client(metrics), "c", "none", deadline=_deadline()) is None

    assert len(metrics.calls) == 1
    assert cache.ttls[(MEMBER_CACHE_NAMESPACE, "c:none")] == 120
    assert metrics_scrape.NEGATIVE_TTL_SECONDS["timeout"] == 30, "a project read keeps its 30 s"
