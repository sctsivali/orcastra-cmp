"""Synthetic filesystem/client tests. Never connect to Vault, Docker or any live API."""

from __future__ import annotations

import base64
import contextlib
import copy
import dataclasses
import io
import json
import os
import stat
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import phase_b_vault_bootstrap as vault

RUN = "0f2a5b15-f5ec-464b-8785-0b4b88c3b358"
PHASE = "bf7ace11-d2d8-447c-89b1-06ad154aa79d"
B1 = "cdf24503-772e-4ad0-a194-04e965c6e44d"
ISSUER = "bfa722a6-0c66-4cd0-926e-948b33d508b8"
KEY_ID = "e1c6728d-d597-42d3-a188-09c785a89386"
ROOT_TOKEN = "hvs.SyntheticRootTokenNeverUse"
BACKEND_TOKEN = "hvs.SyntheticBackendTokenNeverUse"
UNSEAL_KEY = "42" * 32
CA = "-----BEGIN CERTIFICATE-----\nU3ludGhldGljIFFBIGZpeHR1cmU=\n-----END CERTIFICATE-----\n"


def seal(plan: dict) -> dict:
    core = dict(plan)
    core.pop("proposal_digest", None)
    return {**core, "proposal_digest": vault.canonical_digest(core)}


def ready_plan() -> dict:
    return seal({"schema_version": 1, "phase": "B2", "phase_b_id": PHASE,
                 "ready_for_guest_execution": True, "remaining_gates": [],
                 "target": {"vm_name": vault.LAB, "vm_run_id": RUN, "b1_phase_id": B1,
                            "phase_a_digest": "384bc2030c890ea26f054ece6725a0876fd95e95a6ccfdfe53368d17e3eb863e",
                            "project": "default", "profiles": [], "outer_nic_count": 0},
                 "vault_bootstrap": vault.vault_contract(RUN, PHASE)})


def seal_status(initialized: bool) -> dict:
    return {"initialized": initialized, "sealed": not initialized, "type": "shamir", "version": vault.VERSION,
            "storage_type": "file", "recovery_seal": False, "progress": 0, "migration": False,
            "n": int(initialized), "t": int(initialized)}


