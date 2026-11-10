"""`GET /integrations/clusters` says what state each cluster is in, from caches only.

The route also backs MCP `orcastra_list_clusters`. The state comes from what the server already
holds, never a probe on the key's request, and an unknown state is null. Name and type come from
the stored registration (`display_name`, `type`). A tripped cluster stays a row with a curated
reason instead of moving to `unreachable` with the breaker's raw exception text, which can carry
the Vault address.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import app.routers.integrations as integrations
from app.core.api_key_permissions import granted_level_name
from app.core.cache import NODES_AVAILABILITY_CACHE_KEY, NODES_STATUS_CACHE_KEY
from app.services.lxd import health
from app.services.lxd import status as lxd_status

VAULT_TEXT = "Failed to read https://vault.internal:8200/v1/secret/data/lxd/clusters/c-down/certs"


def _now() -> datetime:
    # Per call, never at import. The suite reaches this file minutes after collection, and a
    # trip stamped "5 s ago" from an import-time clock is already past the breaker's 60 s TTL.
    return datetime.now(timezone.utc)


def _row(name: str, status: str, reason_code: str, checked_at: datetime, node_type: Optional[str] = None):
    return {
        "name": name, "display_name": name, "endpoint": f"https://{name}:8443", "status": status,
        "reason_code": reason_code, "reason": None if status == "online" else "Not answering.",
        "node_type": node_type, "checked_at": checked_at.isoformat(),
    }


def _body(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"clusters": rows, "online_count": 0, "total_count": len(rows), "source": "probe",
            "generated_at": _now().isoformat()}


class _Cache:
    def __init__(self, availability: Optional[Dict[str, Any]] = None, nodes: Optional[Dict[str, Any]] = None):
        self.availability, self.nodes = availability, nodes
        self.reads: List[str] = []
        self.writes: List[str] = []

    def get(self, namespace: str, key: str) -> Any:
        self.reads.append(key)
        if key.startswith(NODES_AVAILABILITY_CACHE_KEY):
            return self.availability
        if key.startswith(NODES_STATUS_CACHE_KEY):
            return self.nodes
        return None

    def set(self, namespace: str, key: str, value: Any, ttl: Any = None) -> None:
        self.writes.append(key)


class _CertManager:
    metadata: Dict[str, Dict[str, Any]] = {}

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    def get_cluster_info(self, cluster_id: str) -> Dict[str, Any]:
        if cluster_id == "c-gone":
            raise RuntimeError(VAULT_TEXT)
        return {"metadata": self.metadata.get(cluster_id, {"endpoint": f"https://{cluster_id}:8443"})}


def _key(*clusters: str, permissions: Optional[List[str]] = None, projects: Optional[List[str]] = None):
    grants = []
    for cluster_id in clusters:
        grant: Dict[str, Any] = {"cluster_id": cluster_id, "permissions": permissions or ["read"]}
        if projects is not None:
            grant["projects"] = projects
        grants.append(grant)
    return {"id": "oak_test", "capabilities": ["inventory.read"], "cluster_permissions": grants}


@pytest.fixture
def world(monkeypatch):
    cache = _Cache()
    monkeypatch.setattr(lxd_status, "get_cache_manager", lambda: cache)
    monkeypatch.setattr(
        lxd_status, "get_node_metadata_store",
        lambda: SimpleNamespace(get_node_display_name=lambda _cid: None),
    )

    def _forbidden(*_: Any, **__: Any) -> None:
        raise AssertionError("listing a key's clusters must never probe one")

    monkeypatch.setattr(lxd_status, "probe_clusters", _forbidden)
    monkeypatch.setattr(lxd_status, "probe_cluster", _forbidden)
    monkeypatch.setattr(
        integrations, "get_settings",
        lambda: SimpleNamespace(vault_enabled=True, vault_token="t", vault_addr="http://v", vault_clusters_path="lxd"),
    )
    monkeypatch.setattr(integrations, "get_vault_client", lambda *_: object())
    import app.services.lxd_cert_manager as cert_module

    monkeypatch.setattr(cert_module, "LXDCertificateManager", _CertManager)
    _CertManager.metadata = {}
    tripped: List[str] = []
    yield SimpleNamespace(cache=cache, tripped=tripped)
    for cluster_id in tripped:
        health.clear(cluster_id)


def _trip(world, cluster_id: str, kind: str = health.KIND_OFFLINE, at: Optional[datetime] = None) -> None:
    health.record_failure(cluster_id, VAULT_TEXT, kind=kind)
    if at is not None:
        with health._lock:
            _, reason, recorded_kind = health._failed[cluster_id]
            health._failed[cluster_id] = (at.timestamp(), reason, recorded_kind)
            health._last_failed[cluster_id] = at.timestamp()
    world.tripped.append(cluster_id)


def _list(api_key: Dict[str, Any]) -> Dict[str, Any]:
    return asyncio.run(integrations.list_integration_clusters(cluster_id=None, api_key=api_key))


def _by_id(body: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {row["id"]: row for row in body["clusters"]}


# ── the registration ───────────────────────────────────────────────────────────────────────


def test_the_row_names_and_types_the_cluster_from_what_vault_stores(world) -> None:
    _CertManager.metadata = {"c1": {"display_name": "Rack A", "type": "MicroCloud"}}
    (row,) = integrations._fetch_cluster_info(_CertManager(), "c1")
    assert row["name"] == "Rack A"
    assert row["server_type"] == "microcloud"


def test_a_cluster_without_a_stored_name_or_type_keeps_the_old_fallbacks(world) -> None:
    (row,) = integrations._fetch_cluster_info(_CertManager(), "c1")
    assert row["name"] == "c1"
    assert row["server_type"] == "lxd"


def test_the_registration_row_adds_no_keys() -> None:
    """The sync payload is stored by the integrator as sent, so a new key is a contract change."""
    (row,) = integrations._fetch_cluster_info(_CertManager(), "c1")
    assert set(row) == {
        "id", "name", "endpoint", "server_type", "registered_at", "zone", "location_data",
        "region_id", "region_name", "city", "country_code", "country_name", "latitude",
        "longitude", "facility_name", "facility_provider",
    }


# ── the state ──────────────────────────────────────────────────────────────────────────────


def test_a_cached_row_supplies_the_state_and_a_live_type(world) -> None:
    checked = _now() - timedelta(seconds=20)
    world.cache.availability = _body([_row("c1", "online", "ok", checked, node_type="Incus")])
    row = _by_id(_list(_key("c1")))["c1"]
    assert row["status"] == "online" and row["reason_code"] == "ok" and row["reason"] is None
    assert datetime.fromisoformat(row["checked_at"]) == checked
    assert row["server_type"] == "incus"


def test_a_cluster_nothing_checked_reads_as_unknown_not_offline(world) -> None:
    row = _by_id(_list(_key("c1")))["c1"]
    assert (row["status"], row["reason_code"], row["reason"], row["checked_at"]) == (None, None, None, None)


def test_a_warm_nodes_body_answers_when_the_availability_key_is_cold(world) -> None:
    world.cache.nodes = {"collected_at": _now().isoformat(), "nodes": [
        {"name": "c1", "endpoint": "https://c1:8443", "connected": False, "reachable": True, "auth": "untrusted"},
    ]}
    row = _by_id(_list(_key("c1")))["c1"]
    assert row["status"] == "not_authorized"


def test_the_cache_is_read_unscoped_and_never_written(world) -> None:
    _list(_key("c1"))
    assert world.cache.reads == [
        f"{NODES_AVAILABILITY_CACHE_KEY}:scope=all", f"{NODES_STATUS_CACHE_KEY}:scope=all",
    ]
    assert world.cache.writes == []


def test_a_tripped_cluster_stays_a_row_with_a_curated_reason(world) -> None:
    _trip(world, "c-down")
    body = _list(_key("c-down"))
    row = _by_id(body)["c-down"]
    assert row["status"] == "offline"
    assert row["reason"] and "vault" not in row["reason"].lower()
    assert body["partial"] is False and body["unreachable"] == []
    assert "vault.internal" not in json.dumps(body) and "/v1/" not in json.dumps(body)


def test_a_cluster_refusing_the_certificate_reads_not_authorized(world) -> None:
    _trip(world, "c-down", kind=health.KIND_UNAUTHORIZED)
    assert _by_id(_list(_key("c-down")))["c-down"]["status"] == "not_authorized"


def test_a_trip_newer_than_the_cache_wins(world) -> None:
    world.cache.availability = _body([_row("c-down", "online", "ok", _now() - timedelta(seconds=40))])
    _trip(world, "c-down", at=_now() - timedelta(seconds=5))
    row = _by_id(_list(_key("c-down")))["c-down"]
    assert row["status"] == "offline"
    assert datetime.fromisoformat(row["checked_at"]) == pytest.approx(_now() - timedelta(seconds=5), abs=timedelta(seconds=1))


def test_a_cache_row_newer_than_the_trip_wins(world) -> None:
    """Another worker reached the cluster after this one tripped."""
    _trip(world, "c-down", at=_now() - timedelta(seconds=40))
    world.cache.availability = _body([_row("c-down", "online", "ok", _now() - timedelta(seconds=5))])
    assert _by_id(_list(_key("c-down")))["c-down"]["status"] == "online"


def test_an_unreadable_cache_leaves_the_rows_listed(world, monkeypatch) -> None:
    def _broken() -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(lxd_status, "get_cache_manager", _broken)
    row = _by_id(_list(_key("c1")))["c1"]
    assert row["status"] is None and row["access"]["level"] == "read"


# ── the access and the misses ──────────────────────────────────────────────────────────────


def test_each_row_says_what_this_key_holds_there(world) -> None:
    rows = _by_id(_list(_key("c1", permissions=["read", "write"], projects=["tenant-a"])))
    assert rows["c1"]["access"] == {"level": "write", "projects": ["tenant-a"]}
    assert _by_id(_list(_key("c1")))["c1"]["access"] == {"level": "read", "projects": ["default"]}


@pytest.mark.parametrize("held, name", [
    ([], None), (["read"], "read"), (["admin", "read"], "admin"), (["bogus"], "read"),
])
def test_the_level_name_is_the_level_the_check_enforces(held, name) -> None:
    assert granted_level_name(held) == name


def test_an_unreadable_registration_is_a_sanitized_miss(world) -> None:
    body = _list(_key("c1", "c-gone"))
    assert set(_by_id(body)) == {"c1"}
    assert body["partial"] is True
    (miss,) = body["unreachable"]
    assert miss["cluster_id"] == "c-gone"
    assert "vault.internal" not in miss["reason"] and "/v1/" not in miss["reason"]


def test_every_fan_out_miss_reason_is_sanitized() -> None:
    def fetch(_ctx: Any, _cluster_id: str) -> List[Dict[str, Any]]:
        raise RuntimeError(VAULT_TEXT)

    fanout = asyncio.run(integrations._gather_per_cluster(fetch, None, ["c1"], "test"))
    assert "vault.internal" not in fanout.unreachable[0]["reason"]


def test_the_sync_payload_carries_the_same_rows() -> None:
    import inspect

    source = inspect.getsource(integrations.sync_data)
    assert "_gather_cluster_rows(api_key, cert_manager, cluster_ids)" in source
    assert "_fetch_cluster_info" not in source
