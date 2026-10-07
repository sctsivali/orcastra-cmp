"""The /1.0/metrics read: one scrape per member and project, shared, bounded and never fatal."""

import threading
import time
from types import SimpleNamespace

import pytest
from pylxd.exceptions import LXDAPIException
from requests import exceptions as requests_exc

from app.services.lxd import metrics_scrape
from app.services.lxd.metrics_scrape import project_metrics

BODY = (
    'lxd_memory_MemTotal_bytes{name="web",project="p",type="container"} 100\n'
    'lxd_memory_MemAvailable_bytes{name="web",project="p",type="container"} 60\n'
)


class _Cache:
    def __init__(self):
        self.entries = {}
        self.ttls = {}

    def get(self, namespace, key):
        return self.entries.get((namespace, key))

    def set(self, namespace, key, value, ttl):
        self.entries[(namespace, key)] = value
        self.ttls[(namespace, key)] = ttl


class _Response:
    """What pylxd hands back for a raw, streamed GET."""

    def __init__(self, body: str, headers=None, chunk_delay=0.0, chunk_size=None):
        self._body = body.encode()
        self.headers = headers if headers is not None else {"Content-Length": str(len(self._body))}
        self.encoding = "utf-8"
        self.closed = False
        self._chunk_delay = chunk_delay
        self._chunk_size = chunk_size
        self.read = 0

    def iter_content(self, chunk_size=1):
        size = self._chunk_size or chunk_size
        for start in range(0, len(self._body), size):
            if self._chunk_delay:
                time.sleep(self._chunk_delay)
            self.read += 1
            yield self._body[start : start + size]

    def close(self):
        self.closed = True


class _Metrics:
    def __init__(self, body=BODY, error=None, delay=0.0, headers=None, chunk_delay=0.0, chunk_size=None):
        self.calls = []
        self.body = body
        self.error = error
        self.delay = delay
        self.headers = headers
        self.chunk_delay = chunk_delay
        self.chunk_size = chunk_size
        self.responses = []

    def get(self, params=None, is_api=True, timeout=None, stream=False):
        self.calls.append(
            {"params": dict(params or {}), "is_api": is_api, "timeout": timeout, "stream": stream}
        )
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        response = _Response(self.body, self.headers, self.chunk_delay, self.chunk_size)
        self.responses.append(response)
        return response


def _client(metrics, extensions=("metrics",)):
    return SimpleNamespace(
        host_info={"api_extensions": list(extensions)},
        api=SimpleNamespace(metrics=metrics),
    )


@pytest.fixture
def cache(monkeypatch):
    fake = _Cache()
    monkeypatch.setattr(metrics_scrape, "_cache", lambda: fake)
    return fake


def _deadline():
    return time.monotonic() + 10


def test_a_standalone_host_takes_no_target_and_reads_raw_text(cache):
    metrics = _Metrics()

    scrape = project_metrics(_client(metrics), "c", "none", "p", deadline=_deadline())

    assert scrape is not None and scrape.series["web"].mem_available == 60
    assert metrics.calls[0]["params"] == {"project": "p"}
    assert metrics.calls[0]["is_api"] is False
    assert metrics.calls[0]["stream"] is True
    assert metrics.responses[0].closed


def test_a_clustered_member_is_targeted(cache):
    metrics = _Metrics()

    project_metrics(_client(metrics), "c", "member-2", "p", deadline=_deadline())

    assert metrics.calls[0]["params"] == {"project": "p", "target": "member-2"}


def test_a_cached_scrape_is_reused_within_its_ttl(cache):
    metrics = _Metrics()
    client = _client(metrics)

    first = project_metrics(client, "c", "none", "p", deadline=_deadline())
    second = project_metrics(client, "c", "none", "p", deadline=_deadline())

    assert len(metrics.calls) == 1
    assert second is not None and second.collected_at == first.collected_at
    assert cache.ttls[(metrics_scrape.CACHE_NAMESPACE, "c:none:p")] == 10


def test_concurrent_misses_scrape_once(cache):
    metrics = _Metrics(delay=0.2)
    client = _client(metrics)
    barrier = threading.Barrier(8)
    results = []

    def read():
        barrier.wait()
        results.append(project_metrics(client, "c", "none", "p", deadline=_deadline()))

    threads = [threading.Thread(target=read) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(metrics.calls) == 1
    assert all(result is not None for result in results)


@pytest.mark.parametrize(
    "error, kind",
    [
        (requests_exc.ReadTimeout("slow"), "timeout"),
        (requests_exc.ConnectionError("down"), "timeout"),
        (LXDAPIException(SimpleNamespace(status_code=403, json=lambda: {"error": "no"})), "refused"),
    ],
)
def test_failures_are_cached_by_kind_and_never_raise(cache, error, kind):
    metrics = _Metrics(error=error)
    client = _client(metrics)

    assert project_metrics(client, "c", "none", "p", deadline=_deadline()) is None
    assert project_metrics(client, "c", "none", "p", deadline=_deadline()) is None

    assert len(metrics.calls) == 1, "a cached failure must not be retried on every poll"
    key = (metrics_scrape.CACHE_NAMESPACE, "c:none:p")
    assert cache.entries[key] == {"failed": kind}
    assert cache.ttls[key] == metrics_scrape.NEGATIVE_TTL_SECONDS[kind]


def test_a_daemon_without_the_extension_is_not_asked(cache):
    metrics = _Metrics()

    assert project_metrics(_client(metrics, extensions=()), "c", "none", "p", deadline=_deadline()) is None
    assert metrics.calls == []


def test_an_oversized_body_is_refused_while_streaming(cache, monkeypatch):
    monkeypatch.setattr(metrics_scrape, "MAX_BODY_BYTES", 40)
    metrics = _Metrics(headers={}, chunk_size=16)

    assert project_metrics(_client(metrics), "c", "none", "p", deadline=_deadline()) is None
    assert cache.entries[(metrics_scrape.CACHE_NAMESPACE, "c:none:p")] == {"failed": "too_large"}
    assert metrics.responses[0].read <= 3, "the read stops at the cap, not at the end of the body"
    assert metrics.responses[0].closed


def test_a_declared_oversize_is_refused_before_reading(cache, monkeypatch):
    monkeypatch.setattr(metrics_scrape, "MAX_BODY_BYTES", 40)
    metrics = _Metrics(headers={"Content-Length": "999999999"})

    assert project_metrics(_client(metrics), "c", "none", "p", deadline=_deadline()) is None
    assert metrics.responses[0].read == 0
    assert metrics.responses[0].closed


def test_a_dripping_body_is_abandoned_at_the_deadline(cache):
    metrics = _Metrics(headers={}, chunk_size=8, chunk_delay=0.3)

    started = time.monotonic()
    result = project_metrics(_client(metrics), "c", "none", "p", deadline=time.monotonic() + 4.5)

    assert result is None
    assert time.monotonic() - started < 4.5
    assert cache.entries[(metrics_scrape.CACHE_NAMESPACE, "c:none:p")] == {"failed": "timeout"}


def test_an_unrecognisable_member_is_never_targeted_or_cached(cache):
    metrics = _Metrics()

    assert project_metrics(_client(metrics), "c", "x" * 5000, "p", deadline=_deadline()) is None
    assert project_metrics(_client(metrics), "c", "../etc", "p", deadline=_deadline()) is None
    assert metrics.calls == []
    assert cache.entries == {}


def test_too_little_budget_skips_the_scrape(cache):
    metrics = _Metrics()

    assert project_metrics(_client(metrics), "c", "none", "p", deadline=time.monotonic() + 1) is None
    assert metrics.calls == []
