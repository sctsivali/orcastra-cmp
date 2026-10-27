"""The release check reads one public file, from fixed hosts, and keeps the last good copy
(services/release_check/service.py, core/safe_http.py).

It sends nothing that identifies the deployment, never follows a redirect off the feed's hosts,
falls back to the documentation site when GitHub does not answer, backs off after failures, and
stops speaking once its copy is a week old rather than repeat an old verdict.
"""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from app.core import config
from app.core.safe_http import SafeHTTPError, fetch_guarded
from app.services.release_check import service
from app.services.release_check.store import ReleaseCheckStore
from tests._release_manifest import body

NOW = 1_800_000_000.0


@pytest.fixture
def store(monkeypatch):
    local = ReleaseCheckStore(redis=lambda: None)
    monkeypatch.setattr(service, "get_store", lambda: local)
    return local


@pytest.fixture
def settings(monkeypatch):
    def apply(**values):
        current = config.get_settings()
        patched = current.model_copy(update=values)
        monkeypatch.setattr(service, "get_settings", lambda: patched)
        monkeypatch.setattr(config, "get_settings", lambda: patched)
        return patched
    return apply


@pytest.fixture
def feed(monkeypatch):
    calls = []
    answers = {}

    async def fake(url, **kwargs):
        calls.append((url, kwargs))
        answer = answers.get(url)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise SafeHTTPError("upstream returned 404")
        return "application/json", answer

    monkeypatch.setattr(service, "fetch_guarded", fake)
    return calls, answers


def test_the_published_feed_is_read_from_github_with_nothing_identifying(store, settings, feed) -> None:
    settings(release_check_enabled=True, release_feed_url="", app_version="1.0.0-RC4-hotfix2")
    calls, answers = feed
    answers[service.PRIMARY_URL] = body()

    state = asyncio.run(service.check(now=NOW))

    assert (state["source"], state["error"], state["failures"]) == ("github", None, 0)
    url, kwargs = calls[0]
    assert kwargs["headers"] == {"User-Agent": "Orcastra-CMP-release-check/1", "Accept": "application/json"}
    assert kwargs["require_https"] is True
    assert kwargs["allowed_hosts"] == service.PUBLIC_HOSTS
    assert "RC4" not in url and "hotfix" not in url
    assert service.describe(state, now=NOW)["level"] == "warning"


def test_the_documentation_copy_answers_when_github_does_not(store, settings, feed) -> None:
    settings(release_check_enabled=True, release_feed_url="")
    calls, answers = feed
    answers[service.PRIMARY_URL] = SafeHTTPError("could not reach the URL")
    answers[service.MIRROR_URL] = body()

    state = asyncio.run(service.check(now=NOW))
    assert state["source"] == "docs"
    assert [url for url, _ in calls] == [service.PRIMARY_URL, service.MIRROR_URL]


def test_a_failed_read_keeps_the_last_good_manifest_and_backs_off(store, settings, feed) -> None:
    settings(release_check_enabled=True, release_feed_url="", app_version="1.0.0-RC4-hotfix2")
    _calls, answers = feed
    answers[service.PRIMARY_URL] = body()
    good = asyncio.run(service.check(now=NOW))

    answers.clear()
    answers[service.PRIMARY_URL] = b"{not json"
    failed = asyncio.run(service.check(now=NOW + 13 * 3600))

    assert failed["failures"] == 1 and "not JSON" in failed["error"]
    assert failed["manifest"] == good["manifest"] and failed["last_success_at"] == NOW
    assert service.describe(failed, now=NOW + 13 * 3600)["level"] == "warning", "the last good copy still speaks"
    assert service.due(failed, NOW + 13 * 3600 + 3599) is False
    assert service.due(failed, NOW + 13 * 3600 + 3600) is True
    assert service.due({**failed, "failures": 9}, NOW + 13 * 3600 + 11 * 3600) is False
    assert service.due(good, NOW + 12 * 3600 - 1) is False
    assert service.due(good, NOW + 12 * 3600) is True


def test_a_week_old_copy_stops_speaking(store, settings) -> None:
    settings(release_check_enabled=True, app_version="1.0.0-RC4")
    from app.services.release_check.manifest import parse
    from app.services.release_check.store import manifest_to_dict

    state = {"last_attempt_at": NOW, "last_success_at": NOW - 8 * 86400, "manifest": manifest_to_dict(parse(body()))}
    described = service.describe(state, now=NOW)
    assert (described["level"], described["reasons"], described["stale"]) == ("none", ["stale_feed"], True)


def test_turned_off_it_says_so_and_reads_nothing(store, settings, feed) -> None:
    settings(release_check_enabled=False)
    assert service.describe({}, now=NOW)["reasons"] == ["disabled"]


def test_an_operator_mirror_replaces_the_public_feed_and_may_be_private(store, settings, feed) -> None:
    settings(release_check_enabled=True, release_feed_url="https://mirror.corp.internal/manifest.json",
             release_feed_private_cidrs="10.20.0.0/16, bogus")
    calls, answers = feed
    answers["https://mirror.corp.internal/manifest.json"] = body()

    state = asyncio.run(service.check(now=NOW))
    url, kwargs = calls[0]
    assert len(calls) == 1 and state["source"] == "mirror_custom"
    assert kwargs["allowed_hosts"] == frozenset({"mirror.corp.internal"})
    assert [str(n) for n in kwargs["exemptions"]] == ["10.20.0.0/16"]


def test_private_networks_are_never_exempt_for_the_public_feed(settings) -> None:
    settings(release_feed_url="", release_feed_private_cidrs="10.0.0.0/8")
    assert config.release_feed_exemptions() == ()


@pytest.fixture
def public_dns(monkeypatch):
    real = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        if host in ("raw.githubusercontent.com", "evil.example"):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port or 443))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr("app.core.url_safety.socket.getaddrinfo", resolve)


def test_a_redirect_off_the_feeds_hosts_is_refused(public_dns) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/manifest.json"})

    with pytest.raises(SafeHTTPError, match="not one this fetch may reach"):
        asyncio.run(fetch_guarded(
            service.PRIMARY_URL, max_bytes=1024, transport=httpx.MockTransport(handler),
            allowed_hosts=service.PUBLIC_HOSTS,
        ))


def test_an_oversized_feed_is_abandoned_while_streaming(public_dns) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (70 * 1024))

    with pytest.raises(SafeHTTPError, match="size limit"):
        asyncio.run(fetch_guarded(
            service.PRIMARY_URL, max_bytes=64 * 1024, transport=httpx.MockTransport(handler),
            allowed_hosts=service.PUBLIC_HOSTS,
        ))
