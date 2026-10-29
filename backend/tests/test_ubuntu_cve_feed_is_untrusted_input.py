"""Ubuntu's record of a kernel CVE is read from the internet and unsigned, so it is checked like any
other input before a verdict believes it (services/kernel_cve_feed/record.py, service.py).

Only the fields the verdict reads are kept, each type- and size-checked; an entry that does not fit
is dropped on its own; a record for another CVE, or with no kernel package, is not used at all.
The read itself stays on ubuntu.com over https, carries nothing identifying the deployment, stops
at 2 MiB, and a failure keeps the last good copy and backs off.
"""

from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest

from app.core import config
from app.core.safe_http import SafeHTTPError, fetch_guarded
from app.services.kernel_cve_feed import record as record_module
from app.services.kernel_cve_feed import service
from app.services.kernel_cve_feed.record import MAX_BYTES, RecordInvalid, from_dict, parse, to_dict
from app.services.kernel_cve_feed.store import KernelCveFeedStore
from tests._ubuntu_cve_record import CVE, body, published, record

NOW = 1_800_000_000.0


def _with(change):
    data = published()
    change(data)
    return body(data)


# --- the record -------------------------------------------------------------------------------


def test_the_published_record_keeps_only_what_the_verdict_reads() -> None:
    parsed = record()
    assert (parsed.id, parsed.priority, parsed.cvss_score, parsed.cvss_severity) == (CVE, "medium", 9.3, "CRITICAL")
    assert parsed.updated_at == "2026-10-28T17:47:40.655962+00:00"
    assert parsed.status("linux", "jammy") == ("pending", "5.15.0-203.213")
    assert parsed.status("linux-hwe-6.8", "jammy") == ("needed", "")
    assert parsed.status("linux-lowlatency", "noble") == ("pending", "6.8.0-149.149.1")
    # free text where a version would be is not a version
    assert parsed.status("linux", "upstream") == ("released", "")
    assert parsed.status("linux", "trusty") == ("ignored", "")
    assert parsed.status("linux-hwe-6.8", "noble") == ("DNE", "")


def test_unknown_fields_and_packages_other_than_kernels_are_ignored() -> None:
    def change(d):
        d["packages"][0]["script"] = "<b>"
        d["packages"].append({"name": "openssl", "statuses": [
            {"release_codename": "jammy", "status": "needed", "description": ""}]})
        d["evil"] = {"nested": ["x"] * 10}

    parsed = parse(_with(change), CVE)
    assert "openssl" not in parsed.packages
    assert parsed.status("linux", "jammy") == ("pending", "5.15.0-203.213")


@pytest.mark.parametrize("change, message", [
    (lambda d: d.update(id="CVE-2026-88888"), "not for the CVE"),
    (lambda d: d.pop("id"), "not for the CVE"),
    (lambda d: d.update(id=["CVE-2026-99999"]), "not for the CVE"),
    (lambda d: d.update(packages={"linux": []}), "must be a list"),
    (lambda d: d.pop("packages"), "must be a list"),
    (lambda d: d.update(packages=[p for p in d["packages"] if p["name"] == "nothing"]), "no kernel package"),
    (lambda d: [p.update(statuses="pending") for p in d["packages"]], "no kernel package"),
])
def test_a_record_that_breaks_a_rule_is_refused(change, message) -> None:
    with pytest.raises(RecordInvalid, match=message):
        parse(_with(change), CVE)


def test_size_and_shape_limits() -> None:
    with pytest.raises(RecordInvalid, match="2 MiB"):
        parse(b" " * (MAX_BYTES + 1), CVE)
    with pytest.raises(RecordInvalid, match="not JSON"):
        parse(b"<html>", CVE)
    with pytest.raises(RecordInvalid, match="not JSON"):
        parse(b"\xff\xfe", CVE)
    # nesting deep enough to exhaust the decoder's recursion is refused, not raised through
    with pytest.raises(RecordInvalid, match="not JSON"):
        parse(b"[" * 200_000 + b"]" * 200_000, CVE)
    with pytest.raises(RecordInvalid, match="object"):
        parse(b"[]", CVE)
    with pytest.raises(RecordInvalid, match="not for the CVE"):
        parse(body(), "CVE-2026-99999; DROP")


