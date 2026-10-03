"""Hermetic contract tests for the inert real browser guest planner."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import real_browser_guest as guest


def write(path: Path, value: object, mode: int = 0o600) -> None:
    path.write_bytes(guest.canonical(value))
    path.chmod(mode)


def b2() -> dict:
    b2_id = "29772c22-1111-4222-8333-444444444444"
    labels = {"orcastra.qa.lab": guest.LAB, "orcastra.qa.vm_run": guest.VM_RUN_ID, "orcastra.qa.phase_b": b2_id}
    ids = {name: char * 64 for name, char in (("frontend", "a"), ("backend", "b"), ("authentik-server", "c"), ("network", "d"))}
    return {"schema_version": 1, "phase": "B2", "status": "controller_configured_readiness_verified", "b2_id": b2_id,
            "runtime_authority": {"authorization_reference_sha256": "1" * 64, "controller_helper_sha256": "2" * 64, "accepted_operation_id": "3" * 64, "closed": True},
            "labels": labels, "resource_ids": ids,
            "network": {"id": ids["network"], "name": guest.LAB + "-application-29772c22", "internal": True, "published_ports": [], "labels": labels}}


def payload() -> dict:
    return {"schema_version": 1, "closed": True, "control_sha256": guest.PUBLIC_CONTROL_SHA256,
            "image": {"reference": guest.IMAGE, "manifest_digest": guest.MANIFEST, "config_digest": guest.CONFIG, "archive_sha256": "e" * 64, "archive_verified": True, "config_verified": True, "diff_ids": list(guest.DIFF_IDS)},
            "npm": [{"name": name, "version": "1.51.1", "sha256": char * 64, "integrity": integrity, "verified": True}
                    for (name, integrity), char in zip(guest.NPM_INTEGRITIES.items(), "fab", strict=True)]}


class BrowserGuestTests(unittest.TestCase):
    def test_default_is_inert_without_files_or_execution(self):
        with patch.object(guest, "prepare", side_effect=AssertionError("default must not prepare")), patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(guest.main([]), 0)
        self.assertIn("inert", output.getvalue())

    def test_closed_inputs_make_only_new_uuid_private_plan(self):
        with tempfile.TemporaryDirectory() as location:
            root = Path(location)
            b2_file, payload_file = root / "b2.json", root / "payload.json"
            write(b2_file, b2()); write(payload_file, payload())
            output = root / "1b2c3d4e-1111-4222-8333-444444444444"
            helper_hash = hashlib.sha256(Path(guest.__file__).read_bytes()).hexdigest()
            result = guest.prepare(b2_proof=b2_file, payload_proof=payload_file, output=output, expected_helper_sha256=helper_hash)
            self.assertEqual(result["status"], "prepared_not_executed")
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((output / "plan.json").stat().st_mode), 0o600)
            plan = json.loads((output / "plan.json").read_bytes())
            self.assertFalse(plan["container"]["host_network"])
            self.assertEqual(plan["container"]["published_ports"], [])
            self.assertTrue(all(item["read_only"] for item in plan["container"]["mounts"] if item["role"] in {"payload", "credentials"}))
            self.assertEqual(plan["offline_npm"]["version"], "1.51.1")
            with self.assertRaises(ValueError):
                guest.prepare(b2_proof=b2_file, payload_proof=payload_file, output=output, expected_helper_sha256=helper_hash)

    def test_proofs_refuse_open_runtime_widened_network_and_changed_payload(self):
        for mutate in (
            lambda value: value["runtime_authority"].update(closed=False),
            lambda value: value["network"].update(published_ports=["127.0.0.1:9:9"]),
        ):
            candidate = b2(); mutate(candidate)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                guest.verify_b2(candidate)
        for mutate in (
            lambda value: value.update(closed=False),
            lambda value: value["npm"].pop(),
            lambda value: value["image"].update(diff_ids=[]),
        ):
            candidate = payload(); mutate(candidate)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                guest.verify_payload(candidate)

    def test_password_adapter_returns_only_six_passwords_and_refuses_loose_file(self):
        with tempfile.TemporaryDirectory() as location:
            path = Path(location) / "bootstrap.json"
            write(path, {"client_secret": "must-not-return", "actors": {actor: {"password": "Synthetic-password-123", "password_hash": "must-not-return"} for actor in guest.ACTORS}})
            self.assertEqual(set(guest.read_six_passwords(path)), set(guest.ACTORS))
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                guest.read_six_passwords(path)

    def test_materializer_never_runs_npm_and_refuses_reused_workdir(self):
        with tempfile.TemporaryDirectory() as location:
            root = Path(location); proof = payload(); tarballs = {}
            for row in proof["npm"]:
                source = root / (row["name"].replace("/", "-") + ".tgz")
                with tarfile.open(source, "w:gz") as archive:
                    info = tarfile.TarInfo("package/index.js"); body = b"module.exports = 1\n"; info.size = len(body)
                    archive.addfile(info, io.BytesIO(body))
                row["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest(); tarballs[row["name"]] = source
            output = root / "work"
            modules = guest.materialize_npm(tarballs=tarballs, proof=proof, workdir=output)
            self.assertEqual(set(modules), set(guest.NPM_INTEGRITIES))
            with self.assertRaises(FileExistsError):
                guest.materialize_npm(tarballs=tarballs, proof=proof, workdir=output)


if __name__ == "__main__":
    unittest.main()
