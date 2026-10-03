"""Hermetic tests for draft PG assembly and private regular-file loading."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import signal
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import phase_b_pg_caller as caller
import phase_b_pg_loader as loader


def descriptor(body: bytes) -> dict:
    return {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()}


def synthetic_draft() -> dict:
    return {"phase": "B-PG", "gate_id": loader.GATE_ID, "ready_for_execution": False,
            "pending": list(caller.PENDING), "target": {key: value for key, value in loader.TARGET.items() if key != "cluster_alias"},
            "b1_readback": {"proposal_digest": loader.B1_DIGEST, "helper_sha256": loader.B1_HELPER_SHA},
            "executor_sha256": loader.FROZEN["phase_b_pg_guest.py"]["sha256"],
            "runner_sha256": loader.FROZEN["phase_b_pg_runner.py"]["sha256"],
            "source": {"archive": copy.deepcopy(caller.PAYLOADS["source"]),
                       "inventory": {"source_tree_sha256": loader.SOURCE_TREE_SHA, "entries": [{} for _ in range(358)]}},
            "wheels": copy.deepcopy(caller.PAYLOADS["wheels"]),
            "images": {role: {"archive": copy.deepcopy(caller.PAYLOADS[role])} for role in ("postgres", "python")},
            "agent_transfers": {"path": "/srv/" + loader.LAB + "/incoming/" + loader.GATE_ID,
                                "files": sorted(["pg-proposal.json", "phase_b_pg_guest.py", "phase_b_pg_runner.py",
                                                 *(value["filename"] for value in caller.PAYLOADS.values())])}}


def bind_draft(document: dict) -> str:
    value = loader.canonical({key: value for key, value in document.items() if key != "proposal_digest"})
    document["proposal_digest"] = value
    return value


def outer_bytes(bodies: dict, *, mutate=None) -> bytes:
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for index, name in enumerate(sorted(bodies)):
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(bodies[name]), 0o600
            if mutate is not None:
                mutate(index, member)
            archive.addfile(member, io.BytesIO(bodies[name]) if member.isreg() else None)
    return target.getvalue()


def synthetic_bundle() -> tuple[dict, dict, bytes, dict]:
    names = {role: value["filename"] for role, value in caller.PAYLOADS.items()}
    bodies = {name: ("synthetic:" + name).encode() for name in set(loader.FROZEN) | set(names.values()) | loader.CONTROLS}
    members = {name: descriptor(body) for name, body in bodies.items()}
    frozen = {name: members[name] for name in loader.FROZEN}
    body = outer_bytes(bodies)
    manifest = {"schema_version": 1, "phase": "B-PG", "gate_id": loader.GATE_ID, "target": copy.deepcopy(loader.TARGET),
                "proposal_digest": "1" * 64, "payload_files": names, "members": members,
                "archive": {"filename": "nuc2-pg-bundle-" + loader.GATE_ID + ".tar", **descriptor(body)}}
    return manifest, bodies, body, frozen


def stopped_wrapper() -> dict:
    return {"phase": "B-PG", "gate_id": loader.GATE_ID, "vm_run_id": loader.VM_RUN, "proposal_digest": "1" * 64,
            "result": None, "resource_ids": {}, "failure_type": "TimeoutError", "stage": "target_readback",
            "ledger": [{"event": "stopped", "stage": "target_readback", "failure_type": "TimeoutError",
                        "automatic_retry": False, "automatic_cleanup": False}]}


class PrivateFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pg-loader-hermetic-")
        self.path = Path(self.temporary.name)
        self.directory = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(os.close, self.directory)

    def write(self, name: str, body: bytes, *, mode: int = 0o600):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=self.directory)
        with os.fdopen(fd, "wb") as target:
            target.write(body)
        os.chmod(self.path / name, mode)


class DraftTests(PrivateFixture):
    def test_preview_contains_concrete_closed_members_and_no_execution_authority(self):
        document = synthetic_draft()
        with patch.object(caller, "DRAFT_PROPOSAL_DIGEST", bind_draft(document)):
            result = caller.preview(document)
        self.assertIs(result["ready_for_execution"], False)
        self.assertIsNone(result["sealed_manifest"])
        self.assertIsNone(result["manifest_template"]["proposal_digest"])
        self.assertIsNone(result["manifest_template"]["archive"])
        self.assertEqual(len(result["manifest_template"]["members"]), 12)
        self.assertEqual(len(result["guest_transfer_files"]), 7)
        self.assertEqual(result["source_archive"], loader.SOURCE_ARCHIVE)
        self.assertEqual(result["target"]["cluster_alias"], "nuc2")
        self.assertEqual(result["target"]["project"], "default")
        self.assertFalse(result["actual_b1_proof_present"])
        result["target"]["project"] = "synthetic-change"
        result["manifest_template"]["members"]["phase_a.py"]["sha256"] = "f" * 64
        self.assertEqual(loader.TARGET["project"], "default")
        self.assertNotEqual(loader.FROZEN["phase_a.py"]["sha256"], "f" * 64)

    def test_preview_rejects_target_helpers_payloads_and_pending_mismatch(self):
        mutations = [lambda value: value["target"].update(project="other"),
                     lambda value: value["target"].update(vm_run_id=loader.B1_ID),
                     lambda value: value.update(gate_id=loader.VM_RUN),
                     lambda value: value.update(executor_sha256="f" * 64),
                     lambda value: value["b1_readback"].update(proposal_digest="f" * 64),
                     lambda value: value["source"]["archive"].update(filename="historical.tar"),
                     lambda value: value["wheels"].update(size=1),
                     lambda value: value["agent_transfers"]["files"].append("extra.tar"),
                     lambda value: value.update(pending=[]),
                     lambda value: value["source"]["inventory"]["entries"].pop()]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                document = synthetic_draft()
                mutate(document)
                with patch.object(caller, "DRAFT_PROPOSAL_DIGEST", bind_draft(document)), self.assertRaises(ValueError):
                    caller.preview(document)

    def test_preview_refuses_wrong_canonical_binding(self):
        document = synthetic_draft()
        bind_draft(document)
        with self.assertRaises(ValueError):
            caller.preview(document)

    def test_read_public_draft_is_bounded_no_follow_and_single_link(self):
        name = "draft.json"
        self.write(name, b'{"ready_for_execution":false}', mode=0o644)
        with patch.object(caller, "DRAFT_PATH", self.path / name):
            self.assertFalse(caller.read_draft()["ready_for_execution"])
            os.link(self.path / name, self.path / "linked.json")
            with self.assertRaises(PermissionError):
                caller.read_draft()
        os.symlink(name, self.path / "symlink.json")
        with patch.object(caller, "DRAFT_PATH", self.path / "symlink.json"), self.assertRaises(OSError):
            caller.read_draft()

    def test_draft_launcher_has_no_privileged_execution_or_config_read(self):
        body = caller.draft_launcher()
        self.assertIn("exit 78", body)
        for forbidden in ("docker", "Vault", "VAULT_TOKEN", "curl", "ssh", "exec "):
            self.assertNotIn(forbidden, body)

    def test_unsealed_loader_stops_before_input_or_preflight(self):
        reader = Mock()
        with (patch.dict(os.environ, {"VAULT_TOKEN": "SYNTHETIC-SENSITIVE"}),
              patch.object(loader, "new_stage") as stage, patch.object(loader, "reviewed_namespace") as reviewed,
              self.assertRaises(ValueError)):
            loader.run("not-an-approval", "1" * 64, "2" * 64, reader=reader)
        reader.assert_not_called()
        reader.read.assert_not_called()
        stage.assert_not_called()
        reviewed.assert_not_called()
        self.assertIsNone(loader.SEALED_MANIFEST)

    def test_attempted_cli_execution_only_exports_fixed_stopped_output(self):
        output = io.StringIO()
        with (patch.object(sys, "argv", ["loader", "--execute", "--approval-reference", "SYNTHETIC-SENSITIVE"]),
              contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as error):
            loader.main()
        self.assertEqual(error.exception.code, 1)
        self.assertNotIn("SYNTHETIC-SENSITIVE", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["failure_type"], "LoaderStopped")

    def test_absent_draft_main_sanitizes_failure(self):
        output = io.StringIO()
        with (patch.object(caller, "read_draft", side_effect=ValueError("SYNTHETIC-SENSITIVE")),
              contextlib.redirect_stdout(output), self.assertRaises(SystemExit)):
            caller.main()
        self.assertNotIn("SYNTHETIC-SENSITIVE", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["failure_type"], "DraftPreviewStopped")

    def test_cli_preserves_nonzero_status_for_a_synthetic_stopped_summary(self):
        output = io.StringIO()
        summary = loader.strict_json(loader.bounded_output(stopped_wrapper(), "1" * 64))
        with (patch.object(sys, "argv", ["loader", "--execute"]),
              patch.object(loader, "run", return_value=summary), contextlib.redirect_stdout(output),
              self.assertRaises(SystemExit) as error):
            loader.main()
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "stopped")
        self.assertIsNone(loader.SEALED_MANIFEST)

    def test_incomplete_proofs_refuse_current_unready_draft_and_failed_b1(self):
        with self.assertRaises(ValueError):
            loader.check_phase_a({"run_id": loader.VM_RUN, "target": {"cluster_alias": "nuc2", "project": "default"},
                                  "ready_for_execution": False})
        document = synthetic_draft()
        with self.assertRaises(ValueError):
            loader.check_pg_proposal(document, {"proposal_digest": bind_draft(document)})
        failures = [{}, {"phase": "B1", "phase_b_id": loader.B1_ID, "vm_run_id": loader.VM_RUN,
                         "proposal_digest": loader.B1_DIGEST, "failure_type": "TimeoutError", "result": None, "ledger": []},
                    {"phase": "B1", "phase_b_id": loader.VM_RUN, "vm_run_id": loader.VM_RUN,
                     "proposal_digest": "f" * 64, "failure_type": None, "result": {}, "ledger": []}]
        for value in failures:
            with self.subTest(value=value), self.assertRaises(ValueError):
                loader.check_revised_b1(value)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest, self.bodies, self.body, self.frozen = synthetic_bundle()
        source = {"filename": self.manifest["payload_files"]["source"],
                  **self.manifest["members"][self.manifest["payload_files"]["source"]]}
        self.addCleanup(patch.stopall)
        patch.object(loader, "FROZEN", self.frozen).start()
        patch.object(loader, "SOURCE_ARCHIVE", source).start()

    def test_exact_closed_manifest_and_ustar_size(self):
        self.assertIs(loader.validate_manifest(self.manifest, loader.canonical(self.manifest)), self.manifest)
        self.assertEqual(loader.bundle_size(self.manifest["members"]), len(self.body))

    def test_manifest_refuses_widening_target_roles_duplicates_sizes_and_mixed_pins(self):
        mutations = [lambda value: value["target"].update(cluster_alias="other"),
                     lambda value: value["target"].update(outer_nic_count=1),
                     lambda value: value["target"].update(profiles=["default"]),
                     lambda value: value.update(gate_id=loader.B1_ID),
                     lambda value: value["payload_files"].update(python=value["payload_files"]["postgres"]),
                     lambda value: value["members"].update({"../escape": descriptor(b"x")}),
                     lambda value: value["members"]["phase_a.py"].update(sha256="f" * 64),
                     lambda value: value["members"][value["payload_files"]["source"]].update(size=1),
                     lambda value: value["archive"].update(filename="old-bundle.tar"),
                     lambda value: value["archive"].update(size=value["archive"]["size"] + 512),
                     lambda value: value["members"][value["payload_files"]["python"]].update(size=513 * 1024 ** 2),
                     lambda value: value["members"]["approved-plan.json"].update(size=True)]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value = copy.deepcopy(self.manifest)
                mutate(value)
                with self.assertRaises(ValueError):
                    loader.validate_manifest(value, loader.canonical(value))

    def test_manifest_requires_exact_digest(self):
        with self.assertRaises(ValueError):
            loader.validate_manifest(self.manifest, "f" * 64)


class TransferTests(PrivateFixture):
    def test_bounded_hash_exact_stream_and_private_modes(self):
        body = b"x" * (loader.FLUSH + 11)
        reader = io.BytesIO(body)
        loader.stream_exact(reader, self.directory, "payload.tar", descriptor(body), loader.Deadline())
        self.assertEqual(loader.private_bytes(self.directory, "payload.tar", descriptor(body)), body)
        self.assertEqual(stat.S_IMODE((self.path / "payload.tar").stat().st_mode), 0o600)

    def test_short_long_and_hash_mismatch_retain_owned_partial_input(self):
        for name, body, expected in (("short.tar", b"abc", descriptor(b"abcdef")),
                                     ("long.tar", b"abcdef!", descriptor(b"abcdef")),
                                     ("hash.tar", b"abcdef", descriptor(b"ABCDEF"))):
            with self.subTest(name=name), self.assertRaises(ValueError):
                loader.stream_exact(io.BytesIO(body), self.directory, name, expected, loader.Deadline())
            self.assertTrue((self.path / name).is_file())
            self.assertEqual(stat.S_IMODE((self.path / name).stat().st_mode), 0o600)
        self.assertEqual((self.path / "short.tar").read_bytes(), b"abc")
        self.assertEqual((self.path / "hash.tar").read_bytes(), b"abcdef")

    def test_no_overwrite_and_links_modes_fifo_and_path_escape_refused(self):
        self.write("original.tar", b"unchanged")
        with self.assertRaises(FileExistsError):
            loader.new_file(self.directory, "original.tar")
        self.assertEqual((self.path / "original.tar").read_bytes(), b"unchanged")
        os.symlink("original.tar", self.path / "symbolic.tar")
        os.link(self.path / "original.tar", self.path / "hard.tar")
        self.write("public.tar", b"unchanged", mode=0o644)
        os.mkfifo(self.path / "fifo.tar", 0o600)
        for name in ("original.tar", "hard.tar", "symbolic.tar", "public.tar", "fifo.tar"):
            with self.subTest(name=name), self.assertRaises((OSError, PermissionError)):
                loader.private_bytes(self.directory, name, descriptor(b"unchanged"))
        for name in ("../escaped", "/absolute", "nested/path", "non-ascii-\u00e9"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                loader.new_file(self.directory, name)

    def test_exclusive_owned_directory_retains_existing_contents(self):
        fd = loader.new_directory(self.directory, "new-stage")
        os.close(fd)
        self.assertEqual(stat.S_IMODE((self.path / "new-stage").stat().st_mode), 0o700)
        (self.path / "new-stage" / "keep").write_bytes(b"preserve")
        with self.assertRaises(FileExistsError):
            loader.new_directory(self.directory, "new-stage")
        self.assertEqual((self.path / "new-stage" / "keep").read_bytes(), b"preserve")
        os.symlink("new-stage", self.path / "alias")
        with self.assertRaises(FileExistsError):
            loader.new_directory(self.directory, "alias")

    def test_read_timeout_before_write_retains_private_empty_file(self):
        now = [0.0]

        class SlowReader:
            def read(self, maximum):
                now[0] = 2.0
                return b"x"[:maximum]

        with self.assertRaises(TimeoutError):
            loader.stream_exact(SlowReader(), self.directory, "late.tar", descriptor(b"x"),
                                loader.Deadline(clock=lambda: now[0], seconds=1))
        self.assertEqual((self.path / "late.tar").read_bytes(), b"")

    def test_pipe_without_eof_has_finite_timeout_and_no_output(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        with os.fdopen(read_fd, "rb", buffering=0) as source, self.assertRaises(TimeoutError):
            loader.stream_exact(source, self.directory, "pipe.tar", descriptor(b"x"), loader.Deadline(seconds=0.02))
        self.assertEqual((self.path / "pipe.tar").read_bytes(), b"")

    def test_disk_reserve_and_oversized_reader_fail_before_advancing(self):
        usage = Mock(f_bavail=1, f_frsize=1)
        with patch.object(os, "fstatvfs", return_value=usage), self.assertRaises(OSError):
            loader.stream_exact(io.BytesIO(b"x"), self.directory, "reserve.tar", descriptor(b"x"), loader.Deadline())
        self.assertEqual((self.path / "reserve.tar").read_bytes(), b"")
        reader = Mock()
        reader.fileno.side_effect = io.UnsupportedOperation
        reader.read.return_value = b"xx"
        with self.assertRaises(ValueError):
            loader.bounded_read(reader, 1, loader.Deadline())

    def test_deadline_rejects_invalid_budgets_and_keeps_signal_handler_untouched(self):
        initial = signal.getsignal(signal.SIGALRM)
        for seconds in (False, 0, -1, float("nan"), float("inf"), 301):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                loader.Deadline(seconds=seconds)
        self.assertIs(signal.getsignal(signal.SIGALRM), initial)


class ArchiveTests(PrivateFixture):
    def stage(self, body: bytes, manifest: dict):
        loader.stream_exact(io.BytesIO(body), self.directory, "sealed-input.tar", descriptor(body), loader.Deadline())
        loader.unpack_verified(self.directory, manifest, loader.Deadline())

    def test_regular_archive_is_verified_and_unpacked_once(self):
        manifest, bodies, body, _ = synthetic_bundle()
        self.stage(body, manifest)
        self.assertEqual(sorted(path.name for path in self.path.iterdir()), sorted(["sealed-input.tar", *bodies]))
        for name, expected in bodies.items():
            self.assertEqual(loader.private_bytes(self.directory, name, descriptor(expected)), expected)
        with self.assertRaises(FileExistsError):
            loader.unpack_verified(self.directory, manifest, loader.Deadline())

    def test_header_rejection_precedes_link_extension_or_member_body(self):
        manifest, bodies, _, _ = synthetic_bundle()
        mutations = {"symlink": lambda value: setattr(value, "type", tarfile.SYMTYPE),
                     "hardlink": lambda value: setattr(value, "type", tarfile.LNKTYPE),
                     "gnu": lambda value: setattr(value, "type", tarfile.GNUTYPE_LONGNAME),
                     "pax": lambda value: setattr(value, "type", tarfile.XHDTYPE),
                     "duplicate": lambda value: setattr(value, "name", "wrong.tar"),
                     "mode": lambda value: setattr(value, "mode", 0o644),
                     "uid": lambda value: setattr(value, "uid", 1),
                     "mtime": lambda value: setattr(value, "mtime", 1)}
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                fd = loader.new_directory(self.directory, name)
                try:
                    body = outer_bytes(bodies, mutate=lambda index, value, mutate=mutate: mutate(value) if index == 0 else None)
                    altered = copy.deepcopy(manifest)
                    altered["archive"].update(descriptor(body))
                    loader.stream_exact(io.BytesIO(body), fd, "sealed-input.tar", descriptor(body), loader.Deadline())
                    with self.assertRaises(ValueError):
                        loader.unpack_verified(fd, altered, loader.Deadline())
                    self.assertEqual(os.listdir(self.path / name), ["sealed-input.tar"])
                finally:
                    os.close(fd)

    def test_bad_member_hash_retains_input_and_partial_without_dependent_files(self):
        manifest, bodies, body, _ = synthetic_bundle()
        first = min(bodies)
        manifest["members"][first]["sha256"] = "f" * 64
        with self.assertRaises(ValueError):
            self.stage(body, manifest)
        self.assertEqual(sorted(path.name for path in self.path.iterdir()), sorted(["sealed-input.tar", first]))
        self.assertEqual((self.path / first).read_bytes(), bodies[first])

    def test_nonzero_trailing_data_and_short_archive_refused(self):
        manifest, _, body, _ = synthetic_bundle()
        with self.assertRaises(ValueError):
            self.stage(body[:-1] + b"X", manifest)
        fd = loader.new_directory(self.directory, "short")
        try:
            loader.stream_exact(io.BytesIO(body[:-1]), fd, "sealed-input.tar", descriptor(body[:-1]), loader.Deadline())
            with self.assertRaises(ValueError):
                loader.unpack_verified(fd, manifest, loader.Deadline())
        finally:
            os.close(fd)


class AccountingTests(PrivateFixture):
    def accounting(self, *, maximum=b"1000", current=b"900", fields=None):
        default = {"inactive_file": 300, "file": 400, "shmem": 50, "file_dirty": 20, "file_writeback": 10, "unevictable": 5}
        self.write("memory.max", maximum)
        self.write("memory.current", current)
        self.write("memory.stat", "".join(f"{key} {value}\n" for key, value in (default if fields is None else fields).items()).encode())

    def test_only_clean_inactive_file_memory_is_reclaimable(self):
        self.accounting()
        self.assertEqual(loader.memory_available(self.path), 315)

    def test_nonfinite_missing_or_inconsistent_memory_refused(self):
        for name, maximum, current, fields in (("unbounded", b"max", b"900", None),
                                                ("inconsistent", b"100", b"900", None),
                                                ("missing", b"1000", b"900", {"inactive_file": 1})):
            with self.subTest(name=name):
                target = self.path / name
                target.mkdir(mode=0o700)
                for filename, body in (("memory.max", maximum), ("memory.current", current),
                                       ("memory.stat", ("inactive_file 1\n" if fields else "file 1\n").encode())):
                    (target / filename).write_bytes(body)
                with self.assertRaises(ValueError):
                    loader.memory_available(target)

    def test_accounting_links_and_oversized_public_files_refused(self):
        self.write("target", b"1000")
        os.symlink("target", self.path / "memory.max")
        with self.assertRaises(OSError):
            loader.public_kernel_text(self.path / "memory.max", 4096)
        self.write("oversized", b"x" * 4097)
        with self.assertRaises(ValueError):
            loader.public_kernel_text(self.path / "oversized", 4096)

    def test_deepest_mount_controls_disk_requirement(self):
        self.assertTrue(loader.disk_backed(Path("/var/tmp"), "root / ext4 rw 0 0\n"))
        self.assertFalse(loader.disk_backed(Path("/var/tmp"), "root / ext4 rw 0 0\ntmpfs /var/tmp tmpfs rw 0 0\n"))
        self.assertFalse(loader.disk_backed(Path("/var/tmp"), "root /var/other ext4 rw 0 0\n"))
        with self.assertRaises(ValueError):
            loader.disk_backed(Path("/var/tmp"), "malformed")


class OutputTests(unittest.TestCase):
    def test_failure_projection_exposes_fixed_type_counts_and_owned_ids_only(self):
        value = stopped_wrapper()
        value["failure_type"] = "SYNTHETIC-SENSITIVE"
        value["ledger"][0]["failure_type"] = "SYNTHETIC-SENSITIVE"
        value["resource_ids"] = {"postgres": "a" * 64}
        body = loader.bounded_output(value, "1" * 64)
        self.assertNotIn(b"SYNTHETIC-SENSITIVE", body)
        summary = json.loads(body)
        self.assertEqual(summary["failure_type"], "WrapperStopped")
        self.assertEqual(summary["resource_ids"], {"postgres": "a" * 64})
        self.assertEqual(summary["ledger_event_counts"], {"stopped": 1})
        self.assertNotIn("ledger", summary)
        self.assertNotIn("result", summary)

    def test_raw_bodies_extra_fields_wrong_targets_and_oversized_outputs_refused(self):
        mutations = [lambda value: value.update(vm_run_id=loader.B1_ID),
                     lambda value: value.update(proposal_digest="f" * 64),
                     lambda value: value.update(body="SYNTHETIC-SENSITIVE"),
                     lambda value: value["ledger"][0].update(body="SYNTHETIC-SENSITIVE"),
                     lambda value: value["ledger"][0].update(event="SYNTHETIC-SENSITIVE"),
                     lambda value: value.update(stage="SYNTHETIC-SENSITIVE"),
                     lambda value: value.update(failure_type="X" * loader.MAX_OUTPUT),
                     lambda value: value.update(ledger=value["ledger"] * 129),
                     lambda value: value.update(resource_ids={"postgres": "secret"}),
                     lambda value: value.update(resource_ids={"postgres": "a" * 64, "runner": "a" * 64}),
                     lambda value: value.update(result={"raw": "SYNTHETIC-SENSITIVE"})]
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                value = stopped_wrapper()
                mutate(value)
                loader.bounded_output(value, "1" * 64)

    def test_operation_completion_requires_previously_accepted_exact_id(self):
        value = stopped_wrapper()
        operation = "/1.0/operations/11111111-1111-4111-8111-111111111111"
        value["ledger"] = [{"event": "guest_command_accepted", "stage": "prepare_incoming", "operation_id": operation},
                           {"event": "guest_command_completed", "stage": "prepare_incoming", "operation_id": operation,
                            "returncode": 0}]
        self.assertEqual(json.loads(loader.bounded_output(value, "1" * 64))["accepted_operation_ids"], [operation])
        value["ledger"].reverse()
        with self.assertRaises(ValueError):
            loader.bounded_output(value, "1" * 64)

    def test_json_boundaries_refuse_duplicates_nonfinite_nonobjects_and_oversize(self):
        for body in (b'{"x":1,"x":2}', b'{"x":NaN}', b"[]", b"x" * (loader.MAX_JSON + 1)):
            with self.subTest(body=body[:20]), self.assertRaises((TypeError, ValueError)):
                loader.strict_json(body)


if __name__ == "__main__":
    unittest.main()
