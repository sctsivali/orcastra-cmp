"""A key card counts an organization as solely owned unless another owner could still sync it.

The card said "Another owner key also owns it, which keeps that organization syncing" whenever
the owner set had a second row, even when that key had expired or lost org.sync. Revoke then met
the server's own count, which judges only owners that can still sync (able_owners), and was refused
with 409. The card now counts the same way, and with owner sets off every organization is the
primary's alone.
"""

from types import SimpleNamespace

from app.core.auth import AuthenticatedUser
from app.routers import integrations

ADMIN = AuthenticatedUser(user_id="role_admin:ops@example.test", email="ops@example.test", groups=["role_admin"])


def _info(key_id: str):
    return SimpleNamespace(id=key_id, owned_organization_count=None, owned_organization_slugs=[],
                           owned_organization_hidden_count=0, sole_owned_organization_count=None)


def _world(monkeypatch, able: dict, enabled: bool = True):
    async def owned(_db, key_ids):
        return {"oak_a": [(1, "acme"), (2, "globex")]}

    async def visible(_db, _user):
        return None

    async def sets(_db, org_ids):
        return {1: [SimpleNamespace(api_key_id="oak_a"), SimpleNamespace(api_key_id="oak_dead")],
                2: [SimpleNamespace(api_key_id="oak_a"), SimpleNamespace(api_key_id="oak_live")]}

    async def verdicts(key_ids, _reader):
        return {k: able.get(k) for k in key_ids}

    monkeypatch.setattr(integrations, "owned_by_keys", owned)
    monkeypatch.setattr(integrations, "visible_organization_ids", visible)
    monkeypatch.setattr(integrations, "owners_of", sets)
    monkeypatch.setattr(integrations, "able_owners", verdicts)
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: object())
    monkeypatch.setattr(integrations, "multi_owner_enabled", lambda: enabled)


async def test_a_dead_co_owner_leaves_the_organization_solely_owned(monkeypatch) -> None:
    _world(monkeypatch, {"oak_dead": False, "oak_live": True})
    [info] = await integrations._with_ownership(object(), ADMIN, [_info("oak_a")])
    assert info.owned_organization_count == 2
    assert info.sole_owned_organization_count == 1, "acme's other owner expired; globex's still syncs"


async def test_with_owner_sets_off_every_organization_is_the_keys_alone(monkeypatch) -> None:
    _world(monkeypatch, {"oak_dead": True, "oak_live": True}, enabled=False)
    [info] = await integrations._with_ownership(object(), ADMIN, [_info("oak_a")])
    assert info.sole_owned_organization_count == 2
