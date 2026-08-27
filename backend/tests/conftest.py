"""Pytest bootstrap for the backend test suite.

``app.core.database`` raises at *import* time when ``DATABASE_URL`` is unset (see
its module top). Several test modules import router code that transitively pulls
in ``app.core.database``, so without this the ValueError is raised during
collection and takes down the entire ``tests/`` directory (19 passing -> 0
collected). conftest.py is imported before any test module, so setting a default
here keeps collection working. Tests that need a real database override it via
their own environment; this default is only a placeholder to satisfy the import.
"""
import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://test:test@localhost:5432/test",
)


import pytest


@pytest.fixture(autouse=True)
def _reset_session_singletons(monkeypatch):
    """
    Give every test a clean session tracker and registry.

    Both are process-wide module globals, so without this a session registered by one
    test survives into the next and results depend on collection order - a suite that
    passes while proving nothing. Audit writes are stubbed out too: they are
    fire-and-forget tasks that would otherwise reach for a database that is not there
    and burn the retry backoff on every registration.
    """
    from app.services import session_registry, session_tracker

    session_registry._registry = None
    session_tracker._tracker = None
    monkeypatch.setattr(session_tracker, "fire_and_forget_connect", lambda **kw: None)
    monkeypatch.setattr(session_tracker, "fire_and_forget_disconnect", lambda **kw: None)
    yield
    session_registry._registry = None
    session_tracker._tracker = None
