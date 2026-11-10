"""A marketplace may learn how a cluster recovers from a lost member, and nothing else at /1.0.

OrcaHub tells buyers whether a listing survives a host failure. LXD keeps the deciding switch,
`cluster.healing_threshold`, in the server configuration, which the raw proxy does not serve
because the same object carries the server certificate, its listen addresses and, before LXD 6,
`core.trust_password`. GET /integrations/clusters/failover reads it server side and returns
numbers only. These tests check that the numbers arrive and nothing else does.
"""

import inspect
import json
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from app.routers import integrations
from app.services.lxd.failover_config import FAILOVER_CONFIG_DEFAULTS, project_failover_config

SECRETS = (
    "s3cr3t-trust",
    "BEGIN CERTIFICATE",
    "10.20.30.40",
    "member-alpha",
    "oidc.client.id",
)


def _raw_root(*, auth: str = "trusted", config: Dict[str, Any] | None = None, clustered: Any = True) -> Dict[str, Any]:
    """A GET /1.0 metadata block shaped like LXD's, carrying everything that must not leak."""
    base_config = {
        "core.trust_password": "s3cr3t-trust",
        "core.https_address": "10.20.30.40:8443",
        "cluster.https_address": "10.20.30.40:8443",
        "oidc.client.id": "oidc.client.id",
    }
    return {
        "auth": auth,
        "api_extensions": ["clustering", "network_type_ovn"],
        "config": {**base_config, **(config or {})},
        "environment": {
            "addresses": ["10.20.30.40:8443"],
            "certificate": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
            "server_name": "member-alpha",
            "server_clustered": clustered,
        },
    }


def _key(*cluster_ids: str) -> dict:
    return {
        "id": "oak_test",
        "app_name": "test",
        "cluster_permissions": [
            {"cluster_id": c, "permissions": ["read"], "projects": ["default"]} for c in cluster_ids
        ],
    }


# ── the projection ─────────────────────────────────────────────────────────────────────────────

def test_set_thresholds_are_returned_as_numbers() -> None:
    result = project_failover_config(_raw_root(config={
        "cluster.healing_threshold": "120",
        "cluster.offline_threshold": "30",
        "cluster.max_voters": "5",
        "cluster.max_standby": "1",
    }))
    assert result == {
        "clustered": True,
        "healing_threshold_seconds": 120,
        "offline_threshold_seconds": 30,
        "max_voters": 5,
        "max_standby": 1,
    }


def test_an_unset_key_on_a_trusted_read_is_lxds_default() -> None:
    """LXD omits a key left at its default, so omission on a trusted read is a positive answer."""
    result = project_failover_config(_raw_root())
    assert result["healing_threshold_seconds"] == FAILOVER_CONFIG_DEFAULTS["cluster.healing_threshold"] == 0
    assert result["offline_threshold_seconds"] == 20
    assert result["max_voters"] == 3
    assert result["max_standby"] == 2


def test_an_untrusted_read_vouches_for_nothing() -> None:
    """An untrusted answer carries no configuration, so every threshold is unknown (None)."""
    result = project_failover_config(_raw_root(auth="untrusted"))
    assert result["healing_threshold_seconds"] is None
    assert result["offline_threshold_seconds"] is None
    assert result["max_voters"] is None


@pytest.mark.parametrize("raw", ["-1", "abc", "1.5", True, "99999999999", [], {}])
def test_a_malformed_value_is_unknown(raw: Any) -> None:
    result = project_failover_config(_raw_root(config={"cluster.healing_threshold": raw}))
    assert result["healing_threshold_seconds"] is None


def test_a_standalone_server_reports_itself_unclustered() -> None:
    assert project_failover_config(_raw_root(clustered=False))["clustered"] is False


@pytest.mark.parametrize("metadata", [None, "x", [], {}, {"environment": "x"}])
def test_a_shapeless_answer_is_all_unknown(metadata: Any) -> None:
    result = project_failover_config(metadata)
    assert set(result.values()) == {None}


