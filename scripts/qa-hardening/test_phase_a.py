"""Exercise authority and partial-failure boundaries without any live API call."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import outer_plan
import phase_a

RUN_ID = "a232d742-ed88-46e2-8b59-969428a13588"
IMAGE = json.loads(Path(__file__).with_name("ubuntu-image-provenance.json").read_text())["fingerprint"]


class FakeTransport:
    selected_cluster_alias = "approved-qa-registry"

    def __init__(self, plan):
        body = copy.deepcopy(plan["requests"][0]["body"])
        self.vm = {key: body[key] for key in ("name", "architecture", "type", "profiles", "devices", "config")}
        self.vm["config"]["volatile.base_image"] = IMAGE
        self.vm["expanded_devices"] = copy.deepcopy(self.vm["devices"])
        self.vm["expanded_config"] = copy.deepcopy(self.vm["config"])
        self.server_name = "approved-qa-daemon"
        self.free_memory = 34 * phase_a.GIB
        self.free_storage = 250 * phase_a.GIB
        self.collision = False
        self.timeout = False
        self.exec_timeout = False
        self.gets = []
        self.writes = []
        self.commands = []
        self.uploads = []
        self.probe = {"lab_id": outer_plan.LAB_ID, "non_loopback_interfaces": [],
                      "nested_kvm": "available", "api_version": 12}

    def get(self, path):
        self.gets.append(path)
        if path == "/1.0":
            return {"environment": {"server_name": self.server_name, "architectures": ["x86_64"]}}
        if path == "/1.0/resources":
            return {"memory": {"total": self.free_memory, "used": 0}, "cpu": {"total": 8}}
        if path == "/1.0/storage-pools/default/resources":
            return {"space": {"total": self.free_storage, "used": 0}}
        if path == phase_a.VM_PATH:
            return copy.deepcopy(self.vm)
        raise AssertionError(f"Unexpected fixture GET: {path}")

    def exists(self, path):
        return self.collision if path == phase_a.VM_PATH else False

    def send(self, method, path, body):
        self.writes.append((method, path, copy.deepcopy(body)))
        return "/1.0/operations/a232d742-ed88-46e2-8b59-969428a13588"

    def wait(self, operation, timeout):
        if self.timeout:
            raise TimeoutError("Synthetic response loss")
        return {}

    def wait_agent(self, timeout):
        return None

    def upload_probe(self, body, *, record):
        record("guest_file_attempt", stage="probe_upload", path=f"/srv/{outer_plan.LAB_ID}/probe_nested_kvm.py")
        self.uploads.append(body)
        record("guest_file_completed", stage="probe_upload", path=f"/srv/{outer_plan.LAB_ID}/probe_nested_kvm.py")

    def execute(self, command, timeout, capture=False, *, record):
        stage = "nested_kvm_probe" if capture else "cloud_init_wait"
        record("guest_command_attempt", stage=stage, command=command)
        self.commands.append(command)
        operation = "/1.0/operations/a232d742-ed88-46e2-8b59-969428a13588"
        record("guest_command_accepted", stage=stage, operation_id=operation)
        if self.exec_timeout:
            raise TimeoutError("Synthetic accepted command timeout")
        returncode = 0 if not capture or self.probe["nested_kvm"] == "available" else 2
        record("guest_command_completed", stage=stage, operation_id=operation, returncode=returncode)
        if capture:
            return returncode, json.dumps(self.probe)
        return 0, ""


class ExecutorBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="orcastra-qa-hardening-offline-")
        self.addCleanup(self.directory.cleanup)
        self.plan = outer_plan.render_plan(IMAGE, RUN_ID, "approved-qa-registry", "approved-qa-daemon")
        self.transport = FakeTransport(self.plan)
        self.ledger = Path(self.directory.name) / f"{outer_plan.LAB_ID}-{RUN_ID}.jsonl"

    def execute(self, **overrides):
        arguments = {"approval_reference": "synthetic-offline-approval", "approved_plan_digest": outer_plan.plan_digest(self.plan),
                     "expected_server_name": "approved-qa-daemon", "ledger_path": self.ledger}
        arguments.update(overrides)
        return phase_a.execute_phase_a(self.transport, self.plan, **arguments)

    def events(self):
        return [json.loads(line) for line in self.ledger.read_text().splitlines()]

    def test_missing_approval_or_changed_digest_stops_before_any_read(self):
        for overrides in ({"approval_reference": ""}, {"approved_plan_digest": "0" * 64},
                          {"expected_server_name": "unapproved-daemon"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.execute(**overrides)
        self.assertEqual(self.transport.gets, [])
        self.assertFalse(self.ledger.exists())

    def test_registry_selection_and_image_pin_cannot_be_changed(self):
        self.transport.selected_cluster_alias = "other-registry"
        with self.assertRaises(ValueError):
            self.execute()
        self.transport.selected_cluster_alias = "approved-qa-registry"
        self.plan = outer_plan.render_plan("a" * 64, RUN_ID, "approved-qa-registry", "approved-qa-daemon")
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.transport.gets, [])

    def test_daemon_identity_memory_storage_and_collision_refuse_before_write(self):
        for attribute, value in (("server_name", "other-daemon"), ("free_memory", 29 * phase_a.GIB),
                                 ("free_storage", 231 * phase_a.GIB), ("collision", True)):
            with self.subTest(attribute=attribute):
                self.transport = FakeTransport(self.plan)
                setattr(self.transport, attribute, value)
                if self.ledger.exists():
                    self.ledger.unlink()
                with self.assertRaises(phase_a.PhaseAFailure):
                    self.execute()
                self.assertEqual(self.transport.writes, [])
                self.assertEqual(self.events()[-1]["event"], "stopped")

    def test_memory_threshold_preserves_eight_gib_for_fixed_twenty_two_gib_guest(self):
        self.transport.free_memory = 30 * phase_a.GIB
        self.assertEqual(self.execute()["nested_kvm"], "available")
        self.assertEqual(self.transport.writes[0][2]["config"]["limits.memory"], "22GiB")
        self.assertEqual(self.transport.free_memory - 22 * phase_a.GIB, 8 * phase_a.GIB)

    def test_readback_device_or_configuration_drift_never_starts_vm(self):
        for change in ("nic", "root", "expanded_config"):
            with self.subTest(change=change):
                self.transport = FakeTransport(self.plan)
                if change == "nic":
                    self.transport.vm["expanded_devices"]["eth0"] = {"type": "nic", "network": "unapproved"}
                elif change == "root":
                    self.transport.vm["expanded_devices"]["root"]["pool"] = "unapproved"
                else:
                    self.transport.vm["expanded_config"]["raw.qemu"] = "unapproved"
                if self.ledger.exists():
                    self.ledger.unlink()
                with self.assertRaises(phase_a.PhaseAFailure):
                    self.execute()
                self.assertEqual(len(self.transport.writes), 1)
                self.assertEqual(self.transport.commands, [])

    def test_ambiguous_create_is_not_retried_or_deleted(self):
        self.transport.timeout = True
        with self.assertRaises(phase_a.PhaseAFailure):
            self.execute()
        self.assertEqual(len(self.transport.writes), 1)
        self.assertEqual(self.transport.writes[0][:2], ("POST", "/1.0/instances"))
        self.assertFalse(self.events()[-1]["automatic_cleanup"])

    def test_accepted_guest_exec_timeout_keeps_stage_and_operation_without_retry(self):
        self.transport.exec_timeout = True
        with self.assertRaises(phase_a.PhaseAFailure):
            self.execute()
        self.assertEqual(len(self.transport.writes), 2)
        self.assertEqual(len(self.transport.commands), 1)
        accepted = next(event for event in self.events() if event["event"] == "guest_command_accepted")
        self.assertEqual(accepted["stage"], "cloud_init_wait")
        self.assertTrue(accepted["operation_id"].startswith("/1.0/operations/"))
        self.assertEqual(self.events()[-1]["stage"], "cloud_init_wait")
        self.assertFalse(self.events()[-1]["automatic_cleanup"])
        self.assertEqual(self.transport.uploads, [])

    def test_success_binds_ledger_to_run_and_requires_guest_proof(self):
        result = self.execute()
        self.assertEqual(result["nested_kvm"], "available")
        self.assertEqual(len(self.transport.writes), 2)
        self.assertEqual(self.transport.commands, self.plan["guest_actions"]["commands"])
        self.assertEqual(len(self.transport.uploads), 1)
        self.assertTrue(all(event["run_id"] == RUN_ID for event in self.events()))
        self.assertEqual(self.ledger.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.events()[1]["vm_absent"])
        self.assertEqual(self.events()[0]["target"], self.plan["target"])
        self.assertTrue(all(event["recorded_at"] for event in self.events()))
        events = [event["event"] for event in self.events()]
        self.assertLess(events.index("guest_file_attempt"), events.index("guest_file_completed"))
        self.assertEqual(events.count("guest_command_completed"), 2)
        self.assertNotIn("stdout", self.ledger.read_text())
        with self.assertRaises(FileExistsError):
            self.execute()

    def test_missing_kvm_is_blocked_and_preserves_owned_vm_for_review(self):
        self.transport.probe.update(nested_kvm="blocked", api_version=None)
        self.assertEqual(self.execute()["nested_kvm"], "blocked")
        self.assertEqual(len(self.transport.writes), 2)
        self.assertEqual(self.events()[-1]["nested_kvm"], "blocked")

    def test_guest_network_interface_invalidates_nested_proof(self):
        self.transport.probe["non_loopback_interfaces"] = ["eth0"]
        with self.assertRaises(phase_a.PhaseAFailure):
            self.execute()
        self.assertNotIn("guest_probe", [event["event"] for event in self.events()])


class TransportPathTests(unittest.TestCase):
    def setUp(self):
        self.transport = phase_a.PylxdTransport(SimpleNamespace(api=SimpleNamespace(_project=None)),
                                               selected_cluster_alias="approved-qa-registry")

    def test_owned_output_allows_only_default_project_query(self):
        path = f"{phase_a.VM_PATH}/logs/exec-output/exec_example.stdout"
        with patch.object(self.transport, "_node") as node:
            self.transport._output_node(f"{path}?project=default")
            node.assert_called_once_with(path)
        for invalid in (f"{path}?project=neighbor", f"{path}?project=default&project=default",
                        f"{path}#fragment", f"https://unapproved.invalid{path}",
                        "/1.0/instances/neighbor/logs/exec-output/example.stdout"):
            with self.subTest(path=invalid), self.assertRaises(ValueError):
                self.transport._output_node(invalid)

    def test_nondefault_project_is_rejected_before_calls(self):
        with self.assertRaises(ValueError):
            phase_a.PylxdTransport(SimpleNamespace(api=SimpleNamespace(_project="neighbor")),
                                   selected_cluster_alias="approved-qa-registry")

    def test_transport_exec_records_accepted_id_before_wait_timeout(self):
        events = []
        operation = "/1.0/operations/a232d742-ed88-46e2-8b59-969428a13588"
        with patch.object(self.transport, "_node") as node, patch.object(self.transport, "wait") as wait:
            node.return_value.post.return_value.json.return_value = {"operation": operation}
            wait.side_effect = TimeoutError("Synthetic accepted operation timeout")
            with self.assertRaises(TimeoutError):
                self.transport.execute(["cloud-init", "status", "--wait"], 30,
                                       record=lambda event, **details: events.append({"event": event, **details}))
            node.return_value.post.assert_called_once()
            wait.assert_called_once_with(operation, 30)
        self.assertEqual([event["event"] for event in events], ["guest_command_attempt", "guest_command_accepted"])
        self.assertEqual(events[-1]["operation_id"], operation)

    def test_transport_upload_records_synchronous_completion_without_output(self):
        events = []
        with patch.object(self.transport, "_node") as node:
            node.return_value.post.return_value.json.return_value = {"type": "sync"}
            self.transport.upload_probe(b"approved nonsecret probe",
                                        record=lambda event, **details: events.append({"event": event, **details}))
            node.return_value.post.assert_called_once()
        self.assertEqual([event["event"] for event in events], ["guest_file_attempt", "guest_file_completed"])
        self.assertNotIn("approved nonsecret probe", json.dumps(events))


if __name__ == "__main__":
    unittest.main()
