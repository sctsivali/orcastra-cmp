"""Template placeholders and short secrets are reported by name, at boot and in Host Health.

Nothing refused `SECRET_KEY=replace_me_openssl_rand_hex_32`, a value anyone can read in the public
template, though it signs API key hashes and WebSocket tickets. Rotating it invalidates every API
key, so it is reported rather than refused. A worker that could not read its Redis encryption key
from Vault at start is reported too. No value ever appears in a report.
"""

import logging

import pytest

from app.core import encryption, secret_hygiene
from app.services.host_health.checks import configuration
from app.services.host_health.context import CheckContext

GOOD = {"SECRET_KEY": "a" * 64, "NEXTAUTH_SECRET": "b" * 44, "DATABASE_URL": "postgresql+asyncpg://u:strong@db/x"}


def _names(env):
    return [(f.name, f.problem) for f in secret_hygiene.findings(env)]


def test_a_deployment_with_real_secrets_has_no_finding():
    assert _names(GOOD) == []


@pytest.mark.parametrize("value,problem", [
    ("replace_me_openssl_rand_hex_32", "is still the template placeholder"),
    ("REPLACE_ME", "is still the template placeholder"),
    ("short", "is shorter than 32 characters"),
    ("", "is not set"),
])
def test_a_weak_secret_key_is_named(value, problem):
    assert ("SECRET_KEY", problem) in _names({**GOOD, "SECRET_KEY": value})


def test_the_session_key_placeholder_is_named_when_the_backend_can_see_it():
    assert ("NEXTAUTH_SECRET", "is still the template placeholder") in _names(
        {**GOOD, "NEXTAUTH_SECRET": "replace_me_openssl_rand_base64_32"})
    absent = dict(GOOD)
    absent.pop("NEXTAUTH_SECRET")
    assert _names(absent) == []


def test_an_invalid_redis_key_and_a_placeholder_database_password_are_named():
    found = _names({**GOOD, "REDIS_ENCRYPTION_KEY": "replace_me_fernet_key",
                    "DATABASE_URL": "postgresql+asyncpg://u:replace_me_openssl_rand_hex_16@db/x"})
    assert [name for name, _ in found] == ["REDIS_ENCRYPTION_KEY", "POSTGRES_PASSWORD"]


def test_a_valid_redis_key_is_fine():
    from cryptography.fernet import Fernet

    assert _names({**GOOD, "REDIS_ENCRYPTION_KEY": Fernet.generate_key().decode()}) == []


def test_the_boot_report_names_without_values(monkeypatch, caplog):
    secret = "replace_me_openssl_rand_hex_32"
    monkeypatch.setenv("SECRET_KEY", secret)
    caplog.set_level(logging.ERROR, logger="app.core.secret_hygiene")
    secret_hygiene.report_at_startup()
    assert "SECURITY: SECRET_KEY is still the template placeholder" in caplog.text
    assert secret not in caplog.text


def _ctx():
    return CheckContext(settings=None, store=None, now=1000.0)


def test_host_health_is_critical_for_a_placeholder_and_shows_no_value(monkeypatch):
    secret = "replace_me_openssl_rand_hex_32"
    monkeypatch.setenv("SECRET_KEY", secret)
    monkeypatch.setattr(configuration, "encryption_state", lambda: None)
    result = configuration.check(_ctx())
    assert (result.status, result.severity) == ("degraded", "critical")
    assert "SECRET_KEY" in result.summary and "invalidates every API key" in result.remediation
    assert secret not in result.model_dump_json()


def test_host_health_warns_when_the_redis_key_fell_back_from_vault(monkeypatch):
    for name, value in GOOD.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("REDIS_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setattr(configuration, "encryption_state", lambda: {"key_source": "derived", "vault_unreadable": True})
    result = configuration.check(_ctx())
    assert (result.status, result.severity) == ("degraded", "warning")
    assert {f.label: f.value for f in result.facts}["Redis Encryption Key"] == "derived from SECRET_KEY"


def test_host_health_is_ok_with_real_secrets(monkeypatch):
    for name, value in GOOD.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("REDIS_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setattr(configuration, "encryption_state", lambda: {"key_source": "vault", "vault_unreadable": False})
    assert configuration.check(_ctx()).status == "ok"


def test_the_encryption_key_records_a_vault_it_could_not_read(monkeypatch):
    import requests

    monkeypatch.setenv("VAULT_ENABLED", "true")
    monkeypatch.setenv("VAULT_ADDR", "http://vault.test:8200")
    monkeypatch.setenv("VAULT_TOKEN", "t")
    monkeypatch.setenv("SECRET_KEY", "a" * 64)
    monkeypatch.delenv("REDIS_ENCRYPTION_KEY", raising=False)

    def refused(*_a, **_k):
        raise requests.HTTPError("403 Client Error: Forbidden")

    monkeypatch.setattr(requests, "get", refused)
    instance = encryption.DataEncryption()
    assert (instance.key_source, instance.vault_unreadable) == ("derived", True)


def test_a_vault_without_a_key_is_not_a_fallback(monkeypatch):
    import requests

    monkeypatch.setenv("VAULT_ENABLED", "true")
    monkeypatch.setenv("VAULT_ADDR", "http://vault.test:8200")
    monkeypatch.setenv("VAULT_TOKEN", "t")
    monkeypatch.setenv("SECRET_KEY", "a" * 64)
    monkeypatch.delenv("REDIS_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(requests, "get", lambda *a, **k: type("R", (), {"status_code": 404})())
    instance = encryption.DataEncryption()
    assert (instance.key_source, instance.vault_unreadable) == ("derived", False)
