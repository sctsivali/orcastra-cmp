"""Capability discovery is scoped, role-gated and performed outside the request loop."""

import threading
from types import SimpleNamespace

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import org_scope, rbac
from app.core.auth import AuthenticatedUser
from app.core.blocking import BlockingPoolFull, BlockingTimeout
from app.core.database import get_db
from app.core.org_scope import org_scope_dependency
from app.core.rbac import get_current_user
from app.routers import networks
from app.services.lxd_service import get_lxd_service


def app_client(monkeypatch, role, *, metadata=None, error=None):
    calls = []
    state = {}

    async def scope():
        state["loop_thread"] = threading.get_ident()
        token = org_scope._allowed_clusters_var.set({"qa-own"})
        try:
            yield
        finally:
            org_scope._allowed_clusters_var.reset(token)

    def read(host):
        calls.append({"host": host, "thread": threading.get_ident()})
        if error is not None:
            raise error
        return SimpleNamespace(host_info=metadata or {})

    monkeypatch.setattr(rbac, "resolve_roles", rbac._resolve_roles_uncached)
    monkeypatch.setattr(rbac.activity_logger, "log_activity", lambda **kwargs: None)
    app = FastAPI()
    app.include_router(networks.router)
    user = AuthenticatedUser(user_id=f"qa-{role}", email=f"{role}@e2e.invalid", groups=[f"role_{role}"])
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[org_scope_dependency] = scope
    app.dependency_overrides[get_lxd_service] = lambda: SimpleNamespace(get_client_with_reconnect=read)
    app.dependency_overrides[get_db] = lambda: None
    return TestClient(app, raise_server_exceptions=False), calls, state


@pytest.mark.parametrize("role", ["admin", "partner"])
def test_authorized_capability_discovery_returns_only_supported_options(monkeypatch, role):
    client, calls, state = app_client(monkeypatch, role, metadata={
        "environment": {"internal": "not-part-of-this-contract"}, "config": {"internal": "excluded"},
        "api_extensions": ["network_type_ovn", "network_acl", "network_bridge_acl", "network_ovn_acl", "network_ovn_dhcp", "network_ovn_nat"],
    })
    response = client.get("/networks/creation/capabilities?host=qa-own")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"known", "types", "config_keys"}
    assert payload["types"] == ["bridge", "ovn"]
    assert "security.acls" in payload["config_keys"]["bridge"]
    assert "dns.mode" not in payload["config_keys"]["ovn"]
    assert len(calls) == 1
    assert calls[0]["thread"] != state["loop_thread"]


def test_tenant_cannot_discover_creation_capabilities_or_submit_a_network(monkeypatch):
    client, calls, _ = app_client(monkeypatch, "tenant")
    assert client.get("/networks/creation/capabilities?host=qa-own").status_code == 403
    response = client.post("/networks?host=qa-own", json={"name": "qa-network", "type": "bridge"})
    assert response.status_code == 403, response.text
    response = client.put("/networks/qa-network?host=qa-own", json={"description": "denied"})
    assert response.status_code == 403, response.text
    assert calls == []


def test_partner_cannot_probe_a_neighbor_cluster(monkeypatch):
    client, calls, _ = app_client(monkeypatch, "partner")
    response = client.get("/networks/creation/capabilities?host=qa-neighbor")
    assert response.status_code == 403, response.text
    response = client.put("/networks/qa-network?host=qa-neighbor", json={"description": "denied"})
    assert response.status_code == 403, response.text
    assert calls == []


def test_description_is_bounded_before_update_daemon_access(monkeypatch):
    client, calls, _ = app_client(monkeypatch, "partner")
    response = client.put("/networks/qa-network?host=qa-own", json={"description": "x" * 1025})
    assert response.status_code == 422, response.text
    assert calls == []


def test_missing_metadata_is_explicit_and_does_not_advertise_ovn_or_acls(monkeypatch):
    client, _, _ = app_client(monkeypatch, "partner")
    response = client.get("/networks/creation/capabilities?host=qa-own")
    assert response.status_code == 200, response.text
    assert response.json()["known"] is False
    assert response.json()["types"] == ["bridge"]
    assert "security.acls" not in response.json()["config_keys"]["bridge"]


@pytest.mark.parametrize("ranges", [False, True])
def test_capability_response_exposes_ovn_ranges_only_with_the_specific_extension(monkeypatch, ranges):
    extensions = ["network_type_ovn", "network_ovn_dhcp"]
    if ranges:
        extensions.append("ovn_dhcp_ranges")
    client, _, _ = app_client(monkeypatch, "partner", metadata={"api_extensions": extensions})
    response = client.get("/networks/creation/capabilities?host=qa-own")
    assert response.status_code == 200, response.text
    keys = response.json()["config_keys"]["ovn"]
    assert "ipv4.dhcp" in keys and "ipv6.dhcp" in keys
    assert ("ipv4.dhcp.ranges" in keys) is ranges


def test_a_capability_transport_failure_is_unavailable_not_empty_success(monkeypatch):
    client, _, _ = app_client(monkeypatch, "partner", error=requests.ConnectTimeout("synthetic timeout"))
    response = client.get("/networks/creation/capabilities?host=qa-own")
    assert response.status_code == 503, response.text
    assert "config_keys" not in response.json()


@pytest.mark.parametrize(("error", "status"), [(BlockingTimeout("synthetic timeout", 1.0), 504), (BlockingPoolFull("synthetic busy", 1), 503)])
def test_worker_timeout_and_saturation_are_reported(monkeypatch, error, status):
    client, calls, _ = app_client(monkeypatch, "partner")

    async def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(networks, "_run_network_blocking", unavailable)
    response = client.get("/networks/creation/capabilities?host=qa-own")
    assert response.status_code == status, response.text
    assert calls == []
