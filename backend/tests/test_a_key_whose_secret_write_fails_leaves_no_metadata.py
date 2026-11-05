"""A key mint whose secret write fails takes its metadata back out of Vault.

The mint writes two Vault documents, the metadata and then the secret hash. When the second write
failed the caller got 500 but the metadata stayed, so the key listed as Active and could never
authenticate. Nothing could use it, but an operator would read it as a live credential.
"""

from __future__ import annotations

from typing import Any, List

import pytest
from fastapi import HTTPException

from app.routers import integrations


class _Vault:
    def __init__(self, fail_on: int) -> None:
        self.fail_on = fail_on
        self.puts: List[str] = []
        self.metadata_deletes: List[str] = []

    def vault_kv_put(self, path: str, _data: Any) -> None:
        self.puts.append(path)
        if len(self.puts) == self.fail_on:
            raise RuntimeError("Vault is sealed")

    def vault_delete_metadata(self, path: str) -> bool:
        self.metadata_deletes.append(path)
        return True


def _save(vault: _Vault) -> None:
    integrations.save_api_key_to_vault(vault, "oak_test", {"id": "oak_test"}, "hash", raw_secret="raw")


def test_a_failed_secret_write_deletes_the_metadata_it_followed() -> None:
    vault = _Vault(fail_on=2)

    with pytest.raises(HTTPException) as caught:
        _save(vault)

    assert caught.value.status_code == 500
    assert vault.metadata_deletes == [
        f"secret/metadata/{integrations.VAULT_INTEGRATIONS_PATH}/api_keys/oak_test/metadata"
    ]


def test_a_failed_metadata_write_has_nothing_to_take_back() -> None:
    vault = _Vault(fail_on=1)

    with pytest.raises(HTTPException) as caught:
        _save(vault)

    assert caught.value.status_code == 500
    assert vault.metadata_deletes == []


def test_a_clean_mint_deletes_nothing() -> None:
    vault = _Vault(fail_on=0)
    _save(vault)
    assert len(vault.puts) == 2 and vault.metadata_deletes == []
