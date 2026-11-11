"""The backend asks Vault about its own token, and that answer decides what a 403 means.

A refused (expired or revoked) token makes every Vault read answer 403 while Vault itself is
healthy, and Vault answers 403 for a path the policy denies as well. `lookup-self` is about the
token alone, so it is the one question a caller cannot steer. These tests pin the probe: its
states, its cache, one lookup for a burst of callers, and a body that never reaches a log.
"""

from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
import requests

from app.core import vault_token
from app.core.vault_client import VaultClientError

TOKEN_ID = "hvs.SECRET-TOKEN-ID-NEVER-LOGGED"


class _Response:
    def __init__(self, status: int, body: Optional[Dict[str, Any]] = None) -> None:
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self) -> Dict[str, Any]:
        return self._body

    def close(self) -> None:
        pass


class _Vault:
    """Answers lookup-self and the registry LIST the way a real Vault would."""

    def __init__(self, *, lookup: int | Exception = 200, registry: int | Exception = 200, data: Optional[dict] = None):
        self.lookup = lookup
        self.registry = registry
        self.data = data if data is not None else {
            "id": TOKEN_ID, "accessor": "acc-NEVER-LOGGED", "ttl": 2591000, "renewable": True,
            "period": 2592000, "expire_time": "2026-12-11T00:00:00Z", "policies": ["default", "orcastra-policy"],
        }
        self.calls: List[str] = []
        self.gate: Optional[threading.Event] = None

    def get(self, url: str, **_kw: Any) -> _Response:
        self.calls.append("lookup")
        if self.gate is not None:
            self.gate.wait(2)
        if isinstance(self.lookup, Exception):
            raise self.lookup
        return _Response(self.lookup, {"data": self.data} if self.lookup == 200 else {"errors": ["permission denied"]})

    def request(self, method: str, url: str, **_kw: Any) -> _Response:
        self.calls.append(f"{method} {url.split('/v1/', 1)[1]}")
        if isinstance(self.registry, Exception):
            raise self.registry
        return _Response(self.registry, {"data": {"keys": []}})


@pytest.fixture
def vault(monkeypatch) -> _Vault:
    fake = _Vault()
    settings = SimpleNamespace(
        vault_enabled=True, vault_addr="http://vault.test:8200", vault_token=TOKEN_ID,
        vault_clusters_path="secret/data/clusters",
    )
    monkeypatch.setattr(vault_token, "get_settings", lambda: settings)
    monkeypatch.setattr(vault_token.requests, "get", fake.get)
    monkeypatch.setattr(vault_token.requests, "request", fake.request)
    vault_token.reset()
    yield fake
    vault_token.reset()


def test_a_live_token_reads_ok_with_its_lifetime_facts(vault) -> None:
    status = vault_token.status()
    assert status.state == "ok"
    assert (status.ttl_seconds, status.renewable, status.period_seconds) == (2591000, True, 2592000)
    assert status.expire_time == "2026-12-11T00:00:00Z"
    assert status.policies == ("default", "orcastra-policy") and status.is_root is False


def test_a_refused_token_is_one_the_registry_list_also_refuses(vault) -> None:
    vault.lookup, vault.registry = 403, 403
    assert vault_token.status().state == "refused"
    assert vault.calls == ["lookup", "LIST secret/metadata/clusters"]


@pytest.mark.parametrize("registry", [200, 404])
def test_a_token_without_the_default_policy_is_unsupported_not_refused(vault, registry) -> None:
    vault.lookup, vault.registry = 403, registry
    assert vault_token.status().state == "unsupported"
    assert vault_token.refusal_explains(403) is False


def test_vault_not_answering_the_lookup_is_unknown(vault) -> None:
    vault.lookup = requests.ConnectionError("refused")
    assert vault_token.status().state == "unknown"
    assert vault_token.refusal_explains(403) is True


def test_a_root_token_says_so(vault) -> None:
    vault.data = {"ttl": 0, "renewable": False, "policies": ["root"], "expire_time": None}
    status = vault_token.status()
    assert status.state == "ok" and status.is_root is True and status.ttl_seconds == 0


def test_no_vault_configured_asks_nothing(vault, monkeypatch) -> None:
    monkeypatch.setattr(vault_token, "get_settings", lambda: SimpleNamespace(
        vault_enabled=False, vault_addr="", vault_token=None, vault_clusters_path="secret/data/clusters"))
    assert vault_token.status().state == "not_configured"
    assert vault.calls == []


def test_an_answer_is_reused_inside_its_window(vault) -> None:
    vault_token.status()
    vault_token.status()
    assert vault.calls == ["lookup"]


