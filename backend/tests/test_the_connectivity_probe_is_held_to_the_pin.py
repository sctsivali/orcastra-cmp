"""The warnings feed's connectivity probe is held to the cluster's certificate pin.

It sent our client certificate with a bare requests.get(verify=False), the one connection to a
cluster that skipped the fingerprint every other connection is held to, so whatever answered the
endpoint received it. A pinned cluster is now probed through FingerprintHTTPAdapter; a cluster
with no pin yet is probed as before, since a probe must not be what mints one.
"""

from __future__ import annotations

from unittest.mock import patch

from app.services.lxd.connection import FingerprintHTTPAdapter
from app.services.lxd_certs import connectivity


class _Response:
    status_code = 200

    def json(self):
        return {"metadata": []}


def test_a_pinned_cluster_is_probed_through_the_fingerprint_adapter(monkeypatch):
    seen = {}

    def fake_session_get(self, url, **kwargs):
        seen["adapter"] = self.get_adapter(url)
        return _Response()

    monkeypatch.setattr(connectivity.requests.Session, "get", fake_session_get)
    monkeypatch.setattr(connectivity.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("unpinned get")))
    monkeypatch.setattr(
        connectivity.PinState, "from_metadata",
        staticmethod(lambda metadata: type("P", (), {"fingerprint": "ab" * 32})()),
    )
    certs = _certs()
    with patch("app.services.lxd_certs.connectivity.cert_files.materialize", return_value=("/c", "/k")), \
         patch("app.services.lxd_certs.connectivity.health.is_tripped", return_value=False), \
         patch("app.services.lxd_certs.connectivity.health.clear"):
        certs.probe_connection("nuc2")
    assert isinstance(seen["adapter"], FingerprintHTTPAdapter)
    assert seen["adapter"].server_fingerprint == "ab" * 32


def test_an_unpinned_cluster_is_probed_as_before(monkeypatch):
    calls = []
    monkeypatch.setattr(connectivity.requests, "get", lambda url, **k: calls.append(url) or _Response())
    monkeypatch.setattr(connectivity.PinState, "from_metadata", staticmethod(lambda metadata: None))
    certs = _certs()
    with patch("app.services.lxd_certs.connectivity.cert_files.materialize", return_value=("/c", "/k")), \
         patch("app.services.lxd_certs.connectivity.health.is_tripped", return_value=False), \
         patch("app.services.lxd_certs.connectivity.health.clear"):
        certs.probe_connection("nuc2")
    assert calls == ["https://nuc2.invalid:8443/1.0/instances"]


def _certs():
    from app.services.lxd_cert_manager import LXDCertificateManager

    manager = LXDCertificateManager.__new__(LXDCertificateManager)
    manager.clusters_path = "secret/data/clusters"
    manager.vault = type("V", (), {"vault_read": lambda self, path: {"endpoint": "https://nuc2.invalid:8443"}})()
    manager.get_cluster_certificate = lambda cluster_id: {"client.crt": "c", "client.key": "k"}
    return manager
