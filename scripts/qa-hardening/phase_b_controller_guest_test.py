"""Hermetic B2 contracts: synthetic evidence, transport responses and temporary files."""

from __future__ import annotations

import copy
import io
import json
import os
import struct
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import phase_b_controller_guest as guest
import phase_b_controller_offline as offline
import phase_b_controller_plan as plan
import phase_b_pg_guest as pg
from phase_b_controller_apk_test import synthetic_inventory
from phase_b_controller_plan_test import PG_DRAFT, PHASE, synthetic_proofs

AUTHORIZATION = "Synthetic B2 authorization fixture"


def seal_manifest(document):
    document = copy.deepcopy(document)
    document.pop("manifest_sha256", None)
    document["manifest_sha256"] = plan.canonical_digest(document)
    return document


def synthetic_manifest(pins):
    entries = [{"path": name, "size": 1, "sha256": "a" * 64, "mode": 0o644} for name in sorted(offline.REQUIRED_SOURCE)]
    inventory = {"schema_version": 1, "entries": entries, "total_bytes": len(entries)}
    inventory["source_tree_sha256"] = offline.digest(offline.canonical(inventory))
    required_wheels = {"alembic", "asyncpg", "pytest", "pytest-asyncio", "sqlalchemy"}
    names = sorted(required_wheels | {"synthetic-" + str(i).zfill(2) for i in range(60)})
    wheels = [{"name": name, "version": "1.0", "filename": name + "-1.0-py3-none-any.whl", "size": 1, "sha256": "b" * 64} for name in names]
    proofs = synthetic_proofs(pins, full=True)
    proofs["source"].update({"source_tree_sha256": inventory["source_tree_sha256"], "file_count": len(entries)})
    proofs["packages"].update({"source_tree_sha256": inventory["source_tree_sha256"], "wheel_requirements_sha256": offline.digest(pg.requirements_lock(wheels))})
    proofs["executor_review"]["vault_helper_sha256"] = guest.HELPER_PINS["phase_b_vault_bootstrap.py"]
    draft = plan.render_b2(phase_b_id=PHASE, public_pins_bytes=pins, expected_pg_digest=PG_DRAFT, proofs=proofs)
    return seal_manifest({"schema_version": 1, "proposal": guest.seal_proposal(draft, pins), "source_inventory": inventory,
                          "wheels": {"filename": "wheels.tar", "size": 10240, "sha256": proofs["packages"]["wheel_archive_sha256"]},
                          "wheel_inventory": wheels, "npm": {"filename": "npm.tar", "size": 10240,
                          "sha256": proofs["packages"]["npm_tarballs_archive_sha256"]}, "npm_manifest": {"synthetic": True},
                          "apk": {"filename": "apk.tar", "size": 10240, "sha256": proofs["packages"]["apk_archive_sha256"]},
                          "apk_inventory": synthetic_inventory(), "apk_helper_sha256": "d" * 64,
                          "offline_adapter_sha256": "e" * 64, "executor_sha256": proofs["executor_review"]["executor_sha256"],
                          "helper_sha256": dict(guest.HELPER_PINS), "build_revision": "f" * 40,
                          "authorization_reference_sha256": offline.digest(AUTHORIZATION.encode())})


class ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pins = (Path(__file__).parent / "phase_b_public_pins.json").read_bytes()

    def setUp(self):
        self.manifest = synthetic_manifest(self.pins)

    def validate(self, document=None, authorization=AUTHORIZATION):
        document = document or self.manifest
        return guest.validate_manifest(document, document["manifest_sha256"], self.pins, authorization)

    def test_complete_synthetic_prerequisite_contract(self):
        self.assertEqual(self.validate()["proposal"], self.manifest["proposal"])

    def test_draft_without_actual_prerequisite_fields_refuses_seal(self):
        draft = plan.render_b2(phase_b_id=PHASE, public_pins_bytes=self.pins, expected_pg_digest=PG_DRAFT)
        with self.assertRaises(ValueError):
            guest.seal_proposal(draft, self.pins)

    def test_authorization_reference_is_bound(self):
        with self.assertRaises(ValueError):
            self.validate(authorization="different synthetic authority")

    def test_manifest_digest_is_bound(self):
        self.manifest["build_revision"] = "1" * 40
        with self.assertRaises(ValueError):
            self.validate()

    def test_unreviewed_helper_refused_after_manifest_rehash(self):
        self.manifest["helper_sha256"]["phase_b_vault_bootstrap.py"] = "0" * 64
        with self.assertRaises(ValueError):
            self.validate(seal_manifest(self.manifest))

    def test_source_tree_readback_is_bound(self):
        self.manifest["source_inventory"]["entries"][0]["size"] = 2
        with self.assertRaises(ValueError):
            self.validate(seal_manifest(self.manifest))

    def test_package_archive_cannot_change_after_rehash(self):
        self.manifest["npm"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            self.validate(seal_manifest(self.manifest))

    def test_budget_cannot_widen_after_two_digest_rehashes(self):
        self.manifest["proposal"]["resource_budget"]["docker_slice_memory_mib"] += 1
        self.manifest["proposal"].pop("proposal_digest")
        self.manifest["proposal"]["proposal_digest"] = plan.canonical_digest(self.manifest["proposal"])
        with self.assertRaises(ValueError):
            self.validate(seal_manifest(self.manifest))

    def test_vault_review_pin_is_enforced(self):
        draft = plan.render_b2(phase_b_id=PHASE, public_pins_bytes=self.pins, expected_pg_digest=PG_DRAFT, proofs=synthetic_proofs(self.pins, full=True))
        self.manifest["proposal"] = guest.seal_proposal(draft, self.pins)
        with self.assertRaises(ValueError):
            self.validate(seal_manifest(self.manifest))

    def test_validation_does_not_perform_network_filesystem_or_process_actions(self):
        with patch.object(guest.os, "open", side_effect=AssertionError("Unexpected filesystem action")), \
                patch.object(guest.subprocess, "run", side_effect=AssertionError("Unexpected process action")), \
                patch.object(guest.PrivateHTTP, "send", side_effect=AssertionError("Unexpected HTTP action")):
            self.validate()


class PayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pins = (Path(__file__).parent / "phase_b_public_pins.json").read_bytes()
        cls.proposal = synthetic_manifest(pins)["proposal"]
        cls.images = {name: item["config_digest"] for name, item in cls.proposal["images"].items()}
        cls.images.update({"backend": "sha256:" + "1" * 64, "frontend": "sha256:" + "2" * 64})

    def payload(self, role):
        return guest.service_payload(self.proposal, role, "a" * 64, self.images, Path("/synthetic-phase"), {})

    def test_all_eight_services_have_exact_limits_and_no_restart(self):
        for role, (memory, cpu, pids) in plan.SERVICE_LIMITS.items():
            with self.subTest(role=role):
                host = self.payload(role)["HostConfig"]
                self.assertEqual((host["Memory"], host["MemorySwap"], host["NanoCpus"], host["PidsLimit"]), (memory * 1024 ** 2, memory * 1024 ** 2, cpu * 10 ** 6, pids))
                self.assertEqual(host["RestartPolicy"]["Name"], "no")
                self.assertFalse(host["Privileged"])
                self.assertFalse(host["AutoRemove"])
                self.assertEqual(host["SecurityOpt"], ["no-new-privileges:true"])
                self.assertEqual(host["CgroupParent"], plan.LAB + ".slice")

    def test_only_four_exact_loopback_ports_are_published(self):
        for role in plan.SERVICE_LIMITS:
            bindings = self.payload(role)["HostConfig"]["PortBindings"]
            if role in guest.PORTS:
                inside, outside = guest.PORTS[role]
                self.assertEqual(bindings, {str(inside) + "/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(outside)}]})
            else:
                self.assertEqual(bindings, {})

    def test_no_service_mounts_a_runtime_socket(self):
        for role in plan.SERVICE_LIMITS:
            for mount in self.payload(role)["HostConfig"]["Mounts"]:
                self.assertNotIn(mount["Target"], plan.SOCKET_EXCLUSIONS)
                self.assertFalse(mount["Target"].endswith(".sock"))

    def test_vault_uses_nonroot_no_capabilities_and_only_its_config_file(self):
        payload = self.payload("vault")
        self.assertEqual(payload["User"], "100:100")
        self.assertEqual(payload["HostConfig"]["CapAdd"], [])
        binds = [mount for mount in payload["HostConfig"]["Mounts"] if mount["Type"] == "bind"]
        self.assertEqual(len(binds), 1)
        self.assertTrue(binds[0]["ReadOnly"])
        self.assertTrue(binds[0]["Source"].endswith("/vault.hcl"))

    def test_unknown_service_is_denied(self):
        with self.assertRaises(ValueError):
            self.payload("neighbor")


class NetworkReadbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pins = (Path(__file__).parent / "phase_b_public_pins.json").read_bytes()
        cls.proposal = synthetic_manifest(pins)["proposal"]

    def network_response(self):
        reviewed = self.proposal["resources"]["network"]
        return {"Id": "a" * 64, "Name": reviewed["name"], "Created": "2026-10-01T00:00:00Z", "Scope": "local",
                "Driver": "bridge", "Internal": True, "Attachable": False, "EnableIPv6": False,
                "Labels": copy.deepcopy(reviewed["labels"]), "Containers": {},
                "Options": {"com.docker.network.bridge.name": reviewed["bridge_name"],
                            "com.docker.network.bridge.enable_ip_masquerade": "false"},
                "IPAM": {"Driver": "default", "Options": None,
                         "Config": [{"Subnet": reviewed["subnet"], "Gateway": "172.28.66.1"}]}}

    def exercise(self, response, *, denied=False):
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "synthetic-ledger.jsonl"
            controller = object.__new__(guest.Controller)
            controller.created, controller.sequence, controller.phase = {}, 0, PHASE
            controller.deadline, controller.plan, controller.state = time.monotonic() + 10, self.proposal, Path("/synthetic-phase")
            controller.transport = Mock()
            volumes = {}
            def request(method, path, *, body, **_kwargs):
                if method == "POST" and path == "/networks/create":
                    self.assertNotIn("Options", body["IPAM"])
                    return 201, json.dumps({"Id": "a" * 64, "Warning": ""}).encode()
                if method == "GET" and path == "/networks/" + "a" * 64:
                    rows = [json.loads(row) for row in ledger_path.read_text().splitlines()]
                    accepted = next(index for index, row in enumerate(rows) if row["event"] == "resource_accepted")
                    self.assertEqual(rows[accepted]["resource_id"], "a" * 64)
                    self.assertEqual(controller.created["network"], "a" * 64)
                    self.assertLess(accepted, len(rows) - 1)
                    self.assertEqual(rows[-1]["event"], "api_attempt")
                    return 200, json.dumps(response).encode()
                if method == "POST" and path == "/volumes/create":
                    volumes[body["Name"]] = {"Name": body["Name"], "Driver": "local", "Labels": body["Labels"], "Options": body["DriverOpts"]}
                    return 201, json.dumps(volumes[body["Name"]]).encode()
                if method == "GET" and path.startswith("/volumes/"):
                    return 200, json.dumps(volumes[path.removeprefix("/volumes/")]).encode()
                raise AssertionError("Unexpected synthetic Docker operation")
            controller.transport.request.side_effect = request
            with ledger_path.open("x+") as controller.ledger:
                if denied:
                    with self.assertRaises(ValueError):
                        controller.create_network_volumes()
                    self.assertEqual(controller.created, {"network": "a" * 64})
                    self.assertEqual(volumes, {})
                    self.assertEqual(controller.transport.request.call_count, 2)
                else:
                    controller.create_network_volumes()
                    self.assertEqual(len(volumes), len(self.proposal["resources"]["volumes"]))

    def test_realistic_inspect_optional_empty_fields_differ_from_create_body(self):
        for options in (None, {}):
            for configuration in ({}, {"IPRange": ""}, {"AuxiliaryAddresses": None}, {"AuxiliaryAddresses": {}},
                                  {"IPRange": "", "AuxiliaryAddresses": {}}):
                with self.subTest(options=options, configuration=configuration):
                    response = self.network_response()
                    response["IPAM"]["Options"] = options
                    response["IPAM"]["Config"][0].update(configuration)
                    self.exercise(response)
        response = self.network_response()
        response["IPAM"].pop("Options")
        self.exercise(response)

    def test_malformed_nonempty_or_unknown_ipam_stops_after_durable_network_acceptance(self):
        for key, values in {"Driver": (None, "custom"), "Options": ([], False, "", {"unreviewed": "value"}),
                            "Config": (None, [], {}, [None], [{}, {}]), "Unknown": ({},)}.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    response = self.network_response()
                    response["IPAM"][key] = value
                    self.exercise(response, denied=True)
        for missing in ("Driver", "Config"):
            response = self.network_response()
            response["IPAM"].pop(missing)
            self.exercise(response, denied=True)
        response = self.network_response()
        response["IPAM"] = None
        self.exercise(response, denied=True)

    def test_subnet_gateway_ranges_and_auxiliary_addresses_remain_strict(self):
        for key, values in {"Subnet": (None, "172.28.66.0/25"), "Gateway": (None, "172.28.66.2"),
                            "IPRange": (None, False, {}, "172.28.66.128/25"),
                            "AuxiliaryAddresses": ([], False, "", {"reserved": "172.28.66.2"}), "Unknown": ("",)}.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    response = self.network_response()
                    response["IPAM"]["Config"][0][key] = value
                    self.exercise(response, denied=True)
        response = self.network_response()
        response["IPAM"]["Config"].append(copy.deepcopy(response["IPAM"]["Config"][0]))
        self.exercise(response, denied=True)

    def test_network_id_and_security_fields_still_match_the_reviewed_creation(self):
        for key, value in (("Id", "b" * 64), ("Name", "neighbor"), ("Driver", "overlay"), ("Internal", False),
                           ("Attachable", True), ("EnableIPv6", True), ("Labels", {}), ("Options", {}),
                           ("Containers", {"neighbor": {"Name": "neighbor"}})):
            with self.subTest(key=key):
                response = self.network_response()
                response[key] = value
                self.exercise(response, denied=True)


class TransportTests(unittest.TestCase):
    def controller(self, status=201, result=None):
        controller = object.__new__(guest.Controller)
        controller.created, controller.sequence, controller.stage, controller.ledger = {}, 0, "synthetic", io.StringIO()
        controller.phase, controller.deadline = PHASE, time.monotonic() + 10
        controller.transport = Mock()
        controller.transport.request.return_value = (status, json.dumps(result or {"Id": "a" * 64}).encode())
        controller.record = Mock()
        return controller

    def test_accepted_id_exists_before_ledger_callback(self):
        controller = self.controller()
        def record(event, **fields):
            if event == "resource_accepted":
                self.assertEqual(controller.created["backend"], "a" * 64)
        controller.record.side_effect = record
        controller.api("POST", "/containers/create", expected=201, accept="backend")

    def test_accepted_id_survives_ledger_failure(self):
        controller = self.controller()
        def record(event, **fields):
            if event == "resource_accepted":
                raise OSError("Synthetic disk failure")
        controller.record.side_effect = record
        with self.assertRaises(OSError):
            controller.api("POST", "/containers/create", expected=201, accept="backend")
        self.assertEqual(controller.created["backend"], "a" * 64)
        self.assertEqual(controller.transport.request.call_count, 1)

    def test_api_failure_never_retries(self):
        controller = self.controller(status=500, result={"synthetic": True})
        with self.assertRaises(RuntimeError):
            controller.api("POST", "/containers/create", expected=201, accept="backend")
        self.assertEqual(controller.created, {})
        self.assertEqual(controller.transport.request.call_count, 1)

    def test_volume_names_are_exact(self):
        controller = self.controller(result={"Name": "neighbor"})
        with self.assertRaises(ValueError):
            controller.api("POST", "/volumes/create", expected=201, accept="volume:owned")

    def test_response_demultiplex_preserves_streams(self):
        body = struct.pack(">BBBBI", 1, 0, 0, 0, 3) + b"out" + struct.pack(">BBBBI", 2, 0, 0, 0, 3) + b"err"
        self.assertEqual(guest.demultiplex(body), (b"out", b"err"))

    def test_truncated_response_frames_fail(self):
        for body in (b"bad", struct.pack(">BBBBI", 1, 0, 0, 0, 3) + b"a", struct.pack(">BBBBI", 3, 0, 0, 0, 0)):
            with self.subTest(body=body), self.assertRaises(ValueError):
                guest.demultiplex(body)

    def test_loopback_transport_rejects_outside_port_before_connecting(self):
        with patch.object(guest.http.client, "HTTPConnection", side_effect=AssertionError("Unexpected connection")), self.assertRaises(ValueError):
            guest.PrivateHTTP.send(8443, "GET", "/", {}, None, 1, 1024)

    def test_idp_transport_rejects_redirect_authority(self):
        with self.assertRaises(ValueError):
            guest.IdPClient().request("GET", "http://neighbor.invalid/api/v3/core/users/", headers={}, body=None,
                                      timeout_seconds=1, maximum_response_bytes=1024, follow_redirects=False)

    def test_vault_transport_preserves_received_partial_initialization_body_privately(self):
        client = guest.VaultClient(None)
        with patch.object(guest.PrivateHTTP, "send", side_effect=guest.PrivateHTTPFailure(b"synthetic partial body")), \
                self.assertRaises(guest.vault.VaultHTTPFailure) as raised:
            client.request("POST", "/v1/sys/init", token=None, body=b"{}", timeout_seconds=1, max_response_bytes=1024)
        self.assertEqual(raised.exception.partial_body, b"synthetic partial body")
        self.assertNotIn("synthetic partial body", str(raised.exception))


class ContainerReadbackTests(unittest.TestCase):
    def setUp(self):
        pins = (Path(__file__).parent / "phase_b_public_pins.json").read_bytes()
        self.proposal = synthetic_manifest(pins)["proposal"]
        self.controller = object.__new__(guest.Controller)
        self.controller.plan = self.proposal
        self.controller.created = {"frontend": "c" * 64, "network": "a" * 64}
        self.controller.images = {"frontend": "sha256:" + "2" * 64}
        self.controller.image_configs = {"frontend": {"Entrypoint": ["/app/entrypoint.sh"], "Cmd": [], "User": "1001:1001",
                                                     "WorkingDir": "/app", "Env": ["PATH=/synthetic"], "Labels": {"image": "public"}}}
        payload = guest.service_payload(self.proposal, "frontend", "a" * 64, self.controller.images, Path("/synthetic-phase"), {"SYNTHETIC": "value"})
        self.controller.payloads = {"frontend": payload}
        self.item = {"Id": "c" * 64, "Image": "sha256:" + "2" * 64, "AppArmorProfile": "docker-default",
                     "HostConfig": copy.deepcopy(payload["HostConfig"]), "Mounts": [],
                     "Config": {**self.controller.image_configs["frontend"], "Cmd": None,
                                "Env": ["PATH=/synthetic", "SYNTHETIC=value"],
                                "Labels": {"image": "public", **payload["Labels"]}},
                     "State": {"Running": True, "OOMKilled": False, "Dead": False, "Error": "", "Pid": 1234},
                     "NetworkSettings": {"Networks": {self.proposal["resources"]["network"]["name"]: {"NetworkID": "a" * 64}},
                                         "Ports": copy.deepcopy(payload["HostConfig"]["PortBindings"])}}
        self.controller.api = Mock(return_value=self.item)

    def test_exact_readback_accepts_image_labels_null_empty_command_and_separate_tmpfs(self):
        self.assertEqual(self.controller.inspect_container("frontend", running=True), self.item)

    def test_cgroup_v2_null_oom_readback_accepts_only_the_explicit_false_request(self):
        self.assertIs(self.controller.payloads["frontend"]["HostConfig"]["OomKillDisable"], False)
        self.item["HostConfig"]["OomKillDisable"] = None
        self.assertEqual(self.controller.inspect_container("frontend", running=True), self.item)
        for value in (True, 0, 1, 0.0, "false", "", [], {}):
            self.item["HostConfig"]["OomKillDisable"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.controller.inspect_container("frontend", running=True)
        self.item["HostConfig"].pop("OomKillDisable")
        with self.assertRaises(ValueError):
            self.controller.inspect_container("frontend", running=True)

    def test_null_oom_flag_never_relaxes_other_false_security_fields_or_resource_limits(self):
        for field in ("Privileged", "AutoRemove", "PublishAllPorts", "Memory", "MemorySwap", "NanoCpus", "PidsLimit"):
            changed = copy.deepcopy(self.item)
            changed["HostConfig"].update({"OomKillDisable": None, field: None})
            self.controller.api.return_value = changed
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.controller.inspect_container("frontend", running=True)

    def test_nil_empty_security_collections_are_equivalent_without_relaxing_nonempty_values(self):
        self.item["HostConfig"]["CapAdd"] = None
        self.item["HostConfig"]["Devices"] = None
        self.controller.inspect_container("frontend", running=True)
        self.item["HostConfig"]["CapAdd"] = ["SYS_ADMIN"]
        with self.assertRaises(ValueError):
            self.controller.inspect_container("frontend", running=True)

    def test_every_unapproved_identity_mount_namespace_port_or_environment_is_denied(self):
        changes = [lambda d: d.update(Id="d" * 64), lambda d: d.update(Image="sha256:" + "d" * 64),
                   lambda d: d["Config"]["Labels"].update(neighbor="value"),
                   lambda d: d["HostConfig"].update(PidMode="host"), lambda d: d["HostConfig"].update(IpcMode="host"),
                   lambda d: d["HostConfig"].update(Devices=[{"PathOnHost": "/dev/kvm"}]),
                   lambda d: d["HostConfig"].update(ExtraHosts=["neighbor:127.0.0.1"]),
                   lambda d: d["Mounts"].append({"Type": "bind", "Destination": "/run/docker.sock"}),
                   lambda d: d["NetworkSettings"]["Ports"]["2025/tcp"][0].update(HostIp="0.0.0.0"),
                   lambda d: d["Config"]["Env"].append("UNREVIEWED=value"),
                   lambda d: d["Config"]["Env"].append("PATH=/duplicate"),
                   lambda d: d["NetworkSettings"]["Networks"].update(neighbor={"NetworkID": "d" * 64})]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                altered = copy.deepcopy(self.item)
                change(altered)
                self.controller.api.return_value = altered
                with self.assertRaises(ValueError):
                    self.controller.inspect_container("frontend", running=True)

    def test_created_endpoint_can_be_unattached_but_running_endpoint_must_bind_exact_id(self):
        self.item["NetworkSettings"]["Networks"][self.proposal["resources"]["network"]["name"]]["NetworkID"] = ""
        self.item["State"]["Running"] = False
        self.item["NetworkSettings"]["Ports"] = {}
        self.controller.inspect_container("frontend", running=False)
        self.item["State"]["Running"] = True
        with self.assertRaises(ValueError):
            self.controller.inspect_container("frontend", running=True)


class ArchiveReadbackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / "fresh"
        self.destination.mkdir(mode=0o700)
        self.controller = object.__new__(guest.Controller)
        self.controller.deadline = time.monotonic() + 10
        self.controller.record = Mock()
        for patched in (patch.object(pg, "open_regular", side_effect=lambda path: path.open("rb")),
                        patch.object(pg, "new_directory", side_effect=lambda path: path.mkdir(mode=0o700)),
                        patch.object(pg, "open_directory", side_effect=lambda path: os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))):
            patched.start()
            self.addCleanup(patched.stop)
        self.body = b"Synthetic public archive member"
        self.entries = {"frontend/package-lock.json": {"size": len(self.body), "sha256": offline.digest(self.body)}}

    def archive(self, names=None, *, kind=tarfile.REGTYPE):
        path = self.root / "input.tar"
        with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as bundle:
            for name in names or self.entries:
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.type = len(self.body), 0o644, kind
                if kind != tarfile.REGTYPE:
                    info.size, info.linkname = 0, "/synthetic-outside"
                bundle.addfile(info, io.BytesIO(self.body) if kind == tarfile.REGTYPE else None)
        return path

    def test_exact_regular_member_is_hashed_written_and_captured(self):
        captured = self.controller.unpack(self.archive(), self.entries, self.destination)
        self.assertEqual(captured, {"frontend/package-lock.json": self.body})
        self.assertEqual((self.destination / "frontend/package-lock.json").read_bytes(), self.body)

    def test_duplicate_link_traversal_or_hash_change_is_denied(self):
        scenarios = [(list(self.entries) * 2, tarfile.REGTYPE, False), (["../outside"], tarfile.REGTYPE, False),
                     (list(self.entries), tarfile.SYMTYPE, False), (list(self.entries), tarfile.REGTYPE, True)]
        for index, (names, kind, corrupt) in enumerate(scenarios):
            with self.subTest(index=index):
                destination = self.root / ("scenario-" + str(index))
                destination.mkdir(mode=0o700)
                entries = copy.deepcopy(self.entries)
                if corrupt:
                    entries["frontend/package-lock.json"]["sha256"] = "0" * 64
                with self.assertRaises(ValueError):
                    self.controller.unpack(self.archive(names, kind=kind), entries, destination)
        self.assertFalse((self.root / "outside").exists())

    def test_existing_target_and_symlinked_parent_are_never_adopted(self):
        (self.destination / "frontend").mkdir()
        target = self.destination / "frontend/package-lock.json"
        target.write_bytes(b"Synthetic existing state")
        with self.assertRaises(FileExistsError):
            self.controller.unpack(self.archive(), self.entries, self.destination)
        self.assertEqual(target.read_bytes(), b"Synthetic existing state")
        outside = self.root / "outside"
        outside.mkdir()
        another = self.root / "symlink-scenario"
        another.mkdir()
        (another / "frontend").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.controller.unpack(self.archive(), self.entries, another)
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
