"""Offline tests for the pure closed B2 proposal and evidence boundaries."""

from __future__ import annotations

import builtins
import copy
import hashlib
import json
import os
import socket
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import phase_b_controller_plan as plan
from phase_b_idp_bootstrap import expected_idp_spec, validate_proposal
from phase_b_vault_bootstrap import ownership_marker, validate_plan, vault_contract

PHASE = "789018b9-bc4d-4cba-824f-3fcba5d33e12"
PG_DRAFT = "1f717582d99c5f3d8fc5a817326310c9d2614036450ac22f383f44915b80eb45"
SOURCE_TREE = "52a430cfa80d864caf7228075e9201abe748efb9eb9b9e8c36049a775c54974d"
SOURCE_ARCHIVE = "3774d66392fca0542c2e462e5b8eb2e1295e872ba6a2944a705da91436f216f3"


def resign(proposal):
    result = copy.deepcopy(proposal)
    result.pop("proposal_digest", None)
    result["proposal_digest"] = plan.canonical_digest(result)
    return result


def synthetic_proofs(pins_bytes, *, full=False):
    """Synthetic attestations test schemas; they do not assert actual B1/PG success."""
    raw = json.loads(pins_bytes)["images"]
    node = raw[plan.NODE_COMPONENT]
    selection = {"component": plan.NODE_COMPONENT, "owner_decision_sha256": "1" * 64,
                 **{field: node[field] for field in ("index_digest", "manifest_digest", "config_digest")}}
    source = {"source_tree_sha256": SOURCE_TREE, "archive_sha256": SOURCE_ARCHIVE,
              "archive_filename": "controller-source.tar", "archive_size": 10240, "file_count": 358, "public_only_verified": True}
    images = plan.public_images(pins_bytes, selection)
    archives = {component: {"archive_sha256": "2" * 64, "archive_filename": component + "-docker-load.tar.gz",
                            "archive_size": 1024, "archive_verified": True, "expected_image_id": image["config_digest"],
                            "origin_manifest_digest": image["manifest_digest"]} for component, image in images.items()}
    packages = {"source_tree_sha256": SOURCE_TREE, "node_config_digest": node["config_digest"],
                "closure_sha256": "3" * 64, "wheel_archive_sha256": "4" * 64,
                "wheel_requirements_sha256": "5" * 64, "npm_tarballs_archive_sha256": "6" * 64,
                "apk_archive_sha256": "7" * 64, "issues": []}
    result = {"node_selection": selection, "source": source, "images": archives, "packages": packages}
    if full:
        result["b1_success"] = {"phase": "B1", "status": "runtime_verified", "phase_b_id": plan.B1_ID,
                                "vm_run_id": plan.VM_RUN_ID, "proposal_digest": plan.B1_DIGEST,
                                "helper_sha256": plan.B1_HELPER_SHA256, "result_sha256": "d" * 64,
                                "no_nic": True, "socket_protected": True, "systemd_units_verified": True,
                                "cgroup_limits_verified": True, "memory_max_bytes": 14 * 1024 ** 3,
                                "cpu_quota_ratio": 3, "tasks_max": 8192}
        result["pg_success"] = {"phase": "B-PG", "status": "journal_and_migration_verified", "gate_id": plan.PG_GATE_ID,
                                "proposal_digest": PG_DRAFT, "result_sha256": "e" * 64, "target": plan.target(),
                                "labels": {"orcastra.qa.lab": plan.LAB, "orcastra.qa.vm_run": plan.VM_RUN_ID,
                                           "orcastra.qa.b1": plan.B1_ID, "orcastra.qa.pg_gate": plan.PG_GATE_ID},
                                "resource_ids": {"network": "f" * 64, "postgres": "0" * 64, "runner": "1" * 64},
                                "no_outer_nic": True, "container_kernel_limits_verified": True,
                                "postgres_running_verified": True, "runner_completed_verified": True}
        result["executor_review"] = {"executor_sha256": "2" * 64, "vault_helper_sha256": "3" * 64,
                                     "idp_helper_sha256": plan.IDP_HELPER_SHA256, "review_sha256": "5" * 64, "full_executor_reviewed": True}
        result["outer_transport_review"] = {"helper_sha256": "6" * 64, "review_sha256": "7" * 64,
                                            "cluster_alias": "nuc2", "project": "default", "vm_name": plan.LAB,
                                            "vm_run_id": plan.VM_RUN_ID, "vsock_only": True, "no_environment_discovery": True, "no_retries": True}
    return result


class PurePlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # note: this explicit frozen file contains only reviewed public metadata.
        cls.pins_bytes = (Path(__file__).parent / "phase_b_public_pins.json").read_bytes()

    def setUp(self):
        connect = patch.object(socket.socket, "connect", side_effect=AssertionError("Offline test attempted a network connection"))
        connect.start()
        self.addCleanup(connect.stop)

    def render(self, *, proofs=None, phase=PHASE, expected_pg_digest=PG_DRAFT):
        return plan.render_b2(phase_b_id=phase, public_pins_bytes=self.pins_bytes,
                              expected_pg_digest=expected_pg_digest, proofs=proofs)

    def test_draft_has_exact_target_phase_digest_and_no_actual_proof_claims(self):
        result = self.render()
        self.assertEqual(result["phase"], "B2")
        self.assertEqual(result["target"], plan.target())
        self.assertEqual(result["b1_readback"], {"proposal_digest": plan.B1_DIGEST, "helper_sha256": plan.B1_HELPER_SHA256})
        self.assertEqual(result["outer_binding"]["cluster_alias"], "nuc2")
        self.assertEqual(result["outer_binding"]["profiles"], [])
        self.assertEqual(result["outer_binding"]["outer_nic_count"], 0)
        self.assertEqual(set(result["outer_binding"]["expanded_devices"]), {"root"})
        self.assertFalse(result["ready_for_guest_execution"])
        self.assertTrue(all(value is None for value in result["proofs"].values()))
        self.assertIsNone(result["resources"]["retained_pg"])
        self.assertEqual(result["pg_planning_provenance"]["expected_proposal_digest"], PG_DRAFT)
        self.assertIn("draft digest is not execution approval", result["pg_planning_provenance"]["authority"])
        core = dict(result)
        recorded = core.pop("proposal_digest")
        self.assertEqual(recorded, plan.canonical_digest(core))
        self.assertEqual(recorded, hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest())
        self.assertEqual(plan.validate_manifest(result, self.pins_bytes), result)

    def test_current_pg_default_is_planning_provenance_and_never_actual_success(self):
        result = plan.render_b2(phase_b_id=PHASE, public_pins_bytes=self.pins_bytes)
        self.assertEqual(result["pg_planning_provenance"]["gate_id"], "e2a34c63-1f6f-4e6e-8c61-935cd5ec1789")
        self.assertEqual(result["pg_planning_provenance"]["expected_proposal_digest"], PG_DRAFT)
        self.assertEqual(result["pg_planning_provenance"]["helper_sha256"], "dd10d226cc3e38b1b6033919a80a57f009a9cb97541047092ac8cdcd0f53492a")
        self.assertIsNone(result["proofs"]["pg_success"])
        self.assertIsNone(result["resources"]["retained_pg"])
        self.assertFalse(result["ready_for_guest_execution"])
        self.assertTrue(any("Actual successful PostgreSQL" in reason for reason in result["remaining_gates"]))
        self.assertIn("Planning provenance only", result["pg_planning_provenance"]["authority"])
        self.assertEqual(plan.validate_manifest(result, self.pins_bytes), result)

    def test_obsolete_pg_gate_digest_and_helper_cannot_be_rebound_after_rehash(self):
        for gate in ("35c4d18f-cedf-4483-89f9-aa364038e3a2", "9377d5a2-235f-4065-86ca-7de5a685a00e",
                     "cb8d92d8-3888-4f32-a79f-c887da6a7102"):
            proof = synthetic_proofs(self.pins_bytes, full=True)
            proof["pg_success"]["gate_id"] = gate
            proof["pg_success"]["labels"]["orcastra.qa.pg_gate"] = gate
            with self.subTest(gate=gate), self.assertRaises(ValueError):
                self.render(proofs=proof)
        for digest in ("78ad32550167cf916d47443ab1c0c23abfa53f4b3794c5af136983c6456024a3",
                       "0dcd5b51476f07eb4bf0d747a11f39c89698f13025ac5258c0a7ee29af188e1b", "1" * 64):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                self.render(expected_pg_digest=digest)
        for helper in ("4db12be76576f786e1170a68953d1b5285fb46f9a6b4b59064a5a42661476e87",
                       "909200b4ea1921fde1913ca84d65c3444fa6b5d923cdab721d2402275e875d84"):
            candidate = self.render()
            candidate["pg_planning_provenance"]["helper_sha256"] = helper
            with self.subTest(helper=helper), self.assertRaises(ValueError):
                plan.validate_manifest(resign(candidate), self.pins_bytes)

    def test_current_pg_requires_every_actual_success_predicate(self):
        for field in ("no_outer_nic", "container_kernel_limits_verified", "postgres_running_verified", "runner_completed_verified"):
            proofs = synthetic_proofs(self.pins_bytes, full=True)
            proofs["pg_success"][field] = False
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.render(proofs=proofs)
        proofs = synthetic_proofs(self.pins_bytes, full=True)
        proofs["pg_success"]["status"] = "stopped"
        with self.assertRaises(ValueError):
            self.render(proofs=proofs)

    def test_all_material_missing_evidence_is_named_without_fabrication(self):
        result = self.render(expected_pg_digest=None)
        reasons = " ".join(result["remaining_gates"])
        for phrase in ("Node runtime selection", "source tree", "Docker archive", "wheel, npm, and APK",
                       "revised B1", "PostgreSQL", "guest executor", "outer agent transport", "owner execution approval"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, reasons)
        self.assertNotIn("installed", reasons)
        self.assertNotIn("build proof", reasons)
        self.assertNotIn("HTTP listen", reasons)
        self.assertIsNone(result["pg_planning_provenance"]["expected_proposal_digest"])
        self.assertNotIn(plan.NODE_COMPONENT, result["images"])
        self.assertEqual(set(result["images"]), set(plan.IMAGE_REPOSITORIES))

    def test_helper_contracts_and_marker_are_exact_without_self_reference(self):
        result = self.render()
        self.assertEqual(result["vault_bootstrap"], vault_contract(plan.VM_RUN_ID, PHASE))
        self.assertEqual(result["idp_bootstrap"], expected_idp_spec())
        self.assertEqual(result["bootstrap_helper_pins"]["idp_helper_sha256"], plan.IDP_HELPER_SHA256)
        marker = plan.phase_marker(result, self.pins_bytes)
        self.assertEqual(marker, ownership_marker(plan.VM_RUN_ID, PHASE, result["proposal_digest"]))
        self.assertEqual(set(marker), {"lab", "vm_run_id", "phase_b_id", "proposal_digest"})
        self.assertNotIn("ownership_marker_content", result)
        self.assertEqual(result["private_paths"]["phase_root"], f"{plan.ROOT}/state/{PHASE}")
        self.assertEqual(result["private_paths"]["directory_mode"], "0700")
        self.assertEqual(result["private_paths"]["file_mode"], "0600")
        with self.assertRaises(ValueError):
            validate_proposal(result, result["proposal_digest"], "Synthetic draft test")
        with self.assertRaises(ValueError):
            validate_plan(result, result["proposal_digest"], "Synthetic draft test")

    def test_historical_partial_b1_evidence_is_not_current_runtime_success(self):
        proofs = synthetic_proofs(self.pins_bytes, full=True)
        proofs["b1_success"]["phase_b_id"] = "cdf24503-772e-4ad0-a194-04e965c6e44d"
        proofs["b1_success"]["proposal_digest"] = "c70f6bbeb9de8c4fc97394be97e0ac408badcdd3743b6ce28f7d8dcd75a3a2ba"
        with self.assertRaises(ValueError):
            self.render(proofs=proofs)

    def test_render_and_validation_do_no_io_network_subprocess_or_environment_discovery(self):
        with patch.object(builtins, "open", side_effect=AssertionError("Unexpected file read")), \
                patch.object(os, "open", side_effect=AssertionError("Unexpected descriptor read")), \
                patch.object(subprocess, "run", side_effect=AssertionError("Unexpected subprocess")), \
                patch.object(os, "getenv", side_effect=AssertionError("Unexpected environment discovery")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("Unexpected implicit public read")):
            result = self.render()
            plan.validate_manifest(result, self.pins_bytes)
            plan.phase_marker(result, self.pins_bytes)

    def test_canonical_fresh_uuid_required_and_existing_owners_cannot_be_reused(self):
        for value in (plan.VM_RUN_ID, plan.B1_ID, plan.PG_GATE_ID, PHASE.upper(), "../escape", str(uuid.uuid1()), "", False):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                self.render(phase=value)
        other = self.render(phase="1983b859-9da4-4b9c-9ecf-fbaec8fb5806")
        self.assertNotEqual(other["proposal_digest"], self.render()["proposal_digest"])
        self.assertNotEqual(other["resources"]["network"]["name"], self.render()["resources"]["network"]["name"])

    def test_wrong_target_hash_or_ownership_labels_rejected_even_after_rehash(self):
        result = self.render()
        changes = (
            lambda p: p["target"].update(project="other"),
            lambda p: p["target"].update(vm_run_id="1983b859-9da4-4b9c-9ecf-fbaec8fb5806"),
            lambda p: p["target"].update(outer_nic_count=False),
            lambda p: p["outer_binding"].update(cluster_alias="nuc1"),
            lambda p: p["outer_binding"]["expanded_devices"].update(eth0={"type": "nic"}),
            lambda p: p["b1_readback"].update(helper_sha256="0" * 64),
            lambda p: p["b1_readback"].update(proposal_digest="0" * 64),
            lambda p: p["resources"]["containers"]["backend"]["labels"].update({"orcastra.qa.phase_b": plan.B1_ID}),
            lambda p: p["resources"]["containers"]["vault"]["labels"].update({"orcastra.qa.role": "admin"}),
        )
        for change in changes:
            candidate = copy.deepcopy(result)
            change(candidate)
            with self.subTest(change=change), self.assertRaises(ValueError):
                plan.validate_manifest(resign(candidate), self.pins_bytes)
        with self.assertRaises(ValueError):
            plan.validate_manifest({**result, "proposal_digest": "0" * 64}, self.pins_bytes)

    def test_role_escalation_or_helper_contract_changes_rejected(self):
        result = self.render()
        changes = (
            lambda p: p["idp_bootstrap"]["groups"][0].update(is_superuser=True),
            lambda p: p["idp_bootstrap"]["groups"][0]["roles"].append("admin"),
            lambda p: p["idp_bootstrap"]["backend_service"]["global_permissions"].append("change_user"),
            lambda p: p["vault_bootstrap"]["role"].update(allow_any_name=True),
            lambda p: p["vault_bootstrap"]["backend_token"].update(no_default_policy=False),
        )
        for change in changes:
            candidate = copy.deepcopy(result)
            change(candidate)
            with self.subTest(change=change), self.assertRaises(ValueError):
                plan.validate_manifest(resign(candidate), self.pins_bytes)

    def test_fixed_memory_budget_accounts_all_eight_services_pg_build_and_reserve(self):
        result = self.render()
        budget = result["resource_budget"]
        self.assertEqual(len(budget["service_limits"]), 8)
        service_memory = sum(item["memory_mib"] for item in budget["service_limits"].values())
        self.assertEqual(service_memory, 5632)
        self.assertEqual(service_memory + budget["retained_pg_limits"]["total_memory_mib"]
                         + budget["single_build_worker"]["memory_mib"] + budget["slice_memory_reserve_mib"], budget["docker_slice_memory_mib"])
        self.assertEqual(budget["docker_slice_memory_mib"] + budget["os_browser_budget_mib"] + budget["outer_memory_reserve_mib"], budget["vm_memory_mib"])
        total_pids = sum(item["pids"] for item in budget["service_limits"].values()) + budget["retained_pg_limits"]["total_pids"] + budget["single_build_worker"]["pids"]
        self.assertLessEqual(total_pids, budget["docker_slice_tasks"])
        self.assertEqual(budget["single_build_worker"]["count"], 1)
        for service, spec in result["resources"]["containers"].items():
            self.assertEqual(spec["limits"], budget["service_limits"][service])
            self.assertTrue(all(type(value) is int and value > 0 for value in spec["limits"].values()))
        for field, value in (("docker_slice_memory_mib", 15 * 1024), ("slice_memory_reserve_mib", 0), ("vm_memory_mib", 32 * 1024)):
            candidate = copy.deepcopy(result)
            candidate["resource_budget"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                plan.validate_manifest(resign(candidate), self.pins_bytes)

    def test_new_ids_internal_network_loopback_ports_and_socket_exclusions(self):
        result = self.render()
        resources = result["resources"]
        names = [item["name"] for item in resources["containers"].values()] + [item["name"] for item in resources["volumes"].values()] + [resources["network"]["name"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(resources["network"]["internal"])
        self.assertEqual(resources["network"]["external_connections"], [])
        self.assertTrue(all(item["created_id"] is None for item in resources["containers"].values()))
        for item in resources["containers"].values():
            self.assertEqual(item["labels"], plan.labels(PHASE))
            self.assertEqual(item["socket_mounts"], [])
            self.assertEqual(item["restart"], "no")
            self.assertEqual(item["pull_policy"], "never")
            self.assertTrue(all(port.startswith("127.0.0.1:") for port in item["published_ports"]))
        for role in ("authentik-server", "authentik-worker"):
            self.assertEqual(resources["containers"][role]["required_public_environment"],
                             {"AUTHENTIK_LISTEN__HTTP": "0.0.0.0:32090", "AUTHENTIK_LISTEN__HTTPS": ""})
        self.assertEqual(resources["containers"]["authentik-server"]["published_ports"], ["127.0.0.1:32090:32090"])
        self.assertIn("0.0.0.0:32090", " ".join(result["runtime_acceptance"]["gates"]["authentik_runtime"]["assertions"]))
        self.assertEqual(result["execution_policy"]["socket_exclusions"], list(plan.SOCKET_EXCLUSIONS))
        self.assertFalse(result["execution_policy"]["install_b1_again"])
        for item in resources["volumes"].values():
            self.assertTrue(item["private_path"].startswith(f"{plan.ROOT}/state/{PHASE}/"))
            self.assertEqual(item["parent_mode"], "0700")
        candidate = copy.deepcopy(result)
        candidate["resources"]["containers"]["frontend"]["name"] = candidate["resources"]["containers"]["backend"]["name"]
        with self.assertRaises(ValueError):
            plan.validate_manifest(resign(candidate), self.pins_bytes)
        candidate = copy.deepcopy(result)
        candidate["private_paths"]["uploads"] += "/../../outside"
        with self.assertRaises(ValueError):
            plan.validate_manifest(resign(candidate), self.pins_bytes)
        for role in ("authentik-server", "authentik-worker"):
            candidate = copy.deepcopy(result)
            candidate["resources"]["containers"][role]["required_public_environment"]["AUTHENTIK_LISTEN__HTTP"] = "0.0.0.0:9000"
            with self.subTest(role=role), self.assertRaises(ValueError):
                plan.validate_manifest(resign(candidate), self.pins_bytes)
        candidate = copy.deepcopy(result)
        candidate["resources"]["containers"]["authentik-server"]["published_ports"] = ["127.0.0.1:32090:9000"]
        with self.assertRaises(ValueError):
            plan.validate_manifest(resign(candidate), self.pins_bytes)

    def test_no_secret_families_or_raw_values_are_accepted(self):
        for secret in ("password", "token", "credentials", "client_secret", "certificate", "private_key", "environment", "approval"):
            with self.subTest(secret=secret), self.assertRaises(ValueError) as caught:
                self.render(proofs={secret: "sensitive-value-never-echo"})
            self.assertNotIn("sensitive-value-never-echo", str(caught.exception))
        proofs = synthetic_proofs(self.pins_bytes)
        for family in ("source", "packages"):
            candidate = copy.deepcopy(proofs)
            candidate[family]["password"] = "sensitive-value-never-echo"
            with self.subTest(family=family), self.assertRaises(ValueError) as caught:
                self.render(proofs=candidate)
            self.assertNotIn("sensitive-value-never-echo", str(caught.exception))
        result = self.render()
        candidate = {**result, "credentials": "sensitive-value-never-echo"}
        with self.assertRaises(ValueError):
            plan.validate_manifest(resign(candidate), self.pins_bytes)

    def test_all_synthetic_evidence_still_cannot_grant_execution_or_approval(self):
        proofs = synthetic_proofs(self.pins_bytes, full=True)
        result = self.render(proofs=proofs)
        self.assertFalse(result["ready_for_guest_execution"])
        self.assertEqual(set(result["proofs"]), {"source", "images", "packages", "node_selection", "b1_success",
                                               "pg_success", "executor_review", "outer_transport_review"})
        self.assertTrue(all(value is not None for value in result["proofs"].values()))
        self.assertEqual(len(result["remaining_gates"]), 1)
        self.assertIn("new exact owner execution approval", result["remaining_gates"][-1])
        self.assertEqual(result["runtime_acceptance"]["status"], "pending_execution")
        self.assertEqual(plan.validate_manifest(result, self.pins_bytes), result)
        promoted = {**result, "ready_for_guest_execution": True, "remaining_gates": []}
        with self.assertRaises(ValueError):
            plan.validate_manifest(resign(promoted), self.pins_bytes)
        with self.assertRaises(TypeError):
            plan.render_b2(phase_b_id=PHASE, public_pins_bytes=self.pins_bytes, approved=True)

    def test_synthetic_proof_references_and_retained_pg_ids_are_bound_without_adoption(self):
        proofs = synthetic_proofs(self.pins_bytes, full=True)
        result = self.render(proofs=proofs)
        self.assertEqual(result["proofs"]["source"]["source_tree_sha256"], SOURCE_TREE)
        self.assertEqual(result["proofs"]["source"]["archive_sha256"], SOURCE_ARCHIVE)
        retained = result["resources"]["retained_pg"]
        self.assertEqual(retained["resource_ids"], proofs["pg_success"]["resource_ids"])
        self.assertEqual(retained["labels"], proofs["pg_success"]["labels"])
        self.assertNotEqual(retained["network_name"], result["resources"]["network"]["name"])
        self.assertIn("without restart, reuse, adoption, or cleanup", retained["effect"])
        for field, value in (("gate_id", PHASE), ("proposal_digest", "a" * 64), ("runner_completed_verified", False)):
            candidate = copy.deepcopy(proofs)
            candidate["pg_success"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.render(proofs=candidate)
        for field, value in (("helper_sha256", "a" * 64), ("phase_b_id", PHASE), ("cpu_quota_ratio", True), ("memory_max_bytes", 15 * 1024 ** 3)):
            candidate = copy.deepcopy(proofs)
            candidate["b1_success"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.render(proofs=candidate)

    def test_pg_ids_targets_labels_and_numeric_boundaries_are_closed(self):
        proofs = synthetic_proofs(self.pins_bytes, full=True)
        changes = (
            lambda p: p["pg_success"]["resource_ids"].update(runner=p["pg_success"]["resource_ids"]["postgres"]),
            lambda p: p["pg_success"]["resource_ids"].update(runner="short-id"),
            lambda p: p["pg_success"]["labels"].update({"orcastra.qa.b1": PHASE}),
            lambda p: p["pg_success"]["labels"].update({"orcastra.qa.role": "admin"}),
            lambda p: p["pg_success"]["target"].update(project="other"),
            lambda p: p["pg_success"]["target"].update(outer_nic_count=False),
            lambda p: p["pg_success"].update(no_outer_nic=1),
        )
        for change in changes:
            candidate = copy.deepcopy(proofs)
            change(candidate)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.render(proofs=candidate)
        with self.assertRaises(ValueError):
            self.render(proofs=proofs, expected_pg_digest=None)

    def test_selected_node_and_archive_pins_cannot_widen_public_runtime(self):
        proofs = synthetic_proofs(self.pins_bytes)
        result = self.render(proofs=proofs)
        self.assertIn(plan.NODE_COMPONENT, result["images"])
        for field, value in (("component", "frontend_baseline"), ("manifest_digest", "sha256:" + "0" * 64), ("owner_decision_sha256", "short")):
            candidate = copy.deepcopy(proofs)
            candidate["node_selection"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.render(proofs=candidate)
        for field, value in (("archive_filename", "../escape.tar.gz"), ("archive_filename", "/tmp/escape.tar.gz"),
                             ("archive_size", True), ("archive_size", plan.MAX_ARCHIVE_BYTES + 1),
                             ("archive_verified", False), ("expected_image_id", "sha256:" + "0" * 64)):
            candidate = copy.deepcopy(proofs)
            candidate["images"]["redis"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.render(proofs=candidate)
        candidate = copy.deepcopy(proofs)
        candidate["images"]["vault"]["archive_filename"] = candidate["images"]["redis"]["archive_filename"]
        with self.assertRaises(ValueError):
            self.render(proofs=candidate)
        candidate = copy.deepcopy(proofs)
        candidate["images"]["node20"] = candidate["images"][plan.NODE_COMPONENT]
        with self.assertRaises(ValueError):
            self.render(proofs=candidate)

    def test_pre_execution_source_and_package_proofs_bind_selected_runtime(self):
        proofs = synthetic_proofs(self.pins_bytes)
        changes = (
            lambda p: p["source"].update(archive_filename="../private.tar"),
            lambda p: p["source"].update(public_only_verified=False),
            lambda p: p["source"].update(file_count=True),
            lambda p: p["packages"].update(source_tree_sha256="0" * 64),
            lambda p: p["packages"].update(node_config_digest="sha256:" + "0" * 64),
            lambda p: p["packages"].update(issues=["Missing closure"]),
        )
        for change in changes:
            candidate = copy.deepcopy(proofs)
            change(candidate)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.render(proofs=candidate)
        for required in ("source", "node_selection"):
            candidate = copy.deepcopy(proofs)
            candidate.pop(required)
            with self.subTest(required=required), self.assertRaises(ValueError):
                self.render(proofs=candidate)
        candidate = copy.deepcopy(proofs)
        candidate.pop("packages")
        result = self.render(proofs=candidate)
        self.assertIsNone(result["proofs"]["packages"])
        self.assertIn("complete wheel, npm, and APK closure", " ".join(result["remaining_gates"]))
        self.assertIsNone(result["runtime_acceptance"]["expected_bindings"]["package_closure_sha256"])
        self.assertFalse(result["ready_for_guest_execution"])

    def test_npm_tarballs_archive_hash_is_bound_without_a_materialized_cache_claim(self):
        proofs = synthetic_proofs(self.pins_bytes, full=True)
        result = self.render(proofs=proofs)
        archive_digest = proofs["packages"]["npm_tarballs_archive_sha256"]
        self.assertEqual(result["proofs"]["packages"]["npm_tarballs_archive_sha256"], archive_digest)
        self.assertNotIn("npm_cache_sha256", result["proofs"]["packages"])
        acceptance = result["runtime_acceptance"]
        self.assertEqual(acceptance["expected_bindings"]["npm_tarballs_archive_sha256"], archive_digest)
        cache_gate = acceptance["gates"]["npm_cache_materialization"]
        self.assertEqual(cache_gate["status"], "pending_execution")
        assertions = " ".join(cache_gate["assertions"])
        for phrase in ("deterministically", "fresh private npm cache", "verified npm tarballs archive",
                       "lockfile", "integrity", "without public downloads or package script execution"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, assertions)
        self.assertFalse(result["ready_for_guest_execution"])
        self.assertEqual(len(result["remaining_gates"]), 1)
        self.assertNotIn("cache", " ".join(result["remaining_gates"]))
        candidate = copy.deepcopy(proofs)
        candidate["packages"]["npm_tarballs_archive_sha256"] = "8" * 64
        changed = self.render(proofs=candidate)
        self.assertNotEqual(result["proposal_digest"], changed["proposal_digest"])
        self.assertEqual(changed["runtime_acceptance"]["expected_bindings"]["npm_tarballs_archive_sha256"], "8" * 64)
        for value in (None, False, "short", "A" * 64, "sha256:" + archive_digest):
            candidate = copy.deepcopy(proofs)
            candidate["packages"]["npm_tarballs_archive_sha256"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.render(proofs=candidate)
        candidate = copy.deepcopy(result)
        candidate["runtime_acceptance"]["expected_bindings"]["npm_tarballs_archive_sha256"] = "8" * 64
        with self.assertRaises(ValueError):
            plan.validate_manifest(resign(candidate), self.pins_bytes)

    def test_ambiguous_cache_fields_and_pre_execution_materialization_are_rejected(self):
        proofs = synthetic_proofs(self.pins_bytes, full=True)
        for old_only in (True, False):
            candidate = copy.deepcopy(proofs)
            archive_digest = candidate["packages"]["npm_tarballs_archive_sha256"]
            if old_only:
                candidate["packages"].pop("npm_tarballs_archive_sha256")
            candidate["packages"]["npm_cache_sha256"] = archive_digest
            with self.subTest(old_only=old_only), self.assertRaises(ValueError):
                self.render(proofs=candidate)
        for field, value in (("npm_cache_materialization_sha256", "8" * 64), ("npm_cache_materialized", True)):
            candidate = copy.deepcopy(proofs)
            candidate["packages"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.render(proofs=candidate)
        candidate = copy.deepcopy(proofs)
        candidate["packages"].pop("npm_tarballs_archive_sha256")
        with self.assertRaises(ValueError):
            self.render(proofs=candidate)
        candidate = copy.deepcopy(self.render(proofs=proofs))
        candidate["runtime_acceptance"]["gates"]["npm_cache_materialization"].update(status="success", cache_sha256="8" * 64)
        with self.assertRaises(ValueError):
            plan.validate_manifest(resign(candidate), self.pins_bytes)

    def test_runtime_acceptance_has_only_pending_postconditions_and_expected_bindings(self):
        proofs = synthetic_proofs(self.pins_bytes, full=True)
        result = self.render(proofs=proofs)
        acceptance = result["runtime_acceptance"]
        self.assertIn("never prerequisites or authority", acceptance["timing"])
        self.assertEqual(acceptance["status"], "pending_execution")
        self.assertTrue(acceptance["required_for_runtime_success"])
        self.assertEqual(acceptance["expected_bindings"], {
            "phase_b_id": PHASE, "source_tree_sha256": SOURCE_TREE,
            "package_closure_sha256": proofs["packages"]["closure_sha256"],
            "npm_tarballs_archive_sha256": proofs["packages"]["npm_tarballs_archive_sha256"],
            "python_config_digest": result["images"]["backend_base"]["config_digest"],
            "node_config_digest": result["images"][plan.NODE_COMPONENT]["config_digest"],
        })
        self.assertEqual(set(acceptance["gates"]), {"npm_cache_materialization", "installed_dependencies", "build", "application_databases", "authentik_runtime",
                                                    "vault", "idp_configuration", "sso_jwt", "readiness", "isolation_and_limits"})
        for gate in acceptance["gates"].values():
            self.assertEqual(set(gate), {"status", "assertions"})
            self.assertEqual(gate["status"], "pending_execution")
            self.assertTrue(gate["assertions"])
        self.assertIn("network none, pulls disabled", " ".join(acceptance["gates"]["build"]["assertions"]))
        self.assertIn("installed dependency evidence", " ".join(acceptance["gates"]["build"]["assertions"]))
        self.assertIn("fresh-lab SSO login, logout", " ".join(acceptance["gates"]["sso_jwt"]["assertions"]))
        draft = self.render()["runtime_acceptance"]
        for field in ("source_tree_sha256", "package_closure_sha256", "npm_tarballs_archive_sha256", "node_config_digest"):
            self.assertIsNone(draft["expected_bindings"][field])
        self.assertTrue(all(gate["status"] == "pending_execution" for gate in draft["gates"].values()))
        changes = (
            lambda p: p["runtime_acceptance"].update(status="success"),
            lambda p: p["runtime_acceptance"]["gates"]["build"].update(status="success", image_id="sha256:" + "8" * 64),
            lambda p: p["runtime_acceptance"]["gates"].pop("installed_dependencies"),
            lambda p: p["runtime_acceptance"]["gates"]["sso_jwt"].update(assertions=[]),
            lambda p: p["runtime_acceptance"]["expected_bindings"].update(phase_b_id=plan.B1_ID),
            lambda p: p["runtime_acceptance"]["expected_bindings"].update(package_closure_sha256="0" * 64),
            lambda p: p["runtime_acceptance"]["expected_bindings"].update(node_config_digest="sha256:" + "0" * 64),
        )
        for change in changes:
            candidate = copy.deepcopy(result)
            change(candidate)
            with self.subTest(change=change), self.assertRaises(ValueError):
                plan.validate_manifest(resign(candidate), self.pins_bytes)

    def test_runtime_effects_cannot_be_submitted_as_pre_execution_proofs(self):
        proofs = synthetic_proofs(self.pins_bytes, full=True)
        for family in ("npm_cache_materialization", "installed_dependencies", "build", "vault", "idp_configuration", "sso_jwt", "readiness", "runtime_acceptance"):
            for effect in (None, {"phase_b_id": PHASE, "status": "success", "evidence_sha256": "8" * 64},
                           {"token": "sensitive-value-never-echo"}):
                candidate = {**proofs, family: effect}
                with self.subTest(family=family, effect=effect), self.assertRaises(ValueError) as caught:
                    self.render(proofs=candidate)
                self.assertNotIn("sensitive-value-never-echo", str(caught.exception))

    def test_reviews_digest_bounds_and_input_immutability(self):
        proofs = synthetic_proofs(self.pins_bytes, full=True)
        before = copy.deepcopy(proofs)
        result = self.render(proofs=proofs)
        self.assertEqual(proofs, before)
        result["proofs"]["source"]["file_count"] = 1
        self.assertEqual(proofs, before)
        for field, value in (("cluster_alias", "nuc1"), ("vm_run_id", PHASE), ("vsock_only", False), ("no_retries", 1)):
            candidate = copy.deepcopy(proofs)
            candidate["outer_transport_review"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.render(proofs=candidate)
        candidate = copy.deepcopy(proofs)
        candidate["executor_review"]["full_executor_reviewed"] = False
        with self.assertRaises(ValueError):
            self.render(proofs=candidate)
        candidate = copy.deepcopy(proofs)
        candidate["executor_review"]["idp_helper_sha256"] = "4" * 64
        with self.assertRaises(ValueError):
            self.render(proofs=candidate)
        for value in ("0" * 63, "A" * 64, False):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.render(expected_pg_digest=value)
        with self.assertRaises(ValueError):
            plan.canonical_digest({"value": float("nan")})
        with self.assertRaises(ValueError):
            plan.canonical_digest({"public": "x" * (plan.MAX_PROPOSAL_BYTES + 1)})
        with self.assertRaises(ValueError):
            plan.public_images(self.pins_bytes + b" ")
        with self.assertRaises(TypeError):
            plan.public_images(self.pins_bytes.decode())
        with self.assertRaises(ValueError):
            plan._strict_json(b'{"duplicated":1,"duplicated":2}')


if __name__ == "__main__":
    unittest.main()
