"""Pytest bootstrap for the backend test suite.

``app.core.database`` raises at import time when ``DATABASE_URL`` is unset, and
several test modules import router code that pulls it in, so collection would fail
for the whole ``tests/`` directory. conftest.py is imported before any test module,
so a placeholder default is set here; tests that need a real database set their own.

important: the placeholder must be unreachable. Every app lifespan a test starts launches the
background workers (webhooks, task journal, upload recovery, metrics history), and against a
reachable database such as localhost:5432 each would attempt a login on every run. Port 1 on
loopback refuses at once, so the workers fail fast and nothing outside the process is touched.
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
    test survives into the next and results depend on collection order. Audit writes
    are stubbed out too: they are fire-and-forget tasks that would otherwise reach for
    a missing database and wait out the retry backoff on every registration.
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
def _grants_match_as_before_generations(monkeypatch):
    """Unit tests have no database for registration generations (services/cluster_generations.py).

    The default is the state before the legacy backfill: nothing adopted, adoption open, so every
    grant matches by id. Tests of generations put the real reader back.
    """
    from app.services import cluster_generations

    async def _before_generations(*, fresh: bool = False):
        return cluster_generations.Generations(active={}, closed=False)

    monkeypatch.setattr(cluster_generations, "current", _before_generations)


@pytest.fixture(autouse=True)
def _isolate_rate_sampler(monkeypatch):
    """Keep rate baselines in-process and empty for every test.

    important: inside the backend container the `redis` hostname resolves, so without this a
    test would read and write real baselines shared with the running app, and its result would
    depend on what the running dashboard had just sampled.
    """
    from app.core import rate_sampler

    monkeypatch.setattr(rate_sampler, "_redis_client", lambda: None)
    rate_sampler.reset_all_samplers()
    yield
    rate_sampler.reset_all_samplers()


@pytest.fixture(autouse=True)
def _isolate_vault_status():
    """Every test starts with no registry verdict.

    The verdict is module state. A test that drives a registry read into a failure leaves it
    "unknown", and a later test whose fixture registry is empty would then answer 503 depending
    on which file pytest collected before it.
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
def _isolate_pool_resource_failures():
    """A pool that failed in one test must not be refused in the next, nor a late read stand in."""
    from app.services.lxd import fanout, pool_resource_failures

    pool_resource_failures._failed.clear()
    fanout._late.clear()
    yield
    pool_resource_failures._failed.clear()
    fanout._late.clear()


@pytest.fixture(autouse=True)
def _isolate_cluster_breaker():
    """Every test starts with no breaker verdict for any cluster.

    Fan-out reads skip a cluster this worker last saw fail (fanout.known_down), and that memory
    has no TTL, so a failure one test recorded for a cluster would make a later test's read of
    the same cluster answer offline without dialing its stub.
    """
    from app.services.lxd import health

    health.forget_unregistered(())
    yield
    health.forget_unregistered(())


@pytest.fixture(autouse=True)
def _isolate_registration_budget():
    """Registration attempts counted by one test must not spend the next test's budget."""
    from app.services.registration_budget import forget_registration_budget

    forget_registration_budget()
    yield
    forget_registration_budget()


@pytest.fixture(autouse=True)
def _isolate_creator_roles():
    """A creator's role read by one test must not answer for the next."""
    from app.services.creator_role import forget_creator_roles

    forget_creator_roles()
    yield
    forget_creator_roles()


@pytest.fixture(autouse=True)
def _no_other_party_holds_default_for_a_key(monkeypatch):
    """Key doors judge `default` by the creator's party, which needs the database.

    The default is "no other party holds the cluster", so suites about something else stay
    hermetic. test_a_key_cannot_change_a_view_only_default.py puts the real reader back.
    """
    from app.services import key_default_view_only

    async def _not_held(api_key, _cluster_id, _db):
        return str(api_key.get("created_by") or ""), False

    monkeypatch.setattr(key_default_view_only, "_judge", _not_held)
    key_default_view_only.forget_key_default_verdicts()
    yield
    key_default_view_only.forget_key_default_verdicts()


@pytest.fixture(autouse=True)
def _no_party_bound_for_a_key(monkeypatch):
    """A key's grants are narrowed to its creator's bound only where another party holds the
    cluster, which needs the database. The default is "no bound", so the suites that drive `*`
    keys stay hermetic. test_a_key_is_held_to_its_creators_bound.py puts the real reader back.
    """
    from app.services import key_party_bound

    async def _no_bound(_api_key, _cluster_id, _db):
        return "", None

    monkeypatch.setattr(key_party_bound, "_bound", _no_bound)
    key_party_bound.forget_party_bounds()
    yield
    key_party_bound.forget_party_bounds()


@pytest.fixture(autouse=True)
def _no_other_party_holds_default_for_a_schedule(monkeypatch):
    """The schedule worker's view-only `default` rule reads parties from the database.

    Schedule suites run on fakes that answer only the queries they are about, so the default is
    "not refused". test_a_schedule_stops_in_a_view_only_default.py puts the real rule back.
    """
    from app.services.snapshots import schedule_authority

    async def _not_refused(_db, _schedule, *, roles=None):
        return None

    monkeypatch.setattr(schedule_authority, "default_view_only_refusal", _not_refused)


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

    Lifespan never runs here, so the store stays the local one a backend without Redis uses;
    test_ws_ticket_store.py drives the Redis path.
    """
    from app.core.file_ticket import set_file_ticket_store
    from app.core.ws_ticket_store import WsTicketStore, set_ws_ticket_store
    from app.services.ws_identity import forget_identities

    # SECRET_KEY is unset here, so the store gets its signing key directly.
    set_ws_ticket_store(WsTicketStore(mac_key=b"test-only-signing-key"))
    set_file_ticket_store(
        WsTicketStore(mac_key=b"test-only-file-key", ticket_prefix="orcastra:file_ticket", budget=120)
    )
    forget_identities()
    yield
    set_ws_ticket_store(None)
    set_file_ticket_store(None)
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
