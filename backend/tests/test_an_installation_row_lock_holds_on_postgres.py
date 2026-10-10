"""lock_installation against a real PostgreSQL: a second request waits for the first and reads its row.

A row lock cannot be seen in the hermetic suite, whose installs are plain objects. This module runs
against the disposable database CI migrates to head, through the same opt-in as the journal module
(`JOURNAL_TEST_DATABASE_URL`), inserts its own uniquely named rows and deletes them afterwards; it never
truncates. Locally:

    JOURNAL_TEST_DATABASE_URL=postgresql+asyncpg://user:pw@host:5432/scratch_test \
        python3 -m pytest -q tests/test_an_installation_row_lock_holds_on_postgres.py
"""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models.db_models import Organization
from app.models.extension_models import Extension, ExtensionInstallation
from app.services.extension_credential_guard import lock_installation

URL = os.environ.get("JOURNAL_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="JOURNAL_TEST_DATABASE_URL is not set")
if URL and not any(word in URL.rsplit("/", 1)[-1].lower() for word in ("scratch", "test")):
    raise RuntimeError("JOURNAL_TEST_DATABASE_URL must name a scratch or test database")


@asynccontextmanager
async def installation() -> AsyncIterator[Dict[str, object]]:
    engine = create_async_engine(URL, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False, class_=AsyncSession)
    tag = uuid.uuid4().hex[:12]
    ids: Dict[str, int] = {}
    try:
        async with maker() as setup:
            org = Organization(name=f"Lock {tag}", slug=f"lock-{tag}", source="local",
                               created_by="qa@example.test", is_active=True)
            setup.add(org)
            await setup.flush()
            ext = Extension(slug=f"lock-{tag}", name="Lock", publisher="QA", version="1.0.0",
                            owner_email="qa@example.test", requested_capabilities=["inventory.read"],
                            requested_scope=[], manifest={}, manifest_digest="0" * 64)
            setup.add(ext)
            await setup.flush()
            row = ExtensionInstallation(extension_id=ext.id, organization_id=org.id,
                                        granted_capabilities=["inventory.read"],
                                        granted_scope=[{"cluster_id": "c1", "permissions": ["read"], "projects": ["*"]}],
                                        api_key_id=f"oak_lock_{tag}_1", consent_version="1.0.0",
                                        consent_manifest_digest="0" * 64, installed_by="qa@example.test")
            setup.add(row)
            await setup.commit()
            ids = {"org": org.id, "ext": ext.id, "install": row.id}
        yield {"maker": maker, "tag": tag, **ids}
    finally:
        if ids:
            async with maker() as cleanup:
                await cleanup.execute(delete(ExtensionInstallation).where(ExtensionInstallation.id == ids["install"]))
                await cleanup.execute(delete(Extension).where(Extension.id == ids["ext"]))
                await cleanup.execute(delete(Organization).where(Organization.id == ids["org"]))
                await cleanup.commit()
        await engine.dispose()


async def _waits_on_a_lock(maker: async_sessionmaker, pid: int) -> bool:
    async with maker() as probe:
        row = (await probe.execute(
            text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}
        )).first()
    return bool(row) and row[0] == "Lock"


