"""An integration push rebinds personal clusters only for the partner who minted the key.

The member sync ran reconcile_personal_clusters for every pushed email. A key that pushed another
partner's email into its own organization moved that partner's unbound clusters into it, where
the key's own partner is a member. The share audit it passed through checks configuration, not
consent. Now only the key's creator (`created_by`, written when the key is minted) triggers a
rebind, and every rebind leaves a record naming the cluster, where it came from, where it went and
the key. Nothing else of the key reaches that record.
"""

from typing import Any, Dict, List, Optional, Tuple

import pytest
from sqlalchemy import select

from app.core.org_scope import reconcile_personal_clusters
from app.routers import integrations
from app.routers.integrations import SyncDataRequest, SyncMember, SyncOrganization
from app.models.db_models import Organization, OrganizationCluster
from app.services import authentik_sync, integration_cluster_rebind, shared_cluster_audit
from app.services.activity_logger import ACTION_CATEGORIES, HIGH_SEVERITY_ACTIONS, EventCategory
from app.services.activity_logger import get_activity_logger
from app.services.extension_credentials import build_install_metadata
from app.services.integration_cluster_rebind import REBIND_ACTION, creator_of
from app.services.integration_roles import ROLE_PARTNER
from tests._sqlite_session import AsyncSessionFacade, org_tables, sqlite_session
from tests.test_a_capability_grant_leaves_a_record import PARTNER, _NoCache, _RecordingLogger
from tests.test_a_capability_grant_leaves_a_record import _FakeVault
from tests.test_an_integration_key_never_demotes import FakeAuthentik

KEY = "oak_seller"
CREATOR = "seller@example.test"
VICTIM = "victim-partner@example.test"
ATTACKER = "attacker@example.test"
SECRETS = ("oas_do_not_log_me", "hash-do-not-log", "https://callback.example.test")


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """The rebind's two side effects, recorded: the share audit it runs and the record it writes."""
    holder: Dict[str, Any] = {"log": _RecordingLogger(), "shares": []}
    monkeypatch.setattr(integration_cluster_rebind, "activity_logger", holder["log"])
    monkeypatch.setattr(authentik_sync, "get_authentik_sync", lambda: FakeAuthentik({ROLE_PARTNER}))

    async def _share_audit(_db: Any, cluster_id: str, **kwargs: Any) -> None:
        holder["shares"].append(
            (cluster_id, kwargs.get("prospective_organization_id"), kwargs.get("prospective_registered_by"))
        )

    monkeypatch.setattr(shared_cluster_audit, "assert_cluster_safe_to_share", _share_audit)
    return holder


async def _db() -> AsyncSessionFacade:
    db = sqlite_session(*org_tables())
    db.add(Organization(
        name="Mine", slug="mine", source="orcahub", external_id="ext-mine",
        api_key_id=KEY, created_by="system", is_active=True,
    ))
    await db.commit()
    db.add(OrganizationCluster(organization_id=None, cluster_id="creator-cluster", registered_by=CREATOR))
    db.add(OrganizationCluster(organization_id=None, cluster_id="victim-cluster", registered_by=VICTIM))
    await db.commit()
    return db


async def _bound(db: AsyncSessionFacade) -> Dict[str, Optional[int]]:
    rows = (await db.execute(select(OrganizationCluster))).scalars().all()
    return {row.cluster_id: row.organization_id for row in rows}


def _member(email: str, role: str = "TENANT") -> SyncMember:
    return SyncMember(email=email, organization_id="ext-mine", role=role)


def _key(**extra: Any) -> Dict[str, Any]:
    return {
        "id": KEY, "app_name": "OrcaHub", "app_type": "orcahub",
        "capabilities": ["inventory.read", "org.sync"], "cluster_permissions": [],
        "created_by": CREATOR, "app_url": SECRETS[2], "hash": SECRETS[1], "raw": SECRETS[0],
        **extra,
    }


