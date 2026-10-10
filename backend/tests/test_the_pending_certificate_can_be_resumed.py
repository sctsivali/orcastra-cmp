"""Closing the renewal dialog no longer loses the command that finishes it.

Verify reopened from a row showed step two with nothing in it: the command and the certificate
lived only in the response to the original renew. The pending certificate's public half, its
fingerprint and the trust command are now readable again. Never its key.
"""

import pytest

from app.core.cert_fingerprint import lxd_fingerprint
from app.services.lxd_certs import rotation
from tests._cert_rotation_fakes import FakeCertManager


async def test_the_pending_certificate_its_fingerprint_and_its_command_come_back():
    manager = FakeCertManager(cluster_type="Incus")
    pem = manager.add_pending(pending_cert_id="pending-abc", identity_name="orca-nuc9-20261010-101010")

    view = await rotation.pending_view(manager, manager.cluster_id)

    assert view.fingerprint == lxd_fingerprint(pem)
    assert view.certificate == pem and view.pending_cert_id == "pending-abc"
    assert view.cli_command.startswith('cat <<EOF | incus config trust add-certificate - --name="orca-nuc9-20261010-101010"')
    assert "PRIVATE KEY" not in view.model_dump_json()


async def test_no_pending_certificate_is_not_found():
    manager = FakeCertManager()
    with pytest.raises(rotation.CertificateNotFound):
        await rotation.pending_view(manager, manager.cluster_id)