def test_malformed_entries_are_dropped_one_by_one() -> None:
    def change(d):
        linux = d["packages"][0]
        linux["statuses"] = [
            "pending",
            {"release_codename": "JAMMY", "status": "released", "description": "9.9.9-999.999"},
            {"release_codename": "noble; rm -rf", "status": "released", "description": ""},
            {"release_codename": "noble", "status": "fixed", "description": ""},
            {"release_codename": "noble", "status": ["released"], "description": ""},
            {"release_codename": "jammy", "status": "pending", "description": "<script>alert(1)</script>"},
            {"release_codename": "jammy", "status": "released", "description": "5.15.0-999.999"},
            {"release_codename": "focal", "status": "released", "description": 5.15},
            {"release_codename": "xenial", "status": "needed", "description": "5.15.0-1." + "9" * 80},
        ]
        d["packages"][1:1] = [
            "linux-oops",
            {"name": 7, "statuses": []},
            {"name": "linux;rm", "statuses": linux["statuses"]},
            {"name": "linux-" + "x" * 80, "statuses": linux["statuses"]},
            {"name": "linux-kvm", "statuses": [{"release_codename": "jammy", "status": "not-affected"}]},
        ]

    parsed = parse(_with(change), CVE)
    # the first entry for a series wins, and HTML where a version goes leaves no version
    assert parsed.packages["linux"] == {"jammy": ("pending", ""), "focal": ("released", ""), "xenial": ("needed", "")}
    assert not any(";" in name or len(name) > 64 for name in parsed.packages)
    # a later duplicate package does not replace the first one
    assert parsed.status("linux-kvm", "jammy") == ("not-affected", "")


