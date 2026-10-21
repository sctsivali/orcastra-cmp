"""The dashboard's Vault notice reads Vault's own answer, cheaply, and says nothing it should not.

Issue #15: a sealed Vault after a reboot read as "no clusters". `sys/seal-status` answers even
while sealed, so one probe tells sealed, uninitialized and unreachable apart. The probe must cost
one request (the client session's retry adapter made a dead Vault cost about ten seconds), be
shared by a burst of callers, keep its answer briefly, never put a Vault address in front of a
browser, and never run from /health, which the container healthcheck keys on.
"""

import threading
import time
from types import SimpleNamespace

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import vault_probe, vault_status
from app.core.auth import AuthenticatedUser, get_current_user
from app.services import vault_health
from app.services.lxd.lxd_service import get_lxd_service

ADDR = "http://vault.internal.example:8200"


class _Response:
    def __init__(self, status: int, body=None, text: str = ""):
        self.status_code = status
        self._body = body
        self._text = text

    def json(self):
        if self._body is None:
            raise ValueError(self._text)
        return self._body


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    vault_probe.reset()
    vault_status.reset()
    vault_health.reset()
    monkeypatch.setattr(vault_probe, "get_settings", lambda: SimpleNamespace(vault_enabled=True, vault_addr=ADDR))
    yield
    vault_probe.reset()
    vault_status.reset()
    vault_health.reset()


def _answer(monkeypatch: pytest.MonkeyPatch, outcome) -> list:
    calls: list = []

    def fake_get(url, timeout=None, allow_redirects=True):
        calls.append({"url": url, "timeout": timeout, "allow_redirects": allow_redirects})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(vault_probe.requests, "get", fake_get)
    return calls


@pytest.mark.parametrize(
    "outcome,state",
    [
        (_Response(200, {"initialized": True, "sealed": False}), "ok"),
        (_Response(200, {"initialized": True, "sealed": True, "t": 3, "n": 5, "progress": 1}), "sealed"),
        (_Response(200, {"initialized": False, "sealed": True}), "uninitialized"),
        (_Response(503, None, "upstream"), "unreachable"),
        (_Response(200, None, "<html>proxy</html>"), "unreachable"),
        (_Response(200, {"initialized": True}), "unreachable"),
        (requests.ConnectionError(f"HTTPConnectionPool(host='vault.internal.example', port=8200): Max retries exceeded with url: /v1/sys/seal-status"), "unreachable"),
        (requests.Timeout(f"Read timed out. for url: {ADDR}/v1/sys/seal-status"), "unreachable"),
    ],
)
def test_each_answer_maps_to_one_state_in_one_request(monkeypatch, outcome, state) -> None:
    calls = _answer(monkeypatch, outcome)
    probe = vault_probe.seal_probe()
    assert probe.state == state
    assert len(calls) == 1, "one request, never the retrying session"
    assert calls[0]["url"] == f"{ADDR}/v1/sys/seal-status"
    assert calls[0]["timeout"] == (2.0, 3.0)
    assert calls[0]["allow_redirects"] is False
    if state != "ok":
        assert probe.reason and "vault.internal.example" not in probe.reason and ":8200" not in probe.reason


def test_the_answer_is_kept_briefly_and_a_forced_probe_has_a_floor(monkeypatch) -> None:
    calls = _answer(monkeypatch, _Response(200, {"initialized": True, "sealed": True}))
    now = [1_000.0]
    monkeypatch.setattr(vault_probe.time, "time", lambda: now[0])
    vault_probe.seal_probe()
    now[0] += 5
    vault_probe.seal_probe()
    vault_probe.seal_probe(force=True)
    assert len(calls) == 2, "inside the TTL only a forced probe asks again"
    now[0] += 1
    vault_probe.seal_probe(force=True)
    assert len(calls) == 2, "a forced probe still reuses an answer younger than the floor"
    now[0] += 10
    vault_probe.seal_probe()
    assert len(calls) == 3


