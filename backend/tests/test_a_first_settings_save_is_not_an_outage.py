"""Nothing stored yet is not the same as the store being unavailable.

`store_secrets` merges rather than replaces, so it reads what is already there first. A guard was
added to stop a read failure being swallowed, because swallowing it turned the merge into a
replace and one Vault hiccup destroyed every other secret on that installation while answering
200. That guard was right.

It was also too wide. `vault_read` raises for a path that has never been written, and that is
exactly the empty case a first save is in, so the first settings save on every installation
answered 503 "the secret store is unavailable" while Vault was healthy and unsealed.

Both halves are pinned here, because fixing one by breaking the other is the obvious wrong move.
"""

import pytest
from fastapi import HTTPException

from app.services import extension_settings


class VaultThatHasNothingYet:
    """What Vault does for a path nobody has written: a 404, raised, not an empty dict."""

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
    """The half that must survive the fix above. Treating every read failure as empty is what
    turned a merge into a replace and lost secrets nobody retyped."""
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