def test_a_burst_of_callers_shares_one_lookup(vault) -> None:
    vault.gate = threading.Event()
    results: List[str] = []
    threads = [threading.Thread(target=lambda: results.append(vault_token.status().state)) for _ in range(20)]
    for thread in threads:
        thread.start()
    time.sleep(0.05)
    vault.gate.set()
    for thread in threads:
        thread.join(3)
    assert results == ["ok"] * 20
    assert vault.calls == ["lookup"]


def test_a_403_reprobes_a_cached_ok_older_than_five_seconds(vault, monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(vault_token.time, "time", lambda: clock[0])
    assert vault_token.status().state == "ok"
    vault.lookup, vault.registry = 403, 403
    clock[0] += 3
    assert vault_token.refusal_explains(403) is False, "a fresh ok answer is trusted"
    clock[0] += 3
    assert vault_token.refusal_explains(403) is True, "an ok older than 5 s is asked again on a 403"
    assert vault_token.status().state == "refused"


def test_a_flood_of_403s_asks_at_most_once_per_window(vault, monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(vault_token.time, "time", lambda: clock[0])
    vault_token.status()
    for _ in range(1000):
        clock[0] += 0.02
        vault_token.refusal_explains(403)
    # 20 seconds of 403s: the first lookup plus at most one per 5 s window.
    assert vault.calls.count("lookup") <= 5


def test_only_a_403_is_explained_by_the_token(vault) -> None:
    vault.lookup, vault.registry = 403, 403
    assert vault_token.refusal_explains(404) is False
    assert vault_token.refusal_explains(None) is False
    assert vault.calls == []


def test_each_change_is_announced_once(vault) -> None:
    seen: List[tuple] = []
    vault_token.on_change(lambda before, after: seen.append((before.state if before else None, after.state)))
    vault_token.status()
    vault_token.invalidate()
    vault_token.status()
    vault.lookup, vault.registry = 403, 403
    vault_token.invalidate()
    vault_token.status()
    vault.lookup = 200
    vault_token.invalidate()
    vault_token.status()
    assert seen == [(None, "ok"), ("ok", "refused"), ("refused", "ok")]


def test_the_lookup_body_never_reaches_a_log(vault, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    vault_token.status()
    vault.lookup, vault.registry = 403, 403
    vault_token.invalidate()
    vault_token.status()
    text = caplog.text
    assert "SECRET-TOKEN-ID" not in text and "acc-NEVER-LOGGED" not in text
    assert "refused" in text


# The store-failed classifier every Vault reader shares.

def _vault_error(status: Optional[int], *, transport: bool = False) -> VaultClientError:
    try:
        if transport:
            raise requests.ConnectionError("unreachable")
        raise requests.HTTPError(f"HTTP {status}")
    except requests.RequestException:
        try:
            raise VaultClientError("Vault read failed", status_code=status)
        except VaultClientError as exc:
            return exc


@pytest.mark.parametrize("status", [500, 502, 503, 429])
def test_a_server_side_vault_failure_is_a_store_failure(vault, status) -> None:
    assert vault_token.store_failed(_vault_error(status)) is True


def test_an_unreached_vault_is_a_store_failure(vault) -> None:
    assert vault_token.store_failed(_vault_error(None, transport=True)) is True


def test_a_missing_document_is_not_a_store_failure(vault) -> None:
    assert vault_token.store_failed(_vault_error(404)) is False


def test_a_403_is_a_store_failure_only_when_the_token_is_refused(vault) -> None:
    assert vault_token.store_failed(_vault_error(403)) is False
    vault.lookup, vault.registry = 403, 403
    vault_token.invalidate()
    assert vault_token.store_failed(_vault_error(403)) is True


def test_a_path_the_client_refused_before_asking_is_never_a_store_failure(vault) -> None:
    # The dot-segment guard raises with no status and no transport error behind it.
    assert vault_token.store_failed(VaultClientError("Refusing Vault path")) is False


def test_a_wrapped_vault_failure_is_found_in_the_chain(vault) -> None:
    class Wrapper(Exception):
        pass

    try:
        try:
            raise _vault_error(503)
        except VaultClientError as inner:
            raise Wrapper("Could not retrieve cluster info") from inner
    except Wrapper as outer:
        assert vault_token.store_failed(outer) is True
    assert vault_token.store_failed(ValueError("unrelated")) is False


def test_a_peer_transport_error_is_not_vaults_when_only_vault_counts(vault):
    try:
        raise requests.ConnectionError("cluster did not answer")
    except requests.ConnectionError as exc:
        assert vault_token.store_failed(exc) is True
        assert vault_token.store_failed(exc, vault_only=True) is False
    assert vault_token.store_failed(_vault_error(503), vault_only=True) is True
