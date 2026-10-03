"""A fresh registry is empty only when its authenticated KV-v2 mount is readable.

Real requests.Response parsing drives VaultClient and VaultStorageMixin. Every HTTP
request, cache write, audit event and setting is injected and synthetic.
"""

from __future__ import annotations

import copy
import json
import math
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
import requests

from app.core import config, org_scope, vault_client, vault_status
from app.services.lxd_certs.storage import VaultStorageMixin

ADDRESS = "http://vault.synthetic.invalid:18200"
TOKEN = "synthetic-vault-registry-token"
DATA_PATH = "qa-registry/data/clusters"
LIST_PATH = "qa-registry/metadata/clusters"
FLEET = ["qa-a/", "qa-b/"]
NORMALIZED_FLEET = ["qa-a", "qa-b"]
MOUNT = {"data": {"path": "qa-registry/", "type": "kv", "options": {"version": "2"}}}


class SyntheticResponse(requests.Response):
    """Use requests' JSON decoder and HTTP errors, tracking explicit response release."""

    def __init__(self, status: int, body: object):
        super().__init__()
        self.status_code = status
        self._content = body if isinstance(body, bytes) else json.dumps(body).encode()
        self._content_consumed = True
        self.headers["Content-Type"] = "application/json"
        self.closed_count = 0

    def close(self) -> None:
        self.closed_count += 1
        super().close()


class SyntheticSession:
    """An injected request queue with no socket, retries or endpoint discovery."""

    def __init__(self):
        self.pending = deque()
        self.calls = []

    def request(self, method, url, *, headers, timeout, allow_redirects=True):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "timeout": timeout, "allow_redirects": allow_redirects})
        if method not in {"LIST", "GET"} or not url.startswith(ADDRESS + "/v1/"):
            raise AssertionError("A request escaped the synthetic readonly Vault endpoint")
        if not self.pending:
            raise AssertionError("An unplanned retry or HTTP call was attempted")
        response = self.pending.popleft()
        if isinstance(response, Exception):
            raise response
        response.url = url
        return response


class IsolatedCache:
    def __init__(self):
        self.values = {}
        self.writes = []

    def get(self, namespace, path):
        return copy.deepcopy(self.values.get((namespace, path)))

    def set(self, namespace, path, value, ttl):
        self.values[(namespace, path)] = copy.deepcopy(value)
        self.writes.append((namespace, path, copy.deepcopy(value), ttl))


class SyntheticStorage(VaultStorageMixin):
    def __init__(self, client):
        self.vault = client
        self.clusters_path = DATA_PATH


@dataclass
class RegistryHarness:
    storage: SyntheticStorage
    session: SyntheticSession
    cache: IsolatedCache
    audit: list
    settings: SimpleNamespace

    def seed(self) -> None:
        response = SyntheticResponse(200, {"data": {"keys": FLEET}})
        self.session.pending.append(response)
        assert self.storage.list_clusters(use_cache=False) == NORMALIZED_FLEET
        assert vault_status.registry_readable() is True
        assert response.closed_count == 1

    def assert_unreadable(self, snapshot: dict) -> None:
        assert vault_status.registry_readable() is False
        assert vault_status.snapshot()["registry_source"] == "last_known"
        assert vault_status.last_known_registry() == NORMALIZED_FLEET
        assert self.cache.values == snapshot
        assert self.audit[-1]["result"] == "failure"
        assert self.audit[-1]["error_code"] == "VaultListError"


@pytest.fixture
def registry(monkeypatch):
    vault_status.reset()
    session, cache, audit = SyntheticSession(), IsolatedCache(), []
    settings = SimpleNamespace(vault_clusters_path=DATA_PATH)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(vault_client, "get_cache", lambda: cache)
    monkeypatch.setattr(vault_client, "_audit_vault_operation", lambda _action, _path, **fields: audit.append(fields))
    monkeypatch.setattr(vault_client.VaultClient, "_create_session", lambda _self: session)
    monkeypatch.setattr(org_scope, "apply_cluster_filter", lambda ids: list(ids))
    client = vault_client.VaultClient(ADDRESS, TOKEN)
    yield RegistryHarness(SyntheticStorage(client), session, cache, audit, settings)
    vault_status.reset()