def test_counts_are_capped() -> None:
    def many_packages(d):
        d["packages"] = [
            {"name": f"linux-hwe-{i}.0", "statuses": [{"release_codename": "jammy", "status": "needed"}]}
            for i in range(record_module.MAX_PACKAGES + 500)
        ]

    assert len(parse(_with(many_packages), CVE).packages) == record_module.MAX_PACKAGES

    def many_statuses(d):
        d["packages"][0]["statuses"] = [
            {"release_codename": "a" * 2 + chr(97 + i % 26) * (1 + i // 26), "status": "needed", "description": ""}
            for i in range(200)
        ]

    assert len(parse(_with(many_statuses), CVE).packages["linux"]) == record_module.MAX_STATUSES


@pytest.mark.parametrize("field, value, read", [
    ("priority", "urgent", None),
    ("priority", ["medium"], None),
    ("priority", "<b>high</b>", None),
    ("updated_at", "yesterday", None),
    ("updated_at", "2026-10-28T17:47:40+00:00" + " " * 80, None),
    ("cvss3", True, None),
    ("cvss3", 11, None),
    ("cvss3", "9.3", None),
])
def test_a_bad_optional_field_reads_as_absent(field, value, read) -> None:
    data = published()
    data[field] = value
    if field == "cvss3":
        data.pop("impact")
    parsed = parse(body(data), CVE)
    assert getattr(parsed, {"cvss3": "cvss_score"}.get(field, field)) is read


def test_the_cvss_block_is_type_checked() -> None:
    data = published()
    data["impact"]["baseMetricV3"]["cvssV3"].update(baseScore=float("nan"), baseSeverity="SEVERE")
    data["cvss3"] = 8.1
    parsed = parse(body(data), CVE)
    assert (parsed.cvss_score, parsed.cvss_severity) == (8.1, None)
    data["impact"] = "CRITICAL"
    assert parse(body(data), CVE).cvss_score == 8.1


def test_a_stored_copy_is_checked_again_when_read_back() -> None:
    parsed = record()
    assert from_dict(to_dict(parsed), CVE) == parsed
    assert from_dict(json.loads(json.dumps(to_dict(parsed))), CVE) == parsed
    assert from_dict(to_dict(parsed), "CVE-2026-88888") is None
    assert from_dict({"id": CVE, "packages": "poisoned"}, CVE) is None
    assert from_dict("poisoned", CVE) is None
    assert from_dict(None, CVE) is None


# --- the read ---------------------------------------------------------------------------------


@pytest.fixture
def store(monkeypatch):
    local = KernelCveFeedStore(redis=lambda: None)
    monkeypatch.setattr(service, "get_store", lambda: local)
    service._parsed.clear()
    return local


@pytest.fixture
def settings(monkeypatch):
    def apply(**values):
        patched = config.get_settings().model_copy(update=values)
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
    monkeypatch.setattr(service, "tracked_ids", lambda: (CVE,))
    return calls, answers


UBUNTU = f"https://ubuntu.com/security/cves/{CVE}.json"


def test_ubuntu_is_read_over_https_from_its_own_host_with_nothing_identifying(store, settings, feed) -> None:
    settings(kernel_cve_feed_enabled=True, kernel_cve_feed_url="", app_version="1.0.0-RC5")
    calls, answers = feed
    answers[UBUNTU] = body()

    states = asyncio.run(service.check(now=NOW))

    assert (states[CVE]["error"], states[CVE]["failures"], states[CVE]["source"]) == (None, 0, "ubuntu")
    url, kwargs = calls[0]
    assert url == UBUNTU and len(calls) == 1
    assert kwargs["headers"] == {"User-Agent": "Orcastra-CMP-kernel-cve-check/1", "Accept": "application/json"}
    assert kwargs["require_https"] is True
    assert kwargs["allowed_hosts"] == frozenset({"ubuntu.com"})
    assert kwargs["exemptions"] == ()
    assert kwargs["max_bytes"] == 2 * 1024 * 1024
    check = service.read_checks()[CVE]
    assert check.snapshot is not None and check.snapshot.record.priority == "medium"
    assert check.snapshot.checked_at.timestamp() == NOW
    # the raw body is never stored, only the kept fields
    stored = json.dumps(store.read(CVE))
    assert "ipv4: validate" not in stored and "kernel-team-expedite" not in stored


def test_a_failed_read_keeps_the_last_good_copy_and_backs_off(store, settings, feed) -> None:
    settings(kernel_cve_feed_enabled=True, kernel_cve_feed_url="", kernel_cve_feed_interval_hours=6)
    _calls, answers = feed
    answers[UBUNTU] = body()
    good = asyncio.run(service.check(now=NOW))[CVE]

    answers[UBUNTU] = body(dict(published(), id="CVE-2026-11111"))
    failed = asyncio.run(service.check(now=NOW + 7 * 3600))[CVE]

    assert failed["failures"] == 1 and "not for the CVE" in failed["error"]
    assert failed["record"] == good["record"] and failed["last_success_at"] == NOW
    check = service.read_checks()[CVE]
    assert check.snapshot is not None and check.error == failed["error"]
    later = NOW + 7 * 3600
    assert service.due(failed, later + 3599) is False
    assert service.due(failed, later + 3600) is True
    assert service.due({**failed, "failures": 9}, later + 11 * 3600) is False
    assert service.due({**failed, "failures": 9}, later + 12 * 3600) is True
    assert service.due(good, NOW + 6 * 3600 - 1) is False
    assert service.due(good, NOW + 6 * 3600) is True
    assert service.due({}, NOW) is True


def test_an_unexpected_failure_is_recorded_so_the_backoff_still_applies(store, settings, feed) -> None:
    settings(kernel_cve_feed_enabled=True, kernel_cve_feed_url="")
    _calls, answers = feed
    answers[UBUNTU] = ValueError("Port out of range 0-65535")
    state = asyncio.run(service.check(now=NOW))[CVE]
    assert state["failures"] == 1 and state["last_attempt_at"] == NOW
    assert state["error"] == "unexpected ValueError"
    assert service.due(state, NOW + 60) is False


def test_turned_off_no_stored_copy_is_used(store, settings, feed) -> None:
    settings(kernel_cve_feed_enabled=True, kernel_cve_feed_url="")
    _calls, answers = feed
    answers[UBUNTU] = body()
    asyncio.run(service.check(now=NOW))
    settings(kernel_cve_feed_enabled=False)
    assert service.read_checks()[CVE].snapshot is None


def test_an_operator_mirror_replaces_ubuntu_and_may_be_private(store, settings, feed) -> None:
    settings(kernel_cve_feed_enabled=True, kernel_cve_feed_url="https://mirror.corp.internal/cves/{id}.json",
             kernel_cve_feed_private_cidrs="10.20.0.0/16, bogus")
    calls, answers = feed
    answers[f"https://mirror.corp.internal/cves/{CVE}.json"] = body()

    state = asyncio.run(service.check(now=NOW))[CVE]
    url, kwargs = calls[0]
    assert len(calls) == 1 and state["source"] == "mirror_custom" and state["error"] is None
    assert kwargs["allowed_hosts"] == frozenset({"mirror.corp.internal"})
    assert [str(n) for n in kwargs["exemptions"]] == ["10.20.0.0/16"]


def test_a_mirror_url_without_the_id_placeholder_is_never_fetched(store, settings, feed) -> None:
    settings(kernel_cve_feed_enabled=True, kernel_cve_feed_url="https://mirror.corp.internal/cves/latest.json")
    calls, _answers = feed
    state = asyncio.run(service.check(now=NOW))[CVE]
    assert calls == [] and "{id}" in state["error"]


def test_private_networks_are_never_exempt_for_ubuntu_itself(settings) -> None:
    settings(kernel_cve_feed_url="", kernel_cve_feed_private_cidrs="10.0.0.0/8")
    assert config.kernel_cve_feed_exemptions() == ()


@pytest.fixture
def public_dns(monkeypatch):
    real = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):
        if host in ("ubuntu.com", "evil.example"):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port or 443))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr("app.core.url_safety.socket.getaddrinfo", resolve)


