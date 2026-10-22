"""Pytest bootstrap for the backend test suite.

``app.core.database`` raises at *import* time when ``DATABASE_URL`` is unset (see
its module top). Several test modules import router code that transitively pulls
in ``app.core.database``, so without this the ValueError is raised during
collection and takes down the entire ``tests/`` directory (19 passing -> 0
collected). conftest.py is imported before any test module, so setting a default
here keeps collection working. Tests that need a real database override it via
their own environment; this default is only a placeholder to satisfy the import.

important: the placeholder must be unreachable. It used to be localhost:5432, which on a
developer machine is the live dev database: every app lifespan a test starts launches the
background workers (webhooks, task journal, upload recovery, metrics history), and each one
logged in as "test", about 470 failed logins per run. Port 1 on loopback refuses at once, so
those workers fail fast and nothing outside the process is touched.
"""
import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://test:test@127.0.0.1:1/test",
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


@pytest.fixture(autouse=True)
def _isolate_vault_status():
    """Every test starts with no registry verdict.

    note: the verdict is module state. A test that drove a registry read into a failure left it
    "unknown", and a later test whose fixture registry is empty then answered 503: it passed or
    failed depending on which file pytest happened to collect before it.
    """
    from app.core import vault_status

    vault_status.reset()
    yield
    vault_status.reset()


@pytest.fixture(autouse=True)
def _isolate_response_cache(monkeypatch):
    """A memory-only response cache and empty single-flight and SWR state for every test.

    important: inside the backend container the `redis` hostname resolves, so a handler a test
    calls directly would otherwise read and write the running app's cache, and a background
    stale-while-revalidate read could outlive the test that started it.
    """
    from app.core import cache as cache_module
    from app.core import single_flight, swr
    from app.core.cache import CacheManager

    monkeypatch.setattr(cache_module, "_global_cache", CacheManager(redis_url=None))
    single_flight._locks.clear()
    single_flight._waiters.clear()
    single_flight._last_rebuild.clear()
    swr._refreshing.clear()
    yield
    for task in list(swr._refreshing.values()):
        task.cancel()
    swr._refreshing.clear()


@pytest.fixture(autouse=True)
def _isolate_monitoring_history_store():
    """Start every test with an empty history fallback buffer, body memo and import marks.

    The store keeps them as module state, so a point written by one test would otherwise answer
    another test's read.
    """
    from app.services import monitoring_history_store

    monitoring_history_store.reset_local_state()
    yield
    monitoring_history_store.reset_local_state()


@pytest.fixture(autouse=True)
def _isolate_ws_tickets():
    """A fresh in-process ticket store and identity cache for every test.

    note: lifespan never runs here, so the store stays the local one a backend without Redis
    uses; the Redis path is driven explicitly by test_ws_ticket_store.py.
    """
    from app.core.ws_ticket_store import WsTicketStore, set_ws_ticket_store
    from app.services.ws_identity import forget_identities

    # note: SECRET_KEY is unset here, so the store gets its signing key directly.
    set_ws_ticket_store(WsTicketStore(mac_key=b"test-only-signing-key"))
    forget_identities()
    yield
    set_ws_ticket_store(None)
    forget_identities()


@pytest.fixture(autouse=True)
def _rotated_certificate_reads_stay_hermetic(monkeypatch):
    """The refusal branch of _auto_reconnect reads the active certificate from Vault once.

    Settings load backend/.env, so on a machine whose .env enables Vault every refusal test would
    reach for a real Vault. Tests of the rotation itself replace this stub with their own reader.
    """
    from app.services.lxd.cert_rotation import CertificateRotationMixin

    monkeypatch.setattr(
        CertificateRotationMixin, "_read_active_certificate", lambda self, cluster_id: None
    )
