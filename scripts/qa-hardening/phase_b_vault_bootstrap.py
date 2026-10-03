"""Fresh isolated QA Vault bootstrap library. No transport, CLI or environment discovery.

The B2 controller proves the new container, image, labels and loopback publication before
supplying a client. Approval binds the whole frozen B2 proposal. Responses stay private.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import re
import stat
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

LAB = "orcastra-qa-hardening"
ROOT = Path("/srv") / LAB
IMAGE_ID = "sha256:9b1d8cd2a2c4d1879b1acf3030fb266db3cb5ed54f554645f8fc577fffdf8442"
VERSION = "1.21.4"
ENDPOINT = "http://127.0.0.1:32820"
MAX_RESPONSE = 128 * 1024
MAX_PLAN = 1024 * 1024
HTTP_SECONDS = 10
TOTAL_SECONDS = 180
TOKEN_SECONDS = 24 * 3600
CERT_SECONDS = 720 * 3600

ROLE = {
    "allowed_domains": ["orcastra-qa.invalid"], "allow_subdomains": True,
    "allow_bare_domains": False, "allow_glob_domains": False, "allow_any_name": False,
    "allow_localhost": False, "allow_ip_sans": False, "allow_wildcard_certificates": False,
    "allowed_domains_template": False, "allowed_uri_sans_template": False,
    "allowed_uri_sans": [], "allowed_other_sans": [], "allowed_user_ids": [],
    "allowed_serial_numbers": [], "client_flag": True, "server_flag": False,
    "code_signing_flag": False, "email_protection_flag": False, "enforce_hostnames": True,
    "require_cn": True, "cn_validations": ["hostname"], "use_csr_common_name": False,
    "use_csr_sans": False, "key_type": "rsa", "key_bits": 2048,
    "key_usage": ["DigitalSignature", "KeyEncipherment"], "ext_key_usage": [],
    "ext_key_usage_oids": [], "ttl": "720h", "max_ttl": "720h", "issuer_ref": "default",
}

POLICY = '''path "secret/data/clusters/*" {
  capabilities = ["create", "read", "update", "delete"]
}
path "secret/metadata/clusters" {
  capabilities = ["list"]
}
path "secret/metadata/clusters/*" {
  capabilities = ["list", "delete"]
}
path "pki_int/issue/lxd" {
  capabilities = ["update"]
}
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
'''


def canonical_digest(document: dict) -> str:
    body = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(body) > MAX_PLAN:
        raise ValueError("The proposal exceeds its bound")
    return hashlib.sha256(body).hexdigest()


def _sha(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("An exact lowercase SHA256 is required")
    return value


def _uuid(value: object, *, version4: bool = True) -> str:
    if not isinstance(value, str):
        raise TypeError("A canonical UUID is required")
    parsed = uuid.UUID(value)
    if str(parsed) != value or version4 and parsed.version != 4:
        raise ValueError("A canonical UUID is required")
    return value


def ownership_marker(vm_run_id: str, phase_b_id: str, proposal_digest: str) -> dict:
    return {"lab": LAB, "vm_run_id": _uuid(vm_run_id), "phase_b_id": _uuid(phase_b_id),
            "proposal_digest": _sha(proposal_digest)}


def vault_contract(vm_run_id: str, phase_b_id: str) -> dict:
    """Public deterministic section included verbatim in the complete B2 proposal."""
    _uuid(vm_run_id)
    _uuid(phase_b_id)
    return {
        "schema_version": 1, "endpoint": ENDPOINT, "vault_version": VERSION, "image_id": IMAGE_ID,
        "container_name": f"{LAB}-vault-{phase_b_id[:8]}",
        "labels": {"orcastra.qa.lab": LAB, "orcastra.qa.vm_run": vm_run_id, "orcastra.qa.phase_b": phase_b_id},
        "parent_path": str(ROOT / "state" / phase_b_id), "ownership_marker": "owner.json",
        "private_directory": "vault-bootstrap", "ledger_filename": "vault-bootstrap.jsonl",
        "init": {"secret_shares": 1, "secret_threshold": 1},
        "mounts": {
            "secret": {"type": "kv", "options": {"version": "2"}, "description": "Isolated Orcastra QA cluster registry"},
            "pki_int": {"type": "pki", "description": "Isolated Orcastra QA client CA",
                        "config": {"default_lease_ttl": "720h", "max_lease_ttl": "8760h"}},
        },
        "ca": {"common_name": f"Orcastra QA {phase_b_id} CA", "ttl": "8760h", "key_type": "rsa",
               "key_bits": 2048, "max_path_length": 0, "issuer_name": "qa-" + phase_b_id,
               "key_name": "qa-" + phase_b_id},
        "role_name": "lxd", "role": copy.deepcopy(ROLE),
        "policy_name": f"orcastra-qa-backend-{phase_b_id}", "policy": POLICY,
        "backend_token": {"ttl": "24h", "explicit_max_ttl": "24h", "renewable": False,
                          "no_default_policy": True, "no_parent": True, "type": "service", "num_uses": 0,
                          "display_name": "orcastra-qa-backend"},
        "limits": {"http_seconds": HTTP_SECONDS, "total_seconds": TOTAL_SECONDS, "response_bytes": MAX_RESPONSE},
        "retention": "Retain private initialization/root/backend material and partial responses; no retry, adoption or cleanup",
    }


@dataclass(frozen=True)
class VaultBinding:
    """Caller attestation backed by the approved phase's immutable creation ledger."""

    endpoint: str
    container_id: str
    container_name: str
    image_id: str
    labels: dict[str, str]
    creation_ledger_sha256: str
    created_for_phase: bool
    owned_loopback_verified: bool


