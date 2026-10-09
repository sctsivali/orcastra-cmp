"""A bookkeeping write to a key's Vault metadata never undoes a change made while it ran.

The last-used stamp and the stored callback URL each read the whole metadata document and put
it back. A revoke, a narrowed grant or another stored field written between that read and that
write was lost, so a revoked key could come back to life and a first sync could drop its
callback URL. Both writes now use KV v2 check-and-set and read again when they lose.
"""

import copy
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest
import requests

from app.core import vault_client as vault_module
from app.core.vault_client import VaultCasMismatch, VaultClient
from app.routers import integrations
from app.services.integration_callback_url import callback_url_tag

KEY = "oak_bookkeeping"
PATH = f"secret/data/{integrations.VAULT_INTEGRATIONS_PATH}/api_keys/{KEY}/metadata"
START = {"id": KEY, "is_active": True, "is_revoked": False, "cluster_permissions": [{"cluster_id": "c", "projects": ["*"]}]}


class _KvV2:
    """KV v2 with versions and check-and-set. `between` runs once after the first read."""

    def __init__(self, data: Optional[Dict[str, Any]] = None) -> None:
        self.versions: List[Optional[Dict[str, Any]]] = [copy.deepcopy(data)] if data is not None else []
        self.puts: List[Tuple[Dict[str, Any], Optional[int]]] = []
        self.between: Optional[Callable[["_KvV2"], None]] = None

    def current(self) -> Optional[Dict[str, Any]]:
        return copy.deepcopy(self.versions[-1]) if self.versions else None

    def write(self, data: Optional[Dict[str, Any]]) -> None:
        self.versions.append(copy.deepcopy(data))

    def vault_read_versioned(self, path: str) -> Tuple[Optional[Dict[str, Any]], int]:
        assert path == PATH
        seen = (self.current(), len(self.versions))
        if self.between is not None:
            hook, self.between = self.between, None
            hook(self)
        return seen

    def vault_kv_put(self, path: str, data: Dict[str, Any], cas: Optional[int] = None) -> Dict[str, Any]:
        assert path == PATH
        self.puts.append((copy.deepcopy(data), cas))
        if cas is not None and cas != len(self.versions):
            raise VaultCasMismatch(f"{path} changed after version {cas}")
        self.write(data)
        return {}


def _revoke(kv: _KvV2) -> None:
    kv.write(dict(kv.current(), is_active=False, is_revoked=True))


def test_the_last_used_stamp_lands_and_keeps_every_other_field() -> None:
    kv = _KvV2(START)
    integrations.update_api_key_last_used(kv, KEY)
    final = kv.current()
    assert final["last_used_at"] and {k: v for k, v in final.items() if k != "last_used_at"} == START
    assert kv.puts[0][1] == 1, "the write did not name the version it read"


def test_a_revoke_made_during_the_last_used_write_stays_revoked() -> None:
    kv = _KvV2(START)
    kv.between = _revoke
    integrations.update_api_key_last_used(kv, KEY)
    final = kv.current()
    assert final["is_active"] is False and final["is_revoked"] is True, "the bookkeeping write brought the key back"
    assert "last_used_at" not in final, "a revoked key was stamped as used"


def test_a_narrowed_grant_made_during_the_last_used_write_stays_narrow() -> None:
    kv = _KvV2(START)
    narrow = [{"cluster_id": "c", "projects": ["web"]}]
    kv.between = lambda store: store.write(dict(store.current(), cluster_permissions=narrow))
    integrations.update_api_key_last_used(kv, KEY)
    final = kv.current()
    assert final["cluster_permissions"] == narrow and final["last_used_at"]
    assert [cas for _data, cas in kv.puts] == [1, 2], "the lost write was not retried on the new version"


def test_a_key_deleted_before_the_stamp_is_never_written_again() -> None:
    kv = _KvV2(START)
    kv.write(None)
    integrations.update_api_key_last_used(kv, KEY)
    assert integrations._merge_into_key_metadata(kv, KEY, {"app_url": "https://hub.example.test"}) is None
    assert kv.puts == [] and kv.current() is None


def test_a_stored_callback_url_survives_a_concurrent_stamp() -> None:
    kv = _KvV2(START)
    kv.between = lambda store: store.write(dict(store.current(), last_used_at="2026-09-24T00:00:00"))
    assert integrations._merge_into_key_metadata(kv, KEY, {"app_url": "https://hub.example.test"})
    final = kv.current()
    assert final["app_url"] == "https://hub.example.test" and final["last_used_at"] == "2026-09-24T00:00:00"


def test_an_unchanged_callback_url_is_not_written_again(monkeypatch) -> None:
    """The same URL stored again by a sync with the same secret: the tag is unchanged too."""
    kv = _KvV2(dict(START, app_url="https://hub.example.test", app_url_secret_tag=callback_url_tag("h")))
    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: kv)
    assert integrations._store_app_url_in_vault(KEY, "https://hub.example.test", secret_hash="h") is True
    assert kv.puts == []


