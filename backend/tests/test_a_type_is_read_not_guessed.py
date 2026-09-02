"""A cluster's hypervisor family must be read from a trusted payload, never guessed.

Registration probed `/1.0` without a certificate and stored whatever came back as the cluster
type. LXD answers an untrusted caller with api_extensions and nothing else: no `environment`,
so no `server`, no `server_name`, no `server_clustered`. Every MicroCloud signal lives in that
block, so the detector always fell through to its "LXD" default and registration persisted the
default as though it were a reading. Timeouts and non-200s took the same default.

Measured on the cluster that reported this, unauthenticated:
    auth      = untrusted
    metadata  = api_extensions, api_status, api_version, auth, auth_methods,
                client_certificate, config, public
    environment = absent

Nothing then re-derived it: the list endpoint read the stored string, and the live-detection
fallback was gated behind "a type was already stored", which registration guaranteed. The only
corrective path was Check Authentication, which was itself broken by the double-slash 404 -
see test_a_trailing_slash_endpoint_never_double_slashes. The two defects held each other up.

Second rule pinned here: `server_clustered` answers "is this clustered", not "is this
MicroCloud". The cluster that reported this returns server_clustered=true with server="lxd",
and a plain `lxc cluster enable` host does the same, so the flag cannot separate them.
"""

import unittest

from app.core.type_utils import (
    _pre_auth_type_from_payload,
    detect_cluster_type_from_metadata,
    detect_clustered_from_metadata,
    resolve_cluster_type,
    resolve_clustered,
)

# The real shape of an untrusted /1.0, captured from central2.jkt.orcastra.io.
UNTRUSTED = {
    "auth": "untrusted",
    "auth_methods": ["tls"],
    "api_status": "stable",
    "api_version": "1.0",
    "api_extensions": ["storage_zfs_remove_snapshots", "network", "clustering"],
    "public": False,
}

# The same host, once the certificate is trusted. MicroCloud reports server "lxd".
MICROCLOUD = {
    "auth": "trusted",
    "api_extensions": ["storage_zfs_remove_snapshots", "clustering"],
    "environment": {
        "server": "lxd",
        "server_clustered": True,
        "server_name": "microcloud30",
        "server_version": "5.21.6",
    },
}

# A plain LXD cluster: clustered, but nothing says MicroCloud.
LXD_CLUSTER = {
    "auth": "trusted",
    "api_extensions": ["clustering"],
    "environment": {"server": "lxd", "server_clustered": True, "server_name": "nuc2"},
}

STANDALONE = {
    "auth": "trusted",
    "api_extensions": [],
    "environment": {"server": "lxd", "server_clustered": False, "server_name": "pandora"},
}

INCUS_UNTRUSTED = {
    "auth": "untrusted",
    "api_extensions": ["dev_incus_events", "restrict_dev_incus"],
}


class _Client:
    def __init__(self, host_info):
        self.host_info = host_info


class PreAuthDetectionTests(unittest.TestCase):
    def test_an_untrusted_payload_yields_no_type(self):
        self.assertIsNone(_pre_auth_type_from_payload(UNTRUSTED))

    def test_a_payload_without_an_environment_yields_no_type(self):
        self.assertIsNone(_pre_auth_type_from_payload({"api_extensions": ["clustering"]}))

    def test_incus_is_still_identifiable_before_trust(self):
        """Incus publishes dev_incus extensions to an untrusted caller, so it needs no wait."""
        self.assertEqual(_pre_auth_type_from_payload(INCUS_UNTRUSTED), "Incus")

    def test_a_trusted_payload_is_read_normally(self):
        self.assertEqual(_pre_auth_type_from_payload(MICROCLOUD), "MicroCloud")


class ClusteredIsNotMicroCloudTests(unittest.TestCase):
    def test_a_clustered_lxd_host_is_not_relabelled_microcloud(self):
        self.assertEqual(detect_cluster_type_from_metadata(LXD_CLUSTER), "LXD")
        self.assertTrue(detect_clustered_from_metadata(LXD_CLUSTER))

    def test_microcloud_still_reads_as_microcloud(self):
        self.assertEqual(detect_cluster_type_from_metadata(MICROCLOUD), "MicroCloud")
        self.assertTrue(detect_clustered_from_metadata(MICROCLOUD))

    def test_a_standalone_host_reports_not_clustered(self):
        self.assertEqual(detect_cluster_type_from_metadata(STANDALONE), "LXD")
        self.assertIs(detect_clustered_from_metadata(STANDALONE), False)

    def test_an_unknown_clustering_state_is_none_not_false(self):
        """Absent is not the same as standalone, and the UI renders the two differently."""
        self.assertIsNone(detect_clustered_from_metadata(UNTRUSTED))


class LiveOutranksStoredTests(unittest.TestCase):
    def test_a_trusted_payload_overrides_a_stale_stored_type(self):
        self.assertEqual(resolve_cluster_type("LXD", _Client(MICROCLOUD)), "MicroCloud")

    def test_a_stored_value_is_used_when_there_is_no_client(self):
        self.assertEqual(resolve_cluster_type("microcloud", None), "MicroCloud")

    def test_an_untrusted_client_does_not_override(self):
        self.assertEqual(resolve_cluster_type("Incus", _Client(UNTRUSTED)), "Incus")

    def test_nothing_known_reports_nothing_rather_than_lxd(self):
        self.assertIsNone(resolve_cluster_type(None, None))
        self.assertIsNone(resolve_cluster_type(None, _Client(UNTRUSTED)))

    def test_a_trusted_client_outranks_the_stored_clustering_state(self):
        """Same two-source contract as the family: live wins, stored answers when nobody can."""
        self.assertTrue(resolve_clustered(None, _Client(MICROCLOUD)))
        self.assertIs(resolve_clustered(None, _Client(STANDALONE)), False)
        # A host that says it is standalone overrides a stale True, and vice versa.
        self.assertIs(resolve_clustered(True, _Client(STANDALONE)), False)
        self.assertTrue(resolve_clustered(False, _Client(MICROCLOUD)))

    def test_the_stored_clustering_state_answers_when_there_is_no_client(self):
        """An offline host that proved itself clustered must not read the same as one that
        never proved anything. Both used to answer None, which is what let the members picker
        offer a cluster pending trust."""
        self.assertTrue(resolve_clustered(True, None))
        self.assertIs(resolve_clustered(False, None), False)
        self.assertTrue(resolve_clustered(True, _Client(UNTRUSTED)))

    def test_nothing_stored_and_nothing_trusted_reports_unknown(self):
        self.assertIsNone(resolve_clustered(None, None))
        self.assertIsNone(resolve_clustered(None, _Client(UNTRUSTED)))
        # Anything that is not a bool is not a reading. A legacy record holding a string must
        # not be coerced into a claim.
        self.assertIsNone(resolve_clustered("true", None))

    def test_resolving_costs_no_request(self):
        """host_info is pylxd's cached body; resolving must not reach for the network."""

        class _Exploding:
            host_info = MICROCLOUD

            def __getattr__(self, name):
                raise AssertionError(f"resolve must not touch client.{name}")

        self.assertEqual(resolve_cluster_type(None, _Exploding()), "MicroCloud")


if __name__ == "__main__":
    unittest.main()