def assert_bounded_requests(calls, *, expected_methods):
    assert [call["method"] for call in calls] == expected_methods
    for call in calls:
        assert call["headers"] == {"X-Vault-Token": TOKEN}
        assert call["allow_redirects"] is False
        assert isinstance(call["timeout"], tuple) and len(call["timeout"]) == 2
        assert all(isinstance(value, (int, float)) and math.isfinite(value) and value > 0
                   for value in call["timeout"])
        assert sum(call["timeout"]) <= (13 if call["method"] == "LIST" else 5)


def test_a_fresh_kv_v2_registry_is_authoritative_empty(registry):
    """A 404 can mean no keys, but the same token must first prove the actual mount."""
    missing = SyntheticResponse(404, {"errors": []})
    proof = SyntheticResponse(200, MOUNT)
    registry.session.pending.extend([missing, proof])

    assert registry.storage.list_clusters(use_cache=False) == []
    assert vault_status.registry_readable() is True
    assert vault_status.snapshot()["registry_source"] == "vault"
    assert vault_status.last_known_registry() == []
    assert registry.storage.vault.last_known_list(LIST_PATH) == []
    assert registry.cache.values == {("vault_list", LIST_PATH): [],
                                     (vault_client.LAST_GOOD_LIST_NAMESPACE, LIST_PATH): []}
    assert registry.cache.writes == [("vault_list", LIST_PATH, [], 600),
                                     (vault_client.LAST_GOOD_LIST_NAMESPACE, LIST_PATH, [], 7 * 24 * 3600)]
    assert registry.audit == [{"duration_ms": registry.audit[0]["duration_ms"], "details": {"key_count": 0}}]
    assert_bounded_requests(registry.session.calls, expected_methods=["LIST", "GET"])
    assert registry.session.calls[0]["url"] == ADDRESS + "/v1/" + LIST_PATH
    assert registry.session.calls[1]["url"] == ADDRESS + "/v1/sys/internal/ui/mounts/" + LIST_PATH
    assert missing.closed_count == proof.closed_count == 1
    assert not registry.session.pending


@pytest.mark.parametrize("keys", [[], FLEET], ids=["empty", "registered-clusters"])
def test_a_successful_list_is_authoritative_without_a_mount_probe(registry, keys):
    response = SyntheticResponse(200, {"data": {"keys": keys}})
    registry.session.pending.append(response)

    assert registry.storage.list_clusters(use_cache=False) == [key.rstrip("/") for key in keys]
    assert vault_status.registry_readable() is True
    assert registry.cache.values[(vault_client.LAST_GOOD_LIST_NAMESPACE, LIST_PATH)] == keys
    assert vault_status.last_known_registry() == [key.rstrip("/") for key in keys]
    assert_bounded_requests(registry.session.calls, expected_methods=["LIST"])
    assert response.closed_count == 1


@pytest.mark.parametrize("status, body", [
    (403, {"errors": ["permission denied"]}),
    (503, {"errors": ["sealed"]}),
    (301, {"errors": []}),
    (302, {"errors": []}),
    (307, {"errors": []}),
    (404, {"errors": ["not found"]}),
    (404, {"errors": [], "warnings": []}),
    (404, {}),
    (404, None),
    (404, []),
    (404, {"errors": None}),
    (404, b"not-json"),
    (404, b'{"errors":["denied"],"errors":[]}'),
], ids=["denied", "sealed", "permanent-redirect", "redirect", "temporary-redirect",
        "nonempty-error", "extra-fields", "missing-errors", "null", "array", "null-errors",
        "malformed-json", "duplicate-errors"])
def test_a_list_failure_never_probes_a_mount_or_replaces_last_known(registry, status, body):
    registry.seed()
    snapshot = copy.deepcopy(registry.cache.values)
    response = SyntheticResponse(status, body)
    registry.session.pending.append(response)

    assert registry.storage.list_clusters(use_cache=False) == NORMALIZED_FLEET
    registry.assert_unreadable(snapshot)
    assert_bounded_requests(registry.session.calls[1:], expected_methods=["LIST"])
    assert response.closed_count == 1
    assert not registry.session.pending


