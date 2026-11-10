"""One member-name rule wherever a member reaches a ?target=, a cache key or a log line.

The rule lives in app.core.member_names and uses fullmatch, since re.match with "$" accepts a
trailing newline, and every standalone spelling ("None", "none", "") compares the same way.
"""

import time
from types import SimpleNamespace

import pytest

from app.core.member_names import is_member_name, is_standalone_location
from app.services.lxd import instance_metrics
from app.services.lxd.metrics_scrape import project_metrics
from tests.test_a_slow_member_read_is_not_an_internal_error import _app, _connect_refused
from tests.test_lxd_metrics_scrape_cache import _client, _Metrics, cache  # noqa: F401 - fixture

LONGEST = "a" * 253


@pytest.mark.parametrize(
    "name", ["node1", "node_1", "node-1.lab.example", LONGEST, "1node", "Member20"]
)
def test_member_names_a_host_would_carry_are_accepted(name):
    assert is_member_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "node1\n", "\nnode1", " node1", "a" * 254, "..", ".", "../etc", "a/b", "a\\b", "-n", "_n",
        "@gpu", "nöde", "none", "None", "NONE", "", None, 5,
    ],
)
def test_anything_else_is_refused(name):
    assert not is_member_name(name)


@pytest.mark.parametrize("value", [None, "", "none", "None", " NONE "])
def test_every_standalone_spelling_is_standalone(value):
    assert is_standalone_location(value)


@pytest.mark.parametrize("value", ["node1", "nonexistent", 5])
def test_a_member_is_not_standalone(value):
    assert not is_standalone_location(value)


def _deadline():
    return time.monotonic() + 10


def test_a_scrape_treats_every_standalone_spelling_as_one_entry(cache):  # noqa: F811
    metrics = _Metrics()
    client = _client(metrics)

    project_metrics(client, "c", "None", "p", deadline=_deadline())
    project_metrics(client, "c", "none", "p", deadline=_deadline())

    assert len(metrics.calls) == 1, "the second spelling must hit the first one's cache entry"
    assert metrics.calls[0]["params"] == {"project": "p"}, "a standalone host takes no target"


@pytest.mark.parametrize("member", ["node1\n", "a" * 254, "../etc", "@gpu"])
def test_a_scrape_never_targets_an_unrecognisable_member(cache, member):  # noqa: F811
    metrics = _Metrics()

    assert project_metrics(_client(metrics), "c", member, "p", deadline=_deadline()) is None
    assert metrics.calls == []
    assert cache.entries == {}


@pytest.mark.parametrize("member", ["member_2.lab", LONGEST])
def test_a_scrape_targets_a_hostname_shaped_member(cache, member):  # noqa: F811
    metrics = _Metrics()

    project_metrics(_client(metrics), "c", member, "p", deadline=_deadline())

    assert metrics.calls[0]["params"] == {"project": "p", "target": member}


class _Resources:
    def __init__(self):
        self.calls = []

    def get(self, params=None):
        self.calls.append(dict(params or {}))
        return SimpleNamespace(json=lambda: {"metadata": {"cpu": {"total": 8}}})


@pytest.fixture
def resources():
    instance_metrics._host_threads_cache.clear()
    fake = _Resources()
    yield fake
    instance_metrics._host_threads_cache.clear()


def _threads(resources, member):
    client = SimpleNamespace(api=SimpleNamespace(resources=resources))
    return instance_metrics.host_threads(client, "c", member)


def test_host_threads_reads_a_standalone_host_without_a_target(resources):
    assert _threads(resources, "None") == 8
    assert _threads(resources, "none") == 8
    assert resources.calls == [{}], "every standalone spelling shares one cache entry"


def test_host_threads_targets_a_member_and_refuses_a_newline(resources):
    assert _threads(resources, "member_2.lab") == 8
    assert _threads(resources, "node1\n") is None
    assert resources.calls == [{"target": "member_2.lab"}]


class _RecordingClient:
    """Any read fails as a refused connection; the calls show whether the route got that far."""

    def __init__(self):
        self.calls = 0
        self.instances = None
        outer = self

        class _Node:
            def __getitem__(self, item):
                return self

            def __getattr__(self, item):
                return self

            def get(self, params=None):
                outer.calls += 1
                raise _connect_refused()

        self.api = _Node()


@pytest.mark.parametrize("member", ["m1%0A", "%2E%2E", "None", "a" * 254])
@pytest.mark.parametrize("suffix", ["", "/resources"])
def test_the_member_routes_refuse_a_bad_name_before_any_read(member, suffix):
    client = _RecordingClient()

    response = _app(client).get(f"/cluster-members/{member}{suffix}", params={"node": "c"})

    assert response.status_code == 422, response.text[:200]
    assert client.calls == 0
    assert member not in response.text, "the refused value is never echoed back"


def test_a_hostname_shaped_member_reaches_the_cluster():
    client = _RecordingClient()

    response = _app(client).get("/cluster-members/member_2.lab.example/resources", params={"node": "c"})

    assert response.status_code != 422
    assert client.calls >= 1