async def _sync(db: AsyncSessionFacade, api_key: Dict[str, Any], members: List[SyncMember]) -> Any:
    request = SyncDataRequest(
        include_clusters=False, include_projects=False, include_instances=False,
        organizations=[SyncOrganization(id="ext-mine", name="Mine", slug="mine")],
        members=members,
    )
    return await integrations.sync_data(request, api_key, None, db)


# ── the probe: another partner's email ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("creator", [CREATOR, None])
async def test_pushing_another_partners_email_moves_none_of_their_clusters(
    world: Dict[str, Any], creator: Optional[str]
) -> None:
    db = await _db()
    await integrations._upsert_synced_members(
        db, [_member(VICTIM), _member(ATTACKER, "PARTNER")], KEY, creator, ("ext-mine",)
    )
    assert (await _bound(db))["victim-cluster"] is None
    assert world["shares"] == []
    assert world["log"].calls == []


async def test_the_whole_sync_rebinds_only_for_the_creator(world: Dict[str, Any]) -> None:
    db = await _db()
    result = await _sync(db, _key(), [_member(CREATOR, "PARTNER"), _member(VICTIM)])
    assert result.members_synced == 2
    assert await _bound(db) == {"creator-cluster": 1, "victim-cluster": None}
    assert world["shares"] == [("creator-cluster", 1, CREATOR)]
    (call,) = world["log"].calls
    assert call["action"] == REBIND_ACTION
    assert call["target_id"] == "creator-cluster" and call["target_organization_id"] == 1
    assert call["details"] == {
        "cluster_id": "creator-cluster",
        "from_organization_id": None,
        "to_organization_id": 1,
        "registered_by": CREATOR,
        "api_key_id": KEY,
    }
    assert not any(secret in repr(call) for secret in SECRETS)


@pytest.mark.parametrize("created_by", ["absent", None, "", "   ", 42])
async def test_a_key_without_a_creator_rebinds_nothing(world: Dict[str, Any], created_by: Any) -> None:
    api_key = _key()
    if created_by == "absent":
        del api_key["created_by"]
    else:
        api_key["created_by"] = created_by
    db = await _db()
    await _sync(db, api_key, [_member(CREATOR, "PARTNER"), _member(VICTIM)])
    assert await _bound(db) == {"creator-cluster": None, "victim-cluster": None}
    assert world["shares"] == [] and world["log"].calls == []


async def test_another_spelling_of_the_creator_is_not_the_creator(world: Dict[str, Any]) -> None:
    """A cluster registered under the other spelling, so only the creator comparison stops it."""
    other = CREATOR.upper()
    db = await _db()
    db.add(OrganizationCluster(organization_id=None, cluster_id="other-spelling", registered_by=other))
    await db.commit()
    await _sync(db, _key(), [_member(other, "PARTNER")])
    assert (await _bound(db))["other-spelling"] is None
    assert world["shares"] == [] and world["log"].calls == []

    # control: the same push under a key whose creator is that spelling does move it
    await _sync(db, _key(created_by=other), [_member(other, "PARTNER")])
    assert (await _bound(db))["other-spelling"] == 1


def test_the_creator_comparison_is_exact() -> None:
    assert integration_cluster_rebind.may_rebind(CREATOR, CREATOR)
    assert not integration_cluster_rebind.may_rebind(CREATOR.upper(), CREATOR)
    assert not integration_cluster_rebind.may_rebind(f" {CREATOR}", CREATOR)
    assert not integration_cluster_rebind.may_rebind(CREATOR, None)


# ── the creator's own push ──────────────────────────────────────────────────────────────────────

async def test_the_creators_push_binds_their_clusters_once(world: Dict[str, Any]) -> None:
    db = await _db()
    await integrations._upsert_synced_members(db, [_member(CREATOR, "PARTNER")], KEY, CREATOR, ("ext-mine",))
    assert await _bound(db) == {"creator-cluster": 1, "victim-cluster": None}
    assert len(world["log"].calls) == 1

    await integrations._upsert_synced_members(db, [_member(CREATOR, "PARTNER")], KEY, CREATOR, ("ext-mine",))
    assert len(world["log"].calls) == 1, "a repeat push moves nothing, so it records nothing"


