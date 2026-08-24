"""The status derivation is the one place that turns a probe into a word an operator reads.

Three call sites used to answer the question three different ways: /clusters collapsed any
failure to NOT_AUTHORIZED, /regions collapsed any failure to OFFLINE, and /monitoring invented
"pending_trust" by substring-matching the error text. This pins the single answer.
"""

import ast
import inspect
import unittest

from app.models.cluster_status import AvailabilityReason, ClusterStatus, derive_status


class DeriveStatusTests(unittest.TestCase):
    def test_connected_is_online(self):
        status, reason, _ = derive_status(connected=True, reachable=True)
        self.assertEqual(status, ClusterStatus.ONLINE)
        self.assertEqual(reason, AvailabilityReason.OK)

    def test_connected_wins_even_when_reachable_is_unknown(self):
        # A body cached before `reachable` existed still reports a working cluster correctly.
        status, _, _ = derive_status(connected=True, reachable=None)
        self.assertEqual(status, ClusterStatus.ONLINE)

    def test_answered_but_refused_is_not_authorized(self):
        status, reason, message = derive_status(connected=False, reachable=True)
        self.assertEqual(status, ClusterStatus.NOT_AUTHORIZED)
        self.assertEqual(reason, AvailabilityReason.UNTRUSTED_CERTIFICATE)
        self.assertIn("certificate", message.lower())

    def test_never_answered_is_offline(self):
        status, reason, _ = derive_status(connected=False, reachable=False)
        self.assertEqual(status, ClusterStatus.OFFLINE)
        self.assertEqual(reason, AvailabilityReason.UNREACHABLE)

    def test_a_dead_cluster_is_never_labelled_pending_trust(self):
        """The headline defect. An unreachable host is not a certificate problem."""
        status, _, _ = derive_status(
            connected=False,
            reachable=False,
            error="Cannot connect to LXD endpoint. Check if server is running.",
        )
        self.assertEqual(status, ClusterStatus.OFFLINE)
        self.assertNotEqual(status, ClusterStatus.NOT_AUTHORIZED)

    def test_no_certificate_is_pending(self):
        status, reason, _ = derive_status(
            connected=False, reachable=False, has_certificate=False
        )
        self.assertEqual(status, ClusterStatus.PENDING)
        self.assertEqual(reason, AvailabilityReason.NO_CERTIFICATE)

    def test_auth_present_without_reachable_still_means_it_answered(self):
        # `auth` is only ever populated after host_info was read, so it is a second
        # structural signal that the host answered.
        status, _, _ = derive_status(connected=False, reachable=None, auth="untrusted")
        self.assertEqual(status, ClusterStatus.NOT_AUTHORIZED)

    def test_unknown_defaults_to_offline_not_error(self):
        """A picker must not present an unconfirmed cluster as usable, and ERROR is reserved
        for a fault in us. Defaulting the other way would paint a whole fleet red on a bug."""
        status, _, _ = derive_status(connected=False, reachable=None)
        self.assertEqual(status, ClusterStatus.OFFLINE)

    def test_the_caller_error_survives_into_the_message(self):
        _, _, message = derive_status(
            connected=False, reachable=False, error="Connection timeout."
        )
        self.assertEqual(message, "Connection timeout.")

    def test_it_never_reads_the_error_text(self):
        """Structural, not behavioural. Guessing the status from a message is what produced
        "pending_trust", and a reworded backend message would silently change the UI."""
        source = inspect.getsource(derive_status)
        tree = ast.parse(source.lstrip())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {"lower", "upper", "find"}:
                self.fail(f"derive_status must not inspect message text (.{node.attr}())")
            if isinstance(node, ast.Compare) and any(
                isinstance(op, ast.In) for op in node.ops
            ):
                self.fail("derive_status must not substring-match the error text")


if __name__ == "__main__":
    unittest.main()