async def test_a_second_request_waits_for_the_first_and_reads_its_new_key() -> None:
    async with installation() as world:
        maker, tag = world["maker"], world["tag"]
        async with maker() as first, maker() as second:
            stale = (await second.execute(
                select(ExtensionInstallation).where(ExtensionInstallation.id == world["install"])
            )).scalar_one()
            second_pid = (await second.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            mine = await lock_installation(first, await first.get(ExtensionInstallation, world["install"]))
            mine.api_key_id = f"oak_lock_{tag}_2"
            mine.credential_generation = 2
            await first.flush()

            waiting = asyncio.create_task(lock_installation(second, stale))
            for _ in range(200):
                if await _waits_on_a_lock(maker, second_pid):
                    break
                await asyncio.sleep(0.05)
            else:
                waiting.cancel()
                pytest.fail("the second request did not wait for the first one's row lock")
            assert not waiting.done()
            await first.commit()

            fresh = await asyncio.wait_for(waiting, 10)
            assert fresh is stale, "populate_existing must refresh the session's own copy"
            assert (fresh.api_key_id, fresh.credential_generation) == (f"oak_lock_{tag}_2", 2)
            await second.rollback()


async def test_the_lock_leaves_a_foreign_key_check_free() -> None:
    # note: an organization delete's RESTRICT check takes FOR KEY SHARE on the installation rows; a
    # rotation holding FOR NO KEY UPDATE across its Vault calls must not hold it up.
    async with installation() as world:
        maker = world["maker"]
        async with maker() as first, maker() as other:
            await lock_installation(first, await first.get(ExtensionInstallation, world["install"]))
            shared = other.execute(text(
                "SELECT id FROM extension_installations WHERE id = :id FOR KEY SHARE"
            ), {"id": world["install"]})
            assert (await asyncio.wait_for(shared, 5)).scalar_one() == world["install"]
            await other.rollback()
            await first.rollback()


async def test_one_failed_row_does_not_end_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    # note: a rollback expires every object in the session; the sweep used to read the next
    # installation's attributes after one, which raises on an AsyncSession, so one failed row ended the
    # pass for every other installation and the orphan pass too.
    from app.models.db_models import OrganizationCluster
    from app.services import extension_reconcile as sweep

    engine = create_async_engine(URL, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False, class_=AsyncSession)
    tag = uuid.uuid4().hex[:12]
    ids: Dict[str, list] = {"install": [], "ext": [], "org": []}
    grant = [{"cluster_id": f"kept-{tag}", "permissions": ["read"], "projects": ["*"]},
             {"cluster_id": f"lost-{tag}", "permissions": ["read"], "projects": ["*"]}]
    try:
        async with maker() as setup:
            org = Organization(name=f"Sweep {tag}", slug=f"sweep-{tag}", source="local",
                               created_by="qa@example.test", is_active=True)
            setup.add(org)
            await setup.flush()
            setup.add(OrganizationCluster(organization_id=org.id, cluster_id=f"kept-{tag}"))
            ids["org"].append(org.id)
            for n in (1, 2):
                ext = Extension(slug=f"sweep-{tag}-{n}", name="Sweep", publisher="QA", version="1.0.0",
                                owner_email="qa@example.test", requested_capabilities=["inventory.read"],
                                requested_scope=[], manifest={}, manifest_digest="0" * 64)
                setup.add(ext)
                await setup.flush()
                row = ExtensionInstallation(extension_id=ext.id, organization_id=org.id,
                                            granted_capabilities=["inventory.read"], granted_scope=grant,
                                            api_key_id=f"oak_sweep_{tag}_{n}", consent_version="1.0.0",
                                            consent_manifest_digest="0" * 64, installed_by="qa@example.test")
                setup.add(row)
                await setup.flush()
                ids["ext"].append(ext.id)
                ids["install"].append(row.id)
            await setup.commit()

        rewritten = []

        def _rewrite(_client, install):
            if install.api_key_id.endswith("_1"):
                raise RuntimeError("vault is sealed for this one")
            rewritten.append(install.api_key_id)
            return {}

        monkeypatch.setattr(sweep, "_rewrite_key_grant", _rewrite)
        monkeypatch.setattr(sweep, "evict_install_cache", lambda _k: None)
        monkeypatch.setattr(sweep.activity_logger, "log_activity", lambda **_kw: None)
        async with maker() as db:
            await sweep._narrow_drifted_scope(db, object())
        assert rewritten == [f"oak_sweep_{tag}_2"], "the pass ended at the first failed row"
        async with maker() as check:
            scopes = dict((await check.execute(
                select(ExtensionInstallation.api_key_id, ExtensionInstallation.granted_scope)
                .where(ExtensionInstallation.id.in_(ids["install"]))
            )).all())
        assert scopes[f"oak_sweep_{tag}_2"] == grant[:1] and scopes[f"oak_sweep_{tag}_1"] == grant
    finally:
        async with maker() as cleanup:
            await cleanup.execute(delete(ExtensionInstallation).where(ExtensionInstallation.id.in_(ids["install"])))
            await cleanup.execute(delete(Extension).where(Extension.id.in_(ids["ext"])))
            await cleanup.execute(delete(OrganizationCluster).where(OrganizationCluster.organization_id.in_(ids["org"])))
            await cleanup.execute(delete(Organization).where(Organization.id.in_(ids["org"])))
            await cleanup.commit()
        await engine.dispose()