async def test_the_record_reaches_the_index_unredacted() -> None:
    details = {
        "cluster_id": "c", "from_organization_id": None, "to_organization_id": 1,
        "registered_by": CREATOR, "api_key_id": KEY,
    }
    assert get_activity_logger()._sanitize_details(dict(details)) == details


def test_the_rebind_action_is_catalogued_high_severity() -> None:
    assert REBIND_ACTION == "integration.cluster.rebind"
    assert ACTION_CATEGORIES.get(REBIND_ACTION) is EventCategory.ADMIN
    assert REBIND_ACTION in HIGH_SEVERITY_ACTIONS


# ── reconcile_personal_clusters reports what it moved ────────────────────────────────────────────

async def test_each_move_is_reported_and_a_dropped_duplicate_is_not(world: Dict[str, Any]) -> None:
    db = await _db()
    db.add(OrganizationCluster(organization_id=1, cluster_id="already-there"))
    db.add(OrganizationCluster(organization_id=None, cluster_id="already-there", registered_by=CREATOR))
    await db.commit()
    await integrations._upsert_synced_members(db, [_member(CREATOR, "PARTNER")], KEY, None, ("ext-mine",))

    moved: List[Tuple[str, Optional[int], int]] = []
    rebound = await reconcile_personal_clusters(
        db, CREATOR, on_rebind=lambda *move: moved.append(move)
    )
    assert rebound == 1
    assert moved == [("creator-cluster", None, 1)]
    rows = (await db.execute(
        select(OrganizationCluster.organization_id).where(OrganizationCluster.cluster_id == "already-there")
    )).scalars().all()
    assert rows == [1]


# ── the creator is the minting login ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("metadata,expected", [
    ({"created_by": CREATOR}, CREATOR),
    ({"created_by": "  "}, None),
    ({"created_by": None}, None),
    ({}, None),
    ({"created_by": ["x"]}, None),
])
def test_creator_of(metadata: Dict[str, Any], expected: Optional[str]) -> None:
    assert creator_of(metadata) == expected


async def test_a_partner_minting_a_key_is_written_as_its_creator(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _nothing(*_a: Any, **_k: Any) -> None:
        return None

    vault = _FakeVault({})
    monkeypatch.setenv("SECRET_KEY", "a-test-secret-key-for-hmac")
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: vault)
    monkeypatch.setattr(integrations, "get_cache_manager", lambda: _NoCache())
    monkeypatch.setattr(integrations, "activity_logger", _RecordingLogger())
    monkeypatch.setattr(integrations, "reconcile_personal_clusters", _nothing)
    monkeypatch.setattr(integrations, "_assert_granted_projects_exist", _nothing)
    created = await integrations.create_api_key(
        integrations.APIKeyCreateRequest(name="k", app_name="OrcaHub", app_type="orcahub"),
        None, PARTNER, None, None,
    )
    (metadata,) = [data for path, data in vault.writes if path.endswith(f"/{created.api_key_id}/metadata")]
    assert metadata["created_by"] == PARTNER.email
    assert creator_of(metadata) == PARTNER.email


def test_an_add_on_credential_has_no_creator_to_rebind_for() -> None:
    """Its created_by names the installer, but a third party holds the key."""
    metadata = build_install_metadata(
        key_id="oak_addon", extension_slug="x", extension_name="X", install_id=1,
        organization_id=1, organization_slug="mine", capabilities=["org.sync"],
        cluster_permissions=[], created_by=CREATOR, expire_days=30,
    )
    assert metadata["created_by"] == CREATOR
    assert creator_of(metadata) is None


async def test_an_add_on_pushing_its_installer_moves_nothing(world: Dict[str, Any]) -> None:
    db = await _db()
    await _sync(db, _key(extension_install_id=7), [_member(CREATOR, "PARTNER")])
    assert await _bound(db) == {"creator-cluster": None, "victim-cluster": None}
    assert world["shares"] == [] and world["log"].calls == []