def test_a_write_that_keeps_losing_gives_up_without_clobbering() -> None:
    kv = _KvV2(START)

    def always(store: _KvV2) -> None:
        store.write(dict(store.current(), counter=len(store.versions)))
        store.between = always

    kv.between = always
    with pytest.raises(integrations.VaultClientError):
        integrations._merge_into_key_metadata(kv, KEY, {"app_url": "https://hub.example.test"})
    assert "app_url" not in kv.current()


@pytest.mark.parametrize("first,second", [
    ({"cluster_permissions": [{"cluster_id": "c", "projects": ["web"]}]}, {"is_revoked": True, "is_active": False}),
    ({"is_revoked": True, "is_active": False}, {"cluster_permissions": [{"cluster_id": "c", "projects": ["web"]}]}),
], ids=["revoke-during-a-permissions-edit", "permissions-edit-during-a-revoke"])
def test_a_revoke_and_a_permissions_edit_never_undo_each_other(first, second) -> None:
    kv = _KvV2(START)
    kv.between = lambda store: store.write(dict(store.current(), **second))
    assert integrations._merge_into_key_metadata(kv, KEY, first) is not None
    final = kv.current()
    assert {k: final[k] for k in {**first, **second}} == {**first, **second}


class _Session:
    def __init__(self, answers: Dict[str, Tuple[int, Any]]) -> None:
        self.answers, self.sent = answers, []

    def _answer(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.sent.append((method, url, kwargs.get("json")))
        status, body = self.answers[method]
        response = requests.Response()
        response.status_code = status
        response._content = requests.compat.json.dumps(body).encode()
        return response

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("POST", url, **kwargs)


def _client(session: _Session) -> VaultClient:
    client = VaultClient.__new__(VaultClient)
    client.vault_addr, client.headers, client.session = "https://vault.example.test", {}, session
    return client


def test_the_vault_client_sends_cas_and_names_a_lost_race(monkeypatch) -> None:
    monkeypatch.setattr(vault_module, "get_cache", lambda: None)
    lost = _Session({"POST": (400, {"errors": ["check-and-set parameter did not match the current version"]})})
    with pytest.raises(VaultCasMismatch):
        _client(lost).vault_kv_put(PATH, {"a": 1}, cas=3)
    assert lost.sent[0][2] == {"data": {"a": 1}, "options": {"cas": 3}}
    plain = _Session({"POST": (200, {"data": {"version": 4}})})
    _client(plain).vault_kv_put(PATH, {"a": 1})
    assert plain.sent[0][2] == {"data": {"a": 1}}, "a write without cas changed shape"


def test_the_vault_client_reads_the_version_it_writes_against() -> None:
    held = _Session({"GET": (200, {"data": {"data": {"a": 1}, "metadata": {"version": 7}}})})
    assert _client(held).vault_read_versioned(PATH) == ({"a": 1}, 7)
    gone = _Session({"GET": (404, {"errors": []})})
    assert _client(gone).vault_read_versioned(PATH) == (None, 0)
    deleted = _Session({"GET": (404, {"data": {"data": None, "metadata": {"version": 9, "deletion_time": "x"}}})})
    assert _client(deleted).vault_read_versioned(PATH) == (None, 9)


def test_only_the_version_mismatch_counts_as_a_lost_race(monkeypatch) -> None:
    monkeypatch.setattr(vault_module, "get_cache", lambda: None)
    other = _Session({"POST": (400, {"errors": ["check-and-set parameter required for this call"]})})
    with pytest.raises(vault_module.VaultClientError) as caught:
        _client(other).vault_kv_put(PATH, {"a": 1}, cas=3)
    assert not isinstance(caught.value, VaultCasMismatch)


@pytest.mark.parametrize("status,body", [(503, {"errors": ["sealed"]}), (403, {"errors": ["denied"]})])
def test_a_versioned_read_names_a_vault_failure(status, body) -> None:
    with pytest.raises(vault_module.VaultClientError):
        _client(_Session({"GET": (status, body)})).vault_read_versioned(PATH)


def test_key_metadata_never_reaches_the_shared_cache(monkeypatch) -> None:
    """A read racing a revoke or a grant change must not put the old document back for every worker."""
    touched: List[str] = []

    class _Cache:
        def get(self, _ns: str, key: str) -> Any:
            touched.append(key)
            return {"is_active": True, "is_revoked": False}

        def set(self, _ns: str, key: str, *_a: Any, **_k: Any) -> None:
            touched.append(key)

    monkeypatch.setattr(vault_module, "get_cache", lambda: _Cache())
    live = _Session({"GET": (200, {"data": {"data": {"is_active": False, "is_revoked": True}}})})
    assert _client(live).vault_read(PATH) == {"is_active": False, "is_revoked": True}
    assert touched == []
    client = _client(live)
    assert client._is_cacheable_path("secret/data/clusters/c1/metadata") is True
    assert client._is_cacheable_path(PATH) is False
