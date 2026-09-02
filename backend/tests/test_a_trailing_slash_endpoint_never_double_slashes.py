"""An endpoint stored with a trailing slash must still reach the hypervisor.

A cluster registered as "https://host:8443/" was probed at "https://host:8443//1.0/instances".
LXD's router does not collapse the double slash: it answers 404, which is neither the 200 that
means trusted nor the 403 that means "add the certificate", so probe_connection fell through to
its catch-all and reported `Unexpected response: HTTP 404`. The operator saw "Not Authenticated"
on a cluster whose certificate was correctly trusted, while the clusters list showed the same
cluster Online, because that path connects through pylxd, which rebuilds the URL from its parts.

Measured against the live cluster that reported it:
    200  https://host/1.0
    403  https://host/1.0/instances
    404  https://host//1.0/instances

These pin the join, not the transport: every URL built onto a stored endpoint is asserted to
carry exactly one slash before `1.0`.
"""

import unittest
from unittest.mock import patch

from app.core.url_safety import normalize_endpoint
from app.services.lxd_cert_manager import LXDCertificateManager


SPELLINGS = [
    "https://host:8443",
    "https://host:8443/",
    "https://host:8443///",
    "  https://host:8443/  ",
]


class _Response:
    status_code = 403


class _Vault:
    def __init__(self, endpoint):
        self._endpoint = endpoint

    def vault_read(self, path):
        if path.endswith("/metadata"):
            return {"endpoint": self._endpoint, "display_name": "host", "type": "LXD"}
        return {"client.crt": "crt", "client.key": "key"}


class NormalizeEndpointTests(unittest.TestCase):
    def test_every_spelling_collapses_to_one_form(self):
        for spelling in SPELLINGS:
            self.assertEqual(normalize_endpoint(spelling), "https://host:8443")

    def test_a_path_component_is_preserved(self):
        # Only trailing slashes go. A reverse-proxied LXD at a sub-path stays addressable.
        self.assertEqual(normalize_endpoint("https://host/lxd/"), "https://host/lxd")

    def test_none_and_empty_do_not_raise(self):
        self.assertEqual(normalize_endpoint(None), "")
        self.assertEqual(normalize_endpoint("   "), "")


class ProbeUrlTests(unittest.TestCase):
    """probe_connection is the call behind the Check Authentication button."""

    def _probe_url_for(self, stored_endpoint):
        manager = LXDCertificateManager.__new__(LXDCertificateManager)
        manager.vault = _Vault(stored_endpoint)
        manager.clusters_path = "secret/data/clusters"

        seen = {}

        def _capture(url, **kwargs):
            seen["url"] = url
            return _Response()

        with patch("app.services.lxd_certs.connectivity.requests.get", _capture), \
             patch("app.services.lxd_certs.connectivity.cert_files.materialize",
                   return_value=("/tmp/c.crt", "/tmp/c.key")), \
             patch("app.services.lxd_certs.connectivity.health.is_tripped", return_value=False), \
             patch("app.services.lxd_certs.connectivity.health.clear"):
            manager.probe_connection("central2", use_breaker=False)
        return seen["url"]

    def test_no_spelling_produces_a_double_slash(self):
        for spelling in SPELLINGS:
            url = self._probe_url_for(spelling)
            self.assertNotIn("//1.0", url, f"double slash built from {spelling!r}")
            self.assertEqual(url, "https://host:8443/1.0/instances", f"from {spelling!r}")

    def test_a_trailing_slash_reads_as_pending_trust_not_a_protocol_error(self):
        """403 is the honest answer for an untrusted certificate, and it must survive."""
        manager = LXDCertificateManager.__new__(LXDCertificateManager)
        manager.vault = _Vault("https://host:8443/")
        manager.clusters_path = "secret/data/clusters"

        with patch("app.services.lxd_certs.connectivity.requests.get",
                   lambda url, **kw: _Response()), \
             patch("app.services.lxd_certs.connectivity.cert_files.materialize",
                   return_value=("/tmp/c.crt", "/tmp/c.key")), \
             patch("app.services.lxd_certs.connectivity.health.is_tripped", return_value=False), \
             patch("app.services.lxd_certs.connectivity.health.clear"):
            probe = manager.probe_connection("central2", use_breaker=False)

        self.assertTrue(probe.reachable)
        self.assertFalse(probe.trusted)
        self.assertNotIn("404", probe.message)
        self.assertIn("not trusted", probe.message.lower())


if __name__ == "__main__":
    unittest.main()