@pytest.mark.parametrize("stored_path, configured_path", [
    ("qa-registry/data/other", DATA_PATH),
    (DATA_PATH, "different-registry/data/clusters"),
    ("qa-registry/clusters", "qa-registry/clusters"),
    ("pki/certs", "pki/certs"),
    ("qa-registry/data/clusters/", DATA_PATH),
], ids=["other-registry", "wrong-configured-prefix", "kv-v1-path", "pki-path", "trailing-slash"])
def test_an_empty_404_at_another_path_cannot_prove_this_registry(registry, stored_path, configured_path):
    registry.seed()
    snapshot = copy.deepcopy(registry.cache.values)
    registry.storage.clusters_path = stored_path
    registry.settings.vault_clusters_path = configured_path
    response = SyntheticResponse(404, {"errors": []})
    registry.session.pending.append(response)

    assert registry.storage.list_clusters(use_cache=False) == NORMALIZED_FLEET
    registry.assert_unreadable(snapshot)
    assert_bounded_requests(registry.session.calls[1:], expected_methods=["LIST"])
    assert response.closed_count == 1


@pytest.mark.parametrize("status, body", [
    (403, MOUNT), (404, MOUNT), (503, MOUNT), (302, MOUNT), (307, MOUNT),
    (200, {"data": {"path": "qa-registry/", "type": "kv", "options": {"version": "1"}}}),
    (200, {"data": {"path": "qa-registry/", "type": "pki", "options": {"version": "2"}}}),
    (200, {"data": {"path": "neighbor/", "type": "kv", "options": {"version": "2"}}}),
    (200, {"data": {"path": "qa-registry", "type": "kv", "options": {"version": "2"}}}),
    (200, {"data": {"path": "qa-registry/", "type": "kv", "options": {"version": 2}}}),
    (200, {"data": {"path": "qa-registry/", "type": "kv", "options": {"version": None}}}),
    (200, {"data": {"path": "qa-registry/", "type": "kv", "options": {}}}),
    (200, {"data": {"path": "qa-registry/", "type": "kv", "options": None}}),
    (200, {"data": {"path": "qa-registry/", "type": "kv", "options": []}}),
    (200, {"data": None}), (200, []), (200, None), (200, {}), (200, b"not-json"),
    (200, b'{"data":{"path":"qa-registry/","type":"kv","options":{"version":"1","version":"2"}}}'),
    (200, b'{"data":{"path":"neighbor/","path":"qa-registry/","type":"kv","options":{"version":"2"}}}'),
], ids=["denied", "missing", "unavailable", "redirect", "temporary-redirect", "kv-v1", "pki",
        "neighbor-mount", "noncanonical-mount", "numeric-version", "null-version", "missing-version",
        "null-options", "array-options", "null-data", "array", "null", "missing-data", "malformed-json",
        "duplicate-version", "duplicate-mount"])
def test_failed_mount_proof_preserves_last_known_and_releases_both_responses(registry, status, body):
    registry.seed()
    snapshot = copy.deepcopy(registry.cache.values)
    missing, proof = SyntheticResponse(404, {"errors": []}), SyntheticResponse(status, body)
    registry.session.pending.extend([missing, proof])

    assert registry.storage.list_clusters(use_cache=False) == NORMALIZED_FLEET
    registry.assert_unreadable(snapshot)
    assert_bounded_requests(registry.session.calls[1:], expected_methods=["LIST", "GET"])
    assert missing.closed_count == proof.closed_count == 1
    assert not registry.session.pending


@pytest.mark.parametrize("error", [requests.exceptions.Timeout("Synthetic proof timeout"),
                                  requests.exceptions.ConnectionError("Synthetic proof connection failure")],
                         ids=["timeout", "connection-error"])
def test_mount_transport_failure_remains_an_unreadable_registry(registry, error):
    registry.seed()
    snapshot = copy.deepcopy(registry.cache.values)
    missing = SyntheticResponse(404, {"errors": []})
    registry.session.pending.extend([missing, error])

    assert registry.storage.list_clusters(use_cache=False) == NORMALIZED_FLEET
    registry.assert_unreadable(snapshot)
    assert_bounded_requests(registry.session.calls[1:], expected_methods=["LIST", "GET"])
    assert missing.closed_count == 1


@pytest.mark.parametrize("body", [
    {}, {"data": {}}, {"data": None}, {"data": []}, None, [],
    {"data": {"keys": None}}, {"data": {"keys": ""}}, {"data": {"keys": {}}},
    {"data": {"keys": [""]}}, {"data": {"keys": ["qa-a/", None]}}, {"data": {"keys": [False]}},
    b"not-json", b'{"data":{"keys":["qa-a/"]},"data":{"keys":[]}}',
    b'{"data":{"keys":["qa-a/"],"keys":[]}}',
], ids=["no-data", "no-keys", "null-data", "array-data", "null", "array", "null-keys", "string-keys",
        "object-keys", "empty-key", "null-key", "boolean-key", "malformed-json", "duplicate-data", "duplicate-keys"])
