"""A rescope that withdraws org.sync from an add-on that owns organizations is refused like an edit.

The capability edit and an uninstall both refuse (409 key_owns_organizations) when the key is the
only owner able to sync an active OrcaHub organization, since its sync and member updates would
stop. A rescope wrote the narrower capability set onto the credential without that check, so an
operator could freeze the organizations an add-on owns by consenting to a smaller grant. It now runs
the same refusal, before the row or the credential changes, and hands the primary to a co-owner
that can still sync when there is one.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, List

import pytest

from app.services import extension_install
from app.services.integration_key_ownership import KeyOwnsOrganizations


@asynccontextmanager
async def _held(*_a: Any, **_k: Any):
    yield


@pytest.fixture
def world(monkeypatch) -> SimpleNamespace:
    state = SimpleNamespace(asked=[], rewrites=[], install=SimpleNamespace(
        id=5, organization_id=9, granted_capabilities=["inventory.read", "org.sync"], granted_scope=[],
        api_key_id="oak_addon", previous_api_key_id=None,
    ), refuse=False)

    async def refuse(_db, key_id, **kw):
        state.asked.append((key_id, kw["withdrawn"]))
        if state.refuse:
            raise KeyOwnsOrganizations({"error": "key_owns_organizations"}, {})
        return []

    async def same(*_a, **_k):
        return state.install

    async def nothing(*_a, **_k):
        return None

    async def stamped(grants):
        return list(grants)

    async def no_orgs(*_a, **_k):
        return [1]

    async def no_handoffs(_db, _key, handoffs, remove):
        return []

    monkeypatch.setattr(extension_install.extension_service, "manifest_matches", lambda *_a: True)
    monkeypatch.setattr(extension_install, "assert_install_scope_within_partner", nothing)
    monkeypatch.setattr(extension_install.cluster_generations, "stamped", stamped)
    monkeypatch.setattr(extension_install, "lock_installation", same)
    monkeypatch.setattr(extension_install, "live_credentials", lambda install: [install.api_key_id])
    monkeypatch.setattr(extension_install, "serialize_key_ownership", _held)
    monkeypatch.setattr(extension_install, "serialize_org_owners", _held)
    monkeypatch.setattr(extension_install, "refuse_if_key_owns_organizations", refuse)
    monkeypatch.setattr("app.services.integration_key_ownership.owned_org_ids_to_lock", no_orgs)
    monkeypatch.setattr("app.services.integration_key_ownership.apply_owner_handoffs", no_handoffs)
    monkeypatch.setattr(extension_install, "_rewrite_key_grant", lambda _v, install: state.rewrites.append(
        sorted(install.granted_capabilities)) or {})
    monkeypatch.setattr("app.services.webhooks.emitter.emit_installation_event", nothing)
    return state


class _DB:
    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None


async def _rescope(world, capabilities: List[str]):
    extension = SimpleNamespace(requested_capabilities=["inventory.read", "org.sync"], requested_scope={},
                                manifest={})
    return await extension_install.rescope_install(
        _DB(), object(), install=world.install, extension=extension, user=SimpleNamespace(email="p@x"),
        is_admin=False, consent_version="1", consent_digest="d", capabilities=capabilities, grants=[],
    )


async def test_withdrawing_org_sync_from_a_sole_owner_is_refused_before_anything_changes(world) -> None:
    world.refuse = True
    with pytest.raises(KeyOwnsOrganizations):
        await _rescope(world, ["inventory.read"])
    assert world.asked == [("oak_addon", ("org.sync",))]
    assert world.rewrites == [] and world.install.granted_capabilities == ["inventory.read", "org.sync"]


async def test_a_rescope_that_keeps_org_sync_asks_nothing(world) -> None:
    await _rescope(world, ["inventory.read", "org.sync"])
    assert world.asked == [] and world.rewrites == [["inventory.read", "org.sync"]]