def test_a_burst_shares_one_probe(monkeypatch) -> None:
    calls: list = []

    def slow_get(url, timeout=None, allow_redirects=True):
        calls.append(url)
        time.sleep(0.2)
        return _Response(200, {"initialized": True, "sealed": True})

    monkeypatch.setattr(vault_probe.requests, "get", slow_get)
    results: list = []
    threads = [threading.Thread(target=lambda: results.append(vault_probe.seal_probe().state)) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == ["sealed"] * 5
    assert len(calls) == 1


def test_a_deployment_without_vault_is_not_probed(monkeypatch) -> None:
    calls = _answer(monkeypatch, AssertionError("must not be called"))
    monkeypatch.setattr(vault_probe, "get_settings", lambda: SimpleNamespace(vault_enabled=False, vault_addr=ADDR))
    assert vault_probe.seal_probe().state == "not_configured"
    assert calls == []


class _CertManager:
    def __init__(self, answers: bool):
        self.answers = answers
        self.reads = 0

    def read_registry(self, use_cache=True):
        self.reads += 1
        if self.answers:
            vault_status.mark_readable()
        else:
            vault_status.mark_unreadable("Forbidden: permission denied")


def test_an_unsealed_vault_whose_registry_fails_is_unavailable(monkeypatch) -> None:
    _answer(monkeypatch, _Response(200, {"initialized": True, "sealed": False}))
    vault_status.mark_unreadable("VaultClientError: 503 sealed")
    manager = _CertManager(answers=False)
    health = vault_health.resolve_vault_health(manager)
    assert (health.state, manager.reads) == ("unavailable", 1)
    assert health.registry_error.startswith("Forbidden")


def test_after_an_unseal_one_confirming_read_clears_the_registry_state(monkeypatch) -> None:
    _answer(monkeypatch, _Response(200, {"initialized": True, "sealed": False}))
    vault_status.mark_unreadable("VaultClientError: 503 sealed")
    manager = _CertManager(answers=True)
    health = vault_health.resolve_vault_health(manager)
    assert (health.state, manager.reads, health.registry_source) == ("ok", 1, "vault")


def test_confirming_reads_are_spaced(monkeypatch) -> None:
    _answer(monkeypatch, _Response(200, {"initialized": True, "sealed": False}))
    vault_status.mark_unreadable("Forbidden: permission denied")
    manager = _CertManager(answers=False)
    for _ in range(5):
        assert vault_health.resolve_vault_health(manager).state == "unavailable"
    assert manager.reads == 1, "a poll every few seconds must not become a LIST every few seconds"


def test_nothing_tried_yet_is_not_a_failure(monkeypatch) -> None:
    _answer(monkeypatch, _Response(200, {"initialized": True, "sealed": False}))
    manager = _CertManager(answers=True)
    assert vault_health.resolve_vault_health(manager).state == "ok"
    assert manager.reads == 0


def test_a_sealed_vault_is_reported_without_reading_the_registry(monkeypatch) -> None:
    _answer(monkeypatch, _Response(200, {"initialized": True, "sealed": True}))
    vault_status.mark_unreadable("VaultClientError: 503 sealed")
    manager = _CertManager(answers=True)
    assert vault_health.resolve_vault_health(manager).state == "sealed"
    assert manager.reads == 0


def _client(groups: list) -> TestClient:
    from app.routers import system

    app = FastAPI()
    app.include_router(system.router)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(user_id="u", email="u@example.test", groups=groups)
    app.dependency_overrides[get_lxd_service] = lambda: SimpleNamespace(cert_manager=_CertManager(answers=True))
    return TestClient(app, raise_server_exceptions=False)


def test_every_role_sees_the_state_and_only_an_admin_sees_why(monkeypatch) -> None:
    _answer(monkeypatch, _Response(200, {"initialized": True, "sealed": True}))
    admin = _client(["role_admin"]).get("/system/vault-status")
    assert admin.status_code == 200
    assert admin.headers["cache-control"] == "no-store"
    assert admin.json()["state"] == "sealed"
    assert admin.json()["detail"]["reason"] == "Vault reports it is sealed"
    for groups in (["role_partner"], ["role_tenant"]):
        body = _client(groups).get("/system/vault-status").json()
        assert body["state"] == "sealed"
        assert "detail" not in body


def test_check_now_reprobes(monkeypatch) -> None:
    calls = _answer(monkeypatch, _Response(200, {"initialized": True, "sealed": True}))
    now = [1_000.0]
    monkeypatch.setattr(vault_probe.time, "time", lambda: now[0])
    client = _client(["role_tenant"])
    client.get("/system/vault-status")
    now[0] += 3
    client.get("/system/vault-status")
    client.get("/system/vault-status?fresh=true")
    assert len(calls) == 2


def test_health_reports_the_last_probe_without_asking_vault(monkeypatch) -> None:
    import os

    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
    os.environ.setdefault("SECRET_KEY", "test-secret-key")
    from app.main import create_app

    calls = _answer(monkeypatch, AssertionError("/health must never probe Vault"))
    client = TestClient(create_app(), raise_server_exceptions=False)
    assert client.get("/health").json()["vault"]["probe"] is None
    _answer(monkeypatch, _Response(200, {"initialized": True, "sealed": True}))
    vault_probe.seal_probe()
    calls = _answer(monkeypatch, AssertionError("/health must never probe Vault"))
    body = client.get("/health").json()
    assert body["vault"]["probe"]["state"] == "sealed"
    assert calls == []


def test_peek_never_waits_for_a_probe_in_flight(monkeypatch) -> None:
    """/health runs on the event loop: it must not queue behind a probe of a dead Vault."""
    release = threading.Event()

    def hanging_get(url, timeout=None, allow_redirects=True):
        release.wait(5)
        return _Response(200, {"initialized": True, "sealed": True})

    monkeypatch.setattr(vault_probe.requests, "get", hanging_get)
    worker = threading.Thread(target=vault_probe.seal_probe)
    worker.start()
    time.sleep(0.1)
    started = time.monotonic()
    assert vault_probe.peek() is None
    assert time.monotonic() - started < 0.05
    release.set()
    worker.join()
    assert vault_probe.peek().state == "sealed"


def test_the_answer_is_stamped_when_it_arrives(monkeypatch) -> None:
    clock = [1_000.0]
    monkeypatch.setattr(vault_probe.time, "time", lambda: clock[0])

    def slow_get(url, timeout=None, allow_redirects=True):
        clock[0] += 3  # a full read timeout
        raise requests.Timeout("Read timed out.")

    calls = []
    monkeypatch.setattr(vault_probe.requests, "get", lambda *a, **k: (calls.append(1), slow_get(*a, **k))[1])
    assert vault_probe.seal_probe().checked_at == 1_003.0
    vault_probe.seal_probe(force=True)
    assert len(calls) == 1, "a forced probe right after a slow one reuses it instead of looping"


@pytest.mark.parametrize(
    "raw",
    [
        "ConnectTimeoutError(<urllib3.connection.HTTPConnection object at 0x7f>, 'Connection to 10.25.3.4 timed out. (connect timeout=2.0)')",
        "NameResolutionError(\"<urllib3.connection.HTTPConnection object>: Failed to resolve 'vault.internal.example' ([Errno -2])\")",
        "HTTPConnectionPool(host='vault.internal.example', port=8200): Max retries exceeded",
        "ConnectionError: ('10.1.2.3', 8200) refused",
        f"Read timed out. for url: {ADDR}/v1/sys/seal-status",
    ],
)
def test_no_reason_names_the_vault_host(raw: str) -> None:
    scrubbed = vault_status.public_error(raw)
    for leaked in ("10.25.3.4", "10.1.2.3", "vault.internal.example", "8200"):
        assert leaked not in scrubbed