def test_a_redirect_off_ubuntu_is_refused(public_dns) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": f"https://evil.example/{CVE}.json"})

    with pytest.raises(SafeHTTPError, match="not one this fetch may reach"):
        asyncio.run(fetch_guarded(
            UBUNTU, max_bytes=MAX_BYTES, transport=httpx.MockTransport(handler),
            allowed_hosts=service.PUBLIC_HOSTS, require_https=True,
        ))


def test_another_host_and_plain_http_are_refused_before_any_request(public_dns) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("no request may be sent")

    with pytest.raises(SafeHTTPError, match="not one this fetch may reach"):
        asyncio.run(fetch_guarded(
            f"https://evil.example/{CVE}.json", max_bytes=MAX_BYTES, transport=httpx.MockTransport(handler),
            allowed_hosts=service.PUBLIC_HOSTS, require_https=True,
        ))
    with pytest.raises(SafeHTTPError):
        asyncio.run(fetch_guarded(
            f"http://ubuntu.com/security/cves/{CVE}.json", max_bytes=MAX_BYTES,
            transport=httpx.MockTransport(handler), allowed_hosts=service.PUBLIC_HOSTS, require_https=True,
        ))


def test_an_oversized_record_is_abandoned_while_streaming(public_dns) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_BYTES + 1024))

    with pytest.raises(SafeHTTPError, match="size limit"):
        asyncio.run(fetch_guarded(
            UBUNTU, max_bytes=MAX_BYTES, transport=httpx.MockTransport(handler),
            allowed_hosts=service.PUBLIC_HOSTS, require_https=True,
        ))
