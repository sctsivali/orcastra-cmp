"""An add-on credential that owns OrcaHub organizations keeps them through rotation and uninstall.

An add-on may hold org.sync, so its credential can sync, and so own, an OrcaHub organization; only
the owner key may sync it afterwards. A rotation moves the old credential's organizations to the
new one in the same commit as the installation row. Uninstall destroys both credentials, so it is
refused while either owns an organization, and the answer points to suspending the add-on instead.
The reconcile sweep still revokes a credential that must stop, and records the organizations that
freezes.
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List, Optional

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.auth import AuthenticatedUser
from app.models.db_models import Organization
from app.routers import extension_installs, integrations
from app.services import activity_logger as activity_logger_module
from app.services import extension_install, integration_key_ownership
from app.services import extension_reconcile as sweep
from app.services.integration_key_ownership import OWNERSHIP_UNKNOWN_DETAIL, TRANSFER_ACTION, KeyOwnsOrganizations
from tests._sqlite_session import AsyncSessionFacade, org_tables, sqlite_session
from tests.test_a_capability_grant_leaves_a_record import _FakeVault, _RecordingLogger
from tests.test_the_one_org_rule_holds_through_the_sync_route import send

OLD, NEW = "oak_addon_gen1", "oak_addon_gen2"
PARTNER = AuthenticatedUser(user_id="p1", email="partner@example.test", groups=["role_partner"])
ADMIN = AuthenticatedUser(user_id="a1", email="admin@example.test", groups=["role_admin"])


class Session(AsyncSessionFacade):
    """The SQLite facade, with refresh tolerating the plain install object these tests hand in."""

    async def refresh(self, instance: Any) -> None:
        if isinstance(instance, Organization):
            self.sync.refresh(instance)


def world_db() -> Session:
    db = sqlite_session(*org_tables())
    return Session(db.sync)


def own(db: Session, key_id: str, slug: str, *, active: bool = True) -> None:
    db.add(Organization(name=slug.title(), slug=slug, source="orcahub", external_id=f"ext-{slug}",
                        api_key_id=key_id, created_by="system", is_active=active))
    db.sync.commit()


def owners(db: Session) -> Dict[str, Optional[str]]:
    db.sync.expire_all()
    return dict(db.sync.execute(select(Organization.slug, Organization.api_key_id)).all())


def addon_key(key_id: str) -> Dict[str, Any]:
    return {"id": key_id, "name": "backup-bot @ acme", "app_name": "Backup Bot", "app_type": "automation",
            "capabilities": ["inventory.read", "org.sync"], "created_by": PARTNER.email,
            "created_at": "2026-09-01T00:00:00+00:00", "expires_at": None, "is_active": True, "is_revoked": False,
            "cluster_permissions": [], "extension_install_id": 7}


async def unchanged(_db: Any, record: Any) -> Any:
    """lock_installation as production sees it here: the same object, nothing committed in between."""
    return record


def install(api_key_id: str = OLD, previous: Optional[str] = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=7, organization_id=1, api_key_id=api_key_id, previous_api_key_id=previous,
        previous_key_expires_at=None, credential_generation=1, last_scope_change_at=None, updated_at=None,
        granted_capabilities=["inventory.read", "org.sync"], granted_scope=[],
    )


@pytest.fixture
def rotation(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    monkeypatch.setenv("SECRET_KEY", "a-test-secret-key-for-hmac")
    vault = _FakeVault(addon_key(OLD))
    log = _RecordingLogger()
    locks: List[str] = []
    real_lock = integration_key_ownership.serialize_key_ownership

    @asynccontextmanager
    async def _seam(db: Any, key_id: str) -> AsyncIterator[None]:
        locks.append(key_id)
        async with real_lock(db, key_id):
            yield

    monkeypatch.setattr(extension_install, "serialize_key_ownership", _seam)
    monkeypatch.setattr(extension_install, "lock_installation", unchanged)
    monkeypatch.setattr(activity_logger_module, "get_activity_logger", lambda: log)
    monkeypatch.setattr(extension_install, "evict_install_cache", lambda _k: None)
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: SimpleNamespace(set=lambda *a, **k: None))
    monkeypatch.setattr(
        extension_install, "get_settings",
        lambda: SimpleNamespace(extension_credential_min_rotate_seconds=0, extension_credential_max_grace_seconds=600,
                                extension_credential_expire_days=30),
    )
    return {"vault": vault, "log": log, "locks": locks}


async def rotate(db: Session, vault: Any, record: SimpleNamespace, grace: int = 0, *,
                 actor: str = ADMIN.email, is_admin: bool = True) -> str:
    _record, new_key, _secret = await extension_install.rotate_credential(
        db, vault, install=record, extension=SimpleNamespace(slug="backup-bot", name="Backup Bot"),
        organization_slug="acme", actor=actor, is_admin=is_admin, grace_seconds=grace,
    )
    return new_key


# Rotation

@pytest.mark.parametrize("grace", [0, 300], ids=["no-grace", "grace"])
async def test_a_rotation_carries_the_organizations_to_the_new_credential(rotation: Dict[str, Any], grace: int) -> None:
    db = world_db()
    own(db, OLD, "addon-org")
    own(db, OLD, "addon-retired", active=False)
    own(db, "oak_someone_else", "theirs")
    record = install()
    new_key = await rotate(db, rotation["vault"], record, grace)
    assert record.api_key_id == new_key != OLD
    assert owners(db) == {"addon-org": new_key, "addon-retired": new_key, "theirs": "oak_someone_else"}
    assert rotation["locks"] == [OLD]
    records = [c for c in rotation["log"].calls if c["action"] == TRANSFER_ACTION]
    assert [(r["target_organization_slug"], r["details"]["trigger"]) for r in records] == [
        ("addon-org", "extension.install.credential.rotate"), ("addon-retired", "extension.install.credential.rotate"),
    ]
    assert all((r["details"]["from_key_id"], r["details"]["to_key_id"]) == (OLD, new_key) for r in records)
    assert all(r["before_state"] == {"owner_key_id": OLD} and r["after_state"] == {"owner_key_id": new_key}
               for r in records)


async def test_the_new_credential_syncs_them_and_the_old_one_is_refused(rotation: Dict[str, Any]) -> None:
    db = world_db()
    own(db, OLD, "addon-org")
    new_key = await rotate(db, rotation["vault"], install(), grace=300)
    body = {"include_clusters": False, "include_projects": False,
            "organizations": [{"id": "ext-addon-org", "name": "Renamed", "slug": "addon-org"}]}
    stale = await send(addon_key(OLD), db, body)
    assert stale.json()["organizations_synced"] == 0, "the old credential still synced inside its grace"
    fresh = await send(addon_key(new_key), db, body)
    assert fresh.json()["organizations_synced"] == 1


async def test_a_rotation_of_a_credential_that_owns_nothing_moves_nothing(rotation: Dict[str, Any]) -> None:
    db = world_db()
    own(db, "oak_someone_else", "theirs")
    await rotate(db, rotation["vault"], install())
    assert owners(db) == {"theirs": "oak_someone_else"}
    assert [c for c in rotation["log"].calls if c["action"] == TRANSFER_ACTION] == []


# Uninstall

@pytest.fixture
def teardown(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    torn: List[str] = []

    async def _teardown(_db: Any, _vault: Any, *, install_id: int, api_key_id: str,
                        previous_api_key_id: Optional[str] = None) -> List[str]:
        torn.append(api_key_id)
        return ["vault:api_key_metadata"]

    monkeypatch.setattr(extension_install, "teardown_install", _teardown)
    monkeypatch.setattr(extension_install, "lock_installation", unchanged)
    return torn


@pytest.mark.parametrize("current, previous", [(OLD, None), (NEW, OLD)], ids=["current-key", "previous-key"])
async def test_uninstalling_an_owner_credential_is_refused(teardown: List[str], current: str,
                                                           previous: Optional[str]) -> None:
    db = world_db()
    own(db, OLD, "addon-org")
    with pytest.raises(KeyOwnsOrganizations) as refused:
        await extension_install.uninstall(db, object(), install=install(current, previous), user=ADMIN)
    detail = refused.value.detail
    assert refused.value.status_code == 409
    assert (detail["error"], detail["action"], detail["api_key_id"]) == ("key_owns_organizations", "uninstall", OLD)
    assert detail["message"].startswith("This key owns 1 OrcaHub organization (addon-org). Uninstalling the add-on")
    assert detail["message"].endswith("Suspend the add-on to stop it now.")
    assert detail["remedies"] == ["transfer_owner", "delete_organization", "suspend_add_on"]
    assert teardown == [] and owners(db) == {"addon-org": OLD}


async def test_a_partner_is_told_only_what_it_may_see(teardown: List[str]) -> None:
    db = world_db()
    own(db, OLD, "addon-org")
    with pytest.raises(KeyOwnsOrganizations) as refused:
        await extension_install.uninstall(db, object(), install=install(), user=PARTNER)
    assert refused.value.detail["organization_slugs"] == [] and refused.value.detail["hidden_organization_count"] == 1
    assert "addon-org" not in refused.value.detail["message"]


async def test_an_add_on_that_owns_nothing_uninstalls_as_before(teardown: List[str]) -> None:
    db = world_db()
    own(db, "oak_someone_else", "theirs")
    own(db, OLD, "retired", active=False)
    assert await extension_install.uninstall(db, object(), install=install(), user=ADMIN) == ["vault:api_key_metadata"]
    assert teardown == [OLD]


async def test_ownership_that_cannot_be_read_uninstalls_nothing(teardown: List[str]) -> None:
    class _Down:
        async def execute(self, *_a: Any, **_k: Any) -> Any:
            raise ConnectionError("database is down: postgresql://user:pw@db/app")

    with pytest.raises(HTTPException) as refused:
        await extension_install.uninstall(_Down(), object(), install=install(), user=ADMIN)
    assert (refused.value.status_code, refused.value.detail) == (503, OWNERSHIP_UNKNOWN_DETAIL)
    assert teardown == []


async def test_the_route_records_the_refusal_on_the_uninstall_action(
    teardown: List[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db = world_db()
    own(db, OLD, "addon-org")
    log = _RecordingLogger()
    monkeypatch.setattr(extension_installs, "activity_logger", log)
    monkeypatch.setattr(extension_installs, "get_vault_integrations_client", lambda: object())

    async def _load(_db: Any, _install_id: int, _user: Any) -> tuple:
        return install(), SimpleNamespace(slug="backup-bot"), "acme"

    monkeypatch.setattr(extension_installs, "_load", _load)
    with pytest.raises(KeyOwnsOrganizations):
        await extension_installs.uninstall(7, None, ADMIN, db)
    (record,) = log.calls
    assert (record["action"], record["result"], record["error_code"]) == (
        "extension.install.uninstall", "denied", "key_owns_organizations",
    )
    assert record["details"]["owned_organization_slugs"] == ["addon-org"]
    assert record["details"]["owned_organization_count"] == 1
    assert teardown == []


# The reconcile sweep still revokes, and records what that freezes.

async def test_the_sweep_reads_what_a_credential_owns() -> None:
    db = world_db()
    own(db, OLD, "b-org")
    own(db, OLD, "a-org")
    own(db, OLD, "retired", active=False)
    assert await sweep._owned_by(db, OLD) == ["a-org", "b-org"]
    assert await sweep._owned_by(db, "oak_nothing") == []


async def test_an_unreadable_ownership_is_recorded_as_unknown() -> None:
    rolled: List[bool] = []

    class _Down:
        async def execute(self, *_a: Any, **_k: Any) -> Any:
            raise ConnectionError("database is down")

        async def rollback(self) -> None:
            rolled.append(True)

    assert await sweep._owned_by(_Down(), OLD) is None and rolled == [True]
    assert sweep._frozen_fields(None) == {"owned_organization_count": None}


def test_an_orphan_that_owned_organizations_is_revoked_and_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_the_reconcile_sweep_narrows_and_reclaims import FakeInstall, FakeVault, OrphanDB

    log = _RecordingLogger()
    vault = FakeVault([{"id": "orphan", "extension_install_id": 99}, {"id": "clean", "extension_install_id": 98}])

    async def _owned(_db: Any, key_id: str) -> List[str]:
        return ["addon-org"] if key_id == "orphan" else []

    monkeypatch.setattr("app.routers.integrations.list_api_keys_from_vault", lambda _c: vault.keys)
    monkeypatch.setattr(sweep, "revoke_install_credential", lambda _c, key_id: vault.revoked.append(key_id))
    monkeypatch.setattr(sweep, "activity_logger", log)
    monkeypatch.setattr(sweep, "_owned_by", _owned)
    db = OrphanDB(installs=[FakeInstall(id=1, organization_id=3, api_key_id="live", granted_scope=[])])
    assert asyncio.run(sweep._revoke_orphaned_credentials(db, vault)) == 2
    assert vault.revoked == ["orphan", "clean"], "owning organizations must not keep an orphan alive"
    details = {c["details"]["key_id"]: c["details"] for c in log.calls}
    assert details["orphan"]["owned_organization_slugs"] == ["addon-org"]
    assert details["orphan"]["warning"] == sweep.FROZEN_WARNING
    assert "warning" not in details["clean"] and details["clean"]["owned_organization_count"] == 0


def test_a_credential_left_reaching_nothing_is_revoked_and_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_the_reconcile_sweep_narrows_and_reclaims import CredentialStore, FakeInstall, ScopeDB, lock_from

    log = _RecordingLogger()
    store = CredentialStore()
    record = FakeInstall(id=2, organization_id=3, api_key_id=OLD, granted_scope=[{"cluster_id": "sold-on"}])

    async def _bound(_db: Any, _org: int) -> set:
        return set()

    async def _owned(_db: Any, _key: str) -> List[str]:
        return ["addon-org"]

    monkeypatch.setattr(sweep, "_bound_clusters", _bound)
    monkeypatch.setattr(sweep, "evict_install_cache", lambda _k: None)
    monkeypatch.setattr(sweep, "activity_logger", log)
    monkeypatch.setattr(sweep, "_rewrite_key_grant", store.rewrite)
    monkeypatch.setattr(sweep, "revoke_install_credential", store.revoke)
    monkeypatch.setattr(sweep, "_owned_by", _owned)
    monkeypatch.setattr(sweep, "lock_installation_id", lock_from([record]))
    assert asyncio.run(sweep._narrow_drifted_scope(ScopeDB(installs=[record], bindings={}), object())) == 1
    assert store.revoked == [OLD]
    (narrowed,) = log.calls
    assert narrowed["details"]["owned_organization_slugs"] == ["addon-org"]
    assert narrowed["details"]["warning"] == sweep.FROZEN_WARNING
