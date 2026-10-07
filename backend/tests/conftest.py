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


@pytest.fixture(autouse=True)
def _default_to_no_active_migration_fence(monkeypatch):
    """Unit tests opt into migration locks explicitly instead of reaching a real database."""
    from app.services import migration_fence

    async def _no_locked_job(*_args, **_kwargs):
        return None

    async def _ordinary_operation(*_args, **_kwargs):
        return False

    async def _no_organization_clusters(*_args, **_kwargs):
        return set()

    class _FenceSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(migration_fence, "_active_locked_job_id", _no_locked_job)
    monkeypatch.setattr(migration_fence, "_active_project_lock_job_id", _no_locked_job)
    monkeypatch.setattr(migration_fence, "_active_cluster_lock_job_id", _no_locked_job)
    monkeypatch.setattr(migration_fence, "_organization_cluster_ids", _no_organization_clusters)
    monkeypatch.setattr(migration_fence, "_migration_owns_operation", _ordinary_operation)
    monkeypatch.setattr(migration_fence, "async_session_maker", _FenceSession)


@pytest.fixture(autouse=True)
def _isolate_rate_sampler(monkeypatch):
    """Keep rate baselines in-process and empty for every test.

    important: inside the backend container the `redis` hostname resolves, so without this a
    test would read and write real baselines shared with the running app, and its result would
    depend on what the live dashboard had sampled a second earlier.
    """
    from app.core import rate_sampler

    monkeypatch.setattr(rate_sampler, "_redis_client", lambda: None)
    rate_sampler.reset_all_samplers()
    yield
    rate_sampler.reset_all_samplers()
