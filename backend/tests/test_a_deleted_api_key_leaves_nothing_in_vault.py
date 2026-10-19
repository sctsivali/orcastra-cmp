"""Deleting an API key removes every version of every record under it from Vault.

A KV v2 data delete only marks the latest version deleted. Deleted keys stayed in the LIST,
their secret could be undeleted from version history, and the reconcile sweep read every one
back as a 404 each pass: 1286 of them, about 17 audit lines a second, on the dev deployment.
"""

from typing import List

from app.routers.integrations import delete_api_key_from_vault


class _Vault:
    def __init__(self, users: List[str], failing: str = ""):
        self.users = users
        self.failing = failing
        self.data_fails = False
        self.metadata_deletes: List[str] = []
        self.data_deletes: List[str] = []

    def vault_list(self, path: str, use_cache: bool = True) -> List[str]:
        assert path == "secret/metadata/integrations/api_keys/oak_1/users" and use_cache is False
        return self.users

    def vault_delete_metadata(self, path: str) -> bool:
        if path.endswith(self.failing) and self.failing:
            raise RuntimeError("vault said no")
        self.metadata_deletes.append(path)
        return True

    def vault_delete(self, path: str) -> bool:
        if self.data_fails:
            raise RuntimeError("vault said no again")
        self.data_deletes.append(path)
        return True


BASE = "secret/metadata/integrations/api_keys/oak_1"


def test_every_record_is_destroyed_not_soft_deleted_descriptor_last():
    vault = _Vault(users=["u1", "u2/"])
    assert delete_api_key_from_vault(vault, "oak_1") is True
    assert vault.metadata_deletes == [
        f"{BASE}/users/u1", f"{BASE}/users/u2", f"{BASE}/secret", f"{BASE}/metadata",
    ]
    assert vault.data_deletes == []


def test_a_refused_permanent_delete_falls_back_to_a_data_delete():
    vault = _Vault(users=[], failing="/metadata")
    assert delete_api_key_from_vault(vault, "oak_1") is True
    assert vault.metadata_deletes == [f"{BASE}/secret"]
    assert vault.data_deletes == ["secret/data/integrations/api_keys/oak_1/metadata"]


def test_a_record_that_cannot_be_deleted_at_all_is_reported(caplog):
    vault = _Vault(users=[], failing="/secret")
    vault.data_fails = True
    assert delete_api_key_from_vault(vault, "oak_1") is False
    assert vault.metadata_deletes == [f"{BASE}/metadata"]
    assert "left 1 Vault record(s) behind" in caplog.text


def test_the_delete_route_answers_503_on_an_incomplete_delete():
    import pytest
    from fastapi import HTTPException

    from app.routers import integrations

    with pytest.raises(HTTPException) as refused:
        integrations._raise_if_incomplete(False)
    assert refused.value.status_code == 503
    integrations._raise_if_incomplete(True)


def test_an_unlistable_users_folder_still_deletes_the_key():
    class _NoList(_Vault):
        def vault_list(self, path, use_cache=True):
            raise RuntimeError("404")

    vault = _NoList(users=[])
    assert delete_api_key_from_vault(vault, "oak_1") is True
    assert vault.metadata_deletes == [f"{BASE}/secret", f"{BASE}/metadata"]