def script(plan: dict) -> list[dict]:
    contract = plan["vault_bootstrap"]
    policy = contract["policy_name"]
    builtin = {"sys/": {"type": "system"}, "identity/": {"type": "identity"}, "cubbyhole/": {"type": "cubbyhole"}}
    mounts = {**copy.deepcopy(builtin), **{name + "/": copy.deepcopy(data) for name, data in contract["mounts"].items()}}
    role = copy.deepcopy(contract["role"])
    role.update({"ttl": vault.CERT_SECONDS, "max_ttl": vault.CERT_SECONDS})
    auth = {"client_token": BACKEND_TOKEN, "policies": [policy], "token_policies": [policy], "identity_policies": [],
            "renewable": False, "token_type": "service", "lease_duration": vault.TOKEN_SECONDS, "orphan": True}
    lookup = {"id": BACKEND_TOKEN, "policies": [policy], "identity_policies": [], "entity_id": "",
              "renewable": False, "orphan": True, "type": "service", "num_uses": 0,
              "explicit_max_ttl": vault.TOKEN_SECONDS, "creation_ttl": vault.TOKEN_SECONDS,
              "ttl": vault.TOKEN_SECONDS - 1, "meta": {"lab": vault.LAB, "phase_b_id": PHASE}}
    entries = [
        ("fresh_init_read", "GET", "/v1/sys/init", 200, {"initialized": False}),
        ("fresh_seal_read", "GET", "/v1/sys/seal-status", 200, seal_status(False)),
        ("initialize", "POST", "/v1/sys/init", 200,
         {"keys": [UNSEAL_KEY], "keys_base64": [base64.b64encode(bytes.fromhex(UNSEAL_KEY)).decode()], "root_token": ROOT_TOKEN}),
        ("unseal", "POST", "/v1/sys/unseal", 200, seal_status(True)),
        ("unseal_readback", "GET", "/v1/sys/seal-status", 200, seal_status(True)),
        ("baseline_mounts", "GET", "/v1/sys/mounts", 200, {"data": builtin}),
        ("baseline_policy", "GET", "/v1/sys/policies/acl/" + policy, 404, {"errors": []}),
        ("mount_secret", "POST", "/v1/sys/mounts/secret", 204, None),
        ("mount_pki_int", "POST", "/v1/sys/mounts/pki_int", 204, None),
        ("ca_generate", "POST", "/v1/pki_int/root/generate/internal", 200,
         {"data": {"certificate": CA, "issuer_id": ISSUER, "key_id": KEY_ID,
                   "issuer_name": "qa-" + PHASE, "key_name": "qa-" + PHASE}}),
        ("role_absent", "GET", "/v1/pki_int/roles/lxd", 404, {"errors": []}),
        ("role_create", "POST", "/v1/pki_int/roles/lxd", 204, None),
        ("policy_create", "PUT", "/v1/sys/policies/acl/" + policy, 204, None),
        ("token_create", "POST", "/v1/auth/token/create", 200, {"auth": auth}),
        ("mounts_readback", "GET", "/v1/sys/mounts", 200, {"data": mounts}),
        ("pki_tune_readback", "GET", "/v1/sys/mounts/pki_int/tune", 200,
         {"data": {"default_lease_ttl": vault.CERT_SECONDS, "max_lease_ttl": 8760 * 3600}}),
        ("issuer_readback", "GET", "/v1/pki_int/config/issuers", 200,
         {"data": {"default": ISSUER, "default_follows_latest_issuer": False}}),
        ("issuer_identity_readback", "GET", "/v1/pki_int/issuer/" + ISSUER, 200,
         {"data": {"issuer_id": ISSUER, "key_id": KEY_ID, "issuer_name": "qa-" + PHASE, "certificate": CA}}),
        ("role_readback", "GET", "/v1/pki_int/roles/lxd", 200, {"data": role}),
        ("policy_readback", "GET", "/v1/sys/policies/acl/" + policy, 200, {"data": {"name": policy, "policy": vault.POLICY}}),
        ("token_lookup_self", "GET", "/v1/auth/token/lookup-self", 200, {"data": lookup}),
        ("capabilities_readback", "POST", "/v1/sys/capabilities", 200, {"data": vault.capability_contract()}),
        ("forbidden_read_1", "GET", "/v1/sys/mounts", 403, {"errors": ["permission denied"]}),
        ("forbidden_read_2", "GET", "/v1/secret/data/unrelated", 403, {"errors": ["permission denied"]}),
        ("registry_empty_readback", "LIST", "/v1/secret/metadata/clusters", 404, {"errors": []}),
    ]
    return [{"stage": stage, "method": method, "path": path, "status": status, "document": document}
            for stage, method, path, status, document in entries]


class FakeClient:
    def __init__(self, plan: dict, events: list[dict], private: Path):
        contract = plan["vault_bootstrap"]
        self.binding = vault.VaultBinding(contract["endpoint"], "a" * 64, contract["container_name"], vault.IMAGE_ID,
                                          dict(contract["labels"]), "b" * 64, True, True)
        self.entries, self.calls, self.events, self.private = script(plan), [], events, private

    def request(self, method, path, *, token, body, timeout_seconds, max_response_bytes):
        entry = self.entries[len(self.calls)]
        assert (method, path) == (entry["method"], entry["path"])
        assert any(event.get("event") == "http_attempt" and event["stage"] == entry["stage"] for event in self.events)
        assert 0 < timeout_seconds <= vault.HTTP_SECONDS
        assert max_response_bytes == vault.MAX_RESPONSE
        if entry["stage"] == "unseal":
            assert (self.private / "root-token").read_bytes() == ROOT_TOKEN.encode()
            assert (self.private / "unseal-key").read_bytes() == UNSEAL_KEY.encode()
        self.calls.append({"stage": entry["stage"], "method": method, "path": path, "token": token,
                           "body": None if body is None else json.loads(body), "timeout": timeout_seconds})
        if "error" in entry:
            raise entry["error"]
        response_body = entry.get("raw", b"" if entry["document"] is None else json.dumps(entry["document"]).encode())
        return vault.HTTPResponse(entry["status"], response_body, entry.get("complete", True))

    def entry(self, stage: str) -> dict:
        return next(item for item in self.entries if item["stage"] == stage)


