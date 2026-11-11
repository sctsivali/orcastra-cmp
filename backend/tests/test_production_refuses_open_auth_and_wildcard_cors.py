"""Production refuses to start with authentication off or a wildcard among the CORS origins.

AUTH_ENABLED=false makes every caller an Administrator (core/rbac.py), and a CORS list such as
`https://app.example,*` slipped past the guard that refused a bare `*`. Both are development
settings: with DEBUG=false the app refuses to start and says why; with DEBUG=true they still work.
"""

import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/test")
os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app import main  # noqa: E402
from app.core.config import Settings  # noqa: E402


def _settings(**values) -> Settings:
    base = {"DATABASE_URL": os.environ["DATABASE_URL"], "SECRET_KEY": "x" * 64, "DEBUG": False,
            "AUTH_ENABLED": True, "CORS_ORIGINS": "https://app.example", "APP_VERSION": "1.0.0-test"}
    base.update(values)
    return Settings(**base)


def _build(monkeypatch, **values):
    monkeypatch.setattr(main, "get_settings", lambda: _settings(**values))
    return main.create_app()


def test_production_refuses_authentication_off(monkeypatch):
    with pytest.raises(RuntimeError, match="AUTH_ENABLED=false is forbidden in production"):
        _build(monkeypatch, AUTH_ENABLED=False)


@pytest.mark.parametrize("origins", ["*", "https://app.example,*", "*,https://app.example", " * "])
def test_production_refuses_a_wildcard_origin_anywhere_in_the_list(monkeypatch, origins):
    with pytest.raises(RuntimeError, match="forbidden in production"):
        _build(monkeypatch, CORS_ORIGINS=origins)


def test_development_keeps_both_switches(monkeypatch):
    assert _build(monkeypatch, DEBUG=True, AUTH_ENABLED=False, CORS_ORIGINS="*") is not None


def test_production_with_explicit_origins_and_auth_starts(monkeypatch):
    assert _build(monkeypatch) is not None
