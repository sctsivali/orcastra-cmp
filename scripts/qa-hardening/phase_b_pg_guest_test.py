"""Offline safety/contract tests; no socket, container, database or guest calls."""

from __future__ import annotations

import builtins
import copy
import hashlib
import io
import json
import os
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import phase_b_b1_guest as b1
import phase_b_pg_guest as guest
from phase_b_pg_gate import render_plan
from phase_b_vault_bootstrap_test import OwnedRootMetadata

VM_RUN = "0f2a5b15-f5ec-464b-8785-0b4b88c3b358"
B1_RUN = "0366fe77-c9d2-445c-8f79-7a923410bddd"
GATE = "35c4d18f-cedf-4483-89f9-aa364038e3a2"
PHASE_A = "384bc2030c890ea26f054ece6725a0876fd95e95a6ccfdfe53368d17e3eb863e"
IDS = {"network": "a" * 64, "postgres": "b" * 64, "runner": "c" * 64}
NAMES = ["test_case_" + str(index) for index in range(14)]


def runner_mountinfo(venv_flags="rw,nosuid,nodev,relatime", tmp_flags="rw,nosuid,nodev,noexec,relatime") -> str:
    return ("100 90 0:10 / / ro,relatime - overlay overlay ro\n"
            f"101 100 0:11 / /opt/qa-venv {venv_flags} - tmpfs tmpfs rw,size=524288k,mode=755\n"
            f"102 100 0:12 / /tmp {tmp_flags} - tmpfs tmpfs rw,size=131072k\n")


