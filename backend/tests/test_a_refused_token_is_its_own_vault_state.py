"""A Vault that is healthy but refuses the dashboard's token says so, by name, everywhere.

Before, an expired or revoked token surfaced as "unavailable" (the registry read failed) or, for
API keys, as a wrong key. The Vault notice, its status route, Host Health and /health now name the
state "token_refused", with the fix, ahead of the registry state and behind Vault's own seal.
"""

import os
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import vault_probe, vault_status, vault_token
from app.core.auth import AuthenticatedUser, get_current_user
from app.services import vault_health
from app.services.host_health.checks import vault as vault_check
from app.services.lxd.lxd_service import get_lxd_service

ADDR = "http://vault.internal.example:8200"


class _CertManager:
    def __init__(self) -> None:
        self.reads = 0

    def read_registry(self, use_cache: bool = True):
        self.reads += 1
        vault_status.mark_unreadable("Forbidden: permission denied")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for module in (vault_probe, vault_status, vault_health):
        module.reset()
    vault_token.reset()
    yield
    for module in (vault_probe, vault_status, vault_health):
        module.reset()
    vault_token.reset()


def _seal(monkeypatch, state: str) -> None:
    monkeypatch.setattr(vault_probe, "seal_probe",
                        lambda force=False: vault_probe.SealProbe(state, 1.0, None if state == "ok" else f"Vault reports it is {state}"))


def _token(monkeypatch, state: str) -> None:
    monkeypatch.setattr(vault_token, "status", lambda *, max_age=None, settings=None: vault_token.TokenStatus(state, 1.0))


def test_a_healthy_vault_that_refuses_the_token_is_token_refused(monkeypatch) -> None:
    _seal(monkeypatch, "ok")
    _token(monkeypatch, "refused")
    vault_status.mark_unreadable("Forbidden: permission denied")
    manager = _CertManager()
    health = vault_health.resolve_vault_health(manager)
    assert health.state == "token_refused"
    assert "expired or revoked" in health.reason
    assert manager.reads == 0, "no confirming registry read while the token is known to be refused"


def test_a_sealed_vault_is_still_sealed_whatever_the_token(monkeypatch) -> None:
    _seal(monkeypatch, "sealed")
    _token(monkeypatch, "refused")
    assert vault_health.resolve_vault_health(_CertManager()).state == "sealed"


@pytest.mark.parametrize("token", ["ok", "unsupported", "unknown", "not_configured"])
def test_only_a_refused_token_changes_the_state(monkeypatch, token) -> None:
    _seal(monkeypatch, "ok")
    _token(monkeypatch, token)
    assert vault_health.resolve_vault_health(_CertManager()).state == "ok"


def _client(groups: list) -> TestClient:
    from app.routers import system

    app = FastAPI()
    app.include_router(system.router)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(user_id="u", email="u@example.test", groups=groups)
    app.dependency_overrides[get_lxd_service] = lambda: SimpleNamespace(cert_manager=_CertManager())
    return TestClient(app, raise_server_exceptions=False)


def test_every_role_sees_token_refused_and_only_an_admin_sees_why(monkeypatch) -> None:
    _seal(monkeypatch, "ok")
    _token(monkeypatch, "refused")
    admin = _client(["role_admin"]).get("/system/vault-status").json()
    assert admin["state"] == "token_refused"
    assert "expired or revoked" in admin["detail"]["reason"]
    for groups in (["role_partner"], ["role_tenant"]):
        body = _client(groups).get("/system/vault-status").json()
        assert body["state"] == "token_refused" and "detail" not in body


def _host_ctx(**overrides):
    from app.services.host_health.context import CheckContext

    settings = dict(vault_enabled=True, vault_addr=ADDR, vault_token="t0k3n", vault_unseal_ui_enabled=True,
                    auth_enabled=True, vault_clusters_path="secret/data/clusters")
    settings.update(overrides)
    return CheckContext(settings=SimpleNamespace(**settings), store=None, now=1_000.0)


def _host_health(monkeypatch, state: str) -> None:
    monkeypatch.setattr(vault_check, "resolve_vault_health",
                        lambda cert_manager, force=False: SimpleNamespace(state=state, registry_source="last_known"))
    monkeypatch.setattr(vault_check, "_leader", lambda addr: {})
    monkeypatch.setattr("app.services.lxd.lxd_service.get_lxd_service", lambda: SimpleNamespace(cert_manager=None))


def test_host_health_names_the_refused_token_and_the_fix(monkeypatch) -> None:
    _host_health(monkeypatch, "token_refused")
    result = vault_check.check(_host_ctx())
    assert (result.status, result.severity) == ("down", "critical")
    assert "expired or was revoked" in result.summary
    assert "-period=720h" in result.remediation and "VAULT_TOKEN" in result.remediation
    assert "t0k3n" not in result.model_dump_json()


def test_host_health_says_when_the_token_cannot_look_itself_up(monkeypatch) -> None:
    _host_health(monkeypatch, "ok")
    _token(monkeypatch, "unsupported")
    result = vault_check.check(_host_ctx())
    facts = {f.label: f.value for f in result.facts}
    assert "default policy" in facts["Token Expiry"]
    assert any("cannot look itself up" in a for a in result.advisories)


def test_health_reports_the_token_state_without_asking_vault(monkeypatch) -> None:
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
    os.environ.setdefault("SECRET_KEY", "test-secret-key")
    from app.main import create_app

    client = TestClient(create_app(), raise_server_exceptions=False)
    assert client.get("/health").json()["vault"]["token"] is None
    vault_token.remember(vault_token.TokenStatus("refused", 5.0))

    def never(*_a, **_k):
        raise AssertionError("/health must never ask Vault")

    monkeypatch.setattr(vault_token.requests, "get", never)
    token = client.get("/health").json()["vault"]["token"]
    assert token == {"state": "refused", "checked_at": 5.0}
