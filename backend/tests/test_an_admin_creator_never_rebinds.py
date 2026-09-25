"""A key whose creator holds role_admin never rebinds that Admin's personal clusters.

require_partner admits an Admin, so an Admin can mint an integration key, and its created_by is
the Admin. Whoever holds that key could push the Admin's email into an org the key claimed and
move the Admin's unbound clusters there. Nothing changed in Authentik, a rebind record was
written, and the clusters had moved.

The role is read from Authentik when the push arrives. The key's metadata names who minted it and
not their role, there is no users table, and a role can change after the key was minted. A role
Authentik cannot answer for rebinds nothing. The strict read itself, against Authentik's HTTP
answers, is in test_a_creator_role_authentik_cannot_confirm_rebinds_nothing.
"""

import logging
from typing import Any, Dict

import pytest

from app.routers import integrations
from app.services import authentik_sync, integration_cluster_rebind
from app.services.integration_roles import ROLE_ADMIN, ROLE_PARTNER
from tests._sqlite_session import org_tables, sqlite_session
from tests.test_a_capability_grant_leaves_a_record import PARTNER as PARTNER_USER
from tests.test_a_capability_grant_leaves_a_record import _FakeVault, _NoCache, _RecordingLogger
from tests.test_a_push_rebinds_only_into_the_org_its_key_claimed import (
    ATTACKER,
    PARTNER,
    SECRETS,
    body,
    bound,
    key,
    member,
    org,
    org_id,
    personal_cluster,
    push,
    world,  # noqa: F401 - the fixture
)

ADMIN = "admin@example.test"
K_ADMIN = "oak_admin_minted"


async def test_a_key_an_admin_minted_never_moves_the_admins_clusters(
    world: Dict[str, Any], caplog: pytest.LogCaptureFixture  # noqa: F811
) -> None:
    world["directory"].groups[ADMIN] = {ROLE_ADMIN}
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "admin-cluster", ADMIN)
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)

    await push(db, key(K_ADMIN, ADMIN), body(
        [org("ext-a", "held")],
        [member(ADMIN, "ext-a", "PARTNER"), member(ATTACKER, "ext-a", "PARTNER")],
    ))
    assert (await bound(db))["admin-cluster"] is None
    assert world["shares"] == [] and world["log"].calls == []
    assert world["directory"].groups[ADMIN] == {ROLE_ADMIN}
    assert not [c for c in world["directory"].changes if c[0] == ADMIN]
    (warning,) = [r for r in caplog.records if r.name == integration_cluster_rebind.__name__]
    assert K_ADMIN in warning.getMessage() and "is an Admin" in warning.getMessage()
    assert not any(secret in caplog.text for secret in SECRETS)


async def test_a_role_authentik_cannot_answer_for_rebinds_nothing(
    world: Dict[str, Any], caplog: pytest.LogCaptureFixture  # noqa: F811
) -> None:
    world["directory"].groups[PARTNER] = None
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "partner-cluster", PARTNER)
    caplog.set_level(logging.WARNING, logger=integration_cluster_rebind.__name__)

    await push(db, key("oak_partner", PARTNER),
               body([org("ext-p", "partner")], [member(PARTNER, "ext-p", "PARTNER")]))
    assert (await bound(db))["partner-cluster"] is None
    assert world["shares"] == [] and world["log"].calls == []
    (warning,) = [r for r in caplog.records if r.name == integration_cluster_rebind.__name__]
    assert "oak_partner" in warning.getMessage()
    assert "could not be read (authentik_unreachable)" in warning.getMessage()


async def test_an_unconfigured_authentik_rebinds_nothing(
    world: Dict[str, Any], monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The real service, with no Authentik configured, answers None and so fails closed."""
    real = authentik_sync.AuthentikSyncService()
    assert real.enabled is False
    monkeypatch.setattr(authentik_sync, "get_authentik_sync", lambda: real)
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "partner-cluster", PARTNER)
    await push(db, key("oak_partner", PARTNER),
               body([org("ext-p", "partner")], [member(PARTNER, "ext-p", "PARTNER")]))
    assert (await bound(db))["partner-cluster"] is None
    assert world["log"].calls == []


async def test_the_role_is_the_one_held_when_the_push_arrives(world: Dict[str, Any]) -> None:  # noqa: F811
    """Minted as a partner, promoted to Admin since: the push moves nothing."""
    db = sqlite_session(*org_tables())
    await personal_cluster(db, "partner-cluster", PARTNER)
    world["directory"].groups[PARTNER] = {ROLE_ADMIN}
    await push(db, key("oak_partner", PARTNER),
               body([org("ext-p", "partner")], [member(PARTNER, "ext-p", "PARTNER")]))
    assert (await bound(db))["partner-cluster"] is None

    world["directory"].groups[PARTNER] = {ROLE_PARTNER}
    await push(db, key("oak_partner", PARTNER),
               body([org("ext-p", "partner")], [member(PARTNER, "ext-p", "PARTNER")]))
    assert (await bound(db))["partner-cluster"] == await org_id(db, "partner")
    assert len(world["log"].calls) == 1


async def test_the_role_is_read_only_when_something_would_move(world: Dict[str, Any]) -> None:  # noqa: F811
    """The member upsert reads each member's groups once. The rebind adds one read, and only
    when the creator has a personal cluster and one destination."""
    db = sqlite_session(*org_tables())
    await push(db, key("oak_partner", PARTNER),
               body([org("ext-p", "partner")], [member(PARTNER, "ext-p", "PARTNER")]))
    assert world["directory"].reads == [PARTNER]

    await personal_cluster(db, "partner-cluster", PARTNER)
    await push(db, key("oak_partner", PARTNER),
               body([org("ext-p", "partner")], [member(PARTNER, "ext-p", "PARTNER")]))
    assert world["directory"].reads == [PARTNER, PARTNER, PARTNER]
    assert (await bound(db))["partner-cluster"] == await org_id(db, "partner")


async def test_a_minted_key_records_no_role_to_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Why the role comes from Authentik: the key's own metadata carries none."""
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
        None, PARTNER_USER, None, None,
    )
    (metadata,) = [data for path, data in vault.writes if path.endswith(f"/{created.api_key_id}/metadata")]
    assert metadata["created_by"] == PARTNER_USER.email
    assert not {name for name in metadata if "role" in name or "group" in name}
