"""Synthetic clients and temporary private state only; never contact an IdP."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
import uuid
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import phase_b_idp_bootstrap as idp
from phase_b_vault_bootstrap_test import OwnedRootMetadata

BOOTSTRAP_TOKEN = "synthetic-bootstrap-" + "a" * 48
SERVICE_TOKEN = "synthetic-service-" + "b" * 48
SYNTHETIC_PASSWORD = "private-synthetic-password-with-no-real-identity"
SYNTHETIC_HASH = "pbkdf2_sha256$1000000$syntheticSalt$" + base64.b64encode(b"h" * 32).decode()
MAPPING_EXPRESSIONS = {
    "openid": "# Synthetic fixture for the pinned semantics\nreturn {}\n",
    "email": "return {'email': request.user.email, 'email_verified': False}\n",
    "offline_access": "return {}\n",
    "profile": """avatar = request.user.avatar
return delete_none_values({
    'name': request.user.name,
    'given_name': ak_obj_attr(request.user, 'given_name', 'name'),
    'family_name': ak_obj_attr(request.user, 'family_name'),
    'preferred_username': request.user.username,
    'nickname': request.user.username,
    'groups': [group.name for group in request.user.groups.all()],
    'picture': avatar if avatar and not avatar.startswith('data:') else None,
})
""",
}


def synthetic_proposal() -> dict:
    proposal = {
        "schema_version": 1, "phase": "B2", "phase_b_id": str(uuid.uuid4()),
        "ready_for_guest_execution": True, "remaining_gates": [],
        "target": {"vm_name": idp.LAB, "vm_run_id": idp.VM_RUN_ID, "phase_a_digest": idp.PHASE_A_DIGEST,
                   "b1_phase_id": idp.B1_ID, "project": "default", "profiles": [], "outer_nic_count": 0},
        "b1_readback": {"proposal_digest": idp.B1_DIGEST, "helper_sha256": idp.B1_HELPER},
        "idp_bootstrap": idp.expected_idp_spec(), "other_component": {"draft_test_fixture": True},
    }
    proposal["proposal_digest"] = idp.canonical_digest(proposal)
    return proposal


def reseal(proposal: dict) -> dict:
    proposal.pop("proposal_digest", None)
    proposal["proposal_digest"] = idp.canonical_digest(proposal)
    return proposal


def synthetic_attestation(proposal: dict) -> dict:
    return {
        "container_id": "c" * 64, "created_id": "c" * 64, "created_for_phase": proposal["phase_b_id"],
        "labels": {"orcastra.qa.lab": idp.LAB, "orcastra.qa.vm_run": idp.VM_RUN_ID,
                   "orcastra.qa.phase_b": proposal["phase_b_id"]},
        "image_id": idp.IMAGE_CONFIG, "origin_manifest_digest": idp.IMAGE_MANIFEST,
        "endpoint": idp.API_ORIGIN, "host_header": idp.API_HOST,
        "published_ports": {"32090/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32090"}]},
        "new_container_verified": True, "fresh_idp_database_verified": True,
        "full_config_verified": True, "runtime_boundary_verified": True, "readiness_verified": True,
    }


class SyntheticClient:
    """A strict local Authentik-shaped fake, including ten-row permission pagination."""

    def __init__(self, proposal: dict):
        self.proposal = proposal
        self.calls = []
        self.groups, self.users, self.roles = {}, {}, {}
        self.grants, self.globals = [], []
        self.token, self.signing, self.provider, self.application = None, None, None, None
        self.before_request, self.response_hook = None, None
        self.flows = [{**value, "pk": str(uuid.uuid4())} for value in idp.FLOWS.values()]
        self.mappings = [{**value, "pk": str(uuid.uuid4()), "expression": MAPPING_EXPRESSIONS[value["scope_name"]]}
                         for value in idp.expected_idp_spec()["mappings"]]

    @staticmethod
    def page(rows: list, query: dict, *, size: int = 100) -> dict:
        page = int(query.get("page", ["1"])[0])
        total_pages = max(1, (len(rows) + size - 1) // size)
        start, end = (page - 1) * size, min(page * size, len(rows))
        return {"pagination": {"count": len(rows), "next": page + 1 if page < total_pages else 0,
                               "previous": page - 1, "current": page, "total_pages": total_pages,
                               "start_index": start + 1 if rows else 0, "end_index": end},
                "results": copy.deepcopy(rows[start:end]), "autocomplete": {}}

    def request(self, method, url, *, headers, body, timeout_seconds, maximum_response_bytes, follow_redirects):
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.netloc != "127.0.0.1:32090" or parsed.fragment:
            raise AssertionError("A request escaped the fixed loopback API")
        if headers["Host"] != idp.API_HOST or follow_redirects or not 0 < timeout_seconds <= 15:
            raise AssertionError("A request escaped its host/deadline policy")
        if maximum_response_bytes != idp.MAX_RESPONSE:
            raise AssertionError("A request lacks the exact response bound")
        payload = None if body is None else json.loads(body)
        scoped = headers["Authorization"] == "Bearer " + SERVICE_TOKEN
        if headers["Authorization"] not in {"Bearer " + BOOTSTRAP_TOKEN, "Bearer " + SERVICE_TOKEN}:
            raise AssertionError("An unexpected credential reached the fake")
        self.calls.append((method, parsed.path, parsed.query, payload, scoped))
        if self.before_request:
            self.before_request(method, parsed.path, payload)
        status, data = self._route(method, parsed.path, parse_qs(parsed.query), payload, scoped)
        response = idp.BoundedResponse(status, b"" if data is None else json.dumps(data).encode(), 0.001)
        if self.response_hook:
            response = self.response_hook(method, parsed.path, payload, response)
        return response

    def _route(self, method, path, query, payload, scoped):
        base = "/api/v3"
        if method == "GET" and path == base + "/core/users/me/":
            if not scoped:
                return 200, {"user": {"username": "akadmin", "is_superuser": True}, "users": []}
            user = next(row for row in self.users.values() if row["username"] == idp.SERVICE)
            role = next(iter(self.roles.values()))
            return 200, {"user": {"pk": user["pk"], "username": user["username"], "is_superuser": False,
                                  "groups": [], "roles": [{"pk": role["pk"], "name": role["name"]}],
                                  "system_permissions": []}, "users": []}
        if method == "GET" and path == base + "/flows/instances/":
            return 200, self.page([r for r in self.flows if r["slug"] == query["slug"][0]], query)
        if method == "GET" and path == base + "/propertymappings/provider/scope/":
            return 200, self.page([r for r in self.mappings if r["managed"] == query["managed"][0]], query)
        if method == "GET" and path.startswith(base + "/propertymappings/provider/scope/"):
            return 200, next(r for r in self.mappings if r["pk"] == path.rstrip("/").split("/")[-1])
        if method == "GET" and path == base + "/rbac/permissions/":
            if "role" in query:
                return 200, self.page(self.globals, query)
            return 200, self.page([{"id": 1, "codename": query["codename"][0],
                                    "model": query["content_type__model"][0], "app_label": "authentik_core"}], query)
        if method == "GET" and path == base + "/rbac/permissions/roles/":
            return 200, self.page(self.grants, query, size=10)
        if method == "POST" and path.startswith(base + "/rbac/permissions/assigned_by_roles/"):
            if set(payload) != {"permissions", "model", "object_pk"} or not payload["object_pk"]:
                raise AssertionError("A global or malformed permission assignment was attempted")
            result = []
            for permission in payload["permissions"]:
                identifier = str(len(self.grants) + 1)
                self.grants.append({"id": int(identifier), "app_label": "authentik_core",
                                    "model": payload["model"].split(".")[1],
                                    "object_pk": payload["object_pk"], "codename": permission.split(".")[1]})
                result.append({"id": identifier})
            return 200, result
        if path == base + "/core/groups/":
            if method == "GET":
                return 200, self.page([r for r in self.groups.values() if query["search"][0] in r["name"]], query)
            row = {**payload, "pk": str(uuid.uuid4()), "roles_obj": [], "parents_obj": None,
                   "children_obj": None, "inherited_roles_obj": None}
            self.groups[row["pk"]] = row
            return 201, copy.deepcopy(row)
        if method == "GET" and path.startswith(base + "/core/groups/"):
            row = copy.deepcopy(self.groups[path.rstrip("/").split("/")[-1]])
            for field, option in (("parents_obj", "include_parents"), ("children_obj", "include_children"),
                                  ("inherited_roles_obj", "include_inherited_roles")):
                if query.get(option) == ["true"]:
                    row[field] = [] if row[field] is None else row[field]
            return 200, row
        if path == base + "/rbac/roles/":
            if method == "GET":
                return 200, self.page(list(self.roles.values()), query)
            row = {**payload, "pk": str(uuid.uuid4())}
            self.roles[row["pk"]] = row
            return 201, row
        if method == "GET" and path.startswith(base + "/rbac/roles/"):
            return 200, self.roles[path.rstrip("/").split("/")[-1]]
        if path == base + "/core/users/":
            if method == "GET":
                rows = list(self.users.values())
                if "email" in query:
                    rows = [r for r in rows if r["email"] == query["email"][0]]
                else:
                    wanted = query["search"][0]
                    rows = [r for r in rows if wanted in r["email"] or wanted in r["username"]]
                return 200, self.page(rows, query)
            if any(k in payload for k in ("password", "password_hash", "permissions", "is_superuser")):
                raise AssertionError("Create-user API received blueprint-only or privilege fields")
            pk = len(self.users) + 10
            row = {**payload, "pk": pk, "is_superuser": False,
                   "groups_obj": [{"pk": g, "name": self.groups[g]["name"], "is_superuser": False}
                                  for g in payload["groups"]],
                   "roles_obj": [{"pk": r, "name": self.roles[r]["name"]} for r in payload["roles"]]}
            self.users[pk] = row
            for group in payload["groups"]:
                self.groups[group]["users"].append(pk)
            return 201, copy.deepcopy(row)
        if method == "POST" and path.endswith("/set_password_hash/"):
            if set(payload) != {"password"} or not payload["password"].startswith("pbkdf2_sha256$"):
                raise AssertionError("Hash import used the wrong API field or plaintext")
            return 204, None
        if method == "GET" and path.startswith(base + "/core/users/"):
            return 200, copy.deepcopy(self.users[int(path.rstrip("/").split("/")[-1])])
        if path == base + "/core/tokens/":
            if method == "GET":
                return 200, self.page([] if self.token is None else [self.token], query)
            if any(k in payload for k in ("key", "expires")):
                raise AssertionError("Token creation guessed unsupported key or expiry semantics")
            self.token = {**payload, "pk": str(uuid.uuid4()), "managed": None,
                          "expires": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}
            return 201, self.token
        if method == "GET" and path.endswith("/view_key/"):
            return 200, {"key": SERVICE_TOKEN}
        if method == "GET" and path.startswith(base + "/core/tokens/"):
            return 200, self.token
        if method == "GET" and path == base + "/crypto/certificatekeypairs/":
            return 200, self.page([] if self.signing is None else [self.signing], query)
        if method == "POST" and path == base + "/crypto/certificatekeypairs/generate/":
            self.signing = {"pk": str(uuid.uuid4()), "name": payload["common_name"], "managed": None,
                            "private_key_available": True, "key_type": "rsa",
                            "fingerprint_sha256": ":".join(["ab"] * 32)}
            return 200, self.signing
        if method == "GET" and path.startswith(base + "/crypto/certificatekeypairs/"):
            return 200, self.signing
        if path == base + "/providers/oauth2/":
            if method == "GET":
                return 200, self.page([] if self.provider is None else [self.provider], query)
            self.provider = {**payload, "pk": 71, "assigned_application_slug": None,
                             "assigned_backchannel_application_slug": None}
            return 201, self.provider
        if method == "GET" and path.endswith("/setup_urls/"):
            return 200, {"issuer": idp.ISSUER, "authorize": "http://" + idp.API_HOST + "/application/o/authorize/",
                         "token": "http://" + idp.API_HOST + "/application/o/token/",
                         "user_info": "http://" + idp.API_HOST + "/application/o/userinfo/",
                         "provider_info": idp.ISSUER + ".well-known/openid-configuration",
                         "logout": idp.ISSUER + "end-session/", "jwks": idp.ISSUER + "jwks/",
                         "dcr_registration": idp.ISSUER + "register/"}
        if method == "GET" and path.startswith(base + "/providers/oauth2/"):
            return 200, self.provider
        if path == base + "/core/applications/":
            if method == "GET":
                return 200, self.page([] if self.application is None else [self.application], query)
            self.application = {**payload, "pk": str(uuid.uuid4())}
            self.provider["assigned_application_slug"] = payload["slug"]
            return 201, self.application
        if method == "GET" and path.startswith(base + "/core/applications/"):
            return 200, self.application
        raise AssertionError("An unreviewed endpoint or method reached the fake")


class ProposalBoundaryTests(unittest.TestCase):
    def test_historical_partial_b1_cannot_satisfy_current_runtime_binding(self):
        proposal = synthetic_proposal()
        proposal["target"]["b1_phase_id"] = "cdf24503-772e-4ad0-a194-04e965c6e44d"
        proposal["b1_readback"]["proposal_digest"] = "c70f6bbeb9de8c4fc97394be97e0ac408badcdd3743b6ce28f7d8dcd75a3a2ba"
        reseal(proposal)
        with self.assertRaises(ValueError):
            idp.validate_proposal(proposal, proposal["proposal_digest"], "synthetic-approval")

    def test_approval_covers_other_components_before_private_access_or_credentials(self):
        proposal = synthetic_proposal()
        proposal["other_component"]["draft_test_fixture"] = False
        client = SyntheticClient(proposal)
        with patch.object(idp, "_open_directory") as opened, patch.object(idp, "_password_pair") as password:
            with self.assertRaises(ValueError):
                idp.bootstrap_idp(proposal, proposal["proposal_digest"], "synthetic-approval", client=client,
                                  private_state_root=Path("/srv") / idp.LAB / "state" / proposal["phase_b_id"],
                                  container_attestation=synthetic_attestation(proposal), bootstrap_token=BOOTSTRAP_TOKEN)
            opened.assert_not_called()
            password.assert_not_called()
        self.assertEqual(client.calls, [])

    def test_draft_extra_groups_grants_and_invalid_source_pins_fail_before_credentials(self):
        changes = (
            lambda p: p.update(ready_for_guest_execution=False),
            lambda p: p.update(remaining_gates=["Actual B1 evidence unavailable"]),
            lambda p: p["idp_bootstrap"]["groups"].append({"name": "neighbor"}),
            lambda p: p["idp_bootstrap"]["backend_service"]["global_permissions"].append("authentik_core.change_user"),
            lambda p: p["idp_bootstrap"]["image"].update(config_digest="sha256:" + "d" * 64),
            lambda p: p["target"].update(vm_run_id=str(uuid.uuid4())),
            lambda p: p["target"].update(profiles=["default"]),
            lambda p: p["b1_readback"].update(proposal_digest="d" * 64),
            lambda p: p.update(phase_b_id=idp.B1_ID),
            lambda p: p["idp_bootstrap"]["passwords"].update(api_action="set_password"),
            lambda p: p.update(schema_version=True),
            lambda p: p["target"].update(outer_nic_count=False),
            lambda p: p["idp_bootstrap"]["signing_key"].update(validity_days=30.0),
            lambda p: p["idp_bootstrap"]["provider"].update(include_claims_in_id_token=1),
        )
        for change in changes:
            with self.subTest(change=changes.index(change)):
                proposal = synthetic_proposal()
                change(proposal)
                reseal(proposal)
                with self.assertRaises(ValueError):
                    idp.validate_proposal(proposal, proposal["proposal_digest"], "synthetic-approval")

    def test_container_adoption_extra_labels_non_loopback_or_unproved_database_are_rejected(self):
        changes = (
            lambda a: a.update(created_id="d" * 64),
            lambda a: a.update(created_for_phase=idp.B1_ID),
            lambda a: a.update(new_container_verified=False),
            lambda a: a.update(fresh_idp_database_verified=False),
            lambda a: a.update(full_config_verified=False),
            lambda a: a["labels"].update(neighbor="true"),
            lambda a: a["published_ports"]["32090/tcp"][0].update(HostIp="0.0.0.0"),
            lambda a: a.update(endpoint="http://localhost:9000"),
            lambda a: a.update(extra="unapproved"),
        )
        proposal = synthetic_proposal()
        for change in changes:
            with self.subTest(change=changes.index(change)):
                attestation = synthetic_attestation(proposal)
                change(attestation)
                with self.assertRaises(ValueError):
                    idp.validate_container_attestation(attestation, proposal["phase_b_id"])

    def test_pbkdf2_password_import_encodes_a_real_valid_django_verifier(self):
        password, encoded = idp._password_pair()
        algorithm, iterations, salt, digest = encoded.split("$")
        self.assertEqual(algorithm, "pbkdf2_sha256")
        self.assertEqual(int(iterations), 1_000_000)
        self.assertGreaterEqual(len(password), 40)
        expected = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
        self.assertEqual(base64.b64decode(digest, validate=True), expected)
        self.assertNotIn(password, encoded)


class SyntheticBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-idp-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.guest, self.markers = self.base / idp.LAB, self.base / "markers"
        self.guest.mkdir(mode=0o700)
        self.markers.mkdir(mode=0o700)
        (self.guest / "state").mkdir(mode=0o700)
        for name, value in ((idp.LAB, idp.LAB), (idp.LAB + "-run", idp.VM_RUN_ID)):
            marker = self.markers / name
            marker.write_text(value + "\n")
            marker.chmod(0o600)
        self.metadata = OwnedRootMetadata(self.base)
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.object(idp, "os", self.metadata))
        patches.enter_context(patch.object(idp, "GUEST_ROOT", self.guest))
        patches.enter_context(patch.object(idp, "MARKER_ROOT", self.markers))
        self.password = patches.enter_context(patch.object(idp, "_password_pair", return_value=(SYNTHETIC_PASSWORD, SYNTHETIC_HASH)))

    def fixture(self):
        proposal = synthetic_proposal()
        root = self.guest / "state" / proposal["phase_b_id"]
        root.mkdir(mode=0o700)
        marker = root / "owner.json"
        marker.write_text(json.dumps({"lab": idp.LAB, "vm_run_id": idp.VM_RUN_ID,
                                     "phase_b_id": proposal["phase_b_id"], "proposal_digest": proposal["proposal_digest"]}))
        marker.chmod(0o600)
        return proposal, root, SyntheticClient(proposal)

    def execute(self, proposal, root, client):
        return idp.bootstrap_idp(proposal, proposal["proposal_digest"], "synthetic-approval", client=client,
                                 private_state_root=root, container_attestation=synthetic_attestation(proposal),
                                 bootstrap_token=BOOTSTRAP_TOKEN)

    def test_configuration_only_success_private_modes_exact_grants_and_no_secret_evidence(self):
        proposal, root, client = self.fixture()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = self.execute(proposal, root, client)
        self.assertTrue(result["configuration_proved"])
        self.assertTrue(result["scoped_backend_permissions_proved"])
        self.assertFalse(result["login_proved"])
        self.assertFalse(result["jwt_proved"])
        self.assertEqual(len(client.grants), 15)
        self.assertEqual(client.globals, [])
        directory = root / "idp-bootstrap"
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in directory.iterdir():
            metadata = path.stat()
            self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
            self.assertEqual((metadata.st_uid, metadata.st_gid), self.metadata.actual_owner)
            self.assertTrue(stat.S_ISREG(metadata.st_mode))
            self.assertEqual(metadata.st_nlink, 1)
        evidence = (directory / "ledger.jsonl").read_text() + json.dumps(result) + stdout.getvalue() + stderr.getvalue()
        for secret in (BOOTSTRAP_TOKEN, SERVICE_TOKEN, SYNTHETIC_PASSWORD, SYNTHETIC_HASH, client.provider["client_secret"]):
            self.assertNotIn(secret, evidence)
        self.assertNotIn("@orcastra-qa.invalid", evidence)
        self.assertLessEqual(len(client.calls), idp.MAX_REQUESTS)
        self.assertEqual(sum(method == "POST" and path.endswith("/providers/oauth2/") for method, path, *_ in client.calls), 1)
        self.assertEqual(sum(method == "POST" and path.endswith("/core/applications/") for method, path, *_ in client.calls), 1)
        self.assertEqual(sum(method == "POST" and path.endswith("/set_password_hash/") for method, path, *_ in client.calls), 6)
        self.assertTrue(any("page=2" in query and path.endswith("/permissions/roles/") for _, path, query, *_ in client.calls))

    def test_existing_or_neighbor_group_stops_without_generated_credentials_or_mutation(self):
        for name in ("role_admin", "role_admin-neighbor"):
            proposal, root, client = self.fixture()
            client.groups[str(uuid.uuid4())] = {"name": name}
            with self.subTest(name=name), self.assertRaises(idp.BootstrapError) as stopped:
                self.execute(proposal, root, client)
            self.assertEqual(stopped.exception.stage, "absence_readback")
            self.assertFalse((root / "idp-bootstrap" / "credentials.json").exists())
            self.assertTrue(all(method == "GET" for method, *_ in client.calls))
        self.password.assert_not_called()

    def test_unknown_permission_or_modified_mapping_is_draft_before_credentials(self):
        for bad in ("permission", "mapping", "flow"):
            proposal, root, client = self.fixture()
            if bad == "mapping":
                client.mappings[2]["expression"] = "return {'groups': ['role_admin']}"
            elif bad == "flow":
                client.flows[0]["pk"] = "not-a-uuid"
            else:
                client.response_hook = lambda method, path, payload, response, fake=client: (
                    idp.BoundedResponse(200, json.dumps(fake.page([], {})).encode(), 0.001)
                    if path == "/api/v3/rbac/permissions/" else response)
            with self.subTest(bad=bad), self.assertRaises(idp.BootstrapError):
                self.execute(proposal, root, client)
            self.assertFalse((root / "idp-bootstrap" / "credentials.json").exists())
            self.assertTrue(all(method == "GET" for method, *_ in client.calls))
        self.password.assert_not_called()

    def test_symlink_wrong_modes_owner_or_existing_helper_state_blocks_private_token_access(self):
        for bad in ("mode", "uid", "gid", "owner", "marker-uid", "marker-gid", "marker-mode", "hardlink", "fifo",
                    "symlink", "existing", "guest-marker"):
            proposal, root, client = self.fixture()
            if bad == "mode":
                root.chmod(0o755)
            elif bad in {"uid", "gid"}:
                self.metadata.owner_overrides[root] = (1001, 0) if bad == "uid" else (0, 1001)
            elif bad == "owner":
                (root / "owner.json").write_text("{}")
            elif bad in {"marker-uid", "marker-gid"}:
                self.metadata.owner_overrides[root / "owner.json"] = (1001, 0) if bad == "marker-uid" else (0, 1001)
            elif bad == "marker-mode":
                (root / "owner.json").chmod(0o644)
            elif bad == "hardlink":
                os.link(root / "owner.json", root / "another-link")
            elif bad == "fifo":
                (root / "owner.json").unlink()
                os.mkfifo(root / "owner.json", 0o600)
            elif bad == "symlink":
                (root / "owner.json").unlink()
                (root / "owner.json").symlink_to(self.markers / idp.LAB)
            elif bad == "existing":
                (root / "idp-bootstrap").mkdir(mode=0o700)
            else:
                (self.markers / (idp.LAB + "-run")).write_text(str(uuid.uuid4()))
            with self.subTest(bad=bad), self.assertRaises(idp.BootstrapError):
                self.execute(proposal, root, client)
            self.assertEqual(client.calls, [])
            self.assertFalse((root / "idp-bootstrap" / "bootstrap-token").exists())
            (self.markers / (idp.LAB + "-run")).write_text(idp.VM_RUN_ID)

    def test_metadata_facade_refuses_foreign_paths_and_descriptors_without_global_root_patch(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-idp-neighbor-") as temporary:
            foreign = Path(temporary) / "public-fixture"
            foreign.write_bytes(b"Synthetic neighboring fixture bytes")
            descriptor = os.open(foreign, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                with self.assertRaises(PermissionError):
                    self.metadata.stat(foreign)
                with self.assertRaises(PermissionError):
                    self.metadata.open(foreign, os.O_RDONLY | os.O_NOFOLLOW)
                with self.assertRaises(PermissionError):
                    self.metadata.fstat(descriptor)
                with self.assertRaises(PermissionError):
                    self.metadata.mkdir(Path(temporary) / "must-not-exist", 0o700)
                self.assertFalse((Path(temporary) / "must-not-exist").exists())
            finally:
                os.close(descriptor)
        self.assertIs(idp.os, self.metadata)
        self.assertEqual(self.metadata.actual_owner, (os.geteuid(), os.getegid()))

    def test_unmodified_root_gate_rejects_actual_or_synthetic_nonroot_before_token_access(self):
        proposal, root, client = self.fixture()
        for uid, gid in ((1001, 0), (0, 1001), (1001, 1001)):
            caller = Mock(wraps=os)
            caller.geteuid.return_value, caller.getegid.return_value = uid, gid
            with self.subTest(uid=uid, gid=gid), patch.object(idp, "os", caller), \
                    patch.object(idp, "_open_directory") as opened, self.assertRaises(idp.BootstrapError) as stopped:
                self.execute(proposal, root, client)
            self.assertEqual(stopped.exception.stage, "private_preflight")
            opened.assert_not_called()
        if os.geteuid() != 0 or os.getegid() != 0:
            with patch.object(idp, "os", os), patch.object(idp, "_open_directory") as opened, \
                    self.assertRaises(idp.BootstrapError) as stopped:
                self.execute(proposal, root, client)
            self.assertEqual(stopped.exception.stage, "private_preflight")
            opened.assert_not_called()
        self.assertEqual(client.calls, [])
        self.assertFalse((root / "idp-bootstrap").exists())
        self.password.assert_not_called()

    def test_accepted_ids_are_durable_before_dependent_call_and_retained_on_ambiguous_failure(self):
        proposal, root, client = self.fixture()
        def before(method, path, payload):
            if method == "POST" and path == "/api/v3/core/users/":
                rows = [json.loads(line) for line in (root / "idp-bootstrap" / "ledger.jsonl").read_text().splitlines()]
                aliases = {row.get("alias") for row in rows if row["event"] == "object_accepted"}
                self.assertTrue(set(idp.ROLE_GROUPS).issubset(aliases))
                self.assertIn("backend-role", aliases)
                raise TimeoutError("ambiguous private upstream body " + SERVICE_TOKEN)
        client.before_request = before
        with self.assertRaises(idp.BootstrapError) as stopped:
            self.execute(proposal, root, client)
        self.assertEqual(set(stopped.exception.accepted_ids), {*idp.ROLE_GROUPS, "backend-role"})
        self.assertNotIn(SERVICE_TOKEN, str(stopped.exception))
        self.assertEqual(sum(method == "POST" and path == "/api/v3/core/users/" for method, path, *_ in client.calls), 1)
        self.assertTrue((root / "idp-bootstrap" / "credentials.json").exists())
        self.assertEqual(json.loads((root / "idp-bootstrap" / "ledger.jsonl").read_text().splitlines()[-1])["event"], "stopped")

    def test_late_accepted_create_captures_id_before_deadline_rejection(self):
        proposal, root, client = self.fixture()
        client.response_hook = lambda method, path, payload, response: (
            idp.BoundedResponse(response.status, response.body, 16.0)
            if method == "POST" and path == "/api/v3/core/groups/" else response)
        with self.assertRaises(idp.BootstrapError) as stopped:
            self.execute(proposal, root, client)
        self.assertEqual(set(stopped.exception.accepted_ids), {"role_admin"})
        self.assertEqual(len(client.groups), 1)

    def test_ledger_failure_retains_accepted_id_and_stops_before_dependent_call(self):
        proposal, root, client = self.fixture()
        bootstrap = idp.IdPBootstrap(proposal, proposal["proposal_digest"], "synthetic-approval", client=client,
                                    private_state_root=root, container_attestation=synthetic_attestation(proposal),
                                    bootstrap_token=BOOTSTRAP_TOKEN)
        record = bootstrap._record
        def fail_durable_write(event, **fields):
            if event in {"object_accepted", "stopped"}:
                raise OSError("synthetic ledger unavailable " + BOOTSTRAP_TOKEN)
            record(event, **fields)
        with patch.object(bootstrap, "_record", side_effect=fail_durable_write), self.assertRaises(idp.BootstrapError) as stopped:
            bootstrap.run()
        self.assertEqual(set(stopped.exception.accepted_ids), {"role_admin"})
        self.assertEqual(len(client.groups), 1)
        self.assertNotIn(BOOTSTRAP_TOKEN, str(stopped.exception))
        self.assertEqual(sum(method == "POST" for method, *_ in client.calls), 1)

    def test_response_bounds_redirect_and_nonfinite_or_duplicate_json_stop_without_retry(self):
        for bad in ("large", "redirect", "nonfinite", "duplicate", "elapsed"):
            proposal, root, client = self.fixture()
            responses = {
                "large": idp.BoundedResponse(200, b"x" * (idp.MAX_RESPONSE + 1), 0.001),
                "redirect": idp.BoundedResponse(302, b'{"token":"private"}', 0.001),
                "nonfinite": idp.BoundedResponse(200, b'{"count":NaN}', 0.001),
                "duplicate": idp.BoundedResponse(200, b'{"user":{},"user":{}}', 0.001),
                "elapsed": idp.BoundedResponse(200, b'{}', float("nan")),
            }
            client.response_hook = lambda *args, selected=responses[bad]: selected
            with self.subTest(bad=bad), self.assertRaises(idp.BootstrapError):
                self.execute(proposal, root, client)
            self.assertEqual(len(client.calls), 1)
            self.assertFalse((root / "idp-bootstrap" / "credentials.json").exists())

    def test_no_plaintext_password_fallback_when_hash_api_is_unavailable(self):
        proposal, root, client = self.fixture()
        client.response_hook = lambda method, path, payload, response: (
            idp.BoundedResponse(404, b'{"detail":"unsupported"}', 0.001)
            if path.endswith("/set_password_hash/") else response)
        with self.assertRaises(idp.BootstrapError):
            self.execute(proposal, root, client)
        self.assertFalse(any(path.endswith("/set_password/") for _, path, *_ in client.calls))
        self.assertEqual(len(client.users), 1)

    def test_request_budget_deadline_and_repeated_run_stop_before_transport(self):
        for bound in ("requests", "deadline"):
            proposal, root, client = self.fixture()
            bootstrap = idp.IdPBootstrap(proposal, proposal["proposal_digest"], "synthetic-approval", client=client,
                                        private_state_root=root, container_attestation=synthetic_attestation(proposal),
                                        bootstrap_token=BOOTSTRAP_TOKEN)
            if bound == "requests":
                bootstrap.requests = idp.MAX_REQUESTS
            else:
                bootstrap.deadline = 0.0
            with self.subTest(bound=bound), self.assertRaises(idp.BootstrapError) as stopped:
                bootstrap.run()
            self.assertEqual(stopped.exception.stage, "absence_readback")
            self.assertEqual(client.calls, [])
            self.assertFalse((root / "idp-bootstrap" / "credentials.json").exists())
            with self.assertRaises(idp.BootstrapError) as repeated:
                bootstrap.run()
            self.assertEqual(repeated.exception.stage, "single_use_guard")
            self.assertEqual(client.calls, [])
        self.password.assert_not_called()

    def test_malformed_or_incomplete_pagination_stops_before_creating_credentials(self):
        for bad in ("boolean-count", "omitted-row", "unsafe-next", "repeated-page"):
            proposal, root, client = self.fixture()
            body = client.page([], {})
            if bad == "boolean-count":
                body["pagination"]["count"] = False
            elif bad == "omitted-row":
                body["pagination"]["count"] = 1
            elif bad == "unsafe-next":
                body["pagination"]["next"] = 2
            else:
                body["pagination"].update(count=2, total_pages=2, next=2, end_index=1, start_index=1)
                body["results"] = [{"name": "neighbor"}]
            def response(method, path, payload, actual, fake=body):
                if path == "/api/v3/core/groups/":
                    return idp.BoundedResponse(200, json.dumps(fake).encode(), 0.001)
                return actual
            client.response_hook = response
            with self.subTest(bad=bad), self.assertRaises(idp.BootstrapError):
                self.execute(proposal, root, client)
            self.assertFalse((root / "idp-bootstrap" / "credentials.json").exists())
            self.assertTrue(all(method == "GET" for method, *_ in client.calls))
        self.password.assert_not_called()

    def test_independent_readback_rejects_extra_privilege_roles_grants_redirect_and_foreign_ids(self):
        changes = {
            "actor-superuser": lambda c: c.users[10].update(is_superuser=True),
            "actor-extra-role": lambda c: c.users[10]["roles"].append(str(uuid.uuid4())),
            "actor-extra-role-object": lambda c: c.users[10]["roles_obj"].append({"pk": str(uuid.uuid4()),
                                                                               "name": "foreign"}),
            "actor-malformed-role-object": lambda c: c.users[10].update(roles_obj=None),
            "actor-extra-group": lambda c: c.users[10]["groups"].append(str(uuid.uuid4())),
            "actor-float-id": lambda c: c.users[10].update(pk=10.0),
            "actor-malformed-group": lambda c: c.users[10]["groups_obj"].append(None),
            "group-parent": lambda c: next(iter(c.groups.values()))["parents"].append(str(uuid.uuid4())),
            "group-extra-role-object": lambda c: next(iter(c.groups.values()))["roles_obj"].append({"pk": str(uuid.uuid4())}),
            "group-inherited-role": lambda c: next(iter(c.groups.values())).update(inherited_roles_obj=[{"name": "foreign"}]),
            "group-float-member": lambda c: next(iter(c.groups.values())).update(users=[10.0]),
            "service-malformed-role-object": lambda c: c.users[16]["roles_obj"][0].update(name="foreign"),
            "backend-role-id": lambda c: next(iter(c.roles.values())).update(pk=str(uuid.uuid4())),
            "global-grant": lambda c: c.globals.append({"codename": "view_user"}),
            "foreign-grant": lambda c: c.grants[0].update(object_pk=str(uuid.uuid4())),
            "foreign-grant-id": lambda c: c.grants[0].update(id=999999),
            "string-grant-readback-id": lambda c: c.grants[0].update(id=str(c.grants[0]["id"])),
            "duplicate-grant": lambda c: c.grants.append(copy.deepcopy(c.grants[0])),
            "provider-extra-grant": lambda c: c.provider["grant_types"].append("client_credentials"),
            "provider-redirect": lambda c: c.provider["redirect_uris"][0].update(matching_mode="regex"),
            "provider-global-issuer": lambda c: c.provider.update(issuer_mode="global"),
            "provider-foreign-key": lambda c: c.provider.update(signing_key=str(uuid.uuid4())),
            "provider-changed-private-secret": lambda c: c.provider.update(client_secret="foreign-private-secret"),
            "provider-float-id": lambda c: c.provider.update(pk=71.0),
            "provider-int-boolean": lambda c: c.provider.update(include_claims_in_id_token=1),
            "app-float-provider": lambda c: c.application.update(provider=71.0),
            "app-foreign-id": lambda c: c.application.update(pk=str(uuid.uuid4())),
        }
        for bad, change in changes.items():
            proposal, root, client = self.fixture()
            changed = False
            def before(method, path, payload, fake=client, mutation=change):
                nonlocal changed
                if not changed and method == "GET" and path == "/api/v3/core/users/10/":
                    mutation(fake)
                    changed = True
            client.before_request = before
            with self.subTest(bad=bad), self.assertRaises(idp.BootstrapError) as stopped:
                self.execute(proposal, root, client)
            self.assertEqual(stopped.exception.stage, "independent_configuration_readback")
            self.assertIn("application", stopped.exception.accepted_ids)


if __name__ == "__main__":
    unittest.main()