def test_a_malformed_200_list_cannot_masquerade_as_authoritative_empty(registry, body):
    registry.seed()
    snapshot = copy.deepcopy(registry.cache.values)
    response = SyntheticResponse(200, body)
    registry.session.pending.append(response)

    assert registry.storage.list_clusters(use_cache=False) == NORMALIZED_FLEET
    registry.assert_unreadable(snapshot)
    assert_bounded_requests(registry.session.calls[1:], expected_methods=["LIST"])
    assert response.closed_count == 1


def test_mount_proof_recovers_after_outage_and_remembers_authoritative_empty(registry):
    registry.seed()
    denied = SyntheticResponse(403, {"errors": ["permission denied"]})
    registry.session.pending.append(denied)
    assert registry.storage.list_clusters(use_cache=False) == NORMALIZED_FLEET
    assert vault_status.registry_readable() is False

    missing, proof = SyntheticResponse(404, {"errors": []}), SyntheticResponse(200, MOUNT)
    registry.session.pending.extend([missing, proof])
    assert registry.storage.list_clusters(use_cache=False) == []
    assert vault_status.registry_readable() is True
    assert vault_status.snapshot()["error"] is None
    assert vault_status.last_known_registry() == []
    assert registry.storage.vault.last_known_list(LIST_PATH) == []

    unavailable = SyntheticResponse(503, {"errors": ["sealed"]})
    registry.session.pending.append(unavailable)
    assert registry.storage.list_clusters(use_cache=False) == []
    assert vault_status.registry_readable() is False
    assert vault_status.snapshot()["registry_source"] == "last_known"
    assert vault_status.last_known_registry() == []
    assert registry.cache.values[(vault_client.LAST_GOOD_LIST_NAMESPACE, LIST_PATH)] == []
    assert_bounded_requests(registry.session.calls, expected_methods=["LIST", "LIST", "LIST", "GET", "LIST"])
    assert all(response.closed_count == 1 for response in (denied, missing, proof, unavailable))


def test_request_scope_never_narrows_the_last_known_fleet_during_failure_or_recovery(registry, monkeypatch):
    monkeypatch.setattr(org_scope, "apply_cluster_filter", lambda ids: [key for key in ids if key == "qa-a"])
    good, denied = SyntheticResponse(200, {"data": {"keys": FLEET}}), SyntheticResponse(403, {"errors": ["denied"]})
    registry.session.pending.extend([good, denied])
    assert registry.storage.list_clusters(use_cache=False) == ["qa-a"]
    assert vault_status.last_known_registry() == NORMALIZED_FLEET
    assert registry.storage.list_clusters(use_cache=False) == ["qa-a"]
    assert vault_status.registry_readable() is False
    assert vault_status.last_known_registry() == NORMALIZED_FLEET
    assert registry.cache.values[(vault_client.LAST_GOOD_LIST_NAMESPACE, LIST_PATH)] == FLEET

    monkeypatch.setattr(org_scope, "apply_cluster_filter", lambda ids: list(ids))
    sealed = SyntheticResponse(503, {"errors": ["sealed"]})
    registry.session.pending.append(sealed)
    assert registry.storage.list_clusters(use_cache=False) == NORMALIZED_FLEET
    assert vault_status.registry_readable() is False

    recovered = SyntheticResponse(200, {"data": {"keys": [*FLEET, "qa-c/"]}})
    registry.session.pending.append(recovered)
    assert registry.storage.list_clusters(use_cache=False) == [*NORMALIZED_FLEET, "qa-c"]
    assert vault_status.registry_readable() is True
    assert vault_status.last_known_registry() == [*NORMALIZED_FLEET, "qa-c"]
    assert registry.cache.values[(vault_client.LAST_GOOD_LIST_NAMESPACE, LIST_PATH)] == [*FLEET, "qa-c/"]
    assert_bounded_requests(registry.session.calls, expected_methods=["LIST"] * 4)
    assert all(response.closed_count == 1 for response in (good, denied, sealed, recovered))
