"""Only Vault metadata is cached. Credentials are read from Vault every time.

The cache used a denylist of /certs, /keys and /secrets, so an integration API key's /secret path,
holding the plaintext secret and its HMAC as `raw` and `hash`, went into Redis and the in-process
LRU for five minutes. Neither field name matched the encryption patterns either, so it sat there in
clear.
"""

from typing import Any, Dict, List, Optional, Tuple

import pytest
from cryptography.fernet import Fernet

from app.core import cache as cache_module
from app.core import vault_client as vc
from app.core.cache import CacheManager
from app.core.encryption import DataEncryption

KEYS = "secret/data/orcastra/integrations/api_keys/oak_test"
CLUSTERS = "secret/data/orcastra/clusters/cluster-a"

NEVER_CACHED = [
    f"{KEYS}/secret",
    f"{KEYS}/users/u1",
    "secret/data/orcastra/my_keys/oak_test/credentials",
    f"{CLUSTERS}/certs",
    f"{CLUSTERS}/secrets",
    f"{CLUSTERS}/pending_certs",
    "pki_int/certs",
    "secret/data/orcastra/something_new",
]
CACHED = [f"{KEYS}/metadata", f"{CLUSTERS}/metadata"]


class _Response:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Dict[str, Any]:
        return {"data": {"data": {"hash": "h", "raw": "oas_x", "name": "cluster-a"}}}


class _Session:
    def __init__(self) -> None:
        self.gets: List[str] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.gets.append(url)
        return _Response()


class _SpyCache:
    def __init__(self) -> None:
        self.sets: List[Tuple[str, str]] = []
        self.gets: List[Tuple[str, str]] = []

    def get(self, namespace: str, key: str) -> Optional[Any]:
        self.gets.append((namespace, key))
        return None

    def set(self, namespace: str, key: str, value: Any, ttl: Optional[int] = None) -> None:
        self.sets.append((namespace, key))

    def delete(self, namespace: str, key: str) -> None:
        return None


@pytest.fixture()
def spy(monkeypatch: pytest.MonkeyPatch) -> _SpyCache:
    cache = _SpyCache()
    monkeypatch.setattr(vc, "get_cache", lambda: cache)
    monkeypatch.setattr(vc, "_audit_vault_operation", lambda *a, **k: None)
    return cache


def _client() -> vc.VaultClient:
    client = vc.VaultClient.__new__(vc.VaultClient)
    client.vault_addr = "http://vault.example.test:8200"
    client.headers = {}
    client.session = _Session()
    return client


@pytest.mark.parametrize("path", NEVER_CACHED)
def test_a_credential_path_is_read_from_vault_and_never_cached(spy: _SpyCache, path: str) -> None:
    client = _client()
    assert client.vault_read(path)["raw"] == "oas_x"
    client.vault_read(path)
    assert spy.sets == [], f"{path} was written to the cache"
    assert spy.gets == [], f"{path} was looked up in the cache"
    assert len(client.session.gets) == 2


@pytest.mark.parametrize("path", CACHED)
def test_metadata_is_still_cached(spy: _SpyCache, path: str) -> None:
    _client().vault_read(path)
    assert spy.sets == [("vault", path)]


@pytest.mark.parametrize(
    "path, expected",
    [
        (f"{KEYS}/metadata/", True),
        (f"{KEYS}/METADATA", True),
        (f"{KEYS}/metadata_backup", False),
        (f"{KEYS}/metadata/secret", False),
        ("", False),
        ("/", False),
    ],
)
def test_the_predicate_is_an_allowlist(path: str, expected: bool) -> None:
    assert _client()._is_cacheable_path(path) is expected


def test_raw_and_hash_are_encrypted_at_rest_in_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    encryption = DataEncryption(Fernet.generate_key().decode())
    monkeypatch.setattr(cache_module, "get_encryption", lambda: encryption)
    manager = CacheManager(redis_url=None)
    path = f"{KEYS}/secret"
    manager.set("vault", path, {"hash": "h-value", "raw": "oas_plaintext", "name": "oak_test"})

    stored, _expiry = manager.memory_cache.get(manager._make_key("vault", path))
    assert stored["raw"] != "oas_plaintext" and encryption.is_encrypted(stored["raw"])
    assert stored["hash"] != "h-value" and encryption.is_encrypted(stored["hash"])
    assert stored["name"] == "oak_test"
    assert manager.get("vault", path) == {
        "hash": "h-value",
        "raw": "oas_plaintext",
        "name": "oak_test",
    }
