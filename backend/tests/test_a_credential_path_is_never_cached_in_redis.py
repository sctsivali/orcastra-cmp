"""Only Vault metadata is cached. Credentials are read from Vault every time.

The cache used a denylist of /certs, /keys and /secrets, so an integration API key's /secret path,
holding the plaintext secret and its HMAC as `raw` and `hash`, went into Redis and the in-process
LRU for five minutes. Neither field name matched the encryption patterns either, so it sat there in
clear.

A read that is never cached is a Vault round trip every time, and vault_read is synchronous, so an
async caller of an uncached path runs it in a thread. The ratchet at the end holds that line.
"""

import ast
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple

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
    # integration key metadata carries revocation and grants, so a stale copy must never be shared
    f"{KEYS}/metadata",
    f"{KEYS}/users/u1",
    "secret/data/orcastra/my_keys/oak_test/credentials",
    f"{CLUSTERS}/certs",
    f"{CLUSTERS}/secrets",
    f"{CLUSTERS}/pending_certs",
    "pki_int/certs",
    "secret/data/orcastra/something_new",
]
CACHED = [f"{CLUSTERS}/metadata", "secret/data/orcastra/my_keys/oak_test/metadata"]


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
        (f"{CLUSTERS}/metadata/", True),
        (f"{CLUSTERS}/METADATA", True),
        (f"{CLUSTERS}/metadata_backup", False),
        (f"{CLUSTERS}/metadata/secret", False),
        (f"{KEYS}/metadata", False),
        (f"{KEYS}/METADATA/", False),
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


# ---------------------------------------------------------------------------
# Uncached reads stay off the event loop
# ---------------------------------------------------------------------------


class _ThreadVault:
    """Answers by the last path segment and records the thread each read ran on."""

    def __init__(self, answers: Dict[str, Dict[str, Any]]) -> None:
        self.answers = answers
        self.reads: List[Tuple[str, int]] = []

    def vault_read(self, path: str, use_cache: bool = True) -> Dict[str, Any]:
        self.reads.append((path.rsplit("/", 1)[-1], threading.get_ident()))
        return dict(self.answers.get(path.rsplit("/", 1)[-1], {}))

    def vault_delete(self, path: str) -> None:
        return None


def _cert_manager(answers: Dict[str, Dict[str, Any]]) -> SimpleNamespace:
    return SimpleNamespace(
        vault=_ThreadVault(answers), list_clusters=lambda use_cache=False: ["cluster-a"]
    )


def _on_loop(vault: _ThreadVault) -> List[str]:
    loop_thread = threading.get_ident()
    return [segment for segment, thread in vault.reads if thread == loop_thread]


async def test_the_certificate_pages_read_vault_off_the_loop() -> None:
    from fastapi import HTTPException

    from app.models.schemas import CertificateVerifyRequest
    from app.routers import cert_manager as router

    manager = _cert_manager({"certs": {"client.crt": "not-a-pem"}})
    await router.list_certificates(cert_manager=manager)
    assert {segment for segment, _ in manager.vault.reads} == {"certs", "metadata", "pending_certs"}
    assert _on_loop(manager.vault) == [], "list_certificates read Vault on the event loop"

    manager = _cert_manager({"certs": {"client.crt": "not-a-pem"}})
    await router.get_certificate(cluster_id="cluster-a", cert_manager=manager)
    assert {segment for segment, _ in manager.vault.reads} == {"certs", "pending_certs"}
    assert _on_loop(manager.vault) == []

    manager = _cert_manager({})
    with pytest.raises(HTTPException):
        await router.verify_renewal(
            request=CertificateVerifyRequest(cluster_id="cluster-a"), cert_manager=manager, _=None
        )
    assert [segment for segment, _ in manager.vault.reads] == ["pending_certs"]
    assert _on_loop(manager.vault) == []

    manager = _cert_manager({})
    await router.cleanup_pending_certificates(cert_manager=manager, _=None)
    assert [segment for segment, _ in manager.vault.reads] == ["pending_certs"]
    assert _on_loop(manager.vault) == []


APP = Path(vc.__file__).resolve().parents[1]

# note: direct reads of uncached paths inside an async body. The cert_manager and
# cluster_registration reads were already uncached before the allowlist. The /secret reads in
# refresh_organizations and _get_orcahub_credentials were cached before, and both move onto the
# auth pool with the integration fixes that rewrite those two functions. The set may shrink and
# must never grow.
ACCEPTED_ON_LOOP_READS = {
    "routers/cert_manager.py::initiate_renewal::cert_path",
    "routers/cert_manager.py::get_system_warnings::cert_path",
    "routers/cluster_registration.py::check_cluster_authentication::"
    "f'{settings.vault_clusters_path}/{cluster_id}/certs'",
}


def _own_body(fn: ast.AsyncFunctionDef) -> Iterator[ast.AST]:
    stack: List[ast.AST] = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _is_metadata(arg: ast.expr) -> bool:
    if isinstance(arg, ast.JoinedStr) and arg.values:
        tail = arg.values[-1]
        return isinstance(tail, ast.Constant) and str(tail.value).rstrip("/").endswith("/metadata")
    if isinstance(arg, ast.Constant):
        return str(arg.value).rstrip("/").endswith("/metadata")
    return isinstance(arg, ast.Name) and arg.id == "metadata_path"


def _direct_reads_on_the_loop() -> List[Tuple[str, bool]]:
    """(site, is_metadata) for every vault_read called directly in an async function body."""
    found: List[Tuple[str, bool]] = []
    for path in APP.rglob("*.py"):
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for node in _own_body(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "vault_read"
                    and node.args
                ):
                    site = f"{path.relative_to(APP)}::{fn.name}::{ast.unparse(node.args[0])}"
                    found.append((site, _is_metadata(node.args[0])))
    return found


def test_no_new_uncached_vault_read_runs_on_the_loop() -> None:
    reads = _direct_reads_on_the_loop()
    assert any(is_metadata for _, is_metadata in reads), (
        "the scan sees no cached metadata read either, so it no longer finds vault_read calls"
    )
    new = sorted({site for site, is_metadata in reads if not is_metadata} - ACCEPTED_ON_LOOP_READS)
    assert not new, (
        "an async function calls vault_read on an uncached path directly, which blocks the event "
        "loop for a Vault round trip. Run it with asyncio.to_thread: " + ", ".join(new)
    )
    fixed = sorted(ACCEPTED_ON_LOOP_READS - {site for site, _ in reads})
    assert not fixed, (
        "these accepted sites no longer read Vault on the loop, so remove them from "
        "ACCEPTED_ON_LOOP_READS and keep the ratchet tight: " + ", ".join(fixed)
    )
