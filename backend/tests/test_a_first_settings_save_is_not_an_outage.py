"""Nothing stored yet is not the same as the store being unavailable.

`store_secrets` merges rather than replaces, so it reads what is already there first. A read
failure must not be swallowed: that turns the merge into a replace, and one Vault error destroys
every other secret on the installation while answering 200.

`vault_read` also raises for a path that has never been written, which is the empty case a first
save is in; that save must succeed rather than answer 503. Both halves are pinned here.
"""

import pytest
from fastapi import HTTPException

from app.services import extension_settings


class VaultThatHasNothingYet:
    """Vault answers a read of a never-written path with a raised 404."""

    def __init__(self) -> None:
        self.written: dict = {}

    def vault_read(self, _path: str):
        raise RuntimeError(
            "Vault read failed: 404 Client Error: Not Found for url: "
            "http://vault:8200/v1/secret/data/extensions/installations/1/settings"
        )

    def vault_kv_put(self, path: str, data: dict) -> None:
        self.written[path] = data


class VaultThatIsDown:
    def vault_read(self, _path: str):
        raise RuntimeError("Vault read failed: 503 Server Error: Service Unavailable")

    def vault_kv_put(self, path: str, data: dict) -> None:  # pragma: no cover - must not be reached
        raise AssertionError("nothing may be written when the store could not be read")


class VaultWithExistingSecrets:
    def __init__(self, existing: dict) -> None:
        self.existing = existing
        self.written: dict = {}

    def vault_read(self, _path: str):
        return dict(self.existing)

    def vault_kv_put(self, path: str, data: dict) -> None:
        self.written[path] = data


def test_the_first_save_on_an_installation_succeeds() -> None:
    vault = VaultThatHasNothingYet()
    names = extension_settings.store_secrets(vault, 1, {"api_token": "s3cret"})

    assert names == ["api_token"]
    assert vault.written, "the first save wrote nothing, so the settings form does not work at all"


def test_a_store_that_is_genuinely_down_still_refuses() -> None:
    """Treating every read failure as empty would turn a merge into a replace and lose secrets."""
    with pytest.raises(HTTPException) as refusal:
        extension_settings.store_secrets(VaultThatIsDown(), 1, {"api_token": "s3cret"})

    assert refusal.value.status_code == 503
    assert "nothing was saved" in str(refusal.value.detail)


def test_a_later_save_merges_rather_than_replaces() -> None:
    """The behaviour the read exists for. Submitting one field must not blank the others."""
    vault = VaultWithExistingSecrets({"api_token": "old", "webhook_key": "keep-me"})
    names = extension_settings.store_secrets(vault, 1, {"api_token": "new"})

    assert names == ["api_token", "webhook_key"]
    stored = next(iter(vault.written.values()))
    assert stored["webhook_key"] == "keep-me", "an unrelated secret was destroyed by a save"
    assert stored["api_token"] == "new"
