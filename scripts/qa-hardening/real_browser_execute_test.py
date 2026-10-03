"""Hermetic browser executor boundaries. No live resources or real credentials."""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent


def module(filename):
    name = "browser_test_" + filename.removesuffix(".py")
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


CORE = module("real_browser_execute.py")
RUNTIME = module("real_browser_runtime.py")
PG = module("phase_b_pg_guest.py")
RUN = "2f15fbaa-220f-4c65-bae2-7f0ec6792061"
B2 = "60c5a8c1-5e76-43b2-813b-21c448c00848"
AUTHORITY = "synthetic unit-test authority; never a live execution grant"


def actual_schema_fixture():
    ids = {name: format(index, "064x") for index, name in enumerate((*CORE.SERVICES, "network"), start=1)}
    images = {"frontend": "sha256:" + "a" * 64, "backend": "sha256:" + "b" * 64, "authentik": "sha256:" + "c" * 64}
    result = {"phase": "B2", "phase_b_id": B2, "status": "controller_configured_readiness_verified", "proposal_digest": "d" * 64,
              "resource_ids": ids, "image_ids": images, "source_tree_sha256": "e" * 64,
              "vault_configuration_verified": True, "idp_configuration_verified": True, "readiness_verified": True,
              "kernel_limits_verified": True, "sso_login_verified": False, "jwt_runtime_verified": False,
              "production_dockerfile_parity": False, "remaining_acceptance": [
                  "Per-role browser SSO login/logout and JWT runtime acceptance", "Real hypervisor resource behavior"]}
    outer = {"schema_version": 1, "closed": True, "operation_id": "b8a9c853-c50a-4584-a204-58a5c982c5eb", "exit_code": 0,
             "target": {"vm": CORE.LAB, "project": "default", "vm_run_id": CORE.VM_RUN}, "b2_id": B2,
             "proposal_digest": result["proposal_digest"], "result_sha256": CORE.sha(CORE.canonical(result)),
             "source_tree_sha256": result["source_tree_sha256"], "controller_helper_sha256": "f" * 64,
             "authorization_reference_sha256": CORE.sha(AUTHORITY.encode()),
             "resource_ids": {name: ids[name] for name in CORE.UPSTREAMS},
             "network": {"id": ids["network"], "name": CORE.LAB + "-application-" + B2[:8], "internal": True,
                         "published_ports": [], "labels": CORE.labels(B2)},
             "image_ids": {"frontend": images["frontend"], "backend": images["backend"], "authentik-server": images["authentik"]}}
    return result, outer


def proof_fixture(npm=None, image_body=b"synthetic opaque image, never loaded"):
    npm = npm or {name: b"synthetic npm fixture " + name.encode() for name in CORE.NPM}
    return {"schema_version": 1, "closed": True, "control_sha256": CORE.CONTROL,
            "image": {"reference": CORE.IMAGE, "manifest_digest": CORE.IMAGE_MANIFEST, "config_digest": CORE.IMAGE_CONFIG,
                      "archive_sha256": CORE.sha(image_body), "archive_verified": True, "config_verified": True, "diff_ids": list(CORE.DIFF_IDS)},
            "npm": [{"name": name, "version": "1.51.1", "sha256": CORE.sha(body), "integrity": CORE.NPM[name], "verified": True}
                    for name, body in npm.items()]}


def runtime_fixture():
    result, outer = actual_schema_fixture()
    adapter = CORE.adapter_from_result(result, outer)
    plan = CORE.runtime_plan(adapter, CORE.payload_contract(proof_fixture()), RUN)
    manifest = {"run_id": RUN, "plan": plan, "proposal_digest": CORE.sha(CORE.canonical(plan)), "helpers": {"real_browser_launch.cjs": "8" * 64},
                "files": {"image": {"sha256": "7" * 64, "size": 123}}}
    instance = RUNTIME.BrowserRun(manifest, adapter, {}, {}, {}, io.BytesIO(), (), {"config": {}}, lambda seconds=30: seconds,
                                  Mock(), Mock(), PG, CORE)
    instance.api = Mock()
    return instance


def inspect_fixture(instance, running=False):
    instance.identifier = "9" * 64
    instance.payload = instance.container_payload()
    payload = instance.payload
    config = {key: copy.deepcopy(payload[key]) for key in ("Labels", "Entrypoint", "Cmd", "User", "WorkingDir", "Env")}
    mounts = [{"Destination": item["Target"], "Source": item["Source"], "Type": "bind", "RW": not item["ReadOnly"], "Propagation": "rprivate"}
              for item in payload["HostConfig"]["Mounts"]]
    return {"Id": instance.identifier, "Image": CORE.IMAGE_CONFIG, "Config": config, "HostConfig": copy.deepcopy(payload["HostConfig"]),
            "State": {"Status": "running" if running else "created", "Running": running, "Pid": 200 if running else 0,
                      "OOMKilled": False, "Dead": False, "Error": "", "ExitCode": 0}, "AppArmorProfile": "docker-default" if running else "",
            "Mounts": mounts, "NetworkSettings": {"Networks": {instance.adapter["network"]["name"]: {
                "NetworkID": instance.adapter["network"]["id"] if running else "", "Aliases": ["browser-" + instance.run]}}, "Ports": {}}}


def successful_results():
    result = {"schema_version": 1, "lab": CORE.LAB, "results": [
        {"case_id": name, "method": row[0], "path": row[1], "status": row[2], "assertion": True} for name, row in RUNTIME.CASES.items()]}
    rows = [{"event": "ledger_started"}, {"event": "organization_created", "organization_id": 11},
            {"event": "organization_created", "organization_id": 12}, {"event": "member_created", "member_id": 21, "organization_id": 11},
            {"event": "member_created", "member_id": 22, "organization_id": 11}, {"event": "member_created", "member_id": 23, "organization_id": 12}]
    return result, b"\n".join(CORE.canonical(row) for row in rows) + b"\n"


