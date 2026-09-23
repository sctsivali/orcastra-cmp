"""
A Vault path built from request data cannot leave the segments its caller named.

Several Vault paths carry request data, such as a user-mapping id or an API key id from a header.
Interpolated raw, `..` segments were collapsed by requests before the request left the process, so
a user-mapping write could land on any KV path the backend token can write, and a `?` started a
query string. Every URL builder now goes through safe_vault_url: dot and empty segments are
refused before any request, and every other segment is percent-encoded.
"""

from typing import Any, Dict, List, Optional, Tuple

import pytest

from app.core import vault_client as vc
from app.routers.integrations import get_user_mappings_from_vault

TRAVERSAL = "secret/data/integrations/api_keys/oak_test/users/../../../../clusters/cluster-a/certs"
# note: a `/certs` path is never cached, so the cache cases use a cacheable `/metadata` target.
# Otherwise they would pass whether or not the check ran before the lookup.
CACHEABLE_TRAVERSAL = (
    "secret/data/integrations/api_keys/oak_test/users/../../../../clusters/cluster-a/metadata"
)


class _Response:
    status_code = 200

    def __init__(self, payload: Optional[Dict[str, Any]] = None) -> None:
        self._payload = payload or {"data": {"data": {"ok": True}, "keys": []}}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Dict[str, Any]:
        return self._payload


class _Session:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, str]] = []

    def _record(self, method: str, url: str) -> _Response:
        self.calls.append((method, url))
        return _Response()

    def get(self, url: str, **kwargs: Any) -> _Response:
        return self._record("GET", url)

    def post(self, url: str, **kwargs: Any) -> _Response:
        return self._record("POST", url)

    def delete(self, url: str, **kwargs: Any) -> _Response:
        return self._record("DELETE", url)

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        return self._record(method, url)


class _Cache:
    def __init__(self) -> None:
        self.entries: Dict[Tuple[str, str], Any] = {}

    def get(self, namespace: str, key: str) -> Any:
        return self.entries.get((namespace, key))

    def set(self, namespace: str, key: str, value: Any, ttl: Optional[int] = None) -> None:
        self.entries[(namespace, key)] = value

    def delete(self, namespace: str, key: str) -> None:
        self.entries.pop((namespace, key), None)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Tuple[vc.VaultClient, _Cache]:
    cache = _Cache()
    monkeypatch.setattr(vc, "get_cache", lambda: cache)
    monkeypatch.setattr(vc, "_audit_vault_operation", lambda *a, **k: None)
    c = vc.VaultClient.__new__(vc.VaultClient)
    c.vault_addr = "http://vault:8200"
    c.headers = {}
    c.session = _Session()
    return c, cache


BUILDERS = {
    "read": lambda c, p: c.vault_read(p),
    "write": lambda c, p: c.vault_write(p, {"k": "v"}),
    "kv_put": lambda c, p: c.vault_kv_put(p, {"k": "v"}),
    "delete": lambda c, p: c.vault_delete(p),
    "delete_metadata": lambda c, p: c.vault_delete_metadata(p),
    "list": lambda c, p: c.vault_list(p),
}


@pytest.mark.parametrize("builder", sorted(BUILDERS))
@pytest.mark.parametrize(
    "path",
    [TRAVERSAL, "secret/data/integrations/api_keys/./metadata", "secret/data//clusters", "", "/"],
    ids=["traversal", "dot", "empty-segment", "empty", "slash"],
)
def test_every_builder_refuses_before_any_request(client: Any, builder: str, path: str) -> None:
    c, _ = client
    with pytest.raises(vc.VaultClientError):
        BUILDERS[builder](c, path)
    assert c.session.calls == []


@pytest.mark.parametrize("builder", sorted(BUILDERS))
def test_a_query_character_stays_in_its_segment(client: Any, builder: str) -> None:
    c, _ = client
    BUILDERS[builder](c, "secret/data/integrations/api_keys/oak_test?list=true/metadata")
    (_method, url), = c.session.calls
    assert url == "http://vault:8200/v1/secret/data/integrations/api_keys/oak_test%3Flist%3Dtrue/metadata"


@pytest.mark.parametrize("builder", sorted(BUILDERS))
def test_a_well_formed_path_is_unchanged(client: Any, builder: str) -> None:
    c, _ = client
    BUILDERS[builder](c, "secret/data/clusters/cluster-a/metadata")
    (_method, url), = c.session.calls
    assert url == "http://vault:8200/v1/secret/data/clusters/cluster-a/metadata"


def test_an_api_key_id_and_a_pki_role_are_unchanged() -> None:
    assert (
        vc.safe_vault_url("http://vault:8200", "secret/data/integrations/api_keys/oak_0f3c9a_x-1.2/secret")
        == "http://vault:8200/v1/secret/data/integrations/api_keys/oak_0f3c9a_x-1.2/secret"
    )
    assert vc.safe_vault_url("http://vault:8200", "pki_int/issue/lxd-client") == (
        "http://vault:8200/v1/pki_int/issue/lxd-client"
    )


def test_a_refused_read_is_not_served_from_cache(client: Any) -> None:
    c, cache = client
    # precondition: this cache really serves a well-formed `/metadata` read.
    cache.set("vault", "secret/data/clusters/cluster-a/metadata", {"cached": True})
    assert c.vault_read("secret/data/clusters/cluster-a/metadata") == {"cached": True}
    cache.set("vault", CACHEABLE_TRAVERSAL, {"cached": "metadata of another cluster"})
    with pytest.raises(vc.VaultClientError):
        c.vault_read(CACHEABLE_TRAVERSAL)
    assert c.session.calls == []


def test_a_refused_list_is_not_served_from_cache(client: Any) -> None:
    c, cache = client
    cache.set("vault_list", CACHEABLE_TRAVERSAL, ["cluster-a/"])
    with pytest.raises(vc.VaultClientError):
        c.vault_list(CACHEABLE_TRAVERSAL)
    assert c.session.calls == []


def test_the_mapping_list_url_is_built_safely(client: Any) -> None:
    c, _ = client
    get_user_mappings_from_vault(c, "oak_test?x=1")
    methods = [call for call in c.session.calls if call[0] == "LIST"]
    assert methods == [(
        "LIST",
        "http://vault:8200/v1/secret/metadata/integrations/api_keys/oak_test%3Fx%3D1/users",
    )]


def test_a_traversal_key_id_never_lists(client: Any) -> None:
    c, _ = client
    assert get_user_mappings_from_vault(c, "../../../clusters") == []
    assert c.session.calls == []