@dataclass(frozen=True, repr=False)
class HTTPResponse:
    status: int
    body: bytes = field(repr=False)
    complete: bool = True

    def __repr__(self) -> str:
        return "HTTPResponse(<private>)"


class VaultHTTPFailure(Exception):
    """Partial response bytes are private, never exception text.

    The client must not log bodies or tokens. It disables redirects, proxies, ambient
    authentication, retry and environment discovery. It enforces finite deadlines/size
    bounds while receiving and attaches received partial bytes when a request fails.
    """

    def __init__(self, partial_body: bytes = b""):
        super().__init__("VaultHTTPFailure")
        self.partial_body = partial_body


class VaultHTTPClient(Protocol):
    binding: VaultBinding

    def request(self, method: str, path: str, *, token: str | None, body: bytes | None,
                timeout_seconds: float, max_response_bytes: int) -> HTTPResponse: ...


class BootstrapFailure(Exception):
    def __init__(self, stage: str, error_type: str, evidence: dict):
        super().__init__(f"{error_type}:{stage}")
        self.stage, self.error_type, self.evidence = stage, error_type, evidence


def _strict_json(body: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON keys are forbidden")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("Non-finite JSON numbers are forbidden")

    document = json.loads(body, object_pairs_hook=pairs, parse_constant=constant)
    if type(document) is not dict:
        raise ValueError("A JSON object is required")
    return document


def validate_plan(plan: dict, approved_digest: str, approval_reference: str) -> dict:
    if (type(plan) is not dict or not isinstance(approval_reference, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/ -]{0,159}", approval_reference)):
        raise ValueError("Exact explicit owner approval is required")
    core = copy.deepcopy(plan)
    digest = core.pop("proposal_digest", None)
    if digest != _sha(approved_digest) or canonical_digest(core) != digest:
        raise ValueError("Approval must bind the complete canonical B2 proposal")
    if (type(core.get("schema_version")) is not int or core["schema_version"] != 1 or core.get("phase") != "B2"
        or core.get("ready_for_guest_execution") is not True or core.get("remaining_gates") != []):
        raise ValueError("An incomplete or draft proposal cannot execute")
    phase = _uuid(core["phase_b_id"])
    target = core["target"]
    run = _uuid(target["vm_run_id"])
    _uuid(target["b1_phase_id"])
    _sha(target["phase_a_digest"])
    if (target.get("vm_name") != LAB or target.get("project") != "default"
        or target.get("profiles") != [] or type(target.get("outer_nic_count")) is not int
        or target["outer_nic_count"] != 0):
        raise ValueError("Only the exact owned no-NIC VM is supported")
    if (type(core.get("vault_bootstrap")) is not dict
        or canonical_digest(core["vault_bootstrap"]) != canonical_digest(vault_contract(run, phase))):
        raise ValueError("The exact reviewed Vault bootstrap contract is required")
    return core


def _directory(path: Path, *, private: bool = False) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("A canonical absolute path is required")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
            metadata = os.fstat(descriptor)
            if metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_mode & 0o022:
                raise PermissionError("Root-owned protected ancestors are required")
        if private and stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
            raise PermissionError("An exact root:root 0700 directory is required")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _file_metadata(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_gid != 0
        or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600):
        raise PermissionError("An exact root:root single-link 0600 regular file is required")


def _read_at(directory: int, name: str, maximum: int) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    try:
        _file_metadata(descriptor)
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            body = source.read(maximum + 1)
        if len(body) > maximum:
            raise ValueError("An owned marker exceeds its bound")
        return body
    finally:
        os.close(descriptor)


def _new_at(directory: int, name: str) -> int:
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=directory)
    try:
        os.fchmod(descriptor, 0o600)
        _file_metadata(descriptor)
        os.fsync(directory)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _write(descriptor: int, body: bytes) -> None:
    view = memoryview(body)
    while view:
        count = os.write(descriptor, view)
        if count <= 0:
            raise OSError("A private write did not advance")
        view = view[count:]
    os.fsync(descriptor)


def _safe_type(error: BaseException) -> str:
    for kind in (BootstrapFailure, VaultHTTPFailure, TimeoutError, PermissionError, FileExistsError,
                 FileNotFoundError, ValueError, TypeError, KeyError, OSError):
        if isinstance(error, kind):
            return kind.__name__
    return "ExternalError"


def _token(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"hvs\.[A-Za-z0-9_-]{12,508}", value):
        raise ValueError("The fresh service token is malformed")
    return value


def _certificate_sha(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("One PEM certificate is required")
    matched = re.fullmatch(r"-----BEGIN CERTIFICATE-----\n([A-Za-z0-9+/=\n]+)\n-----END CERTIFICATE-----\n?", value)
    if matched is None or not base64.b64decode(matched[1].replace("\n", ""), validate=True):
        raise ValueError("A complete standalone PEM certificate is required")
    return hashlib.sha256(value.encode()).hexdigest()


def _data(document: dict) -> dict:
    value = document.get("data")
    if type(value) is not dict:
        raise ValueError("The response lacks its object data")
    return value


def _seal_state(document: dict, *, initialized: bool) -> None:
    if (document.get("initialized") is not initialized or document.get("sealed") is not (not initialized)
        or document.get("type") != "shamir" or document.get("version") != VERSION
        or document.get("storage_type") != "file" or document.get("recovery_seal") is not False
        or document.get("progress") != 0 or document.get("migration", False) is not False
        or any(type(document.get(name)) is not int for name in ("n", "t", "progress"))
        or document.get("n") != (1 if initialized else 0) or document.get("t") != (1 if initialized else 0)):
        raise ValueError("Vault does not match the exact fresh isolated Shamir state")


def capability_contract() -> dict[str, list[str]]:
    crud = ["create", "delete", "read", "update"]
    return {
        **{"secret/data/clusters/qa-probe/" + name: list(crud) for name in ("metadata", "certs", "secrets", "pending_certs")},
        "secret/metadata/clusters": ["list"], "secret/metadata/clusters/qa-probe": ["delete", "list"],
        "secret/metadata/clusters/qa-probe/certs": ["delete", "list"],
        "pki_int/issue/lxd": ["update"], "auth/token/lookup-self": ["read"],
        **{name: ["deny"] for name in ("sys/mounts", "sys/capabilities-self", "sys/policies/acl/default",
                                      "auth/token/create", "secret/data/unrelated", "secret/metadata",
                                      "pki_int/issue/unrelated", "pki_int/root/generate/internal", "pki_int/sign/lxd")},
    }


class _Bootstrap:
    def __init__(self, client: VaultHTTPClient, sink: Callable[[dict], None], clock: Callable[[], float]):
        self.client, self.sink, self.clock = client, sink, clock
        self.deadline = clock() + TOTAL_SECONDS
        self.stage = "approval"
        self.events: list[dict] = []
        self.ledger = self.private = self.parent = None
        self.response_count = 0
        self.root_token = self.backend_token = None
        self.evidence: dict = {"verified": False}

    def record(self, event: str, **fields) -> None:
        entry = {"event": event, "stage": self.stage, **fields}
        self.sink(dict(entry))
        if self.ledger is not None:
            _write(self.ledger, json.dumps(entry, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        self.events.append(entry)

    def remaining(self) -> float:
        remaining = self.deadline - self.clock()
        if not math.isfinite(remaining) or remaining <= 0:
            raise TimeoutError("The finite bootstrap deadline elapsed")
        return min(HTTP_SECONDS, remaining)

    def request(self, stage: str, method: str, path: str, *, token: str | None = None,
                payload: dict | None = None, statuses: tuple[int, ...] = (200,)) -> tuple[int, dict]:
        self.stage = stage
        timeout = self.remaining()
        body = None if payload is None else json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        response_fd = None
        self.record("http_attempt", resource_id=path, method=method)
        try:
            if self.private is not None:
                self.response_count += 1
                filename = f"response-{self.response_count:02d}.bin"
                self.record("file_attempt", resource_id=filename)
                response_fd = _new_at(self.private, filename)
                self.record("file_created", resource_id=filename)
            try:
                timeout = self.remaining()
                response = self.client.request(method, path, token=token, body=body,
                                               timeout_seconds=timeout, max_response_bytes=MAX_RESPONSE)
            except VaultHTTPFailure as error:
                if response_fd is not None and type(error.partial_body) is bytes:
                    _write(response_fd, error.partial_body[:MAX_RESPONSE])
                raise
            if not isinstance(response, HTTPResponse) or type(response.body) is not bytes:
                raise TypeError("A bounded explicit HTTP response is required")
            if response_fd is not None:
                _write(response_fd, response.body[:MAX_RESPONSE])
            if (type(response.status) is not int or len(response.body) > MAX_RESPONSE
                or response.complete is not True or response.status not in statuses):
                raise ValueError("The response is incomplete or unexpected")
            self.remaining()
            document = {} if response.status == 204 and not response.body else _strict_json(response.body)
            if response.status == 204 and document:
                raise ValueError("A no-content acknowledgement cannot have data")
            self.record("http_accepted", resource_id=path, status=response.status)
            return response.status, document
        finally:
            if response_fd is not None:
                os.close(response_fd)

    def save_secret(self, filename: str, body: bytes) -> None:
        self.record("file_attempt", resource_id=filename)
        descriptor = _new_at(self.private, filename)
        try:
            self.record("file_created", resource_id=filename)
            _write(descriptor, body)
        finally:
            os.close(descriptor)

    def prepare(self, core: dict) -> None:
        self.stage = "ownership"
        if os.geteuid() != 0 or os.getegid() != 0:
            raise PermissionError("Only guest root:root may bootstrap")
        contract = core["vault_bootstrap"]
        binding = self.client.binding
        if (not isinstance(binding, VaultBinding) or binding.endpoint != contract["endpoint"]
            or binding.image_id != contract["image_id"] or binding.container_name != contract["container_name"]
            or binding.labels != contract["labels"] or binding.created_for_phase is not True
            or binding.owned_loopback_verified is not True):
            raise ValueError("The client is not bound to this newly created owned loopback Vault")
        _sha(binding.container_id)
        _sha(binding.creation_ledger_sha256)
        self.evidence.update({"phase_b_id": core["phase_b_id"], "vm_run_id": core["target"]["vm_run_id"],
                              "container_id": binding.container_id, "image_id": binding.image_id,
                              "creation_ledger_sha256": binding.creation_ledger_sha256})
        etc = _directory(Path("/etc"))
        try:
            if (_read_at(etc, LAB, 128).strip() != LAB.encode()
                or _read_at(etc, LAB + "-run", 128).strip() != core["target"]["vm_run_id"].encode()):
                raise ValueError("The guest lab/run markers differ")
        finally:
            os.close(etc)
        self.parent = _directory(Path(contract["parent_path"]), private=True)
        marker = _strict_json(_read_at(self.parent, contract["ownership_marker"], 1024))
        if marker != ownership_marker(core["target"]["vm_run_id"], core["phase_b_id"], self.evidence["proposal_digest"]):
            raise ValueError("The exact fresh phase ownership marker is required")
        for name in (contract["private_directory"],):
            try:
                os.stat(name, dir_fd=self.parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise FileExistsError("A prior bootstrap resource cannot be adopted")
        _, document = self.request("fresh_init_read", "GET", "/v1/sys/init")
        if document != {"initialized": False} or document.get("initialized") is not False:
            raise ValueError("An initialized Vault cannot be adopted")
        _, document = self.request("fresh_seal_read", "GET", "/v1/sys/seal-status")
        _seal_state(document, initialized=False)
        self.stage = "private_state"
        self.record("directory_attempt", resource_id=contract["private_directory"])
        os.mkdir(contract["private_directory"], 0o700, dir_fd=self.parent)
        self.private = os.open(contract["private_directory"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                               dir_fd=self.parent)
        os.fchmod(self.private, 0o700)
        metadata = os.fstat(self.private)
        if metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise PermissionError("The new private directory has unexpected ownership/mode")
        os.fsync(self.parent)
        self.record("directory_created", resource_id=contract["private_directory"])
        self.record("file_attempt", resource_id=contract["ledger_filename"])
        self.ledger = _new_at(self.private, contract["ledger_filename"])
        for entry in self.events:
            _write(self.ledger, json.dumps(entry, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        self.record("file_created", resource_id=contract["ledger_filename"])

    def initialize(self, contract: dict) -> None:
        _, document = self.request("initialize", "POST", "/v1/sys/init", payload=contract["init"])
        # important: full or partial init bytes are durable before validation or unseal.
        if (set(document) - {"keys", "keys_base64", "root_token", "recovery_keys", "recovery_keys_base64"}
            or document.get("recovery_keys", []) != [] or document.get("recovery_keys_base64", []) != []
            or type(document.get("keys")) is not list or len(document["keys"]) != 1
            or type(document.get("keys_base64")) is not list or len(document["keys_base64"]) != 1):
            raise ValueError("The fresh initialization material is incomplete")
        root = _token(document["root_token"])
        key, key64 = document["keys"][0], document["keys_base64"][0]
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(key64, str):
            raise ValueError("The disposable one-share Shamir material is malformed")
        if base64.b64decode(key64, validate=True) != bytes.fromhex(key):
            raise ValueError("The two unseal key encodings differ")
        self.save_secret("root-token", root.encode())
        self.save_secret("unseal-key", key.encode())
        self.root_token = root
        self.record("resource_created", resource_id="vault-initialized")
        _, document = self.request("unseal", "POST", "/v1/sys/unseal", payload={"key": key})
        _seal_state(document, initialized=True)
        _, document = self.request("unseal_readback", "GET", "/v1/sys/seal-status")
        _seal_state(document, initialized=True)
        self.record("resource_verified", resource_id="vault-unsealed")

    def configure(self, contract: dict) -> tuple[str, str, str]:
        root = self.root_token
        _, document = self.request("baseline_mounts", "GET", "/v1/sys/mounts", token=root)
        mounts = _data(document)
        builtin = {"cubbyhole/": "cubbyhole", "identity/": "identity", "sys/": "system"}
        if set(mounts) != set(builtin) or any(mounts[name].get("type") != kind for name, kind in builtin.items()):
            raise ValueError("A fresh Vault must contain only built-in mounts")
        _, missing_policy = self.request("baseline_policy", "GET", "/v1/sys/policies/acl/" + contract["policy_name"],
                                         token=root, statuses=(404,))
        if missing_policy != {"errors": []}:
            raise ValueError("An existing or ambiguous backend policy cannot be adopted")
        for mount, payload in contract["mounts"].items():
            self.request("mount_" + mount, "POST", "/v1/sys/mounts/" + mount, token=root, payload=payload, statuses=(204,))
            self.record("resource_created", resource_id=mount + "/")
        _, document = self.request("ca_generate", "POST", "/v1/pki_int/root/generate/internal", token=root, payload=contract["ca"])
        data = _data(document)
        issuer, key = _uuid(data["issuer_id"], version4=False), _uuid(data["key_id"], version4=False)
        if (data.get("issuer_name") != contract["ca"]["issuer_name"] or data.get("key_name") != contract["ca"]["key_name"]
            or any("private" in name.lower() for name in data)):
            raise ValueError("The internal QA CA differs or exported private material")
        ca_sha = _certificate_sha(data.get("certificate"))
        self.record("resource_created", resource_id=issuer)
        self.record("resource_created", resource_id=key)
        _, role_status = self.request("role_absent", "GET", "/v1/pki_int/roles/lxd", token=root, statuses=(404,))
        if role_status != {"errors": []}:
            raise ValueError("An existing or ambiguous client role cannot be adopted")
        self.request("role_create", "POST", "/v1/pki_int/roles/lxd", token=root, payload=contract["role"], statuses=(200, 204))
        self.record("resource_created", resource_id="pki_int/roles/lxd")
        self.request("policy_create", "PUT", "/v1/sys/policies/acl/" + contract["policy_name"], token=root,
                     payload={"policy": contract["policy"]}, statuses=(204,))
        self.record("resource_created", resource_id=contract["policy_name"])
        _, document = self.request("token_create", "POST", "/v1/auth/token/create", token=root,
                                  payload={**contract["backend_token"], "policies": [contract["policy_name"]],
                                           "meta": {"lab": LAB, "phase_b_id": self.evidence["phase_b_id"]}})
        auth = document.get("auth")
        if type(auth) is not dict:
            raise ValueError("The backend token response is incomplete")
        backend = _token(auth.get("client_token"))
        self.save_secret("backend-token", backend.encode())
        self.backend_token = backend
        self.record("resource_created", resource_id="backend-token")
        if (backend == root or auth.get("policies") != [contract["policy_name"]]
            or auth.get("token_policies") != [contract["policy_name"]] or auth.get("identity_policies", []) != []
            or auth.get("renewable") is not False or auth.get("token_type") != "service"
            or type(auth.get("lease_duration")) is not int or auth["lease_duration"] != TOKEN_SECONDS
            or auth.get("orphan") is not True):
            raise ValueError("The backend token inherited unexpected privilege or lifetime")
        return issuer, key, ca_sha

    def verify(self, contract: dict, issuer: str, key: str, ca_sha: str) -> None:
        root, backend = self.root_token, self.backend_token
        _, document = self.request("mounts_readback", "GET", "/v1/sys/mounts", token=root)
        mounts = _data(document)
        if set(mounts) != {"sys/", "identity/", "cubbyhole/", "secret/", "pki_int/"}:
            raise ValueError("The final mount set differs")
        if any(mounts[name].get("type") != kind for name, kind in
               {"sys/": "system", "identity/": "identity", "cubbyhole/": "cubbyhole"}.items()):
            raise ValueError("A built-in mount changed type")
        for name, expected in contract["mounts"].items():
            actual = mounts[name + "/"]
            options = actual.get("options")
            valid_options = options == expected["options"] if "options" in expected else options in ({}, None)
            if (actual.get("type") != expected["type"] or actual.get("description") != expected["description"]
                or not valid_options or actual.get("local", False) is not False or actual.get("seal_wrap", False) is not False):
                raise ValueError("A mount's type, options or scope differs")
        _, document = self.request("pki_tune_readback", "GET", "/v1/sys/mounts/pki_int/tune", token=root)
        data = _data(document)
        if data.get("default_lease_ttl") != CERT_SECONDS or data.get("max_lease_ttl") != 8760 * 3600:
            raise ValueError("The PKI mount TTL bounds differ")
        _, document = self.request("issuer_readback", "GET", "/v1/pki_int/config/issuers", token=root)
        if _data(document).get("default") != issuer or _data(document).get("default_follows_latest_issuer") is not False:
            raise ValueError("The exact owned QA issuer is not the fixed default")
        _, document = self.request("issuer_identity_readback", "GET", "/v1/pki_int/issuer/" + issuer, token=root)
        data = _data(document)
        if data.get("issuer_id") != issuer or data.get("key_id") != key or data.get("issuer_name") != contract["ca"]["issuer_name"]:
            raise ValueError("The owned CA issuer/key identity differs")
        if _certificate_sha(data.get("certificate")) != ca_sha:
            raise ValueError("The issuer certificate readback differs from the newly generated CA")
        _, document = self.request("role_readback", "GET", "/v1/pki_int/roles/lxd", token=root)
        data = _data(document)
        for name, expected in contract["role"].items():
            actual = data.get(name)
            if name in {"ttl", "max_ttl"}:
                expected = CERT_SECONDS
            if type(actual) is not type(expected) or actual != expected:
                raise ValueError("The QA client role readback differs")
        _, document = self.request("policy_readback", "GET", "/v1/sys/policies/acl/" + contract["policy_name"], token=root)
        if _data(document).get("policy") != contract["policy"]:
            raise ValueError("The backend policy text differs")
        _, document = self.request("token_lookup_self", "GET", "/v1/auth/token/lookup-self", token=backend)
        data = _data(document)
        if (data.get("id") != backend or data.get("policies") != [contract["policy_name"]]
            or data.get("identity_policies", []) != [] or data.get("entity_id", "") != ""
            or data.get("renewable") is not False or data.get("orphan") is not True or data.get("type") != "service"
            or data.get("explicit_max_ttl") != TOKEN_SECONDS or data.get("creation_ttl") != TOKEN_SECONDS
            or type(data.get("ttl")) is not int or not 0 < data["ttl"] <= TOKEN_SECONDS or data.get("num_uses") != 0
            or any(type(data.get(name)) is not int for name in ("explicit_max_ttl", "creation_ttl", "num_uses"))
            or data.get("meta") != {"lab": LAB, "phase_b_id": self.evidence["phase_b_id"]}):
            raise ValueError("The independent backend token lookup differs")
        caps = capability_contract()
        # important: root queries the backend token; the backend receives no sys capability.
        _, document = self.request("capabilities_readback", "POST", "/v1/sys/capabilities", token=root,
                                  payload={"token": backend, "paths": list(caps)})
        data = _data(document)
        if set(data) != set(caps) or any(type(data[name]) is not list or sorted(data[name]) != expected for name, expected in caps.items()):
            raise ValueError("Backend capabilities widened or required paths are denied")
        for index, path in enumerate(("/v1/sys/mounts", "/v1/secret/data/unrelated"), 1):
            self.request("forbidden_read_" + str(index), "GET", path, token=backend, statuses=(403,))
        status, document = self.request("registry_empty_readback", "LIST", "/v1/secret/metadata/clusters", token=backend, statuses=(200, 404))
        if status == 404:
            if document != {"errors": []}:
                raise ValueError("Only the exact empty KV-v2 LIST 404 can mean no clusters")
        elif _data(document) != {"keys": []}:
            raise ValueError("A fresh registry unexpectedly contains keys")
        self.evidence.update({"verified": True, "initialized_by_this_run": True, "unsealed_verified": True,
                              "mounts_verified": True, "client_role_verified": True, "policy_verified": True,
                              "token_verified": True, "capabilities_verified": True, "forbidden_reads_denied": True,
                              "registry_empty_verified": True, "cluster_count": 0, "issuer_id": issuer, "key_id": key,
                              "ca_sha256": ca_sha,
                              "policy_sha256": hashlib.sha256(POLICY.encode()).hexdigest(), "response_count": self.response_count})
        self.record("bootstrap_verified", resource_id=self.evidence["container_id"])

    def close(self) -> None:
        self.root_token = self.backend_token = None
        for name in ("private", "ledger", "parent"):
            descriptor = getattr(self, name)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)


def bootstrap_vault(plan: dict, *, approved_digest: str, approval_reference: str, client: VaultHTTPClient,
                    evidence_sink: Callable[[dict], None], monotonic: Callable[[], float] = time.monotonic) -> dict:
    """Execute once after outer approval using only the caller's newly owned lab client.

    The controller durably journals every sanitized sink event and retains private files
    on failure. Retry/cleanup requires new exact approval. Only ``backend-token`` may enter
    the backend; root/unseal/response/ledger files stay guest-private.
    """
    runner = _Bootstrap(client, evidence_sink, monotonic)
    try:
        core = validate_plan(plan, approved_digest, approval_reference)
        runner.evidence["proposal_digest"] = approved_digest
        runner.prepare(core)
        runner.initialize(core["vault_bootstrap"])
        issuer, key, ca_sha = runner.configure(core["vault_bootstrap"])
        runner.verify(core["vault_bootstrap"], issuer, key, ca_sha)
        return dict(runner.evidence)
    # important: client and filesystem exception messages may contain fresh secret material.
    except Exception as error:  # noqa: BLE001
        safe_type = _safe_type(error)
        runner.evidence["verified"] = False
        try:
            runner.record("bootstrap_failed", error_type=safe_type)
        # important: a failed evidence sink must never replace the sanitized primary failure.
        except Exception:  # noqa: BLE001, S110
            pass
        raise BootstrapFailure(runner.stage, safe_type, dict(runner.evidence)) from None
    finally:
        runner.close()
