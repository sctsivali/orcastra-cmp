"""My Keys answers 503 while Vault refuses the dashboard's token, never "no keys" or "not found".

An expired or revoked backend token makes every Vault read answer 403. Read as a policy refusal it
showed operators an empty key list and a "Key not found" download; read as an outage it is a 503
that clears when the token is renewed. A 403 the policy explains, a missing key (404) and the
group cleanup's "incomplete" answer keep their meaning.
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.core import vault_token
from app.core.vault_client import VaultClientError
from app.routers import my_keys


class _Response:
    def __init__(self, status: int, body: Dict[str, Any]):
        self.status_code = status
        self._body = body

    def json(self) -> Dict[str, Any]:
        return self._body


class _Vault:
    vault_addr = "http://vault.invalid"
    headers: Dict[str, str] = {}

    def __init__(self, *, list_status: int = 200, read_status: int = 200, keys: List[str] = ("key_a",)):
        self._list_status = list_status
        self._read_status = read_status
        self._keys = list(keys)
        self.session = SimpleNamespace(get=lambda *_a, **_k: _Response(self._list_status, {"data": {"keys": self._keys}}))

    def vault_read(self, path: str, use_cache: bool = True):
        if self._read_status != 200:
            raise VaultClientError("Vault read failed", status_code=self._read_status)
        return {"id": path.split("/")[-2], "node": "qa-c1", "certificate": "c", "private_key": "k"}


@pytest.fixture
def token(monkeypatch):
    state = {"value": "refused"}
    monkeypatch.setattr(vault_token, "status", lambda *, max_age=None: SimpleNamespace(state=state["value"]))
    return state


def _error(caught) -> str:
    detail = caught.value.detail
    return detail.get("error") if isinstance(detail, dict) else str(detail)


def test_the_key_list_with_a_refused_token_is_503(token):
    with pytest.raises(HTTPException) as caught:
        my_keys.list_keys_from_vault(_Vault(list_status=403))
    assert caught.value.status_code == 503 and _error(caught) == my_keys.KEY_STORE_UNAVAILABLE
    assert caught.value.headers.get("Retry-After")


@pytest.mark.parametrize("status", [500, 503])
def test_the_key_list_while_vault_fails_is_503(token, status):
    with pytest.raises(HTTPException) as caught:
        my_keys.list_keys_from_vault(_Vault(list_status=status))
    assert caught.value.status_code == 503


def test_a_list_the_policy_refuses_stays_an_empty_list(token):
    token["value"] = "ok"
    assert my_keys.list_keys_from_vault(_Vault(list_status=403)) == []


def test_the_group_cleanup_still_gets_an_incomplete_roster_not_an_error(token):
    keys, complete = my_keys.read_key_roster(_Vault(list_status=403))
    assert (keys, complete) == ([], False)


def test_a_download_with_a_refused_token_is_503_not_not_found(token):
    with pytest.raises(HTTPException) as caught:
        my_keys.get_key_from_vault(_Vault(read_status=403), "key_a")
    assert caught.value.status_code == 503


def test_a_missing_key_is_still_not_found(token):
    with pytest.raises(HTTPException) as caught:
        my_keys.get_key_from_vault(_Vault(read_status=404), "key_gone")
    assert caught.value.status_code == 404


def test_authorizing_a_key_with_a_refused_token_is_503_for_every_id(token):
    for key_id in ("key_a", "key_that_never_existed"):
        with pytest.raises(HTTPException) as caught:
            my_keys._authorize_key(_Vault(read_status=403), key_id, "qa@example.test")
        assert caught.value.status_code == 503


def test_authorizing_a_missing_key_with_a_live_token_is_still_404(token):
    token["value"] = "ok"
    with pytest.raises(HTTPException) as caught:
        my_keys._authorize_key(_Vault(read_status=404), "key_gone", "qa@example.test")
    assert caught.value.status_code == 404


def test_authorizing_a_key_never_reads_the_cached_record(token):
    token["value"] = "ok"
    seen = []

    class _Recording(_Vault):
        def vault_read(self, path, use_cache=True):
            seen.append(use_cache)
            return super().vault_read(path)

    my_keys._authorize_key(_Recording(), "key_a", "qa@example.test")
    assert seen == [False], "a cached record would answer while Vault is down and an unknown id would not"
