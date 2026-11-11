"""A cluster whose credentials Vault cannot serve is not an offline cluster.

When Vault is sealed, down or refusing the dashboard's token, `ensure_connection` fails before it
ever contacts the cluster. Recorded as a cluster failure, that tripped the breaker on every
cluster at once, and the breaker then held them down after Vault recovered. A cluster that is
not registered (404) or answers and refuses our certificate keeps being recorded as before. The
Clusters page row names the refused token instead of a generic "could not be read".
"""

from types import SimpleNamespace

import pytest

from app.core import vault_token
from app.core.vault_client import VaultClientError
from app.services.lxd import health
from app.services.lxd.lxd_service import LXDService
from app.services.lxd_certs.types import LXDCertManagerError

CLUSTER = "qa-breaker-cluster"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    health.clear(CLUSTER)
    monkeypatch.setattr("app.core.org_scope.get_current_allowed_clusters", lambda: None, raising=False)
    yield
    health.clear(CLUSTER)


@pytest.fixture
def token(monkeypatch):
    state = {"value": "refused"}
    monkeypatch.setattr(vault_token, "status", lambda *, max_age=None, settings=None: vault_token.TokenStatus(state["value"], 1.0))
    monkeypatch.setattr(vault_token, "peek", lambda: vault_token.TokenStatus(state["value"], 1.0))
    return state


def _service(read_error: Exception) -> LXDService:
    service = LXDService.__new__(LXDService)

    def vault_read(path, use_cache=True):
        raise read_error

    service.cert_manager = SimpleNamespace(vault=SimpleNamespace(vault_read=vault_read))
    service.connection_manager = SimpleNamespace(get_client=lambda cluster_id, **_k: None)
    return service


def _failed(status):
    return VaultClientError("Vault read failed", status_code=status)


def test_a_refused_token_does_not_trip_the_breaker(token):
    assert _service(_failed(403)).ensure_connection(CLUSTER) is False
    assert health.is_tripped(CLUSTER) is False
    assert health.last_failure(CLUSTER) is None


@pytest.mark.parametrize("status", [503, 500])
def test_a_vault_outage_does_not_trip_the_breaker(token, status):
    token["value"] = "ok"
    assert _service(_failed(status)).ensure_connection(CLUSTER) is False
    assert health.last_failure(CLUSTER) is None


def test_an_unregistered_cluster_is_still_recorded(token):
    token["value"] = "ok"
    assert _service(_failed(404)).ensure_connection(CLUSTER) is False
    assert health.last_failure(CLUSTER) is not None


def test_a_403_the_policy_explains_is_still_recorded(token):
    token["value"] = "ok"
    _service(_failed(403)).ensure_connection(CLUSTER)
    assert health.last_failure(CLUSTER) is not None


def _row_manager(info_error: Exception):
    def refuse(cluster_id, include_cert_content=False):
        try:
            raise info_error
        except VaultClientError as exc:
            raise LXDCertManagerError(f"Could not retrieve cluster info: {exc}")

    return SimpleNamespace(
        get_cluster_certificate=lambda cluster_id: refuse(cluster_id),
        get_cluster_info=refuse,
        probe_connection=lambda cluster_id: SimpleNamespace(trusted=False, reachable=None, message=None),
    )


@pytest.fixture
def rows(monkeypatch):
    from app.routers import cluster_registration

    pool = SimpleNamespace(get_cached_client=lambda cid: None, get_client=lambda cid, **_k: None)
    monkeypatch.setattr("app.services.lxd_service.get_lxd_service", lambda: SimpleNamespace(connection_manager=pool))
    return cluster_registration


def test_the_clusters_row_names_the_refused_token(token, rows):
    row = rows._cluster_info_blocking(CLUSTER, _row_manager(_failed(403)))
    assert row.status.value == "error" and row.endpoint == "unknown"
    assert "refuses the dashboard's token" in row.error_message
    assert "Vault read failed" not in row.error_message


def test_a_vault_outage_row_says_vault_cannot_be_read(token, rows):
    token["value"] = "ok"
    row = rows._cluster_info_blocking(CLUSTER, _row_manager(_failed(503)))
    assert "secret store (Vault)" in row.error_message


def test_any_other_unreadable_row_keeps_the_generic_message(token, rows):
    token["value"] = "ok"
    row = rows._cluster_info_blocking(CLUSTER, _row_manager(_failed(404)))
    assert row.error_message == rows._ROW_UNREADABLE
