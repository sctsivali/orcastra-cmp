"""The certificate list carries what an operator needs to match it against `lxc config trust list`.

Each row names the active certificate's LXD fingerprint, the pending one when a renewal waits, and
the superseded trust entries with their state. Rows only ever come from the clusters the caller's
scope lists, so a partner never sees another organization's superseded entries.
"""

from app.core.cert_fingerprint import lxd_fingerprint
from app.models.schemas import CertificateStatus
from app.routers import cert_manager as router
from app.services.lxd_certs import trust_cleanup_store as store
from tests._cert_rotation_fakes import FakeCertManager, SessionMaker


class _ScopedManager(FakeCertManager):
    def list_clusters(self, use_cache=True):
        return [self.cluster_id]


async def test_each_row_names_the_active_pending_and_superseded_certificates():
    manager = _ScopedManager(cluster_type="Incus")
    pending_pem = manager.add_pending()
    db = SessionMaker().db
    old = "a" * 64
    await store.record_superseded(db, cluster_id=manager.cluster_id, fingerprint=old, reason="renew", grace_seconds=600)
    await store.record_superseded(db, cluster_id="foreign", fingerprint="b" * 64, reason="renew", grace_seconds=600)

    response = await router.list_certificates(cert_manager=manager, db=db)

    [row] = response.certificates
    assert row.fingerprint == lxd_fingerprint(manager.current[0])
    assert row.pending_fingerprint == lxd_fingerprint(pending_pem)
    assert row.status == CertificateStatus.PENDING_RENEWAL and row.has_pending_renewal
    assert row.endpoint == "https://10.0.0.9:8443"
    [entry] = row.superseded
    assert entry.fingerprint == old and entry.state == "pending"
    assert entry.manual_command == f"incus config trust remove {old}"
    assert response.total == 1


async def test_a_cluster_waiting_for_trust_has_no_row_and_an_unreadable_one_is_unknown():
    """Same as before this change: an empty certificate is a registration still pending trust,
    while a Vault read that fails is shown as unknown rather than hidden."""
    db = SessionMaker().db
    manager = _ScopedManager()
    manager.vault.data[manager.path("certs")]["client.crt"] = ""
    assert (await router.list_certificates(cert_manager=manager, db=db)).certificates == []

    manager = _ScopedManager()
    del manager.vault.data[manager.path("certs")]
    [row] = (await router.list_certificates(cert_manager=manager, db=db)).certificates
    assert row.status == CertificateStatus.UNKNOWN

    manager = _ScopedManager()
    manager.vault.data[manager.path("certs")]["client.crt"] = "not a pem"
    [row] = (await router.list_certificates(cert_manager=manager, db=db)).certificates
    assert row.status == CertificateStatus.UNKNOWN and row.fingerprint is None
