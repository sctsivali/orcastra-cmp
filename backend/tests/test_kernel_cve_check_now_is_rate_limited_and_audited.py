"""An Administrator's "Check Now" reads Ubuntu at most once a minute across every worker, leaves an
audit record either way, and clears the cached warnings feed so the next read judges with the new
record (routers/security_advisories.py). Reading the status never reaches Ubuntu.
"""

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException, Response

from app.core import config
from app.routers import security_advisories
from app.services.kernel_cve_feed import service
from app.services.kernel_cve_feed.record import to_dict
from app.services.kernel_cve_feed.store import KernelCveFeedStore
from tests._ubuntu_cve_record import CVE, record


@pytest.fixture
def world(monkeypatch):
    store = KernelCveFeedStore(redis=lambda: None)
    events = []
    reads = []
    cleared = []
    outcome = {"error": None}

    class _Logger:
        def log_activity(self, **kwargs):
            events.append(kwargs)

    class _Cache:
        def invalidate_resource_cache(self, *kinds):
            cleared.append(kinds)

    async def fake_check(cve_ids=None, now=None):
        reads.append(now)
        now = time.time()
        if outcome["error"]:
            state = {"last_attempt_at": now, "error": outcome["error"], "failures": 1}
        else:
            state = {"last_attempt_at": now, "last_success_at": now, "error": None, "failures": 0,
                     "source": "ubuntu", "record": to_dict(record())}
        store.write(CVE, state)
        return {CVE: state}

    patched = config.get_settings().model_copy(update={"kernel_cve_feed_enabled": True, "kernel_cve_feed_url": ""})
    monkeypatch.setattr(service, "get_settings", lambda: patched)
    monkeypatch.setattr(service, "get_store", lambda: store)
    monkeypatch.setattr(service, "check", fake_check)
    monkeypatch.setattr(service, "tracked_ids", lambda: (CVE,))
    monkeypatch.setattr(security_advisories, "get_activity_logger", lambda: _Logger())
    monkeypatch.setattr(security_advisories, "get_cache_manager", lambda: _Cache())
    service._parsed.clear()
    return store, events, reads, cleared, outcome


async def test_a_second_check_within_a_minute_is_refused_with_retry_after(world) -> None:
    _store, _events, reads, _cleared, _outcome = world
    first = await security_advisories.check_kernel_cves_now(Response())
    entry = first.advisories[0]
    assert (entry.id, entry.source, entry.ubuntu_priority, entry.stale) == (CVE, "ubuntu", "medium", False)
    assert entry.ubuntu_url == f"https://ubuntu.com/security/{CVE}"

    with pytest.raises(HTTPException) as refused:
        await security_advisories.check_kernel_cves_now(Response())
    assert refused.value.status_code == 429
    assert 1 <= int(refused.value.headers["Retry-After"]) <= 60
    assert "Try again in" in refused.value.detail
    assert len(reads) == 1


async def test_the_check_is_audited_with_its_outcome_and_clears_the_warnings_feed(world) -> None:
    _store, events, _reads, cleared, _outcome = world
    await security_advisories.check_kernel_cves_now(Response())
    assert events[0]["action"] == "system.kernel_cve_check.run"
    assert (events[0]["result"], events[0]["error_message"]) == ("success", None)
    assert events[0]["details"] == {"checked": [CVE], "mirror": False, "priorities": {CVE: "medium"}}
    assert cleared == [("warnings",)]


async def test_a_failed_check_is_audited_as_a_failure_and_still_answers(world) -> None:
    _store, events, _reads, _cleared, outcome = world
    outcome["error"] = "could not reach the URL"
    status = await security_advisories.check_kernel_cves_now(Response())
    assert events[0]["result"] == "failure"
    assert events[0]["error_message"] == f"{CVE}: could not reach the URL"
    entry = status.advisories[0]
    assert (entry.source, entry.checked_at, entry.check_error) == ("built-in", None, "could not reach the URL")


async def test_a_turned_off_check_refuses_to_run(world, monkeypatch) -> None:
    _store, events, reads, _cleared, _outcome = world
    off = config.get_settings().model_copy(update={"kernel_cve_feed_enabled": False})
    monkeypatch.setattr(service, "get_settings", lambda: off)
    with pytest.raises(HTTPException) as refused:
        await security_advisories.check_kernel_cves_now(Response())
    assert refused.value.status_code == 409
    assert reads == [] and events == []


async def test_reading_the_status_never_reaches_ubuntu(world, monkeypatch) -> None:
    _store, _events, reads, _cleared, _outcome = world

    async def no_fetch(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("a GET reached the internet")

    monkeypatch.setattr(service, "fetch_guarded", no_fetch)
    response = Response()
    status = await security_advisories.get_kernel_cve_ubuntu_status(response)
    assert reads == [] and status.enabled is True
    assert [(a.id, a.source, a.checked_at) for a in status.advisories] == [(CVE, "built-in", None)]
    assert response.headers["cache-control"] == "no-store"