def tar_bytes(entries, gzip=False):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz" if gzip else "w") as archive:
        for entry in entries:
            name, body, *kind = entry
            info = tarfile.TarInfo(name)
            info.size = len(body)
            if kind:
                info.type = kind[0]
                info.linkname = "outside"
                info.size = 0
            archive.addfile(info, io.BytesIO(body) if info.isfile() else None)
    return output.getvalue()


class AdapterTests(unittest.TestCase):
    def test_uuid_identity_and_full_canonical_operation_are_separately_bound(self):
        result, outer = actual_schema_fixture()
        adapter = CORE.adapter_from_result(result, outer)
        self.assertEqual(adapter["operation_id"], outer["operation_id"])
        self.assertEqual(adapter["accepted_operation_reference_sha256"], CORE.sha(CORE.canonical(outer)))
        self.assertFalse(result["sso_login_verified"])
        self.assertFalse(result["jwt_runtime_verified"])

    def test_readiness_does_not_become_browser_acceptance(self):
        for field in ("readiness_verified", "kernel_limits_verified", "vault_configuration_verified", "idp_configuration_verified"):
            with self.subTest(field=field):
                result, outer = actual_schema_fixture()
                result[field] = False
                with self.assertRaises(ValueError):
                    CORE.adapter_from_result(result, outer)
        for field in ("sso_login_verified", "jwt_runtime_verified", "production_dockerfile_parity"):
            with self.subTest(field=field):
                result, outer = actual_schema_fixture()
                result[field] = True
                with self.assertRaises(ValueError):
                    CORE.adapter_from_result(result, outer)

    def test_neighbor_id_network_or_authority_widening_is_refused(self):
        mutations = [lambda outer: outer["resource_ids"].update(frontend="9" * 64),
                     lambda outer: outer["network"].update(internal=False), lambda outer: outer["network"]["labels"].update({"orcastra.qa.vm_run": RUN}),
                     lambda outer: outer.update(operation_id="not-an-operation"), lambda outer: outer.update(exit_code=1),
                     lambda outer: outer["network"].update(published_ports=[32025]), lambda outer: outer.update(closed=False),
                     lambda outer: outer.update(unreviewed=True)]
        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                result, outer = actual_schema_fixture()
                mutation(outer)
                with self.assertRaises(ValueError):
                    CORE.adapter_from_result(result, outer)

    def test_two_gib_and_source_proven_ports_are_part_of_the_plan_digest(self):
        result, outer = actual_schema_fixture()
        plan = CORE.runtime_plan(CORE.adapter_from_result(result, outer), CORE.payload_contract(proof_fixture()), RUN)
        self.assertEqual(plan["memory_bytes"], 2 * 1024**3)
        self.assertEqual(plan["proxies"], [[32025, "frontend", 2025], [32405, "backend", 4050], [32090, "authentik-server", 32090]])
        widened = copy.deepcopy(plan)
        widened["proxies"][0][2] = 3000
        self.assertNotEqual(CORE.sha(CORE.canonical(plan)), CORE.sha(CORE.canonical(widened)))

    def test_payload_proof_requires_exact_official_pins_and_three_unique_npm_rows(self):
        mutations = [lambda proof: proof["image"].update(config_digest="sha256:" + "f" * 64),
                     lambda proof: proof["image"].update(archive_verified=False), lambda proof: proof["npm"].append(proof["npm"][0]),
                     lambda proof: proof["npm"][0].update(version="1.52.0"), lambda proof: proof["npm"][0].update(integrity="sha512-unverified")]
        for mutation in mutations:
            proof = proof_fixture()
            mutation(proof)
            with self.assertRaises(ValueError):
                CORE.payload_contract(proof)

    def test_duplicate_and_non_finite_json_are_not_proofs(self):
        for body in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}'):
            with self.assertRaises(ValueError):
                CORE.strict_json(body)

    def test_defaults_do_not_touch_files_sockets_or_credentials(self):
        with (
            patch.object(CORE, "owned_bytes", side_effect=AssertionError("read forbidden")),
            patch.object(CORE, "execute", side_effect=AssertionError("execute forbidden")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(CORE.main(["--manifest", "/nonexistent-private-file"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"status": "inert", "live": False, "runtime_pending": True})


class FixtureOwnership:
    """Project only synthetic guest ownership; retain actual file safety metadata.

    CI does not need root or CAP_CHOWN. The fixture's ancestors represent the
    private guest root; unsafe descendants keep their real modes and types.
    """

    def __init__(self, root):
        self.root = root
        self.owners = {}
        self.requests = []
        self.host_uid, self.host_gid = os.getuid(), os.getgid()
        self.original_lstat = Path.lstat
        self.os = SimpleNamespace(**{name: getattr(os, name) for name in dir(os)})
        self.os.fstat, self.os.chown, self.os.fchown = self.fstat, self.chown, self.fchown

    def metadata(self, path, actual):
        path = Path(path)
        if not path.is_relative_to(self.root) and path not in self.root.parents:
            return actual
        value = SimpleNamespace(**{name: getattr(actual, name) for name in dir(actual) if name.startswith("st_")})
        if path in self.root.parents:
            value.st_uid, value.st_gid = 0, 0
            value.st_mode = stat.S_IFMT(actual.st_mode) | 0o755
        else:
            default = (0, 0) if (actual.st_uid, actual.st_gid) == (self.host_uid, self.host_gid) else (actual.st_uid, actual.st_gid)
            value.st_uid, value.st_gid = self.owners.get((actual.st_dev, actual.st_ino), default)
        return value

    def lstat(self, path, *args, **kwargs):
        return self.metadata(path, self.original_lstat(path, *args, **kwargs))

    def fd_path(self, descriptor):
        return Path(os.readlink("/proc/self/fd/" + str(descriptor)))

    def fstat(self, descriptor):
        return self.metadata(self.fd_path(descriptor), os.fstat(descriptor))

    def chown(self, path, uid, gid):
        path = Path(path)
        if not path.is_relative_to(self.root) or (uid, gid) not in {(0, 0), (1000, 1000)}:
            raise AssertionError("Only synthetic guest-root or browser ownership may be requested")
        actual = path.stat()
        self.owners[(actual.st_dev, actual.st_ino)] = (uid, gid)
        self.requests.append((path, uid, gid))

    def fchown(self, descriptor, uid, gid):
        self.chown(self.fd_path(descriptor), uid, gid)


class PrivateFilesCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="browser-unit-")
        self.directory = Path(self.temporary.name)
        os.chmod(self.directory, 0o700)
        self.root_patch = patch.object(CORE, "ROOT", self.directory)
        self.root_patch.start()
        self.ownership = FixtureOwnership(self.directory)
        self.metadata_patches = [patch.object(CORE, "os", self.ownership.os), patch.object(RUNTIME, "os", self.ownership.os),
                                 patch.object(Path, "lstat", lambda path, *args, **kwargs: self.ownership.lstat(path, *args, **kwargs))]
        for fixture_patch in self.metadata_patches:
            fixture_patch.start()

    def tearDown(self):
        for fixture_patch in reversed(self.metadata_patches):
            fixture_patch.stop()
        self.root_patch.stop()
        self.temporary.cleanup()

    def file(self, name, body=b"bounded", mode=0o600):
        path = self.directory / name
        path.write_bytes(body)
        path.chmod(mode)
        return path


class FileAndArchiveTests(PrivateFilesCase):

    def test_private_owned_regular_read_and_fingerprint(self):
        path = self.file("input")
        self.assertEqual(CORE.owned_bytes(path, 100, mode=0o600), b"bounded")
        with self.assertRaises(ValueError):
            CORE.owned_bytes(path, 2)

    def test_projected_ownership_keeps_foreign_file_and_parent_denials(self):
        path = self.file("input")
        actual = path.stat()
        self.ownership.owners[(actual.st_dev, actual.st_ino)] = (1, 1)
        with self.assertRaises(ValueError):
            CORE.owned_bytes(path, 100, mode=0o600)
        self.ownership.owners[(actual.st_dev, actual.st_ino)] = (0, 0)
        parent = self.directory.stat()
        self.ownership.owners[(parent.st_dev, parent.st_ino)] = (1000, 1000)
        with self.assertRaises(ValueError):
            CORE.owned_bytes(path, 100, mode=0o600)
        self.assertEqual((actual.st_uid, actual.st_gid), (os.getuid(), os.getgid()))

    def test_symlink_hardlink_fifo_public_parent_and_external_path_are_refused(self):
        regular = self.file("target")
        os.symlink(regular, self.directory / "symlink")
        os.link(regular, self.directory / "hardlink")
        os.mkfifo(self.directory / "fifo", 0o600)
        for name in ("target", "symlink", "hardlink", "fifo"):
            with self.subTest(name=name), self.assertRaises((ValueError, OSError)):
                CORE.owned_bytes(self.directory / name, 100)
        child = self.directory / "public"
        child.mkdir(mode=0o777)
        child.chmod(0o777)
        (child / "data").write_bytes(b"bad")
        with self.assertRaises(ValueError):
            CORE.owned_bytes(child / "data", 100)
        with self.assertRaises(ValueError):
            CORE.owned_bytes(Path("/etc/passwd"), 100)

    def test_npm_traversal_links_duplicates_and_alias_paths_never_escape(self):
        bad_entries = [[("package/../../outside", b"bad")], [("package/file", b"", tarfile.SYMTYPE)],
                       [("package/file", b"one"), ("package/file", b"two")], [("package/./file", b"bad")], [("/package/file", b"bad")]]
        for index, entries in enumerate(bad_entries):
            with self.subTest(index=index), self.assertRaises((ValueError, FileExistsError)):
                CORE.extract_npm({"playwright": tar_bytes(entries, gzip=True)}, self.directory / ("node-" + str(index)), lambda: 30)
        self.assertFalse((self.directory / "outside").exists())

    def test_offline_extract_does_not_run_lifecycle_and_writes_read_only_files(self):
        body = tar_bytes([("package/package.json", b'{"scripts":{"postinstall":"must never execute"}}'), ("package/index.js", b"module.exports=1")], gzip=True)
        with patch("subprocess.run", side_effect=AssertionError("lifecycle forbidden")):
            CORE.extract_npm({"playwright": body}, self.directory / "node", lambda: 30)
        result = self.directory / "node/node_modules/playwright/index.js"
        self.assertEqual(result.read_bytes(), b"module.exports=1")
        self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o444)
        with self.assertRaises(FileExistsError):
            CORE.extract_npm({"playwright": body}, self.directory / "node", lambda: 30)

    def test_restrictive_umask_cannot_hide_public_offline_packages_from_browser_user(self):
        destination = self.directory / "node"
        body = tar_bytes([("package/lib/nested/index.js", b"module.exports=1")], gzip=True)
        previous = os.umask(0o077)
        try:
            CORE.extract_npm({"@playwright/test": body}, destination, lambda: 30)
        finally:
            os.umask(previous)
        current = destination
        self.assertEqual(stat.S_IMODE(current.stat().st_mode), 0o755)
        for part in ("node_modules", "@playwright", "test", "lib", "nested"):
            current = current / part
            self.assertEqual(stat.S_IMODE(current.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((current / "index.js").stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)

    def test_image_member_hashes_configuration_and_closed_archive_are_verified(self):
        layers = [b"synthetic layer " + str(index).encode() for index in range(4)]
        diffs = ["sha256:" + CORE.sha(body) for body in layers]
        config = CORE.canonical({"os": "linux", "architecture": "amd64", "rootfs": {"diff_ids": diffs}, "config": {}})
        image_id = "sha256:" + CORE.sha(config)
        names = [value[7:] + "/layer.tar" for value in diffs]
        manifest = CORE.canonical([{"Config": image_id[7:] + ".json", "RepoTags": [CORE.IMAGE], "Layers": names}])
        entries = [(image_id[7:] + ".json", config), ("manifest.json", manifest), *zip(names, layers, strict=True)]
        with patch.object(CORE, "IMAGE_CONFIG", image_id), patch.object(CORE, "DIFF_IDS", tuple(diffs)):
            self.assertEqual(CORE.image_archive(io.BytesIO(tar_bytes(entries)), lambda: 30)["architecture"], "amd64")
            for changed in ([*entries, ("foreign", b"bad")], [*entries, entries[-1]], [*entries[:-1], (names[-1], b"tampered")]):
                with self.assertRaises(ValueError):
                    CORE.image_archive(io.BytesIO(tar_bytes(changed)), lambda: 30)


class RuntimeBoundaryTests(PrivateFilesCase):
    def test_payload_has_one_owned_network_no_public_ports_and_only_evidence_writable(self):
        instance = runtime_fixture()
        payload = instance.container_payload()
        host = payload["HostConfig"]
        self.assertEqual(host["NetworkMode"], instance.adapter["network"]["id"])
        self.assertEqual(host["Memory"], 2 * 1024**3)
        self.assertEqual(host["MemorySwap"], 2 * 1024**3)
        self.assertEqual(host["NanoCpus"], 1_500_000_000)
        self.assertEqual(host["PidsLimit"], 1024)
        self.assertEqual(host["ShmSize"], 1024**3)
        self.assertEqual(host["PortBindings"], {})
        self.assertEqual(host["CapDrop"], ["ALL"])
        self.assertTrue(host["ReadonlyRootfs"])
        self.assertEqual(host["SecurityOpt"], ["no-new-privileges:true"])
        self.assertEqual(host["CgroupParent"], CORE.LAB + ".slice")
        self.assertEqual(host["CgroupnsMode"], "private")
        self.assertEqual([item["Target"] for item in host["Mounts"] if not item["ReadOnly"]], ["/qa"])
        self.assertNotIn("password", str(payload))
        self.assertNotIn("client_secret", str(payload))

    def test_prestart_empty_apparmor_only_accepts_created_false_pid_zero(self):
        for mutate, allowed in ((lambda item: None, True), (lambda item: item["State"].update(Status="exited"), False),
                                (lambda item: item["State"].update(Pid=False), False), (lambda item: item["State"].update(Pid=200), False),
                                (lambda item: item.update(AppArmorProfile="unconfined"), False)):
            instance = runtime_fixture()
            item = inspect_fixture(instance)
            mutate(item)
            instance.api.request.return_value = item
            if allowed:
                instance.inspect(running=False)
            else:
                with self.assertRaises(ValueError):
                    instance.inspect(running=False)

    def test_running_requires_default_apparmor_exact_limits_mounts_and_network(self):
        mutations = [lambda item: item.update(AppArmorProfile=""), lambda item: item["HostConfig"].update(Memory=4 * 1024**3),
                     lambda item: item["HostConfig"].update(NetworkMode="host"), lambda item: item["HostConfig"].update(CapAdd=["SYS_ADMIN"]),
                     lambda item: item["Mounts"][0].update(RW=True), lambda item: item["Mounts"].append({"Destination": "/other"}),
                     lambda item: item["NetworkSettings"]["Networks"].update(foreign={"NetworkID": "7" * 64}),
                     lambda item: item["NetworkSettings"]["Ports"].update({"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "80"}]}),
                     lambda item: item["Config"]["Env"].append("PRIVATE_TOKEN=unapproved")]
        good = runtime_fixture()
        good.api.request.return_value = inspect_fixture(good, True)
        good.inspect(running=True)
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                instance = runtime_fixture()
                item = inspect_fixture(instance, True)
                mutate(item)
                instance.api.request.return_value = item
                with self.assertRaises(ValueError):
                    instance.inspect(running=True)

    def test_bind_only_or_declared_tmpfs_inventory_preserves_the_exact_four_binds(self):
        for reported in (False, True):
            with self.subTest(tmpfs_reported=reported):
                instance = runtime_fixture()
                item = inspect_fixture(instance, True)
                if reported:
                    item["Mounts"].append({"Type": "tmpfs", "Source": "", "Destination": "/tmp", "Mode": "", "RW": True, "Propagation": ""})
                instance.api.request.return_value = item
                self.assertIs(instance.inspect(running=True), item)
                self.assertEqual(len([row for row in item["Mounts"] if row["Type"] == "bind"]), 4)
                self.assertIs(item["HostConfig"]["ReadonlyRootfs"], True)

    def test_reported_tmpfs_cannot_add_sources_destinations_or_writable_inputs(self):
        changes = [lambda item: item["Mounts"][-1].update(Destination="/unapproved"),
                   lambda item: item["Mounts"][-1].update(Source="/private-host"),
                   lambda item: item["Mounts"][-1].update(Propagation="shared"),
                   lambda item: item["Mounts"][-1].update(RW=False),
                   lambda item: item["Mounts"][-1].update(RW=1),
                   lambda item: item["Mounts"].append(copy.deepcopy(item["Mounts"][-1])),
                   lambda item: item["Mounts"].append({"Type": "volume", "Destination": "/extra", "RW": True}),
                   lambda item: item["Mounts"][0].update(Source="/private-host"),
                   lambda item: item["Mounts"][0].update(RW=True),
                   lambda item: item["Mounts"][1].update(Destination=item["Mounts"][0]["Destination"])]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                instance = runtime_fixture()
                item = inspect_fixture(instance, True)
                item["Mounts"].append({"Type": "tmpfs", "Source": "", "Destination": "/tmp", "RW": True})
                change(item)
                instance.api.request.return_value = item
                with self.assertRaises(ValueError):
                    instance.inspect(running=True)

    def test_malformed_mount_inventory_is_denied_before_harness_release(self):
        for mounts in (None, {}, [None], [{"Type": "tmpfs", "Destination": [], "RW": True}]):
            with self.subTest(mounts=mounts):
                instance = runtime_fixture()
                item = inspect_fixture(instance, True)
                item["Mounts"] = mounts
                instance.api.request.return_value = item
                with self.assertRaises(ValueError):
                    instance.inspect(running=True)

    def test_shared_cgroup_v2_oom_normalization_is_narrow_and_requires_the_field(self):
        for actual, accepted in ((False, True), (None, True), (True, False), (0, False), ("false", False), ("missing", False)):
            with self.subTest(actual=actual):
                instance = runtime_fixture()
                item = inspect_fixture(instance, True)
                if actual == "missing":
                    item["HostConfig"].pop("OomKillDisable")
                else:
                    item["HostConfig"]["OomKillDisable"] = actual
                instance.api.request.return_value = item
                if accepted:
                    instance.inspect(running=True)
                else:
                    with self.assertRaises(ValueError):
                        instance.inspect(running=True)
        for field, desired in (("Privileged", False), ("AutoRemove", False), ("ReadonlyRootfs", True)):
            self.assertFalse(PG.host_config_value_matches(field, int(desired), desired))

    def test_accepted_id_is_fsynced_before_failed_inspect_and_never_retried_or_deleted(self):
        instance = runtime_fixture()
        events = []
        instance.memory_budget = Mock()
        instance.api.request.side_effect = lambda *args, **kwargs: {"Id": "9" * 64}
        original_fsync = os.fsync
        with tempfile.TemporaryFile() as ledger:
            instance.ledger = ledger
            def fsync(descriptor):
                original_fsync(descriptor)
                events.append("fsync")
            def inspect(**_kwargs):
                events.append("inspect")
                ledger.seek(0)
                accepted = json.loads(ledger.read())
                self.assertEqual(accepted["event"], "container_accepted")
                self.assertEqual(accepted["container_id"], "9" * 64)
                raise ValueError("untrusted failure secret")
            instance.inspect = inspect
            with patch.object(RUNTIME.os, "fsync", side_effect=fsync), self.assertRaises(ValueError):
                instance.launch()
        self.assertEqual(events, ["fsync", "inspect"])
        calls = instance.api.request.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0], "POST")
        self.assertTrue(calls[0].args[1].startswith("/containers/create?"))

    def test_kernel_proof_failure_prevents_release_and_preserves_accepted_identity(self):
        instance = runtime_fixture()
        instance.memory_budget = Mock()
        instance.api.request.return_value = {"Id": "9" * 64}
        instance.record = Mock()
        instance.inspect = Mock()
        instance.kernel_boundary = Mock(side_effect=ValueError("kernel mismatch"))
        with patch.object(RUNTIME, "new_file") as release, self.assertRaises(ValueError):
            instance.launch()
        release.assert_not_called()
        self.assertEqual(instance.identifier, "9" * 64)
        instance.record.assert_called_once_with("container_accepted", container_id="9" * 64)

    def test_kernel_capability_seccomp_apparmor_and_cgroup_denials(self):
        valid_status = "\n".join(["NoNewPrivs:\t1", "Seccomp:\t2", *(field + ":\t0000000000000000" for field in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"))])
        for bad in ("CapEff:\t0000000000000001", "CapBnd:\t0000000000000001", "Seccomp:\t0", "NoNewPrivs:\t0", "apparmor", "memory", "parent"):
            with self.subTest(bad=bad):
                instance = runtime_fixture()
                instance.identifier = "9" * 64
                instance.inspect = Mock(return_value={"State": {"Pid": 200}})
                instance.runtime = Mock()
                instance.runtime.command.return_value = "ControlGroup=/owned/" + CORE.LAB + ".slice"
                instance.b1.validated_cgroup.side_effect = lambda value, unit: PurePosixPath(value)
                instance.b1.controller_limits.return_value = {"memory": 4 * 1024**3 if bad == "memory" else 2 * 1024**3, "tasks": 1024, "quota": 150000, "period": 100000}
                parent = "/foreign" if bad == "parent" else "/owned/" + CORE.LAB + ".slice"
                status = valid_status
                if ":" in bad:
                    key = bad.split(":", 1)[0]
                    status = "\n".join(bad if row.startswith(key + ":") else row for row in status.splitlines())
                response = {"status": status, "cgroup": "0::" + parent + "/docker-" + instance.identifier + ".scope",
                            "attr/current": "unconfined" if bad == "apparmor" else "docker-default (enforce)"}
                def process(_pid, name, values=response):
                    return values[name]
                with patch.object(RUNTIME, "process_file", side_effect=process), self.assertRaises(ValueError):
                    instance.kernel_boundary()
                instance.runtime.effective_boundaries.assert_not_called()

    def test_only_complete_unique_sanitized_results_can_set_acceptance(self):
        result, ledger = successful_results()
        self.assertEqual(RUNTIME.result_contract(CORE, result, ledger)["passed"], 15)
        changes = [lambda data: data["results"].pop(), lambda data: data["results"].append(data["results"][0]),
                   lambda data: data["results"][0].update(assertion=False), lambda data: data["results"][0].update(token="private"),
                   lambda data: data["results"][0].update(status=403), lambda data: data["results"][0].update(case_id="unknown")]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                damaged = copy.deepcopy(result)
                change(damaged)
                with self.assertRaises(ValueError):
                    RUNTIME.result_contract(CORE, damaged, ledger)
        with self.assertRaises(ValueError):
            RUNTIME.result_contract(CORE, result, ledger.splitlines()[0])

    def test_private_accepted_resource_ids_are_preserved_but_not_in_public_summary(self):
        result, ledger = successful_results()
        instance = runtime_fixture()
        instance.identifier = "9" * 64
        instance.record = Mock()
        launch = CORE.canonical({"schema_version": 1, "run_id": RUN, "exit_code": 0, "status": "completed"})
        reads = [launch, CORE.canonical(result), ledger]
        with patch.object(CORE, "owned_bytes", side_effect=reads), patch.object(RUNTIME, "new_file") as output:
            summary = instance.accept()
        self.assertTrue(summary["sso_login_verified"])
        self.assertTrue(summary["jwt_runtime_verified"])
        self.assertEqual(summary["accepted_organization_count"], 2)
        self.assertNotIn("organization_id", CORE.canonical(summary).decode())
        self.assertNotIn("member_id", CORE.canonical(summary).decode())
        self.assertIn(b'"organization_id":11', ledger)
        output.assert_called_once()

    def test_credentials_adapter_exports_only_six_passwords(self):
        document = {"actors": {name: {"password": "synthetic-password-" + name, "password_hash": "must-remain-private"} for name in RUNTIME.ACTORS},
                    "client_secret": "must-never-export-this-value"}
        selected = RUNTIME.six_passwords(CORE, CORE.canonical(document))
        self.assertEqual(set(selected), set(RUNTIME.ACTORS))
        self.assertNotIn("must-remain-private", str(selected))
        self.assertNotIn("must-never-export-this-value", str(selected))
        document["actors"]["unowned"] = {"password": "synthetic-password", "password_hash": "hash"}
        with self.assertRaises(ValueError):
            RUNTIME.six_passwords(CORE, CORE.canonical(document))

    def test_used_image_fields_allow_docker_empty_defaults_and_refuse_changed_values(self):
        original = {"Env": ["PATH=/usr/bin"], "Cmd": ["/bin/bash"], "User": "", "Labels": None}
        observed = {**original, "Labels": {}, "Entrypoint": [], "Volumes": None, "ExposedPorts": {}, "WorkingDir": "", "AttachStdout": False}
        self.assertTrue(RUNTIME.image_configuration_matches(observed, original))
        for key, value in (("Env", ["PATH=/untrusted"]), ("Cmd", ["untrusted"]), ("User", "unapproved"), ("Volumes", {"/extra": {}}), ("Entrypoint", False)):
            changed = {**observed, key: value}
            self.assertFalse(RUNTIME.image_configuration_matches(changed, original))

    def test_failure_records_no_untrusted_error_text_and_no_cleanup(self):
        instance = runtime_fixture()
        instance.preflight = Mock()
        instance.begin = Mock()
        instance.ledger = Mock()
        instance.load_image = Mock(side_effect=RuntimeError("PRIVATE-NOT-FOR-LOGS"))
        instance.record = Mock()
        with self.assertRaises(RuntimeError):
            instance.execute()
        instance.record.assert_called_once_with("execution_stopped", container_id=None, automatic_retry=False, automatic_cleanup=False)
        self.assertNotIn("PRIVATE-NOT-FOR-LOGS", str(instance.record.call_args))
        instance.api.request.assert_not_called()

    def test_one_attempt_marker_blocks_a_new_uuid_without_overwrite(self):
        root = self.directory
        (root / "state" / B2).mkdir(parents=True, mode=0o700)
        (root / "artifacts").mkdir(mode=0o700)
        with patch.object(RUNTIME, "ROOT", root):
            first = runtime_fixture()
            first.state = root / "state" / RUN
            first.begin()
            first.ledger.close()
            marker = root / "state" / B2 / "browser-attempt.json"
            before = marker.read_bytes()
            second = runtime_fixture()
            second.run = "ef289292-3c51-4a6b-9a0a-98c4a1e83731"
            second.state = root / "state" / second.run
            with self.assertRaises(FileExistsError):
                second.begin()
            self.assertEqual(marker.read_bytes(), before)
            self.assertFalse(second.state.exists())
            second.api.request.assert_not_called()

    def test_guest_reserve_and_aggregate_slice_budget_refuse_start(self):
        for available, current, accepted in ((6 * 1024**3, 12 * 1024**3, True), (6 * 1024**3 - 1024, 0, False), (6 * 1024**3, 12 * 1024**3 + 1, False)):
            with self.subTest(available=available, current=current):
                instance = runtime_fixture()
                instance.runtime = Mock()
                instance.runtime.command.return_value = "ControlGroup=/owned/" + CORE.LAB + ".slice"
                instance.b1.validated_cgroup.side_effect = lambda value, _unit: PurePosixPath(value)
                body = ("MemAvailable: " + str(available // 1024) + " kB\n").encode()
                with patch.object(RUNTIME.os, "open", return_value=7), patch.object(RUNTIME.os, "fdopen", return_value=io.BytesIO(body)), \
                        patch.object(RUNTIME, "kernel_current_memory", return_value=current):
                    if accepted:
                        instance.memory_budget()
                    else:
                        with self.assertRaises(ValueError):
                            instance.memory_budget()
                instance.api.request.assert_not_called()

    def test_incomplete_case_results_do_not_overwrite_private_accepted_ids(self):
        result, ledger = successful_results()
        result["results"].pop()
        instance = runtime_fixture()
        original = bytes(ledger)
        launch = CORE.canonical({"schema_version": 1, "run_id": RUN, "exit_code": 0, "status": "completed"})
        with patch.object(CORE, "owned_bytes", side_effect=[launch, CORE.canonical(result), ledger]), patch.object(RUNTIME, "new_file") as output, self.assertRaises(ValueError):
            instance.accept()
        self.assertEqual(ledger, original)
        self.assertIn(b'"member_id":23', ledger)
        output.assert_not_called()


class CompleteByteGateTests(PrivateFilesCase):
    """Execute gate tests use synthetic immutable image data, never a Docker daemon."""

    def build_gate(self):
        incoming = self.directory / "incoming" / RUN
        incoming.mkdir(parents=True, mode=0o700)
        b2incoming = self.directory / "incoming" / B2
        b2incoming.mkdir(mode=0o700)
        controller = b"synthetic reviewed controller source"
        (b2incoming / "phase_b_controller_guest.py").write_bytes(controller)
        result, outer = actual_schema_fixture()
        outer["controller_helper_sha256"] = CORE.sha(controller)
        result_raw = CORE.canonical(result)
        result_path = self.directory / "state" / B2 / "evidence"
        result_path.mkdir(parents=True, mode=0o700)
        (result_path / "controller-summary.json").write_bytes(result_raw)
        (result_path / "controller-summary.json").chmod(0o600)
        helpers = {}
        for name in CORE.HELPERS:
            body = (HERE / name).read_bytes()
            (incoming / name).write_bytes(body)
            helpers[name] = CORE.sha(body)
        (b2incoming / "phase_b_pg_guest.py").write_bytes((HERE / "phase_b_pg_guest.py").read_bytes())
        buffers = {"control": b"synthetic pinned public control", "harness": b"synthetic frozen harness"}
        files = {}
        for role, body in buffers.items():
            name = role + ".json"
            (incoming / name).write_bytes(body)
            files[role] = {"filename": name, "size": len(body), "sha256": CORE.sha(body)}
        npm = {name: b"synthetic opaque npm " + name.encode() for name in CORE.NPM}
        integrity = {name: "sha512-" + base64.b64encode(hashlib.sha512(body).digest()).decode() for name, body in npm.items()}
        image_body = b"synthetic opaque image archive"
        with patch.object(CORE, "NPM", integrity), patch.object(CORE, "CONTROL", CORE.sha(buffers["control"])):
            proof = proof_fixture(npm, image_body)
            plan = CORE.runtime_plan(CORE.adapter_from_result(result, outer), CORE.payload_contract(proof), RUN)
        files["npm"] = {}
        for index, (name, body) in enumerate(npm.items()):
            filename = "npm-" + str(index) + ".tgz"
            (incoming / filename).write_bytes(body)
            files["npm"][name] = {"filename": filename, "size": len(body), "sha256": CORE.sha(body)}
        (incoming / "image.tar").write_bytes(image_body)
        files["image"] = {"filename": "image.tar", "size": len(image_body), "sha256": CORE.sha(image_body)}
        manifest = {"schema_version": 1, "run_id": RUN, "proposal_digest": CORE.sha(CORE.canonical(plan)), "plan": plan,
                    "outer": outer, "b2_result": {"sha256": CORE.sha(result_raw), "size": len(result_raw)}, "payload_proof": proof,
                    "files": files, "helpers": helpers}
        path = incoming / "browser-manifest.json"
        path.write_bytes(CORE.canonical(manifest))
        return path, manifest, integrity, buffers

    def execute_gate(self, path, manifest, integrity, buffers):
        runner = Mock()
        runner.execute.return_value = {"status": "synthetic-test-only"}
        calls = []
        def frozen(name, body, source):
            calls.append(name)
            return SimpleNamespace(BrowserRun=Mock(return_value=runner))
        with patch.object(CORE, "NPM", integrity), patch.object(CORE, "CONTROL", CORE.sha(buffers["control"])), patch.object(CORE, "HARNESS", CORE.sha(buffers["harness"])), \
                patch.object(CORE, "__file__", str(path.parent / "real_browser_execute.py")), patch.object(CORE, "image_archive", return_value={"config": {}}) as archive_gate, \
                patch.object(CORE, "load_frozen", side_effect=frozen) as imports:
            result = CORE.execute(path, CORE.sha(CORE.canonical(manifest)), AUTHORITY)
        self.assertEqual(result, {"status": "synthetic-test-only"})
        self.assertEqual(calls, ["planner", "b1", "pg", "runtime"])
        archive_gate.assert_called_once()
        self.assertEqual(imports.call_count, 4)
        return runner

    def test_every_byte_gate_passes_before_any_import_or_runtime(self):
        self.execute_gate(*self.build_gate())

    def test_rehashed_unreviewed_pg_helper_stops_before_every_import(self):
        path, manifest, integrity, buffers = self.build_gate()
        body = b"synthetic unreviewed PostgreSQL helper"
        (path.parent / "phase_b_pg_guest.py").write_bytes(body)
        (self.directory / "incoming" / B2 / "phase_b_pg_guest.py").write_bytes(body)
        manifest["helpers"]["phase_b_pg_guest.py"] = CORE.sha(body)
        path.write_bytes(CORE.canonical(manifest))
        with patch.object(CORE, "NPM", integrity), patch.object(CORE, "CONTROL", CORE.sha(buffers["control"])), \
                patch.object(CORE, "HARNESS", CORE.sha(buffers["harness"])), \
                patch.object(CORE, "__file__", str(path.parent / "real_browser_execute.py")), \
                patch.object(CORE, "load_frozen") as imports, patch.object(CORE, "image_archive") as archive_gate, \
                self.assertRaisesRegex(ValueError, "Reviewed planner, B1, or executor source differs"):
            CORE.execute(path, CORE.sha(CORE.canonical(manifest)), AUTHORITY)
        imports.assert_not_called()
        archive_gate.assert_not_called()

    def test_helper_control_harness_npm_image_and_controller_tamper_prevent_every_import(self):
        targets = [*sorted(CORE.HELPERS), "control.json", "harness.json", "npm-0.tgz", "image.tar", "controller"]
        for index, name in enumerate(targets):
            with self.subTest(name=name):
                root = self.directory / ("case-" + str(index))
                root.mkdir(mode=0o700)
                with patch.object(CORE, "ROOT", root):
                    previous = self.directory
                    self.directory = root
                    path, manifest, integrity, buffers = self.build_gate()
                    self.directory = previous
                    target = path.parent / name if name != "controller" else root / "incoming" / B2 / "phase_b_controller_guest.py"
                    target.write_bytes(target.read_bytes() + b"tampered")
                    with patch.object(CORE, "NPM", integrity), patch.object(CORE, "CONTROL", CORE.sha(buffers["control"])), patch.object(CORE, "HARNESS", CORE.sha(buffers["harness"])), \
                            patch.object(CORE, "__file__", str(path.parent / "real_browser_execute.py")), patch.object(CORE, "load_frozen") as imports, \
                            patch.object(CORE, "image_archive") as archive_gate, self.assertRaises(ValueError):
                        CORE.execute(path, CORE.sha(CORE.canonical(manifest)), AUTHORITY)
                    imports.assert_not_called()
                    archive_gate.assert_not_called()

    def test_authority_and_canonical_plan_change_stop_before_import(self):
        path, manifest, integrity, buffers = self.build_gate()
        with patch.object(CORE, "load_frozen") as imports, self.assertRaises(ValueError):
            CORE.execute(path, CORE.sha(CORE.canonical(manifest)), "other-synthetic-authority")
        imports.assert_not_called()
        manifest["plan"]["memory_bytes"] = 4 * 1024**3
        path.write_bytes(CORE.canonical(manifest))
        with patch.object(CORE, "NPM", integrity), patch.object(CORE, "CONTROL", CORE.sha(buffers["control"])), patch.object(CORE, "load_frozen") as imports, self.assertRaises(ValueError):
            CORE.execute(path, CORE.sha(CORE.canonical(manifest)), AUTHORITY)
        imports.assert_not_called()

    def test_redeclared_sha256_cannot_replace_the_known_npm_sha512(self):
        path, manifest, integrity, buffers = self.build_gate()
        target = path.parent / "npm-0.tgz"
        target.write_bytes(target.read_bytes() + b"untrusted-new-package")
        name = next(name for name, row in manifest["files"]["npm"].items() if row["filename"] == "npm-0.tgz")
        digest = CORE.sha(target.read_bytes())
        manifest["files"]["npm"][name].update(sha256=digest, size=target.stat().st_size)
        next(row for row in manifest["payload_proof"]["npm"] if row["name"] == name)["sha256"] = digest
        manifest["plan"]["npm"][name] = digest
        manifest["proposal_digest"] = CORE.sha(CORE.canonical(manifest["plan"]))
        path.write_bytes(CORE.canonical(manifest))
        with patch.object(CORE, "NPM", integrity), patch.object(CORE, "CONTROL", CORE.sha(buffers["control"])), patch.object(CORE, "HARNESS", CORE.sha(buffers["harness"])), \
                patch.object(CORE, "__file__", str(path.parent / "real_browser_execute.py")), patch.object(CORE, "load_frozen") as imports, self.assertRaises(ValueError):
            CORE.execute(path, CORE.sha(CORE.canonical(manifest)), AUTHORITY)
        imports.assert_not_called()


class MaterializationTests(PrivateFilesCase):
    def test_synthetic_private_passwords_have_exact_nonroot_permissions_and_no_extra_secrets(self):
        (self.directory / "state" / B2 / "idp-bootstrap").mkdir(parents=True, mode=0o700)
        source = self.directory / "state" / B2 / "idp-bootstrap/credentials.json"
        document = {"actors": {name: {"password": "synthetic-password-" + name, "password_hash": "NOT-EXPORTED-HASH"} for name in RUNTIME.ACTORS},
                    "client_secret": "NOT-EXPORTED-CLIENT-SECRET"}
        source.write_bytes(CORE.canonical(document))
        source.chmod(0o600)
        instance = runtime_fixture()
        instance.state = self.directory / "state" / RUN
        instance.state.mkdir(mode=0o700)
        instance.evidence = instance.state / "evidence"
        instance.buffers = {"harness": b"// synthetic fixture, never executed"}
        instance.helpers = {"real_browser_launch.cjs": b"// synthetic fixture, never executed"}
        instance.npm = {name: tar_bytes([("package/package.json", b'{"scripts":{"postinstall":"never invoked"}}')], gzip=True) for name in CORE.NPM}
        instance.record = Mock()
        previous = os.umask(0o077)
        try:
            with patch.object(RUNTIME, "ROOT", self.directory), patch("subprocess.run", side_effect=AssertionError("subprocess forbidden")):
                instance.materialize()
        finally:
            os.umask(previous)
        destination = instance.state / "credentials/actors.json"
        data = destination.read_bytes()
        self.assertNotIn(b"NOT-EXPORTED-HASH", data)
        self.assertNotIn(b"NOT-EXPORTED-CLIENT-SECRET", data)
        self.assertEqual(set(CORE.strict_json(data)["actors"]), set(RUNTIME.ACTORS))
        for path, mode in ((destination, 0o600), (destination.parent, 0o700), (instance.evidence, 0o700)):
            information = self.ownership.metadata(path, path.stat())
            self.assertEqual((information.st_uid, information.st_gid, stat.S_IMODE(information.st_mode)), (1000, 1000, mode))
            self.assertIn((path, 1000, 1000), self.ownership.requests)
            self.assertEqual((path.stat().st_uid, path.stat().st_gid), (os.getuid(), os.getgid()))
        self.assertEqual(stat.S_IMODE((instance.state / "input/real_role_harness.cjs").stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE((instance.state / "input").stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((instance.state / "node").stat().st_mode), 0o755)
        self.assertFalse((instance.evidence / "harness").exists())
        self.assertFalse((instance.evidence / "release.json").exists())


if __name__ == "__main__":
    unittest.main()
