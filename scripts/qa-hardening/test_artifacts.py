"""Offline checks for the lab proposal; no live fixture or credentials."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import outer_plan
import probe_nested_kvm

RUN_ID = "a232d742-ed88-46e2-8b59-969428a13588"


class OuterPlanTests(unittest.TestCase):
    def test_unpinned_alias_is_not_executable_provenance(self):
        plan = outer_plan.render_plan(None)
        self.assertFalse(plan["ready_for_execution"])
        self.assertEqual(plan["requests"][0]["body"]["source"]["alias"], "24.04")

    def test_pinned_plan_keeps_exact_owned_id_and_no_network(self):
        fingerprint = "a" * 64
        plan = outer_plan.render_plan(fingerprint, RUN_ID, "approved-qa-host", "approved-qa-daemon")
        self.assertTrue(plan["ready_for_execution"])
        vm = plan["requests"][0]["body"]
        self.assertEqual(vm["source"]["fingerprint"], fingerprint)
        self.assertNotIn("alias", vm["source"])
        self.assertEqual(vm["profiles"], [])
        self.assertEqual(vm["devices"], {
            "root": {"type": "disk", "path": "/", "pool": "default", "size": "120GiB"},
        })
        self.assertEqual(vm["config"]["limits.memory"], "22GiB")
        self.assertEqual(vm["config"]["limits.cpu"], "4")
        self.assertFalse(any(key.startswith("raw.") for key in vm["config"]))
        self.assertEqual(vm["config"]["user.qa.run"], RUN_ID)
        self.assertEqual(plan["target"], {
            "cluster_alias": "approved-qa-host", "project": "default", "server_name": "approved-qa-daemon",
        })
        self.assertIn(RUN_ID, vm["config"]["cloud-init.user-data"])
        self.assertEqual(vm["config"]["cloud-init.network-config"], "version: 2\nethernets: {}\n")
        self.assertEqual([item["path"] for item in plan["requests"]], [
            "/1.0/instances", f"/1.0/instances/{outer_plan.LAB_ID}/state",
        ])

    def test_pinned_image_without_target_and_run_is_not_ready(self):
        self.assertFalse(outer_plan.render_plan("a" * 64)["ready_for_execution"])
        self.assertFalse(outer_plan.render_plan("a" * 64, RUN_ID)["ready_for_execution"])
        self.assertFalse(outer_plan.render_plan("a" * 64, RUN_ID, "qa-registry")["ready_for_execution"])

    def test_invalid_target_or_noncanonical_run_is_refused(self):
        for invalid in ("not-a-uuid", "a232d742-ed88-16e2-8b59-969428a13588", RUN_ID.upper()):
            with self.subTest(run=invalid), self.assertRaises(ValueError):
                outer_plan.render_plan("a" * 64, invalid, "qa-host")
        with self.assertRaises(ValueError):
            outer_plan.render_plan("a" * 64, RUN_ID, "https://fleet.invalid")

    def test_invalid_fingerprint_is_refused(self):
        for invalid in ("", "a" * 63, "a" * 65, "G" * 64, "../image", "ubuntu:24.04"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                outer_plan.render_plan(invalid)

    def test_guest_commands_are_scoped_to_vsock_and_exact_run(self):
        actions = outer_plan.render_plan("a" * 64, RUN_ID, "qa-host")["guest_actions"]
        self.assertEqual(actions["commands"][0], ["cloud-init", "status", "--wait"])
        self.assertEqual(actions["commands"][1][-2:], ["--run-id", RUN_ID])
        self.assertEqual(actions["file_upload"]["mode"], "0600")
        self.assertEqual(len(actions["file_upload"]["sha256"]), 64)

    def test_cleanup_is_exact_and_does_not_delete_pool_or_image(self):
        plan = outer_plan.render_plan(None)
        paths = [request["path"] for request in plan["cleanup_preview"]]
        self.assertEqual(paths, [
            f"/1.0/instances/{outer_plan.LAB_ID}/state", f"/1.0/instances/{outer_plan.LAB_ID}",
        ])


class ProbeGuardTests(unittest.TestCase):
    def test_missing_guest_marker_refuses_before_kvm_access(self):
        with (
            patch.object(probe_nested_kvm.Path, "is_file", return_value=False),
            patch("builtins.open") as open_device,
        ):
            with self.assertRaisesRegex(RuntimeError, "outside the approved"):
                probe_nested_kvm.probe()
            open_device.assert_not_called()

    def test_successful_kvm_probe_requires_api_and_vm_ioctl(self):
        with (
            patch.object(probe_nested_kvm, "require_lab_guest"),
            patch.object(probe_nested_kvm, "guest_interfaces", return_value=[]),
            patch("builtins.open") as open_device,
            patch.object(probe_nested_kvm.fcntl, "ioctl", side_effect=[12, 11]) as ioctl,
            patch.object(probe_nested_kvm.os, "close") as close_vm,
        ):
            device = open_device.return_value.__enter__.return_value
            device.fileno.return_value = 10
            self.assertEqual(probe_nested_kvm.probe()["nested_kvm"], "available")
            self.assertEqual(ioctl.call_count, 2)
            close_vm.assert_called_once_with(11)

    def test_missing_kvm_is_blocked_not_success(self):
        with (
            patch.object(probe_nested_kvm, "require_lab_guest"),
            patch.object(probe_nested_kvm, "guest_interfaces", return_value=[]),
            patch("builtins.open", side_effect=FileNotFoundError(2, "No such device")),
        ):
            outcome = probe_nested_kvm.probe()
            self.assertEqual(outcome["nested_kvm"], "blocked")
            self.assertIsNone(outcome["api_version"])

    def test_unexpected_interface_refuses_before_kvm_access(self):
        with (
            patch.object(probe_nested_kvm, "require_lab_guest"),
            patch.object(probe_nested_kvm, "guest_interfaces", return_value=["eth0"]),
            patch("builtins.open") as open_device,
        ):
            self.assertEqual(probe_nested_kvm.probe()["nested_kvm"], "blocked")
            open_device.assert_not_called()


if __name__ == "__main__":
    unittest.main()
