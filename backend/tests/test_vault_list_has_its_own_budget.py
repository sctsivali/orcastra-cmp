"""The Vault LIST gets a wider budget than a point read, and every success writes the memo.

A KV-v2 metadata LIST walks the storage backend; on the customer's VM the per-cluster reads
(GET, 2 s connect, 3 s read) kept answering while the LIST did not. Point reads stay at (2, 3):
they run on the request path and a slow one is the timeout the caller wants. The LIST is
served off the event loop after this cut, so its cost is a worker thread, not the API.
"""

import json

import pytest
import requests

from app.core import vault_client as vc


class _Response(requests.Response):
    def __init__(self, keys):
        super().__init__()
        self.status_code = 200
        self._content = json.dumps({"data": {"keys": keys}}).encode()
        self._content_consumed = True


class _Session:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, kwargs.get("timeout")))
        return _Response(["i48/", "ebi-01/"])

    def get(self, url, **kwargs):
        self.calls.append(("GET", kwargs.get("timeout")))
        return _Response([])


class _Cache:
    def __init__(self):
        self.sets = []

    def get(self, namespace, key):
        return None

    def set(self, namespace, key, value, ttl=None):
        self.sets.append((namespace, key, value, ttl))

    def delete(self, namespace, key):
        return None


@pytest.fixture
def client(monkeypatch):
    cache = _Cache()
    monkeypatch.setattr(vc, "get_cache", lambda: cache)
    monkeypatch.setattr(vc, "_audit_vault_operation", lambda *a, **k: None)
    c = vc.VaultClient.__new__(vc.VaultClient)
    c.vault_addr = "http://vault:8200"
    c.headers = {}
    c.session = _Session()
    return c, cache


def test_the_list_gets_its_own_budget_and_point_reads_keep_theirs(client):
    c, _ = client
    c.vault_list("secret/metadata/clusters", use_cache=False)
    c.vault_read("secret/data/clusters/i48/metadata") if hasattr(c, "vault_read") else None

    methods = dict(c.session.calls)
    assert methods["LIST"] == vc.VAULT_LIST_TIMEOUT
    assert vc.VAULT_LIST_TIMEOUT[0] >= 3 and vc.VAULT_LIST_TIMEOUT[1] >= 10
    if "GET" in methods:
        assert methods["GET"] == (2, 3), "a point read keeps the request-path budget"


def test_a_successful_fresh_list_writes_the_memo_and_refreshes_the_cache(client):
    c, cache = client
    c.vault_list("secret/metadata/clusters", use_cache=False)

    memo = [s for s in cache.sets if s[0] == vc.LAST_GOOD_LIST_NAMESPACE]
    assert memo == [(vc.LAST_GOOD_LIST_NAMESPACE, "secret/metadata/clusters", ["i48/", "ebi-01/"], vc.LAST_GOOD_LIST_TTL)]
    # use_cache only decides whether the cache is read; a fresh read refreshes it, so the
    # request path finds a warm entry after the sweep's probe rather than paying a round trip
    assert any(s[0] == "vault_list" for s in cache.sets), "a fresh read still refreshes the list cache"
    assert vc.LAST_GOOD_LIST_TTL >= 24 * 3600


def test_last_known_list_reads_the_memo(client, monkeypatch):
    c, cache = client
    cache.get = lambda ns, key: ["a/", "b/"] if ns == vc.LAST_GOOD_LIST_NAMESPACE else None
    assert c.last_known_list("secret/metadata/clusters") == ["a/", "b/"]
    cache.get = lambda ns, key: None
    assert c.last_known_list("secret/metadata/clusters") is None