def test_nothing_that_addresses_or_admits_to_the_server_survives() -> None:
    rendered = json.dumps(project_failover_config(_raw_root(config={"cluster.healing_threshold": "60"})))
    for secret in SECRETS:
        assert secret not in rendered
    assert set(json.loads(rendered)) == {
        "clustered",
        "healing_threshold_seconds",
        "offline_threshold_seconds",
        "max_voters",
        "max_standby",
    }


# ── the endpoint ───────────────────────────────────────────────────────────────────────────────

def test_the_read_requires_inventory_read() -> None:
    parameters = inspect.signature(integrations.list_integration_cluster_failover).parameters
    default = parameters["api_key"].default
    rendered = repr(getattr(default, "dependency", default))
    assert "require_inventory_read" in rendered, rendered


def test_the_fetch_reads_a_fresh_root_and_returns_only_the_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class FakeApi:
        def get(self):
            calls.append("GET /1.0")
            return SimpleNamespace(json=lambda: {"metadata": _raw_root(config={"cluster.healing_threshold": "90"})})

    client = SimpleNamespace(api=FakeApi(), host_info={"config": {"cluster.healing_threshold": "0"}})
    monkeypatch.setattr(integrations, "connect_or_get_client", lambda service, cluster: client)

    rows = integrations._fetch_cluster_failover(object(), "c1")

    assert calls == ["GET /1.0"], "must not answer from the pooled client's cached host_info"
    assert rows == [{
        "cluster_id": "c1",
        "clustered": True,
        "healing_threshold_seconds": 90,
        "offline_threshold_seconds": 20,
        "max_voters": 3,
        "max_standby": 2,
    }]
    rendered = json.dumps(rows)
    for secret in SECRETS:
        assert secret not in rendered


def test_an_unreachable_cluster_raises_rather_than_answering_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults for a cluster that never answered would read as "healing off", a false negative."""
    monkeypatch.setattr(integrations, "connect_or_get_client", lambda service, cluster: None)
    with pytest.raises(Exception) as caught:
        integrations._fetch_cluster_failover(object(), "c1")
    assert getattr(caught.value, "status_code", None) == 503


async def test_the_endpoint_answers_only_the_keys_own_clusters(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    def fake_fetch(service, cluster_id):
        seen.append(cluster_id)
        return [{"cluster_id": cluster_id, **project_failover_config(_raw_root())}]

    monkeypatch.setattr(integrations, "_fetch_cluster_failover", fake_fetch)
    monkeypatch.setattr(integrations.health, "is_tripped", lambda cluster: False)

    body = await integrations.list_integration_cluster_failover(cluster_id="victim", api_key=_key("a", "b"), lxd_service=object())
    assert body == {"clusters": [], "total": 0, "partial": False, "unreachable": []}

    body = await integrations.list_integration_cluster_failover(cluster_id=None, api_key=_key("b", "a"), lxd_service=object())
    assert [row["cluster_id"] for row in body["clusters"]] == ["a", "b"]
    assert sorted(seen) == ["a", "b"]
    assert body["total"] == 2 and body["partial"] is False


async def test_a_silent_cluster_is_reported_not_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_fetch(service, cluster_id):
        if cluster_id == "down":
            raise RuntimeError("connect timeout")
        return [{"cluster_id": cluster_id, **project_failover_config(_raw_root())}]

    monkeypatch.setattr(integrations, "_fetch_cluster_failover", fake_fetch)
    monkeypatch.setattr(integrations.health, "is_tripped", lambda cluster: False)

    body = await integrations.list_integration_cluster_failover(cluster_id=None, api_key=_key("up", "down"), lxd_service=object())
    assert [row["cluster_id"] for row in body["clusters"]] == ["up"]
    assert body["partial"] is True
    assert body["unreachable"][0]["cluster_id"] == "down"


def test_the_deployment_advertises_the_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integrations, "multi_owner_enabled", lambda: False)
    assert integrations._integration_features() == ["cluster_failover_config.v1"]
