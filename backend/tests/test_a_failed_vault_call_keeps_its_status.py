"""Every failed Vault call keeps Vault's HTTP status on the error it raises.

The status is what tells a store that failed (5xx, or a 403 for a refused token) from a
document that is not there (404). A write, delete or LIST that dropped it made every refusal
look like an unreached Vault, and a LIST 403 like an outage no matter why it was refused.
The message text is unchanged, because some callers still match on it.
"""

import json

import pytest
import requests

from app.core import vault_client as vc


class _Response(requests.Response):
    def __init__(self, status, body=None):
        super().__init__()
        self.status_code = status
        self.reason = "status"
        self.url = "http://vault:8200/v1/x"
        self._content = json.dumps(body if body is not None else {"errors": ["permission denied"]}).encode()
        self._content_consumed = True


class _Session:
    def __init__(self, status):
        self.status = status

    def _answer(self, *_a, **_kw):
        return _Response(self.status)

    request = get = post = delete = _answer


class _Cache:
    def get(self, namespace, key):
        return None

    def set(self, namespace, key, value, ttl=None):
        pass

    def delete(self, namespace, key):
        pass


def _client(monkeypatch, status):
    monkeypatch.setattr(vc, "get_cache", lambda: _Cache())
    monkeypatch.setattr(vc, "_audit_vault_operation", lambda *a, **k: None)
    client = vc.VaultClient.__new__(vc.VaultClient)
    client.vault_addr = "http://vault:8200"
    client.headers = {}
    client.session = _Session(status)
    return client


CALLS = {
    "read": lambda c: c.vault_read("secret/data/clusters/qa-c1/metadata"),
    "write": lambda c: c.vault_write("pki_int/issue/orcastra", {"common_name": "x"}),
    "delete": lambda c: c.vault_delete("secret/data/clusters/qa-c1/metadata"),
    "delete_metadata": lambda c: c.vault_delete_metadata("secret/metadata/clusters/qa-c1"),
    "list": lambda c: c.vault_list("secret/metadata/integrations/api_keys", use_cache=False),
}


@pytest.mark.parametrize("status", [403, 500, 503])
@pytest.mark.parametrize("call", sorted(CALLS))
def test_the_error_carries_vaults_status(monkeypatch, call, status):
    with pytest.raises(vc.VaultClientError) as caught:
        CALLS[call](_client(monkeypatch, status))
    assert caught.value.status_code == status


@pytest.mark.parametrize("call", sorted(CALLS))
def test_an_unreached_vault_still_has_no_status(monkeypatch, call):
    client = _client(monkeypatch, 200)

    def unreachable(*_a, **_kw):
        raise requests.ConnectionError("connection refused")

    client.session.request = client.session.get = client.session.post = client.session.delete = unreachable
    with pytest.raises(vc.VaultClientError) as caught:
        CALLS[call](client)
    assert caught.value.status_code is None
    assert isinstance(caught.value.__context__, requests.RequestException)


def test_the_messages_are_unchanged(monkeypatch):
    with pytest.raises(vc.VaultClientError) as caught:
        CALLS["write"](_client(monkeypatch, 403))
    assert str(caught.value).startswith("Vault write failed: 403 Client Error")
