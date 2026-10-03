"""Offline safety and evidence contracts for the fresh PostgreSQL lab gate."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import phase_b_pg_gate as gate
import phase_b_pg_runner as runner

VM_ID = "0f2a5b15-f5ec-464b-8785-0b4b88c3b358"
B1_ID = "0366fe77-c9d2-445c-8f79-7a923410bddd"
GATE_ID = "bf8dd7c7-f474-4bd9-9fb1-2fae9f13e04f"
PHASE_A_SHA = "384bc2030c890ea26f054ece6725a0876fd95e95a6ccfdfe53368d17e3eb863e"


class SourceBoundary(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="orcastra-pg-gate-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = {"backend/app/__init__.py", "backend/alembic/env.py", "backend/alembic.ini",
                      "backend/requirements.txt", "backend/constraints.txt", *gate.TEST_INPUTS}
        for name in self.paths:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# synthetic source\n")
        (self.root / gate.TEST_MODULE).write_text("\n".join(f"def test_case_{i}(): pass" for i in range(14)))
        self.path_patch = patch.object(gate, "git_source_paths", return_value=sorted(self.paths))
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def test_export_includes_exact_test_inputs_and_excludes_state_and_frontend(self):
        for name in ("backend/.env", "backend/certs/private.pem", "frontend/src/app/page.tsx", "backend/tests/other.py"):
            self.paths.add(name)
        self.path_patch.stop()
        with patch.object(gate, "git_source_paths", return_value=sorted(self.paths)):
            inventory = gate.source_inventory(self.root)
        paths = {entry["path"] for entry in inventory["entries"]}
        self.assertTrue(gate.TEST_INPUTS.issubset(paths))
        self.assertFalse(paths.intersection({"backend/.env", "backend/certs/private.pem", "frontend/src/app/page.tsx",
                                             "backend/tests/other.py"}))
        self.assertEqual(len(inventory["tests"]), 14)

    def test_a_symlinked_test_input_is_refused(self):
        path = self.root / "backend/tests/conftest.py"
        path.unlink()
        path.symlink_to(self.root / "backend/requirements.txt")
        with self.assertRaises(OSError):
            gate.source_inventory(self.root)

    def test_a_fifo_test_input_is_refused_without_blocking(self):
        path = self.root / "backend/tests/conftest.py"
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(ValueError):
            gate.source_inventory(self.root)

    def test_changed_case_count_requires_review(self):
        (self.root / gate.TEST_MODULE).write_text("def test_single(): pass\n")
        with self.assertRaises(ValueError):
            gate.source_inventory(self.root)

    def test_source_drift_stops_export_and_preserves_partial_artifact(self):
        inventory = gate.source_inventory(self.root)
        (self.root / "backend/app/__init__.py").write_text("changed source\n")
        destination = self.root / "source.tar"
        with self.assertRaises(ValueError):
            gate.export_source(self.root, inventory, destination)
        self.assertTrue(destination.exists())

    def test_existing_export_is_never_overwritten(self):
        inventory = gate.source_inventory(self.root)
        destination = self.root / "source.tar"
        destination.write_bytes(b"owner artifact")
        with self.assertRaises(FileExistsError):
            gate.export_source(self.root, inventory, destination)
        self.assertEqual(destination.read_bytes(), b"owner artifact")

    def test_draft_plan_separates_database_downgrade_and_cannot_execute(self):
        inventory = gate.source_inventory(self.root)
        plan = gate.render_plan(vm_run_id=VM_ID, phase_a_digest=PHASE_A_SHA, b1_id=B1_ID,
                                gate_id=GATE_ID, inventory=inventory)
        digest = plan.pop("proposal_digest")
        self.assertEqual(gate.canonical_digest(plan), digest)
        self.assertFalse(plan["ready_for_execution"])
        self.assertNotEqual(plan["databases"]["journal"], plan["databases"]["migration"])
        self.assertEqual(plan["network"]["published_ports"], [])
        self.assertTrue(plan["network"]["internal"])
        self.assertEqual(plan["agent_transfers"], {
            "path": f"/srv/{gate.LAB}/incoming/{GATE_ID}",
            "files": ["backend-source.tar", "pg-proposal.json", "phase_b_pg_guest.py", "phase_b_pg_runner.py",
                      "postgres-docker-load.tar.gz", "python-docker-load.tar.gz", "wheels.tar"],
        })
        with self.assertRaises(ValueError):
            runner.run(plan)

    def test_agent_transfer_names_cannot_collide_with_controls_or_escape_incoming(self):
        inventory = gate.source_inventory(self.root)
        for filename in ("pg-proposal.json", "../source.tar", "/source.tar"):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                gate.render_plan(vm_run_id=VM_ID, phase_a_digest=PHASE_A_SHA, b1_id=B1_ID,
                                 gate_id=GATE_ID, inventory=inventory,
                                 source_archive={"filename": filename, "size": 1, "sha256": "a" * 64})


class EvidenceBoundary(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="orcastra-pg-evidence-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.names = [f"test_case_{i}" for i in range(14)]

    def report(self, modifications=None):
        root = ET.Element("testsuites")
        suite = ET.SubElement(root, "testsuite")
        cases = [ET.SubElement(suite, "testcase", name=name, time="0.01") for name in self.names]
        if modifications:
            modifications(cases)
        path = self.root / "tests.xml"
        ET.ElementTree(root).write(path)
        return path

    def test_passing_report_exposes_only_exact_cases_counts_and_finite_duration(self):
        result = runner.junit_summary(self.report(), self.names)
        self.assertEqual(result["passed"], 14)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["test_names"], sorted(self.names))
        self.assertNotIn("password", json.dumps(result))

    def test_skips_and_failures_cannot_be_reported_as_passes(self):
        def edit(cases):
            ET.SubElement(cases[0], "skipped", message="synthetic skip")
            ET.SubElement(cases[1], "failure").text = "sensitive URL must not be copied"
        result = runner.junit_summary(self.report(edit), self.names)
        self.assertEqual(result["passed"], 12)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["failure"], 1)
        self.assertNotIn("sensitive URL", json.dumps(result))

    def test_duplicate_or_unknown_test_identity_is_refused(self):
        for replacement in (self.names[0], "test_unreviewed"):
            with self.subTest(replacement=replacement):
                path = self.report(lambda cases, replacement=replacement: cases[-1].set("name", replacement))
                with self.assertRaises(ValueError):
                    runner.junit_summary(path, self.names)

    def test_non_finite_test_duration_is_refused(self):
        for value in ("NaN", "inf", "-1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                runner.junit_summary(self.report(lambda cases, value=value: cases[0].set("time", value)), self.names)

    def test_duration_sum_cannot_overflow_into_non_finite_evidence(self):
        def edit(cases):
            for case in cases:
                case.set("time", "1e308")
        with self.assertRaises(ValueError):
            runner.junit_summary(self.report(edit), self.names)

    def test_partial_schema_or_wrong_version_is_refused(self):
        snapshot = {"version": runner.HEAD, "objects": {name: True for name in runner.OBJECTS}}
        runner.require_schema(snapshot, runner.HEAD, True)
        snapshot["objects"].pop("task_journal_seq")
        with self.assertRaises(ValueError):
            runner.require_schema(snapshot, runner.HEAD, True)
        with self.assertRaises(ValueError):
            runner.require_schema({"version": "old", "objects": {n: True for n in runner.OBJECTS}}, runner.HEAD, True)

    def test_secret_reader_rejects_public_mode_and_symlink(self):
        path = self.root / "password"
        path.write_text("fresh secret")
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            runner.regular_private(path, 256)
        path.chmod(0o600)
        digest = hashlib.sha256(runner.regular_private(path, 256)).hexdigest()
        self.assertEqual(digest, hashlib.sha256(b"fresh secret").hexdigest())
        link = self.root / "symlink"
        link.symlink_to(path)
        with self.assertRaises(OSError):
            runner.regular_private(link, 256)


class SchemaReadbackBoundary(unittest.IsolatedAsyncioTestCase):
    async def snapshot(self, *, constraint="PRIMARY KEY (id)", index="CREATE INDEX actor_scope ON task_journal (actor_id)", sequence_increment=1):
        async def fetchval(sql, *arguments):
            if "version_num" in sql:
                return runner.HEAD
            if "to_regclass" in sql:
                return arguments[0]
            if "count(*)" in sql:
                return 0
            raise AssertionError("Unexpected synthetic scalar query")

        async def fetch(sql):
            if "information_schema.columns" in sql:
                return [("task_journal", "id", "uuid", "uuid", "NO", None, None, None, None, "NO", None, "NEVER", None)]
            if "pg_constraint" in sql:
                return [("task_journal", "task_journal_pkey", "p", constraint, True, False, False)]
            if "pg_indexes" in sql:
                return [("task_journal", "actor_scope", index)]
            if "pg_sequences" in sql:
                return [("task_journal_seq", "bigint", 1, 1, 9223372036854775807, sequence_increment, False, 1)]
            raise AssertionError("Unexpected synthetic catalog query")

        connection = SimpleNamespace(fetchval=fetchval, fetch=fetch, close=AsyncMock())
        module = SimpleNamespace(connect=AsyncMock(return_value=connection))
        with patch.dict("sys.modules", {"asyncpg": module}):
            result = await runner.schema_snapshot("postgresql+asyncpg://synthetic.invalid/orcastra_journal_test")
        connection.close.assert_awaited_once()
        return result

    async def test_same_public_contract_has_stable_sanitized_digest(self):
        first, second = await self.snapshot(), await self.snapshot()
        self.assertEqual(first["schema_contract_sha256"], second["schema_contract_sha256"])
        self.assertEqual(first["journal_row_counts"], {name: 0 for name in runner.OBJECTS[:3]})
        self.assertNotIn("CREATE INDEX", json.dumps(first))

    async def test_constraint_index_and_sequence_drift_change_contract_without_column_drift(self):
        baseline = await self.snapshot()
        changed = [await self.snapshot(constraint="PRIMARY KEY (id, actor_id)"),
                   await self.snapshot(index="CREATE INDEX actor_scope ON task_journal (actor_id) WHERE operation_id IS NOT NULL"),
                   await self.snapshot(sequence_increment=2)]
        for snapshot in changed:
            with self.subTest(contract=snapshot["schema_contract_sha256"]):
                self.assertEqual(baseline["column_schema_sha256"], snapshot["column_schema_sha256"])
                self.assertNotEqual(baseline["schema_contract_sha256"], snapshot["schema_contract_sha256"])


if __name__ == "__main__":
    unittest.main()
