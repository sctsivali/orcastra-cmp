"""The Permissions pages read an Incus cluster's trust store, and never ask it for groups.

Incus has no /1.0/auth, so its 404 there must not read as an unreachable cluster. An Incus node
lists its trust-store certificates as identities (fingerprint, type, restricted, projects), the
groups list names it apart as unsupported, every group write on it is refused with
groups_not_on_incus, and deleting a certificate refuses the dashboard's own and any member
(server) certificate.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import permissions
from tests.test_an_incus_key_is_a_restricted_certificate_on_its_project import _Incus, _Store

_REAL_DASHBOARD_FINGERPRINTS = permissions._dashboard_fingerprints

OWN = "d" * 64
CLIENT = "e" * 64
MEMBER = "f" * 64


def _incus_store() -> _Store:
    store = _Store()
    store.entries = {
        OWN: {"fingerprint": OWN, "name": "orca-incus-lab-20261108", "type": "client", "restricted": False, "projects": []},
        CLIENT: {"fingerprint": CLIENT, "name": "ci@orcastra", "type": "client", "restricted": True, "projects": ["web"]},
        MEMBER: {"fingerprint": MEMBER, "name": "incus-a2", "type": "server", "restricted": False, "projects": []},
    }
    return store


class _Lxd:
    host_info = {"auth": "trusted", "environment": {"server": "lxd", "server_version": "5.21.8"}}

    def __init__(self):
        groups = SimpleNamespace(get=lambda params=None, timeout=None: SimpleNamespace(json=lambda: {"metadata": []}))
        self.api = SimpleNamespace(auth=SimpleNamespace(groups=groups))


@pytest.fixture
def fleet(monkeypatch):
    incus = _Incus(_incus_store())
    clients = {"incus-lab": incus, "nuc2": _Lxd()}
    manager = SimpleNamespace(
        get_node_names=lambda: list(clients),
        get_client=lambda node: clients.get(node),
        peek_client=lambda node: clients.get(node),
        active_certificate_fingerprint=lambda node: OWN,
    )
    service = SimpleNamespace(connection_manager=manager, get_node_names=lambda: list(clients), cert_manager=object())

    async def _client_for(_service, host):
        return clients.get(host)

    async def _held(*_args, **_kwargs):
        return set()

    async def _whole(*_args, **_kwargs):
        return None

    monkeypatch.setattr(permissions, "client_for", _client_for)
    monkeypatch.setattr(permissions, "clusters_held_elsewhere", _held)
    monkeypatch.setattr(permissions, "verify_whole_cluster_access", _whole)
    monkeypatch.setattr(permissions, "verify_cluster_access", lambda host: None)
    monkeypatch.setattr(permissions, "_dashboard_fingerprints", lambda manager, cluster: {OWN})
    return SimpleNamespace(service=service, incus=incus)


def test_an_incus_node_lists_its_certificates_as_identities(fleet):
    read = permissions._fetch_identities_for_node(fleet.service, "incus-lab")
    rows = {row.id: row for row in read.rows}
    assert rows[CLIENT].engine == "incus" and rows[CLIENT].restricted is True and rows[CLIENT].projects == ["web"]
    assert rows[MEMBER].type == "server" and rows[MEMBER].groups == []


async def test_the_groups_list_names_an_incus_cluster_apart_from_an_unreachable_one(fleet):
    response = await permissions.list_groups(host=None, lxd_service=fleet.service, db=None, user=None)
    assert response.unsupported_nodes == ["incus-lab"]
    assert "incus-lab" not in response.unreachable_nodes


async def test_an_incus_cluster_first_reached_by_the_groups_list_is_still_named_apart(fleet):
    """A worker with no pooled client connects inside the fan-out; the 404 Incus then answers on
    /1.0/auth still marks it unsupported rather than unreachable."""
    manager = fleet.service.connection_manager
    manager.peek_client = lambda node: None
    manager.get_client = lambda node: None
    fleet.service.cert_manager = SimpleNamespace(
        connect_with_vault_cert=lambda node, connection_manager=None: fleet.incus if node == "incus-lab" else None
    )
    response = await permissions.list_groups(host="incus-lab", lxd_service=fleet.service, db=None, user=None)
    assert response.unsupported_nodes == ["incus-lab"] and response.unreachable_nodes == []


async def test_a_group_read_on_incus_is_refused_with_a_reason(fleet):
    with pytest.raises(HTTPException) as caught:
        await permissions.get_group("key_1_group", host="incus-lab", lxd_service=fleet.service, db=None, user=None)
    assert caught.value.status_code == 409 and caught.value.detail["error"] == "groups_not_on_incus"


async def test_the_dashboards_own_certificate_cannot_be_deleted(fleet):
    with pytest.raises(HTTPException) as caught:
        await permissions.delete_identity("tls", OWN, host="incus-lab", lxd_service=fleet.service, _=None, db=None, user=None)
    assert caught.value.status_code == 409 and caught.value.detail["error"] == "own_certificate"
    assert OWN in fleet.incus.api.certificates.entries


async def test_a_member_certificate_cannot_be_deleted(fleet):
    with pytest.raises(HTTPException) as caught:
        await permissions.delete_identity("tls", MEMBER, host="incus-lab", lxd_service=fleet.service, _=None, db=None, user=None)
    assert caught.value.status_code == 409 and caught.value.detail["error"] == "not_a_client"


async def test_a_client_certificate_is_deleted_by_its_exact_fingerprint(fleet, monkeypatch):
    monkeypatch.setattr(permissions, "get_cache_manager", lambda: SimpleNamespace(delete_pattern=lambda *a: None))
    result = await permissions.delete_identity("tls", CLIENT, host="incus-lab", lxd_service=fleet.service, _=None, db=None, user=None)
    assert result["success"] is True
    assert CLIENT not in fleet.incus.api.certificates.entries and OWN in fleet.incus.api.certificates.entries


async def test_a_prefix_is_not_an_identity(fleet):
    with pytest.raises(HTTPException) as caught:
        await permissions.get_identity(CLIENT[:12], host="incus-lab", lxd_service=fleet.service, db=None, user=None)
    assert caught.value.status_code == 404


class _Vault:
    """Vault paths of one cluster; a value that is an exception is raised on read."""

    def __init__(self, leaves):
        self.leaves = leaves

    def vault_read(self, path, use_cache=True):
        from app.core.vault_client import VaultClientError

        value = self.leaves.get(path.rsplit("/", 1)[-1], VaultClientError("not there", status_code=404))
        if isinstance(value, Exception):
            raise value
        return value


def _pem():
    from app.routers.my_keys import generate_certificate

    cert_pem, _key, fingerprint = generate_certificate("orca-incus-lab", 30)
    return cert_pem.decode(), fingerprint


def test_the_dashboards_current_and_pending_certificates_are_both_its_own():
    """During a regenerate or a rotation the certificate a worker presents is not the only one the
    dashboard holds: Vault's current and pending certificates are its own as well."""
    (current, fp1), (pending, fp2) = _pem(), _pem()
    manager = SimpleNamespace(clusters_path="secret/data/clusters",
                              vault=_Vault({"certs": {"client.crt": current}, "pending_certs": {"client.crt": pending}}))
    assert permissions._dashboard_fingerprints(manager, "incus-lab") == {fp1, fp2}


def test_a_cluster_without_pending_or_any_certificate_reads_as_holding_none_of_them():
    manager = SimpleNamespace(clusters_path="secret/data/clusters", vault=_Vault({}))
    assert permissions._dashboard_fingerprints(manager, "incus-lab") == set()


async def test_a_pending_certificate_vault_cannot_read_stops_the_delete(fleet, monkeypatch):
    """The page reader takes any pending-read failure for "no pending certificate"; the delete
    must not, or a rotation's new certificate is open to deletion."""
    from app.core.vault_client import VaultClientError

    current, _fp = _pem()
    monkeypatch.setattr(permissions, "_dashboard_fingerprints", _REAL_DASHBOARD_FINGERPRINTS)
    fleet.service.cert_manager = SimpleNamespace(
        clusters_path="secret/data/clusters",
        vault=_Vault({"certs": {"client.crt": current}, "pending_certs": VaultClientError("sealed", status_code=503)}),
    )
    with pytest.raises(HTTPException) as caught:
        await permissions.delete_identity("tls", CLIENT, host="incus-lab", lxd_service=fleet.service, _=None, db=None, user=None)
    assert caught.value.status_code == 503 and "could not be read" in caught.value.detail
    assert CLIENT in fleet.incus.api.certificates.entries
