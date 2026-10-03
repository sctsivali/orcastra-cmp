"""Offline behavior tests for the narrowly scoped B1 staging and guest runtime."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import subprocess
import tarfile
import tempfile
import unittest
import uuid
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch

import phase_b_b1_guest as guest
import phase_b_b1_stage as staging
from phase_b_b1_plan import render_b1
from phase_b_images import assemble_image

VM_RUN = "0f2a5b15-f5ec-464b-8785-0b4b88c3b358"
PHASE_A = "384bc2030c890ea26f054ece6725a0876fd95e95a6ccfdfe53368d17e3eb863e"


def make_plan() -> dict:
    return render_b1(vm_run_id=VM_RUN, phase_a_digest=PHASE_A, phase_b_id=str(uuid.uuid4()))


def sign(plan: dict) -> str:
    plan.pop("proposal_digest", None)
    plan["proposal_digest"] = guest.canonical_digest(plan)
    return plan["proposal_digest"]


def synthetic_tools() -> dict[str, bytes]:
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
        for name in sorted(guest.BINARIES):
            body = b"synthetic-offline-fixture"
            member = tarfile.TarInfo("docker/" + name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    return {"docker-29.8.1.tgz": archive_bytes.getvalue(), "docker-compose": b"synthetic-compose",
            "docker-buildx": b"synthetic-buildx"}


def bind_tool_fixtures(plan: dict, tools: dict[str, bytes]) -> None:
    for item in plan["tools"]:
        body = tools[item["filename"]]
        item["size"] = len(body)
        item["sha256"] = hashlib.sha256(body).hexdigest()
    sign(plan)


class B1ContractTests(unittest.TestCase):
    def test_proposal_is_independent_of_node_and_application_state(self):
        plan = make_plan()
        self.assertEqual(plan["phase"], "B1")
        self.assertEqual(plan["target"]["expanded_devices"], ["root"])
        self.assertEqual(plan["docker_policy"]["bridge"], "none")
        self.assertEqual(plan["docker_policy"]["hosts"], ["unix:///srv/orcastra-qa-hardening/docker.sock"])
        self.assertEqual(plan["controller_staging"]["expected_tool_bytes"], 184187941)
        bootstrap = guest.RuntimeBootstrap(plan, plan["proposal_digest"], "synthetic-review")
        self.assertEqual(bootstrap.phase_b_id, plan["phase_b_id"])
        self.assertIn("Node24 decision remains separate", plan["b2"])

    def test_missing_approval_or_changed_proposal_is_denied_before_io(self):
        plan = make_plan()
        with self.assertRaises(ValueError):
            guest.RuntimeBootstrap(plan, plan["proposal_digest"], "")
        plan["docker_policy"]["bridge"] = "docker0"
        with self.assertRaises(ValueError):
            guest.RuntimeBootstrap(plan, plan["proposal_digest"], "synthetic-review")

    def test_even_a_resigned_proposal_cannot_widen_scope(self):
        mutations = [lambda plan: plan["docker_policy"].update(hosts=["tcp://0.0.0.0:2375"]),
                     lambda plan: plan["target"].update(expanded_devices=["root", "eth0"]),
                     lambda plan: plan["target"].update(project="adjacent"),
                     lambda plan: plan["systemd_units"].update({guest.LAB + "-docker.service": "ExecStart=/bin/unauthorized"})]
        for mutate in mutations:
            plan = make_plan()
            mutate(plan)
            sign(plan)
            with self.assertRaises(ValueError):
                guest.RuntimeBootstrap(plan, plan["proposal_digest"], "synthetic-review")

    def test_reused_phase_id_paths_are_denied_without_overwrite(self):
        with tempfile.TemporaryDirectory() as location, patch.object(staging, "STAGING_ROOT", Path(location)):
            plan = make_plan()
            stage = Path(location) / (guest.LAB + "-b1-" + plan["phase_b_id"])
            stage.mkdir()
            sentinel = stage / "existing"
            sentinel.write_bytes(b"preserve")
            plan["controller_staging"]["path"] = str(stage)
            sign(plan)
            with self.assertRaises(FileExistsError):
                staging.stage_b1(plan, plan["proposal_digest"])
            self.assertEqual(sentinel.read_bytes(), b"preserve")

    def test_public_stage_verifies_payloads_and_contains_only_five_transfer_files(self):
        with tempfile.TemporaryDirectory() as location, patch.object(staging, "STAGING_ROOT", Path(location)):
            plan = make_plan()
            tools = synthetic_tools()
            bind_tool_fixtures(plan, tools)
            stage = Path(location) / (guest.LAB + "-b1-" + plan["phase_b_id"])
            plan["controller_staging"]["path"] = str(stage)
            sign(plan)
            responses = [io.BytesIO(tools[item["filename"]]) for item in plan["tools"]]
            with patch.object(staging, "public_response", side_effect=responses):
                result = staging.stage_b1(plan, plan["proposal_digest"])
            self.assertEqual(len(result["files"]), 5)
            self.assertEqual(stage.stat().st_mode & 0o777, 0o700)
            for item in result["files"]:
                path = stage / item["filename"]
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"])
            with tarfile.open(stage / "b1-transfer.tar") as archive:
                self.assertEqual(set(archive.getnames()), {item["filename"] for item in result["files"]})
                self.assertTrue(all(member.isfile() and member.mode == 0o600 for member in archive.getmembers()))
            self.assertEqual(result["archive"]["sha256"], hashlib.sha256((stage / "b1-transfer.tar").read_bytes()).hexdigest())

    def test_failed_download_retains_partial_file_without_retry_or_cleanup(self):
        with tempfile.TemporaryDirectory() as location, patch.object(staging, "STAGING_ROOT", Path(location)):
            plan = make_plan()
            tools = synthetic_tools()
            bind_tool_fixtures(plan, tools)
            stage = Path(location) / (guest.LAB + "-b1-" + plan["phase_b_id"])
            plan["controller_staging"]["path"] = str(stage)
            sign(plan)
            with (patch.object(staging, "public_response", return_value=io.BytesIO(b"corrupted")) as request,
                  self.assertRaises(ValueError)):
                staging.stage_b1(plan, plan["proposal_digest"])
            self.assertEqual(request.call_count, 1)
            self.assertEqual((stage / "docker-29.8.1.tgz").read_bytes(), b"corrupted")
            ledger = [json.loads(line) for line in (stage / "public-stage.jsonl").read_text().splitlines()]
            self.assertEqual(ledger[-1]["event"], "stopped")
            self.assertNotIn("corrupted", json.dumps(ledger))

    def test_unknown_public_target_is_denied_without_request(self):
        for url in ["http://download.docker.com/tool", "https://private.invalid/tool",
                    "https://user:secret@github.com/tool", "https://github.com:444/tool"]:
            with patch("urllib.request.OpenerDirector.open") as request, self.assertRaises(ValueError):
                staging.public_response(url)
            request.assert_not_called()

    def test_reviewed_public_cache_is_rehashed_without_network(self):
        with tempfile.TemporaryDirectory() as location, patch.object(staging, "STAGING_ROOT", Path(location)):
            cached = Path(location) / (guest.LAB + "-b1-" + str(uuid.uuid4()))
            cached.mkdir(mode=0o700)
            tools = synthetic_tools()
            for filename, body in tools.items():
                (cached / filename).write_bytes(body)
            plan = make_plan()
            bind_tool_fixtures(plan, tools)
            plan["controller_staging"]["path"] = str(Path(location) / (guest.LAB + "-b1-" + plan["phase_b_id"]))
            sign(plan)
            with patch.object(staging, "public_response") as request:
                output = staging.stage_b1(plan, plan["proposal_digest"], cached)
            request.assert_not_called()
            self.assertEqual(len(output["files"]), 5)

    def test_symlink_public_cache_is_denied_before_reading_files(self):
        with tempfile.TemporaryDirectory() as location, patch.object(staging, "STAGING_ROOT", Path(location)):
            cache = Path(location) / (guest.LAB + "-b1-" + str(uuid.uuid4()))
            cache.symlink_to(Path(location), target_is_directory=True)
            plan = make_plan()
            plan["controller_staging"]["path"] = str(Path(location) / (guest.LAB + "-b1-" + plan["phase_b_id"]))
            sign(plan)
            with self.assertRaises(ValueError):
                staging.stage_b1(plan, plan["proposal_digest"], cache)

    def test_install_timeout_records_stage_and_retains_files_without_retry(self):
        with tempfile.TemporaryDirectory() as location:
            root = Path(location) / "guest"
            root.mkdir()
            units = Path(location) / "units"
            units.mkdir()
            (root / "artifacts").mkdir()
            with patch.object(guest, "ROOT", root), patch.object(guest, "UNIT_DIRECTORY", units):
                plan = make_plan()
                tools = synthetic_tools()
                bind_tool_fixtures(plan, tools)
                bootstrap = guest.RuntimeBootstrap(plan, plan["proposal_digest"], "synthetic-review")
                bootstrap.incoming.mkdir(parents=True)
                for filename, body in tools.items():
                    (bootstrap.incoming / filename).write_bytes(body)

                def command(command, **kwargs):
                    if command[:2] == ["systemctl", "start"]:
                        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
                    return subprocess.CompletedProcess(command, 0, stdout=b"{}", stderr=b"")

                with (patch.object(bootstrap, "preflight"), patch("subprocess.run", side_effect=command) as commands,
                      self.assertRaises(subprocess.TimeoutExpired)):
                    bootstrap.install()
                self.assertEqual(commands.call_count, 3)
                self.assertTrue((units / (guest.LAB + "-docker.service")).exists())
                self.assertTrue((root / "bin/dockerd").exists())
                records = [json.loads(line) for line in (root / "artifacts" / ("b1-" + plan["phase_b_id"] + ".jsonl")).read_text().splitlines()]
                self.assertEqual(records[-1]["stage"], "start_owned_docker")
                self.assertEqual(records[-1]["failure_type"], "TimeoutExpired")
                self.assertEqual(records[-2]["event"], "command_attempt")

    def test_success_requires_fresh_empty_daemon_exact_versions_and_no_nic(self):
        with tempfile.TemporaryDirectory() as location:
            root = Path(location) / "guest"
            root.mkdir()
            units = Path(location) / "units"
            units.mkdir()
            (root / "artifacts").mkdir()
            with patch.object(guest, "ROOT", root), patch.object(guest, "UNIT_DIRECTORY", units):
                plan = make_plan()
                tools = synthetic_tools()
                bind_tool_fixtures(plan, tools)
                bootstrap = guest.RuntimeBootstrap(plan, plan["proposal_digest"], "synthetic-review")
                bootstrap.incoming.mkdir(parents=True)
                for filename, body in tools.items():
                    (bootstrap.incoming / filename).write_bytes(body)

                def command(argv, **kwargs):
                    if "version" in argv and "--format" in argv:
                        body = json.dumps({"Client": {"Version": "29.8.1"}, "Server": {"Version": "29.8.1"}}).encode()
                    elif "info" in argv:
                        body = json.dumps({"DockerRootDir": str(root / "docker-data"), "Driver": "overlay2", "Images": 0, "Containers": 0}).encode()
                    elif "compose" in argv:
                        body = b"5.5.1\n"
                    elif "buildx" in argv:
                        body = b"github.com/docker/buildx v0.37.1 public-commit\n"
                    else:
                        body = b""
                    return subprocess.CompletedProcess(argv, 0, stdout=body, stderr=b"")

                with (patch.object(bootstrap, "preflight"), patch("subprocess.run", side_effect=command),
                      patch("pathlib.Path.iterdir", return_value=iter([Path("lo")])),
                      patch.object(bootstrap, "effective_boundaries", return_value={"socket_protected": True,
                          "systemd_units_verified": True, "cgroup_limits_verified": True,
                          "memory_max_bytes": guest.MEMORY_MAX, "cpu_quota_ratio": 3, "tasks_max": guest.TASKS_MAX})):
                    result = bootstrap.install()
                self.assertEqual(result["status"], "runtime_verified")
                self.assertEqual((result["images"], result["containers"], result["no_nic"]), (0, 0, True))
                self.assertTrue(result["cgroup_limits_verified"])
                self.assertEqual(result["memory_max_bytes"], 15032385536)


class RuntimeBoundaryTests(unittest.TestCase):
    def test_socket_requires_root_ownership_exact_modes_and_protected_parent(self):
        socket_status = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_uid=0, st_gid=0)
        parent_status = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0, st_gid=0)
        self.assertTrue(guest.protected_socket(socket_status, parent_status))
        for field, value in [("st_uid", 1000), ("st_gid", 1000), ("st_mode", stat.S_IFSOCK | 0o666),
                             ("st_mode", stat.S_IFREG | 0o660)]:
            original = getattr(socket_status, field)
            setattr(socket_status, field, value)
            with self.assertRaises(ValueError):
                guest.protected_socket(socket_status, parent_status)
            setattr(socket_status, field, original)
        for field, value in [("st_uid", 1000), ("st_gid", 1000), ("st_mode", stat.S_IFDIR | 0o755),
                             ("st_mode", stat.S_IFLNK | 0o700)]:
            original = getattr(parent_status, field)
            setattr(parent_status, field, value)
            with self.assertRaises(ValueError):
                guest.protected_socket(socket_status, parent_status)
            setattr(parent_status, field, original)

    def test_loaded_cgroup_path_accepts_nesting_and_refuses_traversal_or_other_unit(self):
        unit = guest.LAB + ".slice"
        nested = "/orcastra.slice/orcastra-qa.slice/" + unit
        self.assertEqual(guest.validated_cgroup(nested, unit), PurePosixPath(nested))
        for value in ["relative/" + unit, "/other.slice", "/../" + unit, "//" + unit,
                      "/orcastra.slice//" + unit, "/orcastra.slice/./" + unit, "/injected\n/" + unit]:
            with self.assertRaises(ValueError):
                guest.validated_cgroup(value, unit)

    def test_effective_limits_refuse_unlimited_or_wrong_kernel_values(self):
        approved_slice = {"memory": guest.MEMORY_MAX, "tasks": guest.TASKS_MAX, "quota": 300000, "period": 100000}
        approved_service = {"memory": None, "tasks": guest.TASKS_MAX, "quota": None, "period": 100000}
        result = guest.bounded_controller_limits(approved_slice, approved_service)
        self.assertEqual(result["cpu_quota_ratio"], 3)
        for key, value in [("memory", None), ("memory", guest.MEMORY_MAX + 1), ("tasks", None),
                           ("tasks", guest.TASKS_MAX + 1), ("quota", None), ("quota", 400000), ("quota", 200000)]:
            changed = dict(approved_slice, **{key: value})
            with self.assertRaises(ValueError):
                guest.bounded_controller_limits(changed, approved_service)
        for key, value in [("memory", 1024), ("tasks", None), ("quota", 200000)]:
            with self.assertRaises(ValueError):
                guest.bounded_controller_limits(approved_slice, dict(approved_service, **{key: value}))
        for key, value in [("memory", 1024), ("tasks", 1024), ("quota", 200000)]:
            with self.assertRaises(ValueError):
                guest.validate_ancestor_limits(dict(approved_service, **{key: value}))

    def test_cgroup_reader_refuses_symlink_components_and_malformed_controller(self):
        with tempfile.TemporaryDirectory() as location, patch.object(guest, "CGROUP_DIRECTORY", Path(location)):
            root = Path(location)
            group = root / (guest.LAB + ".slice")
            group.mkdir()
            (group / "memory.max").write_text("-1\n")
            (group / "pids.max").write_text("8192\n")
            (group / "cpu.max").write_text("300000 100000\n")
            with self.assertRaises(ValueError):
                guest.controller_limits(PurePosixPath("/" + group.name))
            link = root / "linked.slice"
            link.symlink_to(group, target_is_directory=True)
            with self.assertRaises(OSError):
                guest.read_cgroup_file(PurePosixPath("/linked.slice"), "memory.max")

    def test_systemd_controlgroup_readback_verifies_actual_nested_limits_and_denies_drift(self):
        with tempfile.TemporaryDirectory() as location:
            root = Path(location) / "guest"
            root.mkdir(mode=0o700)
            (root / "docker.sock").touch()
            unit_directory = Path(location) / "units"
            unit_directory.mkdir()
            cgroup_directory = Path(location) / "cgroup"
            cgroup_directory.mkdir()
            slice_group = PurePosixPath("/orcastra.slice/orcastra-qa.slice/" + guest.LAB + ".slice")
            service_group = slice_group / (guest.LAB + "-docker.service")
            values_by_group = {slice_group: (str(guest.MEMORY_MAX), "300000 100000", str(guest.TASKS_MAX)),
                               service_group: ("max", "max 100000", str(guest.TASKS_MAX)),
                               slice_group.parent: ("max", "max 100000", "max"),
                               slice_group.parent.parent: ("max", "max 100000", "max")}
            for group, values in values_by_group.items():
                directory = cgroup_directory.joinpath(*group.parts[1:])
                directory.mkdir(parents=True, exist_ok=True)
                for filename, value in zip(["memory.max", "cpu.max", "pids.max"], values, strict=True):
                    (directory / filename).write_text(value + "\n")
            with (patch.object(guest, "ROOT", root), patch.object(guest, "UNIT_DIRECTORY", unit_directory),
                  patch.object(guest, "CGROUP_DIRECTORY", cgroup_directory)):
                plan = make_plan()
                bootstrap = guest.RuntimeBootstrap(plan, plan["proposal_digest"], "synthetic-review")
                for unit, body in plan["systemd_units"].items():
                    (unit_directory / unit).write_text(body)
                properties = {}
                for unit, group in [(guest.LAB + ".slice", slice_group), (guest.LAB + "-docker.service", service_group)]:
                    properties[unit] = {"Id": unit, "LoadState": "loaded", "ActiveState": "active",
                                        "FragmentPath": str(unit_directory / unit), "DropInPaths": "", "ControlGroup": str(group),
                                        "Slice": guest.LAB + ".slice", "MemoryMax": str(guest.MEMORY_MAX),
                                        "TasksMax": str(guest.TASKS_MAX), "MainPID": "123", "Delegate": "yes"}

                def command(argv, **kwargs):
                    self.assertTrue(kwargs["read_only"])
                    return "\n".join(key + "=" + value for key, value in properties[argv[2]].items())

                with patch.object(guest, "protected_socket", return_value=True), patch.object(bootstrap, "command", side_effect=command):
                    result = bootstrap.effective_boundaries()
                    self.assertEqual(result["memory_max_bytes"], guest.MEMORY_MAX)
                    self.assertEqual(result["cpu_quota_ratio"], 3)
                    self.assertNotIn("ControlGroup", result)
                    (cgroup_directory.joinpath(*slice_group.parts[1:]) / "memory.max").write_text("max\n")
                    with self.assertRaises(ValueError):
                        bootstrap.effective_boundaries()
                    (cgroup_directory.joinpath(*slice_group.parts[1:]) / "memory.max").write_text(str(guest.MEMORY_MAX) + "\n")
                    properties[guest.LAB + "-docker.service"]["DropInPaths"] = "/unauthorized/override.conf"
                    with self.assertRaises(ValueError):
                        bootstrap.effective_boundaries()


class ImageArchiveTests(unittest.TestCase):
    def test_oci_blob_conversion_preserves_layers_without_extracting_members(self):
        with tempfile.TemporaryDirectory() as location:
            directory = Path(location)
            blobs = directory / "blobs"
            blobs.mkdir()
            layer = io.BytesIO()
            with tarfile.open(fileobj=layer, mode="w") as archive:
                member = tarfile.TarInfo("../../must-never-be-extracted")
                member.size = 6
                archive.addfile(member, io.BytesIO(b"public"))
            import gzip
            uncompressed = layer.getvalue()
            compressed = gzip.compress(uncompressed, mtime=0)
            diff_id = "sha256:" + hashlib.sha256(uncompressed).hexdigest()
            config = json.dumps({"os": "linux", "architecture": "amd64", "rootfs": {"diff_ids": [diff_id]}}).encode()
            config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
            layer_digest = "sha256:" + hashlib.sha256(compressed).hexdigest()
            (blobs / config_digest.removeprefix("sha256:")).write_bytes(config)
            (blobs / layer_digest.removeprefix("sha256:")).write_bytes(compressed)
            image = {"manifest_digest": "sha256:" + "3" * 64, "config_digest": config_digest,
                     "config_bytes": len(config), "uncompressed_layer_digests": [diff_id],
                     "layers": [{"digest": layer_digest, "size": len(compressed), "media_type": "application/vnd.oci.image.layer.v1.tar+gzip"}]}
            output = assemble_image(image, "postgres", blobs, directory / "postgres.tar.gz")
            self.assertEqual(output["expected_image_id"], config_digest)
            with tarfile.open(directory / "postgres.tar.gz") as archive:
                manifest = json.load(archive.extractfile("manifest.json"))
                self.assertEqual(manifest[0]["RepoTags"], [output["local_tag"]])
                self.assertEqual(archive.extractfile(manifest[0]["Layers"][0]).read(), uncompressed)
            self.assertFalse((directory.parent / "must-never-be-extracted").exists())


if __name__ == "__main__":
    unittest.main()
