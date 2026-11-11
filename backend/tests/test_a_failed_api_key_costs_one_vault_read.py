"""A failed API key attempt costs one Vault read, so a flood of bad keys cannot amplify on Vault.

The secret is 256 bits, so guessing it is not the threat; an attacker can only make the backend do
work. A key id Vault does not hold costs one metadata read (Vault answers 404, which the client
never retries), and a wrong secret for a real key one read of the secret document. Both stay inside
the per-client request limit. A lockout on failures would hand anyone who knows a public key id a
way to lock its integration out, so the bound is pinned here instead.
"""

from typing import List

import pytest
from fastapi import HTTPException

from app.core.vault_client import VaultClientError
from app.routers import integrations


class _Vault:
    def __init__(self, *, known: bool) -> None:
        self.known = known
        self.reads: List[str] = []

    def vault_read(self, path: str, use_cache: bool = True):
        self.reads.append(path.rsplit("/", 1)[-1])
        if not self.known:
            raise VaultClientError("Vault read failed: 404 Client Error", status_code=404)
        if path.endswith("/metadata"):
            return {"id": "oak_real", "is_active": True}
        return {"hash": "stored-hash"}


@pytest.fixture
def vault(monkeypatch):
    holder = {}

    def client():
        return holder["vault"]

    monkeypatch.setattr(integrations, "get_vault_integrations_client", client)
    return holder


def test_an_unknown_key_id_costs_one_metadata_read(vault):
    vault["vault"] = _Vault(known=False)
    with pytest.raises(HTTPException) as caught:
        integrations._validate_api_key_blocking("oak_nobody", "oas_guess")
    assert caught.value.status_code == 404
    assert vault["vault"].reads == ["metadata"]


def test_a_wrong_secret_costs_the_two_point_reads_and_nothing_else(vault, monkeypatch):
    monkeypatch.setattr(integrations, "verify_api_key_secret", lambda secret, stored: False)
    vault["vault"] = _Vault(known=True)
    with pytest.raises(HTTPException) as caught:
        integrations._validate_api_key_blocking("oak_real", "oas_wrong")
    assert caught.value.status_code == 401
    assert vault["vault"].reads == ["metadata", "secret"]


def test_a_404_is_never_retried_by_the_vault_client():
    from app.core.vault_client import VaultClient

    retry = VaultClient.__new__(VaultClient)._create_session().get_adapter("http://vault").max_retries
    assert 404 not in retry.status_forcelist and 403 not in retry.status_forcelist


@pytest.mark.parametrize("key_id", ["../clusters/x", "a/b", "", "oak_" + "x" * 200, "oak space", "oak%2f"])
def test_a_key_id_of_the_wrong_shape_never_reaches_vault(vault, key_id):
    vault["vault"] = _Vault(known=True)
    with pytest.raises(HTTPException) as caught:
        integrations._validate_api_key_blocking(key_id, "oas_guess")
    assert caught.value.status_code == 404
    assert vault["vault"].reads == []


def test_a_real_key_id_shape_still_reaches_vault(vault, monkeypatch):
    monkeypatch.setattr(integrations, "verify_api_key_secret", lambda secret, stored: True)
    vault["vault"] = _Vault(known=True)
    assert integrations._validate_api_key_blocking("oak_" + "0123456789abcdef" * 2, "oas_right")["id"] == "oak_real"
