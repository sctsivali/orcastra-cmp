"""Two security actions that reported success while doing nothing when Vault was unreachable.

The request path reads an installation's capabilities and clusters from the Vault key, not from
the database row. So a Vault write that is skipped rather than raised leaves the row saying one
thing and the credential doing another, with the operator shown the row.

- `_rewrite_key_grant` swallowed a read failure and returned. `rescope_install` then committed the
  narrower grant while the credential kept the wider one, so a narrowing silently did not happen.
- `_retire_key` swallowed a read failure and returned. `rotate_install_credential` saves the new
  key first, so the rotation completed while the superseded credential stayed live to its original
  expiry, a year by default. Rotation is what an operator does when a credential may have leaked.

Both now raise. That is safe in both cases because the caller commits the row afterwards, so the
row never diverges: a failed rescope changes nothing, and a failed rotation leaves `api_key_id` on
the old key while the newly written one is claimed by nothing and reclaimed by the reconcile
sweep's orphan pass.

Vault sealing itself is a live condition on this deployment rather than a hypothetical, which is
what makes these reachable.
"""

import pytest
from fastapi import HTTPException

from app.services import extension_credentials as ec
from app.services import extension_install as ei


class UnreachableVault:
    """Reads fail the way a sealed Vault fails."""

    def __init__(self) -> None:
        self.writes: list = []

    def vault_kv_put(self, path, data):  # pragma: no cover - must never be reached
        self.writes.append((path, data))


class WriteOnlyFailsVault:
    """Reads succeed, the write fails. The other half of the same hazard."""

    def __init__(self) -> None:
        self.writes: list = []

    def vault_kv_put(self, path, data):
        raise RuntimeError("vault is sealed")


class FakeInstall:
    id = 7
    api_key_id = "oak_current"
    granted_capabilities = ["inventory.read"]
    granted_scope = [{"cluster_id": "nuc2", "permissions": ["read"], "projects": ["default"]}]
    previous_api_key_id = None
    previous_key_expires_at = None


def _fail_read(_client, _key_id, **_kw):
    raise RuntimeError("vault is sealed")


def _ok_read(_client, _key_id, **_kw):
    return ({"capabilities": ["inventory.read", "lxd.proxy"], "expires_at": None}, "hash")


def test_a_rescope_that_cannot_write_the_grant_refuses(monkeypatch) -> None:
    monkeypatch.setattr("app.routers.integrations.get_api_key_from_vault", _fail_read)
    monkeypatch.setattr("app.routers.integrations.evict_api_key_cache", lambda _k: None)

    with pytest.raises(HTTPException) as refusal:
        ei._rewrite_key_grant(UnreachableVault(), FakeInstall())

    assert refusal.value.status_code == 503
    assert "not applied" in str(refusal.value.detail)


def test_a_rescope_whose_write_fails_also_refuses(monkeypatch) -> None:
    """The read can succeed and the write still fail. Guarding only the read leaves the same hole."""
    monkeypatch.setattr("app.routers.integrations.get_api_key_from_vault", _ok_read)
    monkeypatch.setattr("app.routers.integrations.evict_api_key_cache", lambda _k: None)

    with pytest.raises(HTTPException) as refusal:
        ei._rewrite_key_grant(WriteOnlyFailsVault(), FakeInstall())

    assert refusal.value.status_code == 503


def test_a_rescope_that_can_write_does_not_refuse(monkeypatch) -> None:
    """The positive twin. Without it both tests above would pass against a function that always
    raised, which would break rescope entirely rather than fix it."""
    written: list = []

    class Working:
        def vault_kv_put(self, path, data):
            written.append(data)

    monkeypatch.setattr("app.routers.integrations.get_api_key_from_vault", _ok_read)
    monkeypatch.setattr("app.routers.integrations.evict_api_key_cache", lambda _k: None)

    ei._rewrite_key_grant(Working(), FakeInstall())

    assert written, "the grant was never written"
    assert written[0]["capabilities"] == ["inventory.read"], "the narrowed grant was not applied"


def test_retiring_a_superseded_key_refuses_rather_than_skipping(monkeypatch) -> None:
    monkeypatch.setattr(ec, "get_api_key_from_vault", _fail_read)
    monkeypatch.setattr(ec, "evict_api_key_cache", lambda _k: None)

    with pytest.raises(HTTPException) as refusal:
        ec._retire_key(UnreachableVault(), "oak_old", grace_seconds=600)

    assert refusal.value.status_code == 503
    assert "still works" in str(refusal.value.detail), (
        "the refusal must say the existing credential survived, or an operator will assume the "
        "old one is dead and stop looking"
    )


def test_retiring_a_key_that_can_be_read_still_works(monkeypatch) -> None:
    """The positive twin for the same reason."""
    written: list = []

    class Working:
        def vault_kv_put(self, path, data):
            written.append(data)

    monkeypatch.setattr(ec, "get_api_key_from_vault", _ok_read)
    monkeypatch.setattr(ec, "evict_api_key_cache", lambda _k: None)

    ec._retire_key(Working(), "oak_old", grace_seconds=0)

    assert written, "nothing was written, so nothing was retired"
    assert written[0]["is_revoked"] is True