class OwnedRootMetadata:
    """Emulate root ownership only for this fixture's paths and witnessed descriptors."""

    def __init__(self, root: Path):
        self.root = root.absolute()
        metadata = os.stat(self.root, follow_symlinks=False)
        self.actual_owner = metadata.st_uid, metadata.st_gid
        self.descriptors = {}
        self.owner_overrides = {}

    def __getattr__(self, name):
        return getattr(os, name)

    @staticmethod
    def geteuid():
        return 0

    @staticmethod
    def getegid():
        return 0

    def _path(self, path, dir_fd=None) -> Path:
        candidate = Path(path)
        if ".." in candidate.parts:
            raise PermissionError("The metadata fixture refuses path traversal")
        if not candidate.is_absolute():
            if dir_fd not in self.descriptors:
                raise PermissionError("The metadata fixture requires a witnessed parent descriptor")
            candidate = self.descriptors[dir_fd][0] / candidate
        return candidate

    def _owned(self, path: Path) -> bool:
        return path == self.root or self.root in path.parents

    def _check_path(self, path: Path, *, allow_leaf_symlink=False) -> None:
        if not self._owned(path):
            raise PermissionError("The metadata fixture refuses foreign paths")
        current = self.root
        for part in (None, *path.relative_to(self.root).parts):
            if part is not None:
                current /= part
            try:
                metadata = os.stat(current, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode) and not (current == path and allow_leaf_symlink):
                raise PermissionError("The metadata fixture refuses symlink traversal")

    def _metadata(self, path: Path, metadata):
        fields = list(metadata)
        if (metadata.st_uid, metadata.st_gid) == self.actual_owner:
            fields[4], fields[5] = self.owner_overrides.get(path, (0, 0))
        return os.stat_result(fields)

    def open(self, path, flags, mode=0o777, *, dir_fd=None):
        candidate = self._path(path, dir_fd)
        if self._owned(candidate):
            self._check_path(candidate, allow_leaf_symlink=bool(flags & os.O_NOFOLLOW))
        elif (candidate not in self.root.parents or not flags & os.O_DIRECTORY
              or flags & (os.O_ACCMODE | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
            raise PermissionError("Only read-only ancestor traversal is allowed outside the fixture")
        descriptor = os.open(path, flags, mode, dir_fd=dir_fd)
        metadata = os.fstat(descriptor)
        self.descriptors[descriptor] = candidate, metadata.st_dev, metadata.st_ino
        return descriptor

    def fstat(self, descriptor):
        recorded = self.descriptors.get(descriptor)
        if recorded is None or not self._owned(recorded[0]):
            raise PermissionError("The metadata fixture refuses foreign descriptors")
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != recorded[1:]:
            raise PermissionError("The metadata fixture refuses reused descriptors")
        try:
            self._check_path(recorded[0])
            located = os.stat(recorded[0], follow_symlinks=False)
        except OSError as error:
            raise PermissionError("The metadata fixture lost the owned descriptor path") from error
        if (located.st_dev, located.st_ino) != recorded[1:]:
            raise PermissionError("The metadata fixture refuses replaced descriptor paths")
        return self._metadata(recorded[0], metadata)

    def stat(self, path, *, dir_fd=None, follow_symlinks=True):
        candidate = self._path(path, dir_fd)
        self._check_path(candidate, allow_leaf_symlink=not follow_symlinks)
        return self._metadata(candidate, os.stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks))

    def mkdir(self, path, mode=0o777, *, dir_fd=None):
        self._check_path(self._path(path, dir_fd))
        return os.mkdir(path, mode, dir_fd=dir_fd)

    def close(self, descriptor):
        try:
            os.close(descriptor)
        finally:
            self.descriptors.pop(descriptor, None)


class Fixture:
    def __init__(self, plan: dict | None = None):
        self.plan = ready_plan() if plan is None else plan
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-vault-")
        self.root = Path(self.temporary.name)
        self.parent, self.etc = self.root / "phase", self.root / "etc"
        self.parent.mkdir(mode=0o700)
        self.etc.mkdir(mode=0o700)
        self.metadata = OwnedRootMetadata(self.root)
        self.private = self.parent / "vault-bootstrap"
        self.marker = self.parent / "owner.json"
        self.write(self.marker, json.dumps(vault.ownership_marker(RUN, PHASE, self.plan["proposal_digest"])).encode())
        self.write(self.etc / vault.LAB, (vault.LAB + "\n").encode())
        self.write(self.etc / (vault.LAB + "-run"), (RUN + "\n").encode())
        self.events = []
        self.client = FakeClient(self.plan, self.events, self.private)

    @staticmethod
    def write(path: Path, body: bytes) -> None:
        path.write_bytes(body)
        path.chmod(0o600)

    def directory(self, path: Path, *, private: bool = False) -> int:
        # Only these two exact public paths are redirected into the synthetic tree.
        mapping = {"/etc": self.etc, self.plan["vault_bootstrap"]["parent_path"]: self.parent}
        target = mapping[str(path)]
        descriptor = self.metadata.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        metadata = self.metadata.fstat(descriptor)
        if (metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_mode & 0o022
            or private and stat.S_IMODE(metadata.st_mode) != 0o700):
            self.metadata.close(descriptor)
            raise PermissionError("Synthetic directory mode differs")
        return descriptor

    def run(self, **overrides):
        arguments = {"approved_digest": self.plan["proposal_digest"], "approval_reference": "Synthetic approval 1",
                     "client": self.client, "evidence_sink": self.events.append, "monotonic": lambda: 0.0}
        arguments.update(overrides)
        with patch.object(vault, "os", self.metadata), patch.object(vault, "_directory", side_effect=self.directory), \
                patch("socket.socket", side_effect=AssertionError("No network")):
            return vault.bootstrap_vault(self.plan, **arguments)

    def close(self):
        self.temporary.cleanup()


class VaultBootstrapTests(unittest.TestCase):
    def fixture(self, plan=None):
        fixture = Fixture(plan)
        self.addCleanup(fixture.close)
        return fixture

    def failure(self, fixture, stage=None, **kwargs):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), self.assertRaises(vault.BootstrapFailure) as caught:
            fixture.run(**kwargs)
        error = caught.exception
        if stage:
            self.assertEqual(error.stage, stage)
        self.assertFalse(error.evidence["verified"])
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")
        public = str(error) + repr(error) + json.dumps(error.evidence) + json.dumps(fixture.events)
        for secret in (ROOT_TOKEN, BACKEND_TOKEN, UNSEAL_KEY, CA):
            self.assertNotIn(secret, public)
        return error

    def test_success_exact_flow_private_files_and_no_secret_evidence(self):
        fixture = self.fixture()
        evidence = fixture.run()
        self.assertTrue(evidence["verified"])
        self.assertEqual(evidence["cluster_count"], 0)
        self.assertEqual(evidence["container_id"], "a" * 64)
        self.assertEqual(len(fixture.client.calls), 25)
        self.assertEqual(evidence["response_count"], 23)
        self.assertEqual((fixture.private / "backend-token").read_bytes(), BACKEND_TOKEN.encode())
        self.assertEqual((fixture.private / "root-token").read_bytes(), ROOT_TOKEN.encode())
        self.assertEqual(stat.S_IMODE(fixture.private.stat().st_mode), 0o700)
        self.assertEqual({path.name for path in fixture.parent.iterdir()}, {"owner.json", "vault-bootstrap"})
        for path in fixture.private.iterdir():
            metadata = path.stat()
            self.assertTrue(stat.S_ISREG(metadata.st_mode))
            self.assertEqual((metadata.st_uid, metadata.st_gid), fixture.metadata.actual_owner)
            self.assertEqual(metadata.st_nlink, 1)
            synthetic = fixture.metadata.stat(path)
            self.assertEqual((synthetic.st_uid, synthetic.st_gid), (0, 0))
            self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
        calls = {call["stage"]: call for call in fixture.client.calls}
        for stage in ("fresh_init_read", "fresh_seal_read", "initialize", "unseal", "unseal_readback"):
            self.assertIsNone(calls[stage]["token"])
        for stage in ("token_lookup_self", "forbidden_read_1", "forbidden_read_2", "registry_empty_readback"):
            self.assertEqual(calls[stage]["token"], BACKEND_TOKEN)
        for stage in ("baseline_mounts", "baseline_policy", "mount_secret", "mount_pki_int", "ca_generate",
                      "role_absent", "role_create", "policy_create", "token_create", "mounts_readback",
                      "pki_tune_readback", "issuer_readback", "issuer_identity_readback", "role_readback", "policy_readback"):
            self.assertEqual(calls[stage]["token"], ROOT_TOKEN)
        self.assertEqual(calls["capabilities_readback"]["token"], ROOT_TOKEN)
        self.assertEqual(calls["capabilities_readback"]["body"]["token"], BACKEND_TOKEN)
        self.assertEqual(calls["token_create"]["body"]["no_default_policy"], True)
        self.assertEqual(calls["initialize"]["body"], {"secret_shares": 1, "secret_threshold": 1})
        self.assertEqual(calls["unseal"]["body"], {"key": UNSEAL_KEY})
        self.assertEqual(calls["mount_secret"]["body"], fixture.plan["vault_bootstrap"]["mounts"]["secret"])
        self.assertEqual(calls["ca_generate"]["body"], fixture.plan["vault_bootstrap"]["ca"])
        self.assertEqual(calls["role_create"]["body"], vault.ROLE)
        self.assertEqual(calls["policy_create"]["body"], {"policy": vault.POLICY})
        for secret in (ROOT_TOKEN, BACKEND_TOKEN, UNSEAL_KEY, CA):
            self.assertNotIn(secret, json.dumps(evidence) + json.dumps(fixture.events))
        for resource in (ISSUER, KEY_ID, "pki_int/roles/lxd", fixture.plan["vault_bootstrap"]["policy_name"]):
            self.assertTrue(any(event.get("resource_id") == resource and event["event"] == "resource_created" for event in fixture.events))

    def test_complete_master_approval_and_draft_gate(self):
        for change in (lambda p: p.update(phase="B-PG"), lambda p: p.update(ready_for_guest_execution=False),
                       lambda p: p.update(remaining_gates=["pending"]), lambda p: p["target"].update(outer_nic_count=1),
                       lambda p: p["vault_bootstrap"].update(endpoint="http://localhost:8200"),
                       lambda p: p["vault_bootstrap"]["init"].update(secret_shares=True),
                       lambda p: p["vault_bootstrap"]["role"].update(allow_ip_sans=True)):
            with self.subTest(change=change):
                plan = ready_plan()
                change(plan)
                fixture = self.fixture(seal(plan))
                self.failure(fixture, "approval")
                self.assertEqual(fixture.client.calls, [])
                self.assertFalse(fixture.private.exists())
        fixture = self.fixture()
        fixture.plan["unapproved_other_component"] = "changed"
        self.failure(fixture, "approval")
        for reference in ("", "\nnew approval", 1, "x" * 161):
            self.failure(self.fixture(), "approval", approval_reference=reference)

    def test_owned_client_binding_cannot_adopt_or_discover(self):
        for changed in ({"endpoint": "http://vault:8200"}, {"image_id": "vault:1.21.4"},
                        {"created_for_phase": False}, {"owned_loopback_verified": False},
                        {"container_id": "invalid"}, {"creation_ledger_sha256": "invalid"},
                        {"labels": {"orcastra.qa.lab": "production"}}):
            fixture = self.fixture()
            fixture.client.binding = dataclasses.replace(fixture.client.binding, **changed)
            self.failure(fixture, "ownership")
            self.assertEqual(fixture.client.calls, [])

    def test_owner_modes_links_wrong_run_and_existing_state_rejected(self):
        for kind in ("mode", "uid", "gid", "hardlink", "symlink", "fifo", "wrong-run", "wrong-digest", "duplicate",
                     "directory-mode", "directory-uid", "directory-gid", "existing"):
            with self.subTest(kind=kind):
                fixture = self.fixture()
                if kind == "mode":
                    fixture.marker.chmod(0o644)
                elif kind in {"uid", "gid"}:
                    fixture.metadata.owner_overrides[fixture.marker] = (1001, 0) if kind == "uid" else (0, 1001)
                elif kind == "hardlink":
                    os.link(fixture.marker, fixture.parent / "another-link")
                elif kind == "symlink":
                    fixture.marker.rename(fixture.parent / "marker-backup")
                    fixture.marker.symlink_to("marker-backup")
                elif kind == "fifo":
                    fixture.marker.unlink()
                    os.mkfifo(fixture.marker, 0o600)
                elif kind in {"wrong-run", "wrong-digest"}:
                    data = json.loads(fixture.marker.read_bytes())
                    data["vm_run_id" if kind == "wrong-run" else "proposal_digest"] = "different"
                    fixture.write(fixture.marker, json.dumps(data).encode())
                elif kind == "duplicate":
                    fixture.write(fixture.marker, b'{"lab":"one","lab":"two"}')
                elif kind == "directory-mode":
                    fixture.parent.chmod(0o755)
                elif kind in {"directory-uid", "directory-gid"}:
                    fixture.metadata.owner_overrides[fixture.parent] = (1001, 0) if kind == "directory-uid" else (0, 1001)
                else:
                    fixture.private.symlink_to(fixture.etc, target_is_directory=True)
                self.failure(fixture, "ownership")
                self.assertEqual(fixture.client.calls, [])

    def test_new_private_directory_with_wrong_uid_or_gid_stops_before_initialization(self):
        for ownership in ((1001, 0), (0, 1001)):
            fixture = self.fixture()
            fixture.metadata.owner_overrides[fixture.private] = ownership
            with self.subTest(ownership=ownership):
                self.failure(fixture, "private_state")
            self.assertEqual([call["method"] for call in fixture.client.calls], ["GET", "GET"])
            self.assertTrue(fixture.private.is_dir())
            self.assertEqual(list(fixture.private.iterdir()), [])

    def test_metadata_facade_refuses_foreign_paths_descriptors_and_symlink_escape(self):
        fixture = self.fixture()
        with tempfile.TemporaryDirectory(prefix="synthetic-vault-neighbor-") as temporary:
            foreign = Path(temporary) / "public-fixture"
            foreign.write_bytes(b"Synthetic neighboring fixture bytes")
            descriptor = os.open(foreign, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                with self.assertRaises(PermissionError):
                    fixture.metadata.stat(foreign)
                with self.assertRaises(PermissionError):
                    fixture.metadata.open(foreign, os.O_RDONLY | os.O_NOFOLLOW)
                with self.assertRaises(PermissionError):
                    fixture.metadata.fstat(descriptor)
                with self.assertRaises(PermissionError):
                    fixture.metadata.open("public-fixture", os.O_RDONLY, dir_fd=descriptor)
                escape = fixture.parent / "escape"
                escape.symlink_to(Path(temporary), target_is_directory=True)
                with self.assertRaises(PermissionError):
                    fixture.metadata.open(escape / foreign.name, os.O_RDONLY | os.O_NOFOLLOW)
                movable = fixture.parent / "movable"
                fixture.write(movable, b"Synthetic owned fixture bytes")
                owned = fixture.metadata.open(movable, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    self.assertEqual(fixture.metadata.fstat(owned).st_uid, 0)
                    movable.rename(Path(temporary) / "moved")
                    with self.assertRaises(PermissionError):
                        fixture.metadata.fstat(owned)
                finally:
                    fixture.metadata.close(owned)
            finally:
                os.close(descriptor)
        ancestor = fixture.metadata.open(fixture.root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with self.assertRaises(PermissionError):
                fixture.metadata.fstat(ancestor)
        finally:
            fixture.metadata.close(ancestor)
        self.assertEqual(fixture.metadata.actual_owner, (os.geteuid(), os.getegid()))

    def test_unmodified_root_gate_rejects_actual_or_synthetic_nonroot_before_private_access(self):
        fixture = self.fixture()
        arguments = {"approved_digest": fixture.plan["proposal_digest"], "approval_reference": "Synthetic approval 1",
                     "client": fixture.client, "evidence_sink": fixture.events.append}
        for uid, gid in ((1001, 0), (0, 1001), (1001, 1001)):
            caller = Mock(wraps=os)
            caller.geteuid.return_value, caller.getegid.return_value = uid, gid
            with self.subTest(uid=uid, gid=gid), patch.object(vault, "os", caller), \
                    patch.object(vault, "_directory") as opened, self.assertRaises(vault.BootstrapFailure) as stopped:
                vault.bootstrap_vault(fixture.plan, **arguments)
            self.assertEqual(stopped.exception.stage, "ownership")
            opened.assert_not_called()
        if os.geteuid() != 0 or os.getegid() != 0:
            with patch.object(vault, "_directory") as opened, self.assertRaises(vault.BootstrapFailure) as stopped:
                vault.bootstrap_vault(fixture.plan, **arguments)
            self.assertEqual(stopped.exception.stage, "ownership")
            opened.assert_not_called()
        self.assertEqual(fixture.client.calls, [])
        self.assertFalse(fixture.private.exists())

    def test_initialized_or_inconsistent_fresh_state_rejected_before_any_write(self):
        for mutation in ({"initialized": True}, {"initialized": False, "sealed": False},
                         {"initialized": True, "sealed": True}, {"version": "1.21.3"}, {"type": "awskms"}):
            fixture = self.fixture()
            if set(mutation) == {"initialized"}:
                fixture.client.entries[0]["document"].update(mutation)
                expected = "fresh_init_read"
            else:
                fixture.client.entries[1]["document"].update(mutation)
                expected = "fresh_seal_read"
            self.failure(fixture, expected)
            self.assertFalse(fixture.private.exists())
            self.assertTrue(all(call["method"] == "GET" for call in fixture.client.calls))

    def test_role_and_policy_cannot_be_adopted_or_overwritten(self):
        for stage in ("role_absent", "baseline_policy"):
            fixture = self.fixture()
            fixture.client.entry(stage).update(status=200, document={"data": {"already_exists": True}})
            self.failure(fixture, stage)
            later = {call["stage"] for call in fixture.client.calls}
            self.assertNotIn("role_create", later)
            self.assertNotIn("policy_create", later)

    def test_mount_role_issuer_and_policy_readback_mismatches_fail_closed(self):
        mutations = [
            ("baseline_mounts", lambda d: d["data"].update({"secret/": {"type": "kv"}})),
            ("mounts_readback", lambda d: d["data"]["secret/"].update(options={"version": "1"})),
            ("mounts_readback", lambda d: d["data"].update({"extra/": {"type": "kv"}})),
            ("pki_tune_readback", lambda d: d["data"].update(max_lease_ttl=1)),
            ("issuer_readback", lambda d: d["data"].update(default=KEY_ID)),
            ("issuer_identity_readback", lambda d: d["data"].update(key_id=ISSUER)),
            ("issuer_identity_readback", lambda d: d["data"].update(certificate=CA.replace("U3lu", "QUJD"))),
            ("role_readback", lambda d: d["data"].update(server_flag=True)),
            ("role_readback", lambda d: d["data"].update(client_flag=False)),
            ("role_readback", lambda d: d["data"].update(allow_ip_sans=True)),
            ("role_readback", lambda d: d["data"].update(allowed_domains=["example.com"])),
            ("role_readback", lambda d: d["data"].update(max_ttl=vault.CERT_SECONDS + 1)),
            ("policy_readback", lambda d: d["data"].update(policy=vault.POLICY + 'path "*" {capabilities=["sudo"]}')),
        ]
        for stage, mutation in mutations:
            with self.subTest(stage=stage):
                fixture = self.fixture()
                mutation(fixture.client.entry(stage)["document"])
                self.failure(fixture, stage)
                self.assertEqual(fixture.client.calls[-1]["stage"], stage)
                self.assertTrue((fixture.private / "root-token").exists())

    def test_extra_token_privileges_and_denied_capabilities_are_rejected(self):
        for stage, key, value in (("token_create", "policies", ["root"]), ("token_create", "token_policies", ["default"]),
                                  ("token_create", "client_token", ROOT_TOKEN), ("token_lookup_self", "policies", ["default"]),
                                  ("token_lookup_self", "identity_policies", ["extra"]), ("token_lookup_self", "renewable", True),
                                  ("token_lookup_self", "explicit_max_ttl", 0)):
            fixture = self.fixture()
            fixture.client.entry(stage)["document"]["auth" if stage == "token_create" else "data"][key] = value
            self.failure(fixture, stage)
        for path in ("sys/mounts", "auth/token/create", "secret/data/unrelated", "pki_int/issue/unrelated"):
            fixture = self.fixture()
            fixture.client.entry("capabilities_readback")["document"]["data"][path] = ["read", "sudo"]
            self.failure(fixture, "capabilities_readback")
        fixture = self.fixture()
        fixture.client.entry("capabilities_readback")["document"]["data"]["secret/metadata/clusters"] = ["deny"]
        self.failure(fixture, "capabilities_readback")

    def test_exact_empty_registry_semantics_and_forbidden_reads(self):
        for status, document in ((403, {"errors": ["permission denied"]}), (503, {"errors": ["sealed"]}),
                                 (404, {"errors": ["unsupported path"]}), (404, {}), (200, {"data": {"keys": ["foreign"]}})):
            fixture = self.fixture()
            fixture.client.entry("registry_empty_readback").update(status=status, document=document)
            self.failure(fixture, "registry_empty_readback")
        fixture = self.fixture()
        fixture.client.entry("registry_empty_readback").update(status=200, document={"data": {"keys": []}})
        self.assertTrue(fixture.run()["registry_empty_verified"])
        fixture = self.fixture()
        fixture.client.entry("forbidden_read_1").update(status=200, document={"data": {}})
        self.failure(fixture, "forbidden_read_1")

    def test_partial_malformed_and_oversize_secret_responses_stay_private(self):
        private = b'{"root_token":"' + ROOT_TOKEN.encode()
        for changed in ({"raw": private}, {"raw": private, "complete": False},
                        {"error": vault.VaultHTTPFailure(private)},
                        {"raw": private + b"x" * vault.MAX_RESPONSE},
                        {"raw": b'{"root_token":"' + ROOT_TOKEN.encode() + b'","root_token":"duplicate"}'}):
            fixture = self.fixture()
            fixture.client.entry("initialize").update(changed)
            self.failure(fixture, "initialize")
            self.assertEqual(len(fixture.client.calls), 3)
            response = (fixture.private / "response-01.bin").read_bytes()
            self.assertIn(ROOT_TOKEN.encode(), response)
            self.assertLessEqual(len(response), vault.MAX_RESPONSE)
            self.assertFalse((fixture.private / "unseal-key").exists())
        fixture = self.fixture()
        fixture.client.entry("token_create")["document"]["auth"].pop("token_policies")
        self.failure(fixture, "token_create")
        self.assertEqual((fixture.private / "backend-token").read_bytes(), BACKEND_TOKEN.encode())

    def test_exception_text_and_traceback_never_expose_client_secrets(self):
        fixture = self.fixture()
        fixture.client.entry("initialize")["error"] = RuntimeError(ROOT_TOKEN + " " + UNSEAL_KEY)
        error = self.failure(fixture, "initialize")
        self.assertEqual(error.error_type, "ExternalError")
        rendered = "".join(traceback.format_exception(error))
        self.assertNotIn(ROOT_TOKEN, rendered)
        self.assertTrue(error.__suppress_context__)

    def test_deadline_and_ledger_failure_stop_before_dependents(self):
        fixture = self.fixture()
        ticks = iter([0, vault.TOTAL_SECONDS + 1])
        self.failure(fixture, "fresh_init_read", monotonic=lambda: next(ticks))
        self.assertEqual(fixture.client.calls, [])
        fixture = self.fixture()
        def failed_sink(event):
            if event["stage"] == "initialize":
                raise RuntimeError("Synthetic durable ledger failure")
            fixture.events.append(event)
        self.failure(fixture, "initialize", evidence_sink=failed_sink)
        self.assertEqual(len(fixture.client.calls), 2)

    def test_timeout_after_secret_response_preserves_bytes_without_unseal(self):
        fixture = self.fixture()
        clock = [0.0]
        original = fixture.client.request
        def delayed(*args, **kwargs):
            response = original(*args, **kwargs)
            if fixture.client.calls[-1]["stage"] == "initialize":
                clock[0] = vault.TOTAL_SECONDS + 1
            return response
        fixture.client.request = delayed
        self.failure(fixture, "initialize", monotonic=lambda: clock[0])
        self.assertEqual(len(fixture.client.calls), 3)
        self.assertIn(ROOT_TOKEN.encode(), (fixture.private / "response-01.bin").read_bytes())
        self.assertFalse((fixture.private / "unseal-key").exists())

    def test_retry_cannot_adopt_partial_private_state(self):
        fixture = self.fixture()
        fixture.client.entry("initialize")["error"] = vault.VaultHTTPFailure(b"partial synthetic init")
        self.failure(fixture, "initialize")
        calls = len(fixture.client.calls)
        self.failure(fixture, "ownership")
        self.assertEqual(len(fixture.client.calls), calls)

    def test_no_follow_exclusive_io_and_unprotected_ancestor_rejection(self):
        fixture = self.fixture()
        with patch.object(vault, "os", fixture.metadata):
            descriptor = fixture.metadata.open(fixture.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                created = vault._new_at(descriptor, "exclusive")
                fixture.metadata.close(created)
                with self.assertRaises(FileExistsError):
                    vault._new_at(descriptor, "exclusive")
                (fixture.parent / "linked").symlink_to("exclusive")
                with self.assertRaises(OSError):
                    vault._read_at(descriptor, "linked", 1024)
            finally:
                fixture.metadata.close(descriptor)
        with self.assertRaises(PermissionError):
            vault._directory(fixture.parent)
        with self.assertRaises(ValueError):
            vault._directory(Path("/srv/../tmp"))


if __name__ == "__main__":
    unittest.main()