class OwnedGuestMetadata(OwnedRootMetadata):
    """Represent protected guest ancestors without requiring host root ownership.

    Only witnessed, read-only ancestor directory descriptors receive synthetic
    root ownership and modes. Descendants retain their real safety metadata.
    """

    def fstat(self, descriptor):
        recorded = self.descriptors.get(descriptor)
        if recorded is None or recorded[0] not in self.root.parents:
            return super().fstat(descriptor)
        metadata = os.fstat(descriptor)
        located = os.stat(recorded[0], follow_symlinks=False)
        if (not stat.S_ISDIR(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != recorded[1:]
                or (located.st_dev, located.st_ino) != recorded[1:]):
            raise PermissionError("The synthetic guest lost its witnessed ancestor directory")
        fields = list(metadata)
        fields[0] = stat.S_IFDIR | 0o755
        fields[4], fields[5] = self.owner_overrides.get(recorded[0], (0, 0))
        return os.stat_result(fields)


def metadata(filename: str, body: bytes = b"fixture") -> dict:
    return {"filename": filename, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}


def source_inputs() -> dict[str, bytes]:
    return {"backend/alembic.ini": b"[alembic]\n", "backend/constraints.txt": b"", "backend/requirements.txt": b"",
            "backend/requirements-dev.txt": b"pytest>=8\n", "backend/tests/conftest.py": b"", "backend/pytest.ini": b"[pytest]\n",
            guest.TEST_MODULE: "\n".join("def " + name + "():\n    pass\n" for name in NAMES).encode()}


def source_inventory() -> dict:
    entries = [{"path": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
               for name, body in sorted(source_inputs().items())]
    document = {"schema_version": 1, "scope": "Synthetic offline test inventory", "entries": entries,
                "tests": NAMES, "total_bytes": sum(item["size"] for item in entries)}
    return {**document, "source_tree_sha256": guest.canonical_digest(document)}


def wheels() -> list[dict]:
    names = sorted({"alembic", "asyncpg", "pytest", "pytest-asyncio", "sqlalchemy",
                    *("synthetic-package-" + str(index).zfill(2) for index in range(60))})
    return [{"name": name, "version": "1.0.0", **metadata(name.replace("-", "_") + "-1.0.0-py3-none-any.whl")}
            for name in names]


def ready_plan() -> dict:
    inventory = wheels()
    lock = hashlib.sha256(guest.requirements_lock(inventory)).hexdigest()
    plan = render_plan(vm_run_id=VM_RUN, phase_a_digest=PHASE_A, b1_id=B1_RUN, gate_id=GATE,
                       inventory=source_inventory(), image_archives={role: metadata(role + ".tar.gz") for role in guest.IMAGE_PINS},
                       source_archive=metadata("source.tar"), wheel_archive=metadata("wheels.tar"),
                       wheel_inventory=inventory, wheel_requirements_sha256=lock)
    plan["ready_for_execution"], plan["pending"] = True, []
    return seal(plan)


def seal(plan: dict) -> dict:
    core = dict(plan)
    core.pop("proposal_digest", None)
    plan["proposal_digest"] = guest.canonical_digest(core)
    return plan


def make_tar(path: Path, contents: list[tuple[str, bytes, str]]) -> None:
    with path.open("xb") as target, tarfile.open(fileobj=target, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, body, kind in contents:
            member = tarfile.TarInfo(name)
            member.uid = member.gid = member.mtime = 0
            member.mode = 0o644
            if kind == "symlink":
                member.type, member.linkname = tarfile.SYMTYPE, "/etc/passwd"
                archive.addfile(member)
            elif kind == "hardlink":
                member.type, member.linkname = tarfile.LNKTYPE, "/etc/passwd"
                archive.addfile(member)
            elif kind == "fifo":
                member.type = tarfile.FIFOTYPE
                archive.addfile(member)
            else:
                member.size = len(body)
                archive.addfile(member, io.BytesIO(body))
    path.chmod(0o600)


def image_fixture(path: Path, *, tags=None, layer_body=b"Synthetic layer bytes", declared_body=None) -> dict:
    digest = hashlib.sha256(layer_body if declared_body is None else declared_body).hexdigest()
    config = {"os": "linux", "architecture": "amd64", "rootfs": {"type": "layers", "diff_ids": ["sha256:" + digest]},
              "config": {"Env": ["PATH=/usr/local/bin:/usr/bin"], "Entrypoint": ["docker-entrypoint.sh"],
                         "Cmd": ["postgres"], "User": "", "WorkingDir": ""}}
    body = json.dumps(config, separators=(",", ":")).encode()
    config_sha = hashlib.sha256(body).hexdigest()
    tag = guest.LAB + "/postgres:synthetic"
    manifest = [{"Config": config_sha + ".json", "RepoTags": tags if tags is not None else [tag],
                 "Layers": [digest + "/layer.tar"]}]
    make_tar(path, [(config_sha + ".json", body, "regular"), (digest + "/layer.tar", layer_body, "regular"),
                    ("manifest.json", json.dumps(manifest).encode(), "regular")])
    return {"expected_image_id": "sha256:" + config_sha, "local_tag": tag}


def image_configs() -> dict:
    return {"postgres": {"config": {"Env": ["PATH=/usr/local/bin:/usr/bin"], "User": "", "WorkingDir": "",
                                     "Entrypoint": ["docker-entrypoint.sh"], "Cmd": ["postgres"]}},
            "python": {"config": {"Env": ["PATH=/usr/local/bin:/usr/bin"], "User": "", "WorkingDir": "",
                                   "Entrypoint": None, "Cmd": ["python3"]}}}


def inspected_container(plan: dict, role: str, *, running: bool = False) -> dict:
    expected = guest.container_payload(plan, role, IDS["network"])
    config = copy.deepcopy(image_configs()["postgres" if role == "postgres" else "python"]["config"])
    environment = {value.split("=", 1)[0]: value.split("=", 1)[1] for value in config["Env"]}
    for value in expected.get("Env", []):
        key, value = value.split("=", 1)
        environment[key] = value
    config.update({key: value for key, value in expected.items() if key in {"Labels", "User", "WorkingDir", "Entrypoint", "Cmd"}})
    config["Env"] = [key + "=" + value for key, value in environment.items()]
    mounts = [{"Type": "bind", "Source": mount["Source"], "Destination": mount["Target"], "RW": not mount["ReadOnly"],
               "Propagation": "rprivate"} for mount in expected["HostConfig"]["Mounts"]]
    mounts.extend({"Type": "tmpfs", "Source": "", "Destination": path, "RW": True, "Propagation": ""}
                  for path in expected["HostConfig"]["Tmpfs"])
    return {"Id": IDS[role], "Name": "/" + plan["containers"][role], "Image": expected["Image"], "Config": config,
            "HostConfig": expected["HostConfig"], "AppArmorProfile": "docker-default", "Mounts": mounts,
            "NetworkSettings": {"Networks": {plan["network"]["name"]: {"NetworkID": IDS["network"]}}},
            "State": {"Status": "running" if running else "created", "Running": running, "OOMKilled": False, "Dead": False, "Error": "", "Pid": 123 if running else 0,
                      "ExitCode": 0, "Health": {"Status": "starting"}}}


class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        status, body = reply
        return status, json.dumps(body).encode() if body is not None else b""


class PlanBoundary(unittest.TestCase):
    def test_reviewed_complete_plan_has_finite_budget_and_only_the_two_pinned_images(self):
        plan = ready_plan()
        checked = guest.validate_plan(plan, plan["proposal_digest"], "Synthetic local test approval")
        self.assertEqual(set(checked["images"]), {"postgres", "python"})
        self.assertEqual(checked["timeouts"], guest.TIMEOUTS)
        self.assertEqual(checked["databases"], guest.DATABASES)

    def test_draft_or_unbound_approval_is_refused_before_transport(self):
        plan = ready_plan()
        transport = FakeTransport([])
        with self.assertRaises(ValueError):
            guest.ScratchGate(plan, "0" * 64, "test", transport=transport)
        with self.assertRaises(ValueError):
            guest.ScratchGate(plan, plan["proposal_digest"], "", transport=transport)
        plan["ready_for_execution"] = False
        seal(plan)
        with self.assertRaises(ValueError):
            guest.ScratchGate(plan, plan["proposal_digest"], "test", transport=transport)
        self.assertEqual(transport.calls, [])

    def test_resealed_scope_widening_is_still_refused(self):
        mutations = [lambda p: p["network"].update(internal=False), lambda p: p["network"].update(published_ports=[5432]),
                     lambda p: p["labels"].update({"orcastra.qa.vm_run": GATE}), lambda p: p["target"].update(project="neighbor"),
                     lambda p: p["target"].update(profiles=["default"]), lambda p: p["target"].update(outer_nic_count=1),
                     lambda p: p["resource_budget"].update(runner_cpus=8), lambda p: p["timeouts"].update(runner_seconds=0),
                     lambda p: p["images"]["python"].update(local_tag="python:latest"),
                     lambda p: p["databases"].update(migration="production")]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                plan = ready_plan()
                mutation(plan)
                seal(plan)
                with self.assertRaises(ValueError):
                    guest.validate_plan(plan, plan["proposal_digest"], "test")

    def test_duplicate_package_version_and_lock_drift_are_refused(self):
        entries = wheels()
        entries[-1] = dict(entries[0])
        with self.assertRaises(ValueError):
            guest.requirements_lock(entries)
        plan = ready_plan()
        plan["wheel_requirements_sha256"] = "0" * 64
        seal(plan)
        with self.assertRaises(ValueError):
            guest.validate_plan(plan, plan["proposal_digest"], "test")

    def test_source_env_or_neighbor_test_cannot_be_added(self):
        for path in ["backend/.env", "backend/tests/test_neighbor.py", "backend/app/secrets/token.py", "frontend/src/app/page.tsx"]:
            with self.subTest(path=path):
                plan = ready_plan()
                inventory = plan["source"]["inventory"]
                inventory["entries"].append({"path": path, "size": 0, "sha256": hashlib.sha256(b"").hexdigest()})
                inventory["entries"].sort(key=lambda item: item["path"])
                document = dict(inventory)
                document.pop("source_tree_sha256")
                inventory["source_tree_sha256"] = guest.canonical_digest(document)
                seal(plan)
                with self.assertRaises(ValueError):
                    guest.validate_plan(plan, plan["proposal_digest"], "test")

    def test_json_duplicates_and_nonfinite_values_are_refused(self):
        for body in [b'{"ready":true,"ready":false}', b'{"duration":NaN}', b'{"duration":Infinity}']:
            with self.subTest(body=body), self.assertRaises(ValueError):
                guest.strict_json(body)


class ProtectedFiles(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix=".pg-contract-", dir=Path.cwd())
        self.root = Path(self.directory.name)
        self.metadata = OwnedGuestMetadata(self.root)
        metadata_patch = patch.object(guest, "os", self.metadata)
        metadata_patch.start()
        self.addCleanup(metadata_patch.stop)

    def tearDown(self):
        self.directory.cleanup()

    def test_reader_refuses_symlink_hardlink_fifo_and_public_mode(self):
        regular = self.root / "regular"
        regular.write_bytes(b"synthetic")
        regular.chmod(0o600)
        (self.root / "symlink").symlink_to(regular)
        os.link(regular, self.root / "hardlink")
        os.mkfifo(self.root / "fifo", 0o600)
        public = self.root / "public"
        public.write_bytes(b"synthetic")
        public.chmod(0o644)
        for filename in ["regular", "symlink", "hardlink", "fifo", "public"]:
            with self.subTest(filename=filename), self.assertRaises((OSError, ValueError)):
                guest.regular_bytes(self.root / filename, 128)

    def test_ancestor_symlink_or_writable_directory_is_refused(self):
        protected = self.root / "protected"
        protected.mkdir(mode=0o700)
        (self.root / "alias").symlink_to(protected)
        with self.assertRaises(OSError):
            guest.open_directory(self.root / "alias")
        protected.chmod(0o777)
        with self.assertRaises(ValueError):
            guest.open_directory(protected)

    def test_synthetic_ownership_keeps_foreign_file_parent_and_ancestor_denials(self):
        regular = self.root / "regular"
        regular.write_bytes(b"Synthetic owned file")
        regular.chmod(0o600)
        for path in (regular, self.root, self.root.parent):
            for owner in ((1, 0), (0, 1)):
                with self.subTest(path=path.name, owner=owner):
                    self.metadata.owner_overrides[path] = owner
                    with self.assertRaises(ValueError):
                        guest.regular_bytes(regular, 128)
                    del self.metadata.owner_overrides[path]
        self.assertEqual(guest.regular_bytes(regular, 128), b"Synthetic owned file")
        self.assertEqual((regular.stat().st_uid, regular.stat().st_gid), (os.geteuid(), os.getegid()))

    def test_synthetic_metadata_refuses_foreign_and_replaced_descriptors(self):
        regular = self.root / "regular"
        regular.write_bytes(b"Synthetic descriptor identity")
        descriptor = self.metadata.open(regular, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            self.assertEqual(self.metadata.fstat(descriptor).st_uid, 0)
            regular.rename(self.root / "moved")
            regular.write_bytes(b"Replacement file")
            with self.assertRaises(PermissionError):
                self.metadata.fstat(descriptor)
        finally:
            self.metadata.close(descriptor)
        with tempfile.TemporaryDirectory(prefix=".pg-neighbor-") as location:
            neighbor = Path(location) / "file"
            neighbor.write_bytes(b"Synthetic neighboring file")
            descriptor = os.open(neighbor, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                with self.assertRaises(PermissionError):
                    self.metadata.fstat(descriptor)
                with self.assertRaises(PermissionError):
                    self.metadata.open(neighbor, os.O_RDONLY | os.O_NOFOLLOW)
            finally:
                os.close(descriptor)

    def test_inventory_unpack_does_not_overwrite_existing_files(self):
        archive = self.root / "source.tar"
        make_tar(archive, [(name, body, "regular") for name, body in sorted(source_inputs().items())])
        entries = {entry["path"]: entry for entry in source_inventory()["entries"]}
        destination = self.root / "new"
        destination.mkdir(mode=0o700)
        captured = guest.inspect_files_archive(archive, entries, destination=destination)
        self.assertEqual(captured[guest.TEST_MODULE], source_inputs()[guest.TEST_MODULE])
        before = (destination / guest.TEST_MODULE).read_bytes()
        with self.assertRaises(FileExistsError):
            guest.inspect_files_archive(archive, entries, destination=destination)
        self.assertEqual((destination / guest.TEST_MODULE).read_bytes(), before)

    def test_extra_duplicate_link_fifo_or_traversal_tar_member_is_refused(self):
        contents = [(name, body, "regular") for name, body in sorted(source_inputs().items())]
        entries = {entry["path"]: entry for entry in source_inventory()["entries"]}
        extras = [("backend/.env", b"synthetic", "regular"), contents[0], ("backend/link", b"", "symlink"),
                  ("backend/link", b"", "hardlink"), ("backend/fifo", b"", "fifo"), ("../escape", b"", "regular")]
        for index, extra in enumerate(extras):
            archive = self.root / (str(index) + ".tar")
            make_tar(archive, [*contents, extra])
            with self.subTest(extra=extra[0]), self.assertRaises(ValueError):
                guest.inspect_files_archive(archive, entries)

    def test_regular_tar_content_hash_mismatch_is_refused(self):
        contents = [(name, body, "regular") for name, body in sorted(source_inputs().items())]
        name, body, kind = contents[-1]
        contents[-1] = (name, b"X" * len(body), kind)
        archive = self.root / "drift.tar"
        make_tar(archive, contents)
        with self.assertRaises(ValueError):
            guest.inspect_files_archive(archive, {entry["path"]: entry for entry in source_inventory()["entries"]})

    def test_canonical_long_wheel_path_pax_extension_is_accepted(self):
        name = "wheels/" + "a" * 105 + ".whl"
        archive = self.root / "long.tar"
        make_tar(archive, [(name, b"wheel", "regular")])
        guest.inspect_files_archive(archive, {name: metadata("fixture", b"wheel")})

    def test_reviewed_image_config_and_layers_are_verified_without_extracting(self):
        archive = self.root / "image.tar"
        image = image_fixture(archive)
        config = guest.inspect_image_archive(archive, image)
        self.assertEqual(config["architecture"], "amd64")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["image.tar"])

    def test_shared_repo_tag_or_wrong_diff_id_is_refused_before_image_load(self):
        archive = self.root / "shared.tar"
        image = image_fixture(archive, tags=["postgres:latest"])
        with self.assertRaises(ValueError):
            guest.inspect_image_archive(archive, image)
        archive = self.root / "layer-drift.tar"
        image = image_fixture(archive, layer_body=b"modified", declared_body=b"original")
        with self.assertRaises(ValueError):
            guest.inspect_image_archive(archive, image)


class ContainerBoundary(unittest.TestCase):
    def setUp(self):
        self.plan = ready_plan()

    def scratch(self, replies=()):
        scratch = guest.ScratchGate(self.plan, self.plan["proposal_digest"], "test", transport=FakeTransport(replies))
        scratch.created = dict(IDS)
        scratch.image_configs = image_configs()
        return scratch

    def test_payload_is_internal_nopublish_and_keeps_explicit_entrypoint_caps(self):
        network = guest.network_payload(self.plan)
        self.assertIs(network["Internal"], True)
        self.assertEqual(network["Options"]["com.docker.network.bridge.enable_ip_masquerade"], "false")
        postgres = guest.container_payload(self.plan, "postgres", IDS["network"])
        runner = guest.container_payload(self.plan, "runner", IDS["network"])
        self.assertEqual(postgres["HostConfig"]["CapAdd"], guest.PG_CAPS)
        self.assertEqual(runner["HostConfig"]["CapAdd"], [])
        for payload in [postgres, runner]:
            host = payload["HostConfig"]
            self.assertIs(host["ReadonlyRootfs"], True)
            self.assertIs(host["AutoRemove"], False)
            self.assertEqual(host["PortBindings"], {})
            self.assertFalse(any("sock" in mount["Source"] for mount in host["Mounts"]))
        self.assertFalse(any(value.startswith("POSTGRES_PASSWORD=") for value in postgres["Env"]))
        self.assertEqual(runner["HostConfig"]["MemorySwap"], runner["HostConfig"]["Memory"])

    def test_inspect_accepts_only_exact_owned_binds_and_bounded_tmpfs(self):
        item = inspected_container(self.plan, "runner", running=True)
        self.scratch([(200, item)]).inspect_container("runner", running=True)
        changes = [lambda i: i["HostConfig"].update(Privileged=True),
                   lambda i: i["HostConfig"].update(PidsLimit=-1),
                   lambda i: i["HostConfig"].update(Memory=0),
                   lambda i: i["Mounts"][0].update(RW=True),
                   lambda i: i["Config"]["Labels"].update({"orcastra.qa.vm_run": GATE}),
                   lambda i: i["Config"].update(Entrypoint=["/bin/sh"]),
                   lambda i: i["Config"]["Env"].append("DATABASE_URL=synthetic-foreign"),
                   lambda i: i["NetworkSettings"]["Networks"].update({"neighbor": {"NetworkID": "0" * 64}})]
        for change in changes:
            candidate = copy.deepcopy(item)
            change(candidate)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.scratch([(200, candidate)]).inspect_container("runner", running=True)

    def test_cgroup_v2_null_oom_flag_preserves_the_false_request_and_exact_runtime_readback(self):
        for role in ("postgres", "runner"):
            self.assertIs(guest.container_payload(self.plan, role, IDS["network"])["HostConfig"]["OomKillDisable"], False)
            for value in (None, False):
                with self.subTest(role=role, value=value):
                    item = inspected_container(self.plan, role, running=True)
                    item["HostConfig"]["OomKillDisable"] = value
                    self.scratch([(200, item)]).inspect_container(role, running=True)

    def test_oom_disabled_or_false_equivalent_invalid_types_cannot_pass_inspect(self):
        for role in ("postgres", "runner"):
            for value in (True, 0, 1, 0.0, 1.0, "false", "", [], {}):
                item = inspected_container(self.plan, role, running=True)
                item["HostConfig"]["OomKillDisable"] = value
                with self.subTest(role=role, value=value), self.assertRaises(ValueError):
                    self.scratch([(200, item)]).inspect_container(role, running=True)
            item = inspected_container(self.plan, role, running=True)
            item["HostConfig"].pop("OomKillDisable")
            with self.subTest(role=role, omitted_oom_field=True), self.assertRaises(ValueError):
                self.scratch([(200, item)]).inspect_container(role, running=True)

    def test_null_oom_flag_does_not_relax_other_security_fields_or_resource_limits(self):
        for field in ("Privileged", "AutoRemove", "PublishAllPorts", "Memory", "MemorySwap", "NanoCpus", "PidsLimit"):
            item = inspected_container(self.plan, "runner", running=True)
            item["HostConfig"].update({"OomKillDisable": None, field: None})
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.scratch([(200, item)]).inspect_container("runner", running=True)

    def test_shared_host_comparison_refuses_a_changed_oom_request_or_wrong_boolean_types(self):
        for expected in (True, None, 0, "false"):
            for actual in (None, False, True):
                with self.subTest(expected=expected, actual=actual):
                    self.assertIs(guest.host_config_value_matches("OomKillDisable", actual, expected), False)
        self.assertIs(guest.host_config_value_matches("Privileged", None, False), False)
        self.assertIs(guest.host_config_value_matches("Privileged", 0, False), False)
        self.assertIs(guest.host_config_value_matches("Memory", 1024.0, 1024), False)

    def test_prestart_docker_defaults_accept_empty_profile_and_absent_or_null_image_user(self):
        for image_user in ("absent", None, ""):
            for profile in ("", "docker-default"):
                with self.subTest(image_user=image_user, profile=profile):
                    item = inspected_container(self.plan, "postgres")
                    item["AppArmorProfile"] = profile
                    scratch = self.scratch([(200, item)])
                    if image_user == "absent":
                        scratch.image_configs["postgres"]["config"].pop("User")
                    else:
                        scratch.image_configs["postgres"]["config"]["User"] = image_user
                    self.assertEqual(item["Config"]["User"], "")
                    scratch.inspect_container("postgres", running=False)
        for actual_user in ("0", "0:0", "root", "1000:1000", None, False):
            item = inspected_container(self.plan, "postgres")
            item["Config"]["User"] = actual_user
            scratch = self.scratch([(200, item)])
            scratch.image_configs["postgres"]["config"].pop("User")
            with self.subTest(default_image_actual_user=actual_user), self.assertRaises(ValueError):
                scratch.inspect_container("postgres", running=False)

    def test_started_or_exited_container_requires_default_profile_and_unknown_profiles_fail(self):
        for status, running in (("running", True), ("exited", False)):
            item = inspected_container(self.plan, "runner", running=running)
            item["State"]["Status"] = status
            self.scratch([(200, item)]).inspect_container("runner", running=True if running else None)
            for profile in ("", None, "unconfined", "custom", [], False):
                with self.subTest(status=status, profile=profile):
                    candidate = copy.deepcopy(item)
                    candidate["AppArmorProfile"] = profile
                    with self.assertRaises(ValueError):
                        self.scratch([(200, candidate)]).inspect_container("runner", running=True if running else None)
        for profile in (None, "unconfined", "custom", [], False):
            item = inspected_container(self.plan, "postgres")
            item["AppArmorProfile"] = profile
            with self.subTest(prestart_profile=profile), self.assertRaises(ValueError):
                self.scratch([(200, item)]).inspect_container("postgres", running=False)

    def test_unknown_or_mismatched_lifecycle_never_receives_prestart_profile_allowance(self):
        for field, values in {"Status": (None, "", "paused", "restarting", "dead", "unknown", False),
                              "Running": (None, "false", 0, 1, True), "Pid": (None, False, 0.0, -1, 123)}.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    item = inspected_container(self.plan, "postgres")
                    item["AppArmorProfile"] = ""
                    item["State"][field] = value
                    with self.assertRaises(ValueError):
                        self.scratch([(200, item)]).inspect_container("postgres", running=False)
        for status, actual_running, expected_running in (("created", False, None), ("exited", False, False),
                                                          ("running", False, None), ("exited", True, None)):
            item = inspected_container(self.plan, "postgres", running=actual_running)
            item["State"]["Status"] = status
            with self.subTest(status=status, actual_running=actual_running, expected_running=expected_running), self.assertRaises(ValueError):
                self.scratch([(200, item)]).inspect_container("postgres", running=expected_running)

    def test_user_normalization_preserves_explicit_image_and_request_users_and_exact_commands(self):
        item = inspected_container(self.plan, "postgres")
        item["Config"]["User"] = "1000:1000"
        scratch = self.scratch([(200, item)])
        scratch.image_configs["postgres"]["config"]["User"] = "1000:1000"
        scratch.inspect_container("postgres", running=False)
        for actual_user in ("", None, "0", "root", [], False, 0):
            candidate = copy.deepcopy(item)
            candidate["Config"]["User"] = actual_user
            scratch = self.scratch([(200, candidate)])
            scratch.image_configs["postgres"]["config"]["User"] = "1000:1000"
            with self.subTest(explicit_image_user=actual_user), self.assertRaises(ValueError):
                scratch.inspect_container("postgres", running=False)
        item = inspected_container(self.plan, "runner")
        for actual_user in ("0:0", "", None, "0", "root", "1000:1000", [], False):
            candidate = copy.deepcopy(item)
            candidate["Config"]["User"] = actual_user
            scratch = self.scratch([(200, candidate)])
            scratch.image_configs["python"]["config"].pop("User")
            if actual_user == "0:0":
                scratch.inspect_container("runner", running=False)
            else:
                with self.subTest(explicit_request_user=actual_user), self.assertRaises(ValueError):
                    scratch.inspect_container("runner", running=False)
        for key, value in (("Entrypoint", []), ("Cmd", []), ("WorkingDir", None)):
            item = inspected_container(self.plan, "postgres")
            item["Config"][key] = value
            with self.subTest(exact_command_field=key), self.assertRaises(ValueError):
                self.scratch([(200, item)]).inspect_container("postgres", running=False)

    def test_generated_launch_is_offline_hash_locked_and_waits_for_verified_kernel(self):
        body = guest.launch_source(self.plan, self.plan["proposal_digest"]).decode()
        compile(body, "launch.py", "exec")
        for expected in ["--no-index", "--require-hashes", "--only-binary=:all:", "--approved-digest", "/evidence/execute"]:
            self.assertIn(expected, body)
        for denied in ["docker.sock", "http://", "https://", "DATABASE_URL=", "shell=True"]:
            self.assertNotIn(denied, body)

    def launch_fixture(self, *, released=True, native_failure=False):
        plan = dict(self.plan)
        runner_body = b"Synthetic bounded runner source"
        plan["runner_sha256"] = hashlib.sha256(runner_body).hexdigest()
        commands = []
        logs = []

        class FakePath:
            def __init__(self, name):
                self.name = name

            def exists(self):
                if self.name != "/evidence/execute":
                    raise AssertionError("Unexpected bootstrap file lookup")
                return released

            def read_bytes(self):
                if self.name == "/runner/phase_b_pg_runner.py":
                    return runner_body
                if self.name == "/evidence/execute":
                    return plan["gate_id"].encode()
                raise AssertionError("Unexpected bootstrap file read")

        def open_log(name, mode):
            self.assertEqual(mode, "xb")
            self.assertTrue(name.startswith("/evidence/"))
            logs.append(name)
            return io.BytesIO()

        def run_command(arguments, **kwargs):
            commands.append(arguments)
            self.assertNotIn("DATABASE_URL", kwargs["env"])
            self.assertGreater(kwargs["timeout"], 0)
            return SimpleNamespace(returncode=int(native_failure and "-c" in arguments))

        clock = Mock(return_value=0) if released else Mock(side_effect=[0, 0, 61])
        replacements = {"pathlib": SimpleNamespace(Path=FakePath), "os": SimpleNamespace(umask=Mock()),
                        "time": SimpleNamespace(monotonic=clock, sleep=Mock()),
                        "subprocess": SimpleNamespace(run=run_command, STDOUT=object(), DEVNULL=object())}

        def imports(name, *arguments, **kwargs):
            return replacements[name] if name in replacements else builtins.__import__(name, *arguments, **kwargs)

        namespace = {"__builtins__": {**vars(builtins), "__import__": imports, "open": open_log}}
        body = guest.launch_source(plan, plan["proposal_digest"])
        return body, namespace, commands, logs

    def test_bootstrap_native_import_smoke_precedes_every_database_runner_command(self):
        body, namespace, commands, logs = self.launch_fixture()
        exec(compile(body, "synthetic-offline-launch.py", "exec"), namespace)  # noqa: S102
        self.assertEqual(len(commands), 5)
        smoke = commands[3]
        self.assertEqual(smoke[:2], ["/opt/qa-venv/bin/python", "-c"])
        self.assertIn("from asyncpg.protocol import protocol", smoke[2])
        self.assertIn("from asyncpg.pgproto import pgproto", smoke[2])
        self.assertIn("EXTENSION_SUFFIXES", smoke[2])
        self.assertEqual(commands[-1][1], "/runner/phase_b_pg_runner.py")
        self.assertEqual(logs[3], "/evidence/native-import-private.log")

    def test_failed_native_import_cannot_start_the_database_runner(self):
        body, namespace, commands, logs = self.launch_fixture(native_failure=True)
        with self.assertRaises(RuntimeError):
            exec(compile(body, "synthetic-offline-launch.py", "exec"), namespace)  # noqa: S102
        self.assertEqual(len(commands), 4)
        self.assertFalse(any("/runner/phase_b_pg_runner.py" in command for command in commands))
        self.assertNotIn("/evidence/runner-summary-private.json", logs)

    def test_native_smoke_refuses_pure_python_stand_ins_for_the_pinned_extensions(self):
        body, namespace, commands, _ = self.launch_fixture()
        exec(compile(body, "synthetic-offline-launch.py", "exec"), namespace)  # noqa: S102
        script = commands[3][2]
        for suffix in (".so", ".py"):
            modules = {"asyncpg.protocol": SimpleNamespace(protocol=SimpleNamespace(__file__="protocol" + suffix)),
                       "asyncpg.pgproto": SimpleNamespace(pgproto=SimpleNamespace(__file__="pgproto" + suffix))}

            def imports(name, *arguments, selected=modules, **kwargs):
                return selected[name] if name in selected else builtins.__import__(name, *arguments, **kwargs)

            output = Mock()
            child = {"__builtins__": {**vars(builtins), "__import__": imports, "print": output}}
            if suffix == ".so":
                exec(compile(script, "synthetic-native-smoke.py", "exec"), child)  # noqa: S102
                output.assert_called_once_with("Native asyncpg imports verified")
            else:
                with self.assertRaises(AssertionError):
                    exec(compile(script, "synthetic-native-smoke.py", "exec"), child)  # noqa: S102
                output.assert_not_called()

    def test_no_tooling_or_native_import_runs_until_kernel_release(self):
        body, namespace, commands, logs = self.launch_fixture(released=False)
        with self.assertRaises(TimeoutError):
            exec(compile(body, "synthetic-offline-launch.py", "exec"), namespace)  # noqa: S102
        self.assertEqual(commands, [])
        self.assertEqual(logs, [])

    def test_owned_runner_kernel_mount_readback_reinspects_the_same_pid(self):
        parent = "/orcastra.slice/orcastra-qa.slice/" + guest.LAB + ".slice"
        proc = {"cgroup": "0::" + parent + "/docker-" + IDS["runner"] + ".scope",
                "status": "NoNewPrivs:\t1\nSeccomp:\t2\nCapEff:\t0000000000000000",
                "attr/current": "docker-default (enforce)", "mountinfo": runner_mountinfo()}
        budget = self.plan["resource_budget"]
        limits = {"memory": budget["runner_memory_mib"] * 1024 ** 2, "tasks": budget["runner_pids"],
                  "quota": int(budget["runner_cpus"] * 100000), "period": 100000}
        for final_pid in (123, 124):
            first = inspected_container(self.plan, "runner", running=True)
            last = copy.deepcopy(first)
            last["State"]["Pid"] = final_pid
            scratch = self.scratch([(200, first), (200, last)])
            scratch.b1 = Mock(validated_cgroup=b1.validated_cgroup)
            scratch.b1.controller_limits.return_value = limits
            scratch.runtime = Mock()
            scratch.runtime.command.return_value = "ControlGroup=" + parent + "\n"
            scratch.record = Mock()
            with patch.object(scratch, "process_file", side_effect=lambda pid, name: proc[name]) as reader:
                if final_pid == 123:
                    scratch.verify_container_kernel("runner")
                else:
                    with self.assertRaises(ValueError):
                        scratch.verify_container_kernel("runner")
                self.assertIn(unittest.mock.call(123, "mountinfo"), reader.call_args_list)
            verified = [call for call in scratch.record.call_args_list if call.args[0] == "container_kernel_verified"]
            self.assertEqual(len(verified), int(final_pid == 123))
            self.assertEqual(len(scratch.transport.calls), 2)

    def test_actual_process_budget_and_security_must_match_not_only_inspect_flags(self):
        item = inspected_container(self.plan, "postgres", running=True)
        parent = "/orcastra.slice/orcastra-qa.slice/" + guest.LAB + ".slice"
        group = parent + "/docker-" + IDS["postgres"] + ".scope"
        limits = {"memory": 512 * 1024 ** 2, "tasks": 256, "quota": 50000, "period": 100000}
        proc = {"cgroup": "0::" + group, "status": "NoNewPrivs:\t1\nSeccomp:\t2\nCapEff:\t0000000000000000",
                "attr/current": "docker-default (enforce)"}
        for wrong in [None, "memory", "tasks", "quota", "Seccomp", "AppArmor", "parent"]:
            scratch = self.scratch([(200, item)])
            scratch.b1 = Mock(validated_cgroup=b1.validated_cgroup)
            changed_limits = dict(limits)
            changed_proc = dict(proc)
            if wrong in {"memory", "tasks", "quota"}:
                changed_limits[wrong] = None
            elif wrong == "Seccomp":
                changed_proc["status"] = changed_proc["status"].replace("Seccomp:\t2", "Seccomp:\t0")
            elif wrong == "AppArmor":
                changed_proc["attr/current"] = "unconfined"
            elif wrong == "parent":
                changed_proc["cgroup"] = "0::/other.slice/docker-" + IDS["postgres"] + ".scope"
            scratch.b1.controller_limits.return_value = changed_limits
            scratch.runtime = Mock()
            scratch.runtime.command.return_value = "ControlGroup=" + parent + "\n"
            with patch.object(scratch, "process_file", side_effect=lambda _pid, name, values=changed_proc: values[name]):
                if wrong is None:
                    scratch.verify_container_kernel("postgres")
                else:
                    with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                        scratch.verify_container_kernel("postgres")


class RunnerMountBoundary(unittest.TestCase):
    def test_effective_executable_mount_omits_noexec_and_retains_device_and_suid_guards(self):
        guest.require_runner_mounts(runner_mountinfo())

    def test_docker_default_noexec_venv_refuses_the_first_native_import_boundary(self):
        with self.assertRaises(ValueError):
            guest.require_runner_mounts(runner_mountinfo("rw,nosuid,nodev,noexec,relatime"))

    def test_writable_temporary_directory_must_keep_noexec(self):
        with self.assertRaises(ValueError):
            guest.require_runner_mounts(runner_mountinfo(tmp_flags="rw,nosuid,nodev,relatime"))

    def test_security_flags_must_be_effective_mount_flags_not_superblock_claims(self):
        for role, flags in (("venv", "rw,nodev"), ("venv", "rw,nosuid"), ("tmp", "rw,nodev,noexec"),
                            ("tmp", "rw,nosuid,noexec"), ("venv", "rw,nosuid,nodev,ro"),
                            ("venv", "rw,nosuid,nodev,suid"), ("venv", "rw,nosuid,nodev,dev"),
                            ("venv", "rw,rw,nosuid,nodev"), ("tmp", "rw,nosuid,nodev,exec,noexec")):
            body = runner_mountinfo(**{"venv_flags" if role == "venv" else "tmp_flags": flags})
            body = body.replace("- tmpfs tmpfs rw,", "- tmpfs tmpfs rw,nosuid,nodev,noexec,")
            with self.subTest(role=role, flags=flags), self.assertRaises(ValueError):
                guest.require_runner_mounts(body)

    def test_missing_duplicate_obscured_or_foreign_mount_is_refused(self):
        body = runner_mountinfo()
        changed = ["\n".join(body.splitlines()[:-1]), body + body.splitlines()[1] + "\n",
                   body.replace("/opt/qa-venv ", "/opt/qa-venv-foreign "),
                   body.replace("/ /opt/qa-venv", "/foreign /opt/qa-venv"),
                   body.replace("- tmpfs tmpfs", "- ext4 /dev/synthetic", 1),
                   body + "103 101 0:13 / /opt/qa-venv/lib rw - tmpfs tmpfs rw\n",
                   body + "103 102 0:13 / /tmp/child rw - tmpfs tmpfs rw\n"]
        for value in changed:
            with self.subTest(body=value), self.assertRaises(ValueError):
                guest.require_runner_mounts(value)

    def test_malformed_or_oversized_kernel_mount_data_is_refused(self):
        for value in (None, b"synthetic", "\x00", "synthetic\n", "X" * (64 * 1024 + 1),
                      "X" * 4097, "\n" * 257, runner_mountinfo().replace(" - tmpfs", " tmpfs", 1),
                      runner_mountinfo().replace("0:11", "invalid"), runner_mountinfo().replace("101 100", "x 100")):
            with self.subTest(kind=type(value).__name__), self.assertRaises(ValueError):
                guest.require_runner_mounts(value)

    def test_proc_reader_keeps_closed_pid_names_and_bounded_regular_no_symlink_reads(self):
        with tempfile.TemporaryDirectory(prefix=".pg-proc-contract-", dir=Path.cwd()) as directory:
            root = Path(directory)
            process = root / "123"
            process.mkdir()
            (process / "mountinfo").write_text("x" * (64 * 1024))
            (process / "status").write_text("x" * 8193)
            original_open = os.open

            def open_fixture(path, *arguments, **kwargs):
                return original_open(root if path == "/proc" else path, *arguments, **kwargs)

            with patch.object(guest.os, "open", side_effect=open_fixture):
                self.assertEqual(len(guest.ScratchGate.process_file(123, "mountinfo")), 64 * 1024)
                with self.assertRaises(ValueError):
                    guest.ScratchGate.process_file(123, "status")
                (process / "mountinfo").write_text("x" * (64 * 1024 + 1))
                with self.assertRaises(ValueError):
                    guest.ScratchGate.process_file(123, "mountinfo")
                (process / "mountinfo").unlink()
                (process / "mountinfo").symlink_to(process / "status")
                with self.assertRaises(OSError):
                    guest.ScratchGate.process_file(123, "mountinfo")
        with patch.object(guest.os, "open") as opener:
            for pid, name in ((True, "mountinfo"), (1, "mountinfo"), (0, "mountinfo"), ("123", "mountinfo"),
                              (123, "environ"), (123, "../mountinfo")):
                with self.subTest(pid=pid, name=name), self.assertRaises(ValueError):
                    guest.ScratchGate.process_file(pid, name)
            opener.assert_not_called()


class LedgerBoundary(unittest.TestCase):
    def test_rejected_native_execution_mount_cannot_release_runner_marker(self):
        plan = ready_plan()
        item = inspected_container(plan, "runner", running=True)
        transport = FakeTransport([(200, item)])
        parent = "/orcastra.slice/orcastra-qa.slice/" + guest.LAB + ".slice"
        proc = {"cgroup": "0::" + parent + "/docker-" + IDS["runner"] + ".scope",
                "status": "NoNewPrivs:\t1\nSeccomp:\t2\nCapEff:\t0000000000000000",
                "attr/current": "docker-default (enforce)",
                "mountinfo": runner_mountinfo("rw,nosuid,nodev,noexec,relatime")}
        budget = plan["resource_budget"]
        limits = {"memory": budget["runner_memory_mib"] * 1024 ** 2, "tasks": budget["runner_pids"],
                  "quota": int(budget["runner_cpus"] * 100000), "period": 100000}
        with tempfile.TemporaryDirectory(prefix=".pg-release-contract-", dir=Path.cwd()) as directory:
            root = Path(directory)
            (root / "artifacts").mkdir(mode=0o700)
            scratch = guest.ScratchGate(plan, plan["proposal_digest"], "test", transport=transport)
            scratch.created = dict(IDS)
            scratch.image_configs = image_configs()
            scratch.b1 = Mock(validated_cgroup=b1.validated_cgroup)
            scratch.b1.controller_limits.return_value = limits
            scratch.runtime = Mock()
            scratch.runtime.command.return_value = "ControlGroup=" + parent + "\n"
            actual_verify = scratch.verify_container_kernel

            def prepare():
                (root / "pg").mkdir(mode=0o700)
                scratch.root.mkdir(mode=0o700)
                (scratch.root / "evidence").mkdir(mode=0o700)

            def verify(role):
                if role == "runner":
                    actual_verify(role)

            with patch.object(guest, "ROOT", root), patch.object(guest, "os", OwnedGuestMetadata(root)):
                scratch.root = root / "pg" / GATE
                with patch.object(scratch, "preflight"), patch.object(scratch, "verify_payloads"), \
                     patch.object(scratch, "prepare_paths", side_effect=prepare), patch.object(scratch, "load_images"), \
                     patch.object(scratch, "create_network"), patch.object(scratch, "create_container"), \
                     patch.object(scratch, "wait_container") as waiter, \
                     patch.object(scratch, "verify_container_kernel", side_effect=verify), \
                     patch.object(scratch, "process_file", side_effect=lambda pid, name: proc[name]), \
                     self.assertRaises(ValueError):
                    scratch.execute()
                waiter.assert_called_once_with("postgres")
                self.assertFalse((scratch.root / "evidence" / "execute").exists())
                records = [json.loads(line) for line in (root / "artifacts" / ("pg-" + GATE + ".jsonl")).read_text().splitlines()]
                self.assertFalse(any(row.get("resource") == "runner_execute_marker" for row in records))
                self.assertEqual(records[-1]["event"], "stopped")
                self.assertEqual(records[-1]["resource_ids"], IDS)

    def test_accepted_container_id_survives_wait_timeout_without_retry_or_cleanup(self):
        plan = ready_plan()
        prestart = inspected_container(plan, "postgres")
        prestart["AppArmorProfile"] = ""
        transport = FakeTransport([(201, {"Id": IDS["postgres"], "Warnings": []}),
                                   (200, prestart), (204, None)])
        with tempfile.TemporaryDirectory(prefix=".pg-contract-", dir=Path.cwd()) as directory:
            root = Path(directory)
            (root / "artifacts").mkdir(mode=0o700)
            scratch = guest.ScratchGate(plan, plan["proposal_digest"], "test", transport=transport)
            with patch.object(guest, "ROOT", root), patch.object(guest, "os", OwnedGuestMetadata(root)):
                scratch.root = root / "pg" / GATE
                scratch.image_configs = image_configs()
                scratch.image_configs["postgres"]["config"].pop("User")

                def prepare():
                    (root / "pg").mkdir(mode=0o700)
                    scratch.root.mkdir(mode=0o700)
                    (scratch.root / "evidence").mkdir(mode=0o700)

                def network():
                    scratch.created["network"] = IDS["network"]

                with patch.object(scratch, "preflight"), patch.object(scratch, "verify_payloads"), \
                     patch.object(scratch, "prepare_paths", side_effect=prepare), patch.object(scratch, "load_images"), \
                     patch.object(scratch, "create_network", side_effect=network), \
                     patch.object(scratch, "wait_container", side_effect=TimeoutError("synthetic private timeout")), \
                     self.assertRaises(TimeoutError):
                    scratch.execute()
                records = [json.loads(line) for line in (root / "artifacts" / ("pg-" + GATE + ".jsonl")).read_text().splitlines()]
                accepted = next(row for row in records if row["event"] == "resource_accepted")
                self.assertEqual(accepted["resource_id"], IDS["postgres"])
                self.assertEqual(records[-1]["event"], "stopped")
                self.assertEqual(records[-1]["resource_ids"]["postgres"], IDS["postgres"])
                self.assertEqual([method for method, _, _ in transport.calls], ["POST", "GET", "POST"])
                self.assertFalse(any("remove" in path or "prune" in path or "stop" in path for _, path, _ in transport.calls))
                self.assertNotIn("synthetic private timeout", json.dumps(records))

    def test_post_timeout_is_not_retried_or_misreported_as_success(self):
        plan = ready_plan()
        transport = FakeTransport([TimeoutError("private synthetic body")])
        scratch = guest.ScratchGate(plan, plan["proposal_digest"], "test", transport=transport)
        with self.assertRaises(TimeoutError):
            scratch.api("POST", "/networks/create", body=guest.network_payload(plan), expected=201, accept="network")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(scratch.created, {})

    def test_total_wall_deadline_is_finite_and_stops_before_new_request(self):
        plan = ready_plan()
        transport = FakeTransport([])
        clock = Mock(side_effect=[0, guest.TIMEOUTS["total_seconds"] + 1])
        scratch = guest.ScratchGate(plan, plan["proposal_digest"], "test", transport=transport, clock=clock)
        with self.assertRaises(TimeoutError):
            scratch.api("POST", "/networks/create")
        self.assertEqual(transport.calls, [])


class UnixTransportBoundary(unittest.TestCase):
    def test_transport_uses_only_the_fixed_owned_unix_socket(self):
        connection = guest.UnixConnection("localhost", timeout=3)
        channel = Mock()
        with patch.object(guest.socket, "socket", return_value=channel) as created:
            connection.connect()
        created.assert_called_once_with(guest.socket.AF_UNIX, guest.socket.SOCK_STREAM)
        channel.connect.assert_called_once_with(str(guest.ROOT / "docker.sock"))
        channel.settimeout.assert_called_once_with(3)

    def test_complete_api_deadline_is_restored_after_a_progressing_request(self):
        with patch.object(guest.signal, "getitimer", return_value=(0.0, 0.0)), \
             patch.object(guest.signal, "getsignal", return_value=guest.signal.SIG_DFL), \
             patch.object(guest.signal, "signal") as handler, patch.object(guest.signal, "setitimer") as timer:
            with guest.request_deadline(3):
                alarm = handler.call_args.args[1]
                with self.assertRaises(TimeoutError):
                    alarm(guest.signal.SIGALRM, None)
            self.assertEqual(timer.call_args_list[0].args, (guest.signal.ITIMER_REAL, 3))
            self.assertEqual(timer.call_args_list[-1].args, (guest.signal.ITIMER_REAL, 0))
            self.assertEqual(handler.call_args.args, (guest.signal.SIGALRM, guest.signal.SIG_DFL))

    def test_redirect_body_is_not_followed_and_oversized_response_is_refused(self):
        connection = Mock()
        response = Mock(status=302)
        response.read.return_value = b'{"Location":"https://synthetic.invalid"}'
        connection.getresponse.return_value = response
        with patch.object(guest, "UnixConnection", return_value=connection), patch.object(guest, "request_deadline"):
            status, _ = guest.UnixDocker().request("GET", "/info")
        self.assertEqual(status, 302)
        self.assertEqual(connection.request.call_count, 1)
        connection.close.assert_called_once()
        response.read.return_value = b"x" * (guest.MAX_RESPONSE + 1)
        with patch.object(guest, "UnixConnection", return_value=connection), patch.object(guest, "request_deadline"), \
             self.assertRaises(ValueError):
            guest.UnixDocker().request("GET", "/info")


class EvidenceBoundary(unittest.TestCase):
    def result(self):
        snapshot = {"version": guest.HEAD, "objects": dict.fromkeys(guest.OBJECTS, True),
                    "journal_row_counts": dict.fromkeys(guest.OBJECTS[:3], 0), "column_schema_sha256": "e" * 64,
                    "schema_contract_sha256": "d" * 64}
        result = {"phase": "B-PG", "gate_id": GATE, "status": "journal_and_migration_verified",
                  "tests": {"collected": 14, "passed": 14, "failure": 0, "error": 0, "skipped": 0,
                            "test_names": sorted(NAMES), "duration_seconds": 3.0},
                  **{key: copy.deepcopy(snapshot) for key in ["journal_before", "journal_after", "migration_before", "migration_after"]},
                  "scope_limit": "Public column/default, constraint, index and sequence configuration equality plus journal objects; excludes physical state, sequence current values, grants and data restoration"}
        result["migration_down"] = {**snapshot, "version": guest.DOWNGRADE, "objects": dict.fromkeys(guest.OBJECTS, False),
                                    "journal_row_counts": {}}
        return result

    def test_complete_real_gate_shape_exports_only_allowlisted_counts_booleans_and_hashes(self):
        summary = guest.sanitized_evidence(self.result(), ready_plan())
        self.assertIs(summary["journal_fixture_clean"], True)
        self.assertIs(summary["migration_column_schema_restored"], True)
        self.assertNotIn("journal_before", summary)
        self.assertNotIn("DATABASE_URL", json.dumps(summary))

    def test_missing_test_schema_drift_rows_or_secret_field_blocks_success(self):
        mutations = [lambda r: r["tests"].update(passed=13, skipped=1), lambda r: r["tests"].update(duration_seconds=float("nan")),
                     lambda r: r["migration_after"].update(column_schema_sha256="f" * 64),
                     lambda r: r["migration_after"].update(schema_contract_sha256="f" * 64),
                     lambda r: r["journal_after"]["journal_row_counts"].update(task_journal=1),
                     lambda r: r["migration_down"]["objects"].update(task_journal=True),
                     lambda r: r.update(database_url="synthetic-private-url"),
                     lambda r: r["tests"].update(password="synthetic-private-password")]
        for mutation in mutations:
            result = self.result()
            mutation(result)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                guest.sanitized_evidence(result, ready_plan())


if __name__ == "__main__":
    unittest.main()
