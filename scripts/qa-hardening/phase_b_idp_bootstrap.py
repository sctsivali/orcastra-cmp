"""Configure a fresh approved QA IdP, proving configuration rather than login.

The caller supplies a bounded private HTTP client and proves new containers,
fresh IdP storage and the complete pinned OCI configuration. This module never
discovers connections or credentials and has no executable entry point.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

LAB = "orcastra-qa-hardening"
GUEST_ROOT = Path("/srv") / LAB
MARKER_ROOT = Path("/etc")
VM_RUN_ID = "0f2a5b15-f5ec-464b-8785-0b4b88c3b358"
PHASE_A_DIGEST = "384bc2030c890ea26f054ece6725a0876fd95e95a6ccfdfe53368d17e3eb863e"
B1_ID = "86bee809-561c-4a19-8a69-ea738ae640bb"
B1_DIGEST = "b907a6b4a4e9fe6e58cf28c7a9d1c3266e8b8b5e90eacf56323275e9f63cacbb"
B1_HELPER = "50684d551b719324ced78749631661987e3d7db7f32ca5282b790d691f1969ca"
IMAGE_MANIFEST = "sha256:09782fe56675bc616a0324468f1698e2d9d83c978bc5e426686fb7563517a442"
IMAGE_CONFIG = "sha256:d4d6843a3c120bb1b809048ce8429e0e617f68a3991aaa0779d6f5bd802b34d9"
API_ORIGIN = "http://127.0.0.1:32090"
API_HOST = "auth.orcastra-qa.invalid:32090"
ISSUER = "http://" + API_HOST + "/application/o/" + LAB + "/"
CALLBACK = "http://127.0.0.1:32025/api/auth/callback/authentik"
SERVICE = LAB + "-role-sync"
TOKEN_IDENTIFIER = SERVICE + "-api"
SIGNING_KEY = LAB + "-oidc-signing"
ROLE_GROUPS = ("role_admin", "role_partner", "role_tenant")
GROUP_PERMISSIONS = ("view_group", "add_user_to_group", "remove_user_from_group")
MAX_RESPONSE, MAX_REQUESTS = 256 * 1024, 128
REQUEST_SECONDS, TOTAL_SECONDS = 15.0, 240.0
PASSWORD_ITERATIONS = 1_000_000
PUBLIC_SOURCE = "https://raw.githubusercontent.com/goauthentik/authentik/version/2026.8.3/"
PINNED_SOURCES = (
    "authentik/core/api/users.py", "authentik/core/api/groups.py", "authentik/core/api/tokens.py",
    "authentik/core/api/applications.py", "authentik/core/api/providers.py", "authentik/core/models.py",
    "authentik/core/api/property_mappings.py", "authentik/core/api/utils.py",
    "authentik/lib/validators.py", "authentik/crypto/api.py", "authentik/crypto/models.py", "authentik/crypto/builder.py",
    "authentik/providers/oauth2/api/providers.py", "authentik/providers/oauth2/api/scopes.py",
    "authentik/providers/oauth2/models.py", "authentik/flows/api/flows.py", "authentik/flows/urls.py",
    "authentik/providers/oauth2/urls.py", "authentik/common/oauth/constants.py",
    "authentik/rbac/api/roles.py", "authentik/rbac/api/rbac.py", "authentik/rbac/api/rbac_roles.py",
    "authentik/rbac/api/rbac_assigned_by_roles.py", "authentik/rbac/decorators.py",
    "authentik/rbac/filters.py", "authentik/blueprints/api.py", "authentik/api/pagination.py",
    "blueprints/system/providers-oauth2.yaml",
    "blueprints/default/flow-default-authentication-flow.yaml",
    "blueprints/default/flow-default-provider-authorization-explicit-consent.yaml",
    "blueprints/default/flow-default-provider-invalidation.yaml",
)
# note: the pinned profile mapping supplies groups; no default groups scope exists.
MAPPING_AST = {
    "openid": "a93fc097d478939f47566553fc54a5143f560c231fe0ac4a9748b3566164e923",
    "email": "e02a4bfe613f04c88d4695fe3db863b711a8f2e8b9f4593ece6b22154c332831",
    "profile": "01b74a0a294fb00e8969b04dd6a0fc9fca405c19418c50fde904933dd790534a",
    "offline_access": "a93fc097d478939f47566553fc54a5143f560c231fe0ac4a9748b3566164e923",
}
FLOWS = {
    "authentication_flow": {"slug": "default-authentication-flow", "name": "Welcome to authentik!",
                            "designation": "authentication", "authentication": "none"},
    "authorization_flow": {"slug": "default-provider-authorization-explicit-consent",
                           "name": "Authorize Application", "designation": "authorization",
                           "authentication": "require_authenticated"},
    "invalidation_flow": {"slug": "default-provider-invalidation-flow",
                          "name": "Logged out of application", "designation": "invalidation",
                          "authentication": "none"},
}
ACTORS = tuple(
    {"alias": alias, "username": "qa-" + alias, "name": name,
     "email": alias + "@orcastra-qa.invalid", "role_group": group}
    for alias, name, group in (
        ("admin", "QA Admin", "role_admin"), ("partner-a", "QA Partner A", "role_partner"),
        ("partner-b", "QA Partner B", "role_partner"), ("tenant-a1", "QA Tenant A1", "role_tenant"),
        ("tenant-a2", "QA Tenant A2", "role_tenant"), ("tenant-b1", "QA Tenant B1", "role_tenant"),
    )
)


def canonical_digest(document: object) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _exact(actual: object, expected: object) -> bool:
    # important: Python equality otherwise accepts True as 1 and float IDs as integers.
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_exact(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_exact(a, e) for a, e in zip(actual, expected, strict=True))
    return actual == expected


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("A canonical UUID version 4 is required")
    parsed = uuid.UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("A canonical UUID version 4 is required")
    return value


def _sha(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("A canonical SHA256 digest is required")
    return value


def _id(value: object, kind: str) -> str | int:
    if kind in {"user", "provider", "permission_readback"}:
        if type(value) is not int or not 0 < value < 2 ** 63:
            raise ValueError("A positive object ID is required")
        return value
    if kind == "permission":
        if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value):
            raise ValueError("A positive permission ID is required")
        return value
    return _uuid(value)


def expected_idp_spec() -> dict:
    """Return the closed nonsecret contract for inclusion in the complete B2 proposal."""
    return {
        "schema_version": 1, "source_tag": "version/2026.8.3", "configuration_only": True,
        "image": {"registry": "ghcr.io", "repository": "goauthentik/server", "requested": "2026.8.3",
                  "platform": "linux/amd64", "manifest_digest": IMAGE_MANIFEST, "config_digest": IMAGE_CONFIG},
        "endpoint": API_ORIGIN, "host_header": API_HOST,
        "provider": {"name": LAB, "client_id": LAB, "client_type": "confidential",
                     "grant_types": ["authorization_code", "refresh_token"],
                     "redirect_uris": [{"matching_mode": "strict", "url": CALLBACK,
                                        "redirect_uri_type": "authorization"}],
                     "issuer_mode": "per_provider", "sub_mode": "hashed_user_id",
                     "include_claims_in_id_token": True, "access_code_validity": "minutes=1",
                     "access_token_validity": "hours=1", "refresh_token_validity": "days=1",
                     "refresh_token_threshold": "seconds=0", "encryption_key": None,
                     "jwt_federation_sources": [], "jwt_federation_providers": [],
                     "logout_uri": "", "logout_method": "backchannel"},
        "application": {"name": "Orcastra QA Hardening", "slug": LAB, "backchannel_providers": [],
                        "meta_launch_url": "http://127.0.0.1:32025", "policy_engine_mode": "all",
                        "meta_hide": False, "open_in_new_tab": False},
        "signing_key": {"common_name": SIGNING_KEY, "subject_alt_name": "", "validity_days": 30, "alg": "rsa"},
        "flows": json.loads(json.dumps(FLOWS)),
        "mappings": [{"scope_name": scope, "managed": "goauthentik.io/providers/oauth2/scope-" + scope,
                      "name": "authentik default OAuth Mapping: OpenID '" + scope + "'",
                      "expression_ast_sha256": fingerprint} for scope, fingerprint in MAPPING_AST.items()],
        "groups_claim_scope": "profile", "groups_claim_source": "request.user.groups.all()",
        "groups": [{"name": group, "is_superuser": False, "parents": [], "roles": []} for group in ROLE_GROUPS],
        "actors": json.loads(json.dumps(ACTORS)),
        "passwords": {"algorithm": "pbkdf2_sha256", "iterations": PASSWORD_ITERATIONS,
                      "api_action": "set_password_hash", "request_field": "password"},
        "backend_service": {"username": SERVICE, "name": "QA Role Sync", "email": "role-sync@orcastra-qa.invalid",
                            "type": "service_account", "groups": [], "role_name": SERVICE,
                            "global_permissions": [], "group_permissions": list(GROUP_PERMISSIONS),
                            "user_permissions": ["view_user"], "token_identifier": TOKEN_IDENTIFIER,
                            "token_intent": "api", "token_expiring": True, "maximum_token_seconds": 86400},
        "excluded_runtime_families": ["IdP group creation or deletion", "New IdP users or object grants",
                                      "Actual SSO login/logout", "JWT signature/audience/expiry assertions"],
        "bounds": {"requests": MAX_REQUESTS, "response_bytes": MAX_RESPONSE,
                   "request_seconds": REQUEST_SECONDS, "total_seconds": TOTAL_SECONDS},
    }


def validate_proposal(proposal: dict, approval_digest: str, approval_reference: str) -> dict:
    """Reject changed, partial or draft proposals before private access."""
    if not isinstance(proposal, dict):
        raise TypeError("A complete B2 proposal is required")
    core = dict(proposal)
    digest = core.pop("proposal_digest", None)
    if (not isinstance(approval_reference, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/ -]{0,191}", approval_reference)
        or digest != _sha(approval_digest) or canonical_digest(core) != approval_digest):
        raise ValueError("Exact owner approval must bind the complete B2 proposal")
    if (not _exact(core.get("schema_version"), 1) or core.get("phase") != "B2"
        or core.get("ready_for_guest_execution") is not True or core.get("remaining_gates") != []):
        raise ValueError("A draft or incomplete B2 proposal cannot configure the IdP")
    if _uuid(core.get("phase_b_id")) in {VM_RUN_ID, B1_ID}:
        raise ValueError("B2 requires a new phase ownership ID")
    if not _exact(core.get("target"), {
        "vm_name": LAB, "vm_run_id": VM_RUN_ID, "phase_a_digest": PHASE_A_DIGEST,
        "b1_phase_id": B1_ID, "project": "default", "profiles": [], "outer_nic_count": 0,
    }) or not _exact(core.get("b1_readback"), {"proposal_digest": B1_DIGEST, "helper_sha256": B1_HELPER}):
        raise ValueError("B2 must bind the exact approved disconnected guest and B1 provenance")
    if not _exact(core.get("idp_bootstrap"), expected_idp_spec()):
        raise ValueError("The pinned closed IdP contract differs or permissions remain a draft")
    return json.loads(json.dumps(core, allow_nan=False))


def validate_container_attestation(attestation: dict, phase_b_id: str) -> None:
    """Require immutable new-container proof, never discovery or adoption."""
    required = {"container_id", "created_id", "created_for_phase", "labels", "image_id",
                "origin_manifest_digest", "endpoint", "host_header", "published_ports",
                "new_container_verified", "fresh_idp_database_verified", "full_config_verified",
                "runtime_boundary_verified", "readiness_verified"}
    if not isinstance(attestation, dict) or set(attestation) != required:
        raise ValueError("The exact new owned IdP container attestation is required")
    if (not isinstance(attestation["container_id"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", attestation["container_id"])
        or attestation["created_id"] != attestation["container_id"]
        or attestation["created_for_phase"] != phase_b_id
        or attestation["labels"] != {"orcastra.qa.lab": LAB, "orcastra.qa.vm_run": VM_RUN_ID,
                                     "orcastra.qa.phase_b": phase_b_id}
        or attestation["image_id"] != IMAGE_CONFIG or attestation["origin_manifest_digest"] != IMAGE_MANIFEST
        or attestation["endpoint"] != API_ORIGIN or attestation["host_header"] != API_HOST
        or attestation["published_ports"] != {"32090/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32090"}]}
        or any(attestation[k] is not True for k in required if k.endswith("verified"))):
        raise ValueError("A reused container, database or widened runtime/endpoint cannot be configured")


def _strict_json(body: bytes) -> object:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON keys are forbidden")
            result[key] = value
        return result

    def reject(_value):
        raise ValueError("Non-finite JSON numbers are forbidden")

    return json.loads(body, object_pairs_hook=pairs, parse_constant=reject)


def _open_directory(path: Path, *, private: bool = True) -> int:
    if not path.is_absolute() or ".." in path.parts or str(path) != os.path.normpath(path):
        raise ValueError("An exact absolute guest directory is required")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
        status = os.fstat(directory)
        if status.st_uid != 0 or status.st_gid != 0 or private and stat.S_IMODE(status.st_mode) != 0o700:
            raise PermissionError("Guest private directories must be root:root 0700")
        return directory
    except BaseException:
        os.close(directory)
        raise


def _private_bytes(directory: int, name: str, maximum: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as source:
        status = os.fstat(source.fileno())
        if (not stat.S_ISREG(status.st_mode) or status.st_uid != 0 or status.st_gid != 0
            or stat.S_IMODE(status.st_mode) != 0o600 or status.st_nlink != 1):
            raise PermissionError("Ownership markers must be root:root regular 0600 files")
        body = source.read(maximum + 1)
        if len(body) > maximum:
            raise ValueError("The private marker exceeds its bound")
        return body


def _write_new(directory: int, name: str, body: bytes) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    with os.fdopen(fd, "wb") as target:
        os.fchmod(target.fileno(), 0o600)
        target.write(body)
        target.flush()
        os.fsync(target.fileno())
    os.fsync(directory)


def _password_pair() -> tuple[str, str]:
    password, salt = secrets.token_urlsafe(32), secrets.token_urlsafe(18)
    encoded = base64.b64encode(hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(),
                                                PASSWORD_ITERATIONS)).decode()
    return password, f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${encoded}"


@dataclass(frozen=True, repr=False)
class BoundedResponse:
    status: int
    body: bytes
    elapsed_seconds: float


class PrivateHTTPClient(Protocol):
    """Bound reads/timeouts and never log private headers, payloads, responses or error text."""

    def request(self, method: str, url: str, *, headers: Mapping[str, str], body: bytes | None,
                timeout_seconds: float, maximum_response_bytes: int,
                follow_redirects: bool) -> BoundedResponse: ...


class BootstrapError(RuntimeError):
    """Sanitized failure retaining accepted IDs for separately approved cleanup."""

    def __init__(self, stage: str, accepted_ids: dict):
        super().__init__("IdP bootstrap stopped at " + stage + "; retain private state, no retry or cleanup")
        self.stage, self.accepted_ids = stage, dict(accepted_ids)


class IdPBootstrap:
    def __init__(self, proposal: dict, approval_digest: str, approval_reference: str, *,
                 private_state_root: Path, client: PrivateHTTPClient, container_attestation: dict,
                 bootstrap_token: str):
        self.plan = validate_proposal(proposal, approval_digest, approval_reference)
        self.phase = self.plan["phase_b_id"]
        validate_container_attestation(container_attestation, self.phase)
        self.private_root = Path(private_state_root)
        if self.private_root != GUEST_ROOT / "state" / self.phase:
            raise ValueError("Only the exact approved guest phase state is supported")
        self.approval_digest = approval_digest
        self.approval_reference_hash = hashlib.sha256(approval_reference.encode()).hexdigest()
        self.client, self._bootstrap_token = client, bootstrap_token
        self.stage, self.created = "private_preflight", {}
        self.created_by_kind = {}
        self.ledger, self.directory, self.requests = None, None, 0
        self.started = False
        self.deadline = time.monotonic() + TOTAL_SECONDS
        self.groups, self.users, self.flows, self.mappings = {}, {}, {}, {}

    def _record(self, event: str, **fields) -> None:
        if self.ledger is None:
            return
        row = {"event": event, "stage": self.stage, "phase_b_id": self.phase, "vm_run_id": VM_RUN_ID, **fields}
        self.ledger.write(json.dumps(row, sort_keys=True) + "\n")
        self.ledger.flush()
        os.fsync(self.ledger.fileno())

    def _preflight(self) -> None:
        if os.geteuid() != 0 or os.getegid() != 0:
            raise PermissionError("Only guest root:root may configure the owned IdP")
        for path in (GUEST_ROOT, GUEST_ROOT / "state", self.private_root):
            os.close(_open_directory(path))
        markers = _open_directory(MARKER_ROOT, private=False)
        try:
            if (_private_bytes(markers, LAB, 128).strip() != LAB.encode()
                or _private_bytes(markers, LAB + "-run", 128).strip() != VM_RUN_ID.encode()):
                raise ValueError("The approved guest ownership markers differ")
        finally:
            os.close(markers)
        phase_root = _open_directory(self.private_root)
        try:
            if _strict_json(_private_bytes(phase_root, "owner.json", 2048)) != {
                "lab": LAB, "vm_run_id": VM_RUN_ID, "phase_b_id": self.phase, "proposal_digest": self.approval_digest,
            }:
                raise ValueError("The phase owner marker differs from the exact approval")
            os.mkdir("idp-bootstrap", 0o700, dir_fd=phase_root)
            os.fsync(phase_root)
            self.directory = os.open("idp-bootstrap", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=phase_root)
            os.fchmod(self.directory, 0o700)
        finally:
            os.close(phase_root)
        _write_new(self.directory, "ledger.jsonl", b"")
        fd = os.open("ledger.jsonl", os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, dir_fd=self.directory)
        self.ledger = os.fdopen(fd, "a", encoding="utf-8")
        self._record("approved", proposal_digest=self.approval_digest,
                     approval_reference_sha256=self.approval_reference_hash)
        if (not isinstance(self._bootstrap_token, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{40,255}", self._bootstrap_token)):
            raise ValueError("A privately supplied bounded bootstrap token is required")
        _write_new(self.directory, "bootstrap-token", self._bootstrap_token.encode())

    def _api(self, method: str, path: str, *, payload: dict | None = None, token: str | None = None,
             accepted: tuple[int, ...] = (200,), creating: tuple[str, str] | None = None) -> object:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.requests >= MAX_REQUESTS:
            raise ValueError("The finite IdP request budget is exhausted")
        if method not in {"GET", "POST"} or not path.startswith("/api/v3/"):
            raise ValueError("Only the reviewed configuration API is supported")
        self.requests += 1
        self._record("request_attempt", request_number=self.requests, write=method == "POST")
        started, bound = time.monotonic(), min(REQUEST_SECONDS, remaining)
        response = self.client.request(
            method, API_ORIGIN + path,
            headers={"Host": API_HOST, "Authorization": "Bearer " + (token or self._bootstrap_token),
                     "Content-Type": "application/json"},
            body=None if payload is None else json.dumps(payload, allow_nan=False).encode(),
            timeout_seconds=bound, maximum_response_bytes=MAX_RESPONSE, follow_redirects=False,
        )
        if (not isinstance(response, BoundedResponse) or type(response.status) is not int
            or not 100 <= response.status <= 599
            or not isinstance(response.body, bytes) or len(response.body) > MAX_RESPONSE):
            raise ValueError("The bounded response contract failed")
        self._record("request_answered", request_number=self.requests, status=response.status)
        body = None if not response.body else _strict_json(response.body)
        if creating and response.status in accepted:
            kind, alias = creating
            if kind == "permission":
                if not isinstance(body, list) or len(body) > 3:
                    raise ValueError("Permission assignment did not return bounded accepted IDs")
                pairs = [(alias + ":" + str(i), item.get("id")) for i, item in enumerate(body) if isinstance(item, dict)]
                if len(pairs) != len(body):
                    raise ValueError("Permission assignment IDs are malformed")
            else:
                if not isinstance(body, dict):
                    raise ValueError("Creation did not return its accepted ID")
                pairs = [(alias, body.get("pk"))]
            for key, value in pairs:
                identifier = _id(value, kind)
                self.created[key] = identifier
                self._record("object_accepted", alias=key, object_id=identifier)
                previous = self.created_by_kind.setdefault(kind, set())
                if identifier in previous:
                    raise ValueError("Creation returned an already accepted object ID")
                previous.add(identifier)
        if (type(response.elapsed_seconds) not in {int, float} or not math.isfinite(response.elapsed_seconds)
            or not 0 <= response.elapsed_seconds <= bound or time.monotonic() - started > bound
            or time.monotonic() > self.deadline):
            raise ValueError("The finite request deadline was exceeded")
        if response.status not in accepted:
            raise ValueError("The configuration API refused the request")
        return body

    def _list(self, path: str, filters: dict, *, token: str | None = None) -> list[dict]:
        entries, total, expected_pages = [], None, None
        for page in range(1, 5):
            body = self._api("GET", path + "?" + urlencode({**filters, "page_size": 100, "page": page}), token=token)
            if not isinstance(body, dict) or not isinstance(body.get("results"), list):
                raise TypeError("A complete bounded result page is required")
            pagination = body.get("pagination")
            keys = {"count", "current", "next", "previous", "total_pages", "start_index", "end_index"}
            if (not isinstance(pagination, dict) or not keys.issubset(pagination)
                or any(type(pagination[k]) is not int for k in keys)
                or pagination["current"] != page or pagination["previous"] != page - 1
                or not 0 <= pagination["count"] <= 64 or not 1 <= pagination["total_pages"] <= 4
                or any(not isinstance(item, dict) for item in body["results"])):
                raise ValueError("The result page is malformed or outside its bound")
            if total is None:
                total, expected_pages = pagination["count"], pagination["total_pages"]
            if (pagination["count"] != total or pagination["total_pages"] != expected_pages
                or pagination["start_index"] != (len(entries) + 1 if total else 0)
                or pagination["end_index"] != len(entries) + len(body["results"])):
                raise ValueError("The result set changed or omitted objects")
            entries.extend(body["results"])
            if page == expected_pages:
                if pagination["next"] != 0 or len(entries) != total:
                    raise ValueError("The final result page is incomplete")
                return entries
            if pagination["next"] != page + 1 or not body["results"]:
                raise ValueError("A result page cannot be followed safely")
        raise ValueError("The bounded pagination contract failed")

    def _absent(self) -> None:
        self.stage = "absence_readback"
        bootstrap = self._api("GET", "/api/v3/core/users/me/")
        if (not isinstance(bootstrap, dict) or not isinstance(bootstrap.get("user"), dict)
            or bootstrap.get("original") is not None or bootstrap["user"].get("username") != "akadmin"
            or bootstrap["user"].get("is_superuser") is not True):
            raise ValueError("The fresh bootstrap identity cannot prove a complete hidden application inventory")
        self._record("bootstrap_inventory_authority_verified", verified=True)
        checks = [("/core/groups/", {"search": group}) for group in ROLE_GROUPS]
        checks += [("/rbac/roles/", {"search": SERVICE}), ("/core/tokens/", {"identifier": TOKEN_IDENTIFIER}),
                   ("/providers/oauth2/", {"name": LAB}), ("/providers/oauth2/", {"client_id": LAB}),
                   ("/core/applications/", {"slug": LAB, "superuser_full_list": "true"}),
                   ("/crypto/certificatekeypairs/", {"search": SIGNING_KEY})]
        for actor in (*ACTORS, {"username": SERVICE, "email": "role-sync@orcastra-qa.invalid"}):
            checks += [("/core/users/", {"search": actor["username"]}), ("/core/users/", {"email": actor["email"]})]
        for path, filters in checks:
            if self._list("/api/v3" + path, filters):
                raise ValueError("An existing or neighboring object forbids creation or adoption")
        self._record("owned_object_names_absent", verified=True)

    @staticmethod
    def _verify_mapping(row: dict, mapping: dict) -> None:
        expression = row.get("expression")
        if (any(row.get(k) != mapping[k] for k in ("name", "managed", "scope_name"))
            or not isinstance(expression, str) or len(expression.encode()) > 8192):
            raise ValueError("The pinned managed scope mapping differs")
        fingerprint = hashlib.sha256(ast.dump(ast.parse(expression), include_attributes=False).encode()).hexdigest()
        if fingerprint != mapping["expression_ast_sha256"]:
            raise ValueError("The pinned mapping semantics differ")

    def _defaults(self) -> None:
        self.stage = "pinned_defaults_readback"
        for field, expected in FLOWS.items():
            rows = self._list("/api/v3/flows/instances/", {"slug": expected["slug"]})
            if len(rows) != 1 or any(rows[0].get(k) != v for k, v in expected.items()):
                raise ValueError("The pinned default flow differs")
            self.flows[field] = _uuid(rows[0].get("pk"))
            self._record("default_verified", alias=field, object_id=self.flows[field])
        for mapping in self.plan["idp_bootstrap"]["mappings"]:
            rows = self._list("/api/v3/propertymappings/provider/scope/", {"managed": mapping["managed"]})
            if len(rows) != 1:
                raise ValueError("The exact pinned managed scope mapping is required")
            self._verify_mapping(rows[0], mapping)
            self.mappings[mapping["scope_name"]] = _uuid(rows[0].get("pk"))
            self._record("default_verified", alias="mapping-" + mapping["scope_name"],
                         object_id=self.mappings[mapping["scope_name"]])
        if (len(set(self.flows.values())) != len(self.flows)
            or len(set(self.mappings.values())) != len(self.mappings)):
            raise ValueError("Pinned default object IDs are ambiguous")
        for codename, model in (("view_user", "user"), *((p, "group") for p in GROUP_PERMISSIONS)):
            rows = self._list("/api/v3/rbac/permissions/", {"codename": codename,
                              "content_type__model": model, "content_type__app_label": "authentik_core"})
            if (len(rows) != 1 or rows[0].get("codename") != codename or rows[0].get("model") != model
                or rows[0].get("app_label") != "authentik_core"):
                raise ValueError("The concrete least privilege permission mapping remains a draft")

    def _create(self, kind: str, alias: str, path: str, payload: dict, *, status: int = 201) -> dict:
        self.stage = "create_" + alias
        body = self._api("POST", "/api/v3" + path, payload=payload, accepted=(status,), creating=(kind, alias))
        if not isinstance(body, dict):
            raise TypeError("The owned object create response is malformed")
        return body

    def _configure(self) -> None:
        self.stage = "fresh_private_credentials"
        credentials = {"actors": {}, "client_secret": secrets.token_urlsafe(48)}
        self._client_secret = credentials["client_secret"]
        for actor in ACTORS:
            password, encoded = _password_pair()
            credentials["actors"][actor["alias"]] = {"password": password, "password_hash": encoded}
        _write_new(self.directory, "credentials.json", json.dumps(credentials).encode())
        self._record("private_credentials_written", verified=True)
        for group in ROLE_GROUPS:
            row = self._create("group", group, "/core/groups/", {
                "name": group, "is_superuser": False, "parents": [], "children": [], "roles": [], "users": [],
                "attributes": {"orcastra.qa.phase_b": self.phase},
            })
            self.groups[group] = _uuid(row["pk"])
            self._verify_group(row, group, [])
        role = self._create("role", "backend-role", "/rbac/roles/", {"name": SERVICE})
        if role.get("name") != SERVICE:
            raise ValueError("The new backend role identity differs")
        self.role_id = _uuid(role["pk"])
        for actor in ACTORS:
            row = self._create("user", actor["alias"], "/core/users/", {
                "username": actor["username"], "name": actor["name"], "email": actor["email"],
                "type": "internal", "path": "qa/" + LAB, "is_active": True,
                "groups": [self.groups[actor["role_group"]]], "roles": [],
                "attributes": {"orcastra.qa.phase_b": self.phase},
            })
            self.users[actor["alias"]] = _id(row["pk"], "user")
            self._verify_user(row, actor, [self.groups[actor["role_group"]]], [])
            self.stage = "hash_password_" + actor["alias"]
            self._api("POST", f"/api/v3/core/users/{row['pk']}/set_password_hash/",
                      payload={"password": credentials["actors"][actor["alias"]]["password_hash"]}, accepted=(204,))
            self._record("password_hash_import_accepted", alias=actor["alias"], verified=True)
        for group, pk in self.groups.items():
            self._grant("group", pk, GROUP_PERMISSIONS, "grant-" + group)
        for alias, pk in self.users.items():
            self._grant("user", pk, ("view_user",), "grant-" + alias)
        service = self.plan["idp_bootstrap"]["backend_service"]
        self.service_actor = {k: service[k] for k in ("username", "name", "email", "type")}
        row = self._create("user", "backend-service", "/core/users/", {
            **self.service_actor, "path": "qa/" + LAB, "is_active": True, "groups": [],
            "roles": [self.role_id], "attributes": {"orcastra.qa.phase_b": self.phase},
        })
        self.service_id = _id(row["pk"], "user")
        self._verify_user(row, self.service_actor, [], [self.role_id])
        token = self._create("token", "backend-token", "/core/tokens/", {
            "identifier": TOKEN_IDENTIFIER, "intent": "api", "user": self.service_id,
            "expiring": True, "description": "Synthetic QA role sync",
        })
        self._verify_token(token)
        self.stage = "read_private_backend_token"
        key = self._api("GET", "/api/v3/core/tokens/" + TOKEN_IDENTIFIER + "/view_key/")
        if (not isinstance(key, dict) or set(key) != {"key"} or not isinstance(key["key"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{40,255}", key["key"])):
            raise ValueError("The privately generated backend token is malformed")
        self._service_token = key["key"]
        _write_new(self.directory, "backend-service-token", self._service_token.encode())
        signing = self._create("key", "signing-key", "/crypto/certificatekeypairs/generate/",
                               self.plan["idp_bootstrap"]["signing_key"], status=200)
        self._verify_signing_key(signing)
        provider = self._create("provider", "provider", "/providers/oauth2/", {
            **self.plan["idp_bootstrap"]["provider"], **self.flows, "property_mappings": list(self.mappings.values()),
            "signing_key": signing["pk"], "client_secret": credentials["client_secret"],
        })
        self.provider_id = _id(provider["pk"], "provider")
        self._verify_provider(provider, assigned=False)
        application = self._create("application", "application", "/core/applications/", {
            **self.plan["idp_bootstrap"]["application"], "provider": self.provider_id,
        })
        self._verify_application(application)

    def _grant(self, model: str, pk: str | int, codenames: tuple[str, ...], alias: str) -> None:
        self.stage = alias
        result = self._api("POST", "/api/v3/rbac/permissions/assigned_by_roles/" + self.role_id + "/assign/",
                           payload={"permissions": ["authentik_core." + name for name in codenames],
                                    "model": "authentik_core." + model, "object_pk": str(pk)},
                           creating=("permission", alias))
        if not isinstance(result, list) or len(result) != len(codenames):
            raise ValueError("The API did not accept exactly the approved scoped grants")

    def _verify_group(self, row: dict, group: str, users: list[int], *, expanded: bool = False) -> None:
        if (row.get("pk") != self.groups[group] or row.get("name") != group or row.get("is_superuser") is not False
            or row.get("parents") != [] or row.get("children") != [] or row.get("roles") != []
            or row.get("roles_obj") != []
            or any(row.get(k) != ([] if expanded else None)
                   for k in ("parents_obj", "children_obj", "inherited_roles_obj"))
            or row.get("attributes") != {"orcastra.qa.phase_b": self.phase}
            or not isinstance(row.get("users"), list) or any(type(pk) is not int for pk in row["users"])
            or sorted(row["users"]) != sorted(users)
            or len(row["users"]) != len(set(row["users"]))):
            raise ValueError("The owned role group membership or privilege differs")

    def _verify_user(self, row: dict, actor: dict, groups: list[str], roles: list[str]) -> None:
        _id(row.get("pk"), "user")
        objects = row.get("groups_obj")
        role_objects = [{"pk": pk, "name": SERVICE} for pk in roles]
        if (any(row.get(k) != actor[k] for k in ("username", "name", "email"))
            or row.get("is_active") is not True or row.get("is_superuser") is not False
            or row.get("type") != actor.get("type", "internal") or row.get("groups") != groups
            or row.get("roles") != roles or not _exact(row.get("roles_obj"), role_objects)
            or row.get("path") != "qa/" + LAB
            or row.get("attributes") != {"orcastra.qa.phase_b": self.phase}
            or not isinstance(objects, list) or len(objects) != len(groups)):
            raise ValueError("The owned actor identity, group or privilege differs")
        for obj in objects:
            if (not isinstance(obj, dict) or obj.get("pk") not in groups or obj.get("name") not in ROLE_GROUPS
                or obj.get("is_superuser") is not False or obj.get("pk") != self.groups[obj["name"]]):
                raise ValueError("The actor has a foreign or privileged group")
        if len({obj["pk"] for obj in objects}) != len(groups):
            raise ValueError("The actor group readback is ambiguous")

    def _verify_token(self, row: dict) -> None:
        if not isinstance(row.get("expires"), str):
            raise TypeError("The service token expiry is missing")
        expires = datetime.fromisoformat(row["expires"].replace("Z", "+00:00"))
        if expires.tzinfo is None:
            raise ValueError("The service token expiry lacks UTC context")
        if (row.get("pk") != self.created["backend-token"] or row.get("identifier") != TOKEN_IDENTIFIER
            or not _exact(row.get("user"), self.service_id)
            or row.get("intent") != "api" or row.get("expiring") is not True
            or row.get("managed") not in {None, ""}
            or not 0 < (expires - datetime.now(timezone.utc)).total_seconds() <= 86405):
            raise ValueError("The backend token scope, identity or finite expiry differs")

    def _verify_signing_key(self, row: dict) -> None:
        fingerprint = row.get("fingerprint_sha256")
        if (row.get("pk") != self.created["signing-key"] or row.get("name") != SIGNING_KEY
            or row.get("private_key_available") is not True or row.get("key_type") != "rsa"
            or row.get("managed") not in {None, ""} or not isinstance(fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){31}", fingerprint)
            or any(k in row for k in ("key_data", "certificate_data"))):
            raise ValueError("The newly generated signing key metadata differs")

    def _verify_provider(self, row: dict, *, assigned: bool) -> None:
        expected = {**self.plan["idp_bootstrap"]["provider"], **self.flows, "signing_key": self.created["signing-key"]}
        if (not _exact(row.get("pk"), self.provider_id) or any(not _exact(row.get(k), v) for k, v in expected.items())
            or not isinstance(row.get("client_secret"), str)
            or not secrets.compare_digest(row["client_secret"], self._client_secret)
            or not isinstance(row.get("property_mappings"), list)
            or sorted(row["property_mappings"]) != sorted(self.mappings.values())
            or len(row["property_mappings"]) != len(self.mappings)
            or assigned and row.get("assigned_application_slug") != LAB
            or not assigned and row.get("assigned_application_slug") not in {None, ""}
            or row.get("assigned_backchannel_application_slug") not in {None, ""}):
            raise ValueError("The provider grant, callback, issuer, key or mapping contract differs")

    def _verify_application(self, row: dict) -> None:
        if (row.get("pk") != self.created["application"] or not _exact(row.get("provider"), self.provider_id)
            or any(not _exact(row.get(k), v) for k, v in self.plan["idp_bootstrap"]["application"].items())):
            raise ValueError("The new application identity or provider association differs")

    def _readback(self) -> dict:
        self.stage = "independent_configuration_readback"
        for actor in ACTORS:
            row = self._api("GET", f"/api/v3/core/users/{self.users[actor['alias']]}/")
            if not isinstance(row, dict) or row.get("pk") != self.users[actor["alias"]]:
                raise ValueError("The independently read actor ID differs")
            self._verify_user(row, actor, [self.groups[actor["role_group"]]], [])
        for group, pk in self.groups.items():
            row = self._api("GET", f"/api/v3/core/groups/{pk}/"
                            "?include_parents=true&include_children=true&include_inherited_roles=true")
            if not isinstance(row, dict):
                raise TypeError("The independently read group is malformed")
            self._verify_group(row, group, [self.users[a["alias"]] for a in ACTORS if a["role_group"] == group], expanded=True)
        for mapping in self.plan["idp_bootstrap"]["mappings"]:
            row = self._api("GET", "/api/v3/propertymappings/provider/scope/" + self.mappings[mapping["scope_name"]] + "/")
            if not isinstance(row, dict) or row.get("pk") != self.mappings[mapping["scope_name"]]:
                raise ValueError("The independently read managed mapping ID differs")
            self._verify_mapping(row, mapping)
        role = self._api("GET", "/api/v3/rbac/roles/" + self.role_id + "/")
        if not _exact(role, {"pk": self.role_id, "name": SERVICE}):
            raise ValueError("The independently read owned backend role differs")
        if self._list("/api/v3/rbac/permissions/", {"role": self.role_id}):
            raise ValueError("The backend role acquired a global model permission")
        grants = self._list("/api/v3/rbac/permissions/roles/", {"uuid": self.role_id})
        expected = {("authentik_core", "group", name, pk) for pk in self.groups.values() for name in GROUP_PERMISSIONS}
        expected |= {("authentik_core", "user", "view_user", str(pk)) for pk in self.users.values()}
        actual = {(r.get("app_label"), r.get("model"), r.get("codename"), r.get("object_pk")) for r in grants}
        accepted_grants = {value for key, value in self.created.items() if key.startswith("grant-")}
        read_grants = {str(_id(r.get("id"), "permission_readback")) for r in grants}
        if actual != expected or len(grants) != len(expected) or read_grants != accepted_grants:
            raise ValueError("The backend role has missing, duplicate or foreign object grants")
        service = self._api("GET", f"/api/v3/core/users/{self.service_id}/")
        if not isinstance(service, dict) or service.get("pk") != self.service_id:
            raise ValueError("The independently read service identity differs")
        self._verify_user(service, self.service_actor, [], [self.role_id])
        token = self._api("GET", "/api/v3/core/tokens/" + TOKEN_IDENTIFIER + "/")
        if not isinstance(token, dict):
            raise TypeError("The independently read service token is malformed")
        self._verify_token(token)
        current = self._api("GET", "/api/v3/core/users/me/", token=self._service_token)
        if (not isinstance(current, dict) or not isinstance(current.get("user"), dict)
            or current.get("original") is not None or not _exact(current["user"].get("pk"), self.service_id)
            or current["user"].get("username") != SERVICE or current["user"].get("is_superuser") is not False
            or current["user"].get("groups") != [] or current["user"].get("system_permissions") != []
            or current["user"].get("roles") != [{"name": SERVICE, "pk": self.role_id}]):
            raise ValueError("The scoped API token authenticates with unexpected privilege")
        for actor in ACTORS:
            rows = self._list("/api/v3/core/users/", {"search": actor["email"]}, token=self._service_token)
            if len(rows) != 1 or rows[0].get("pk") != self.users[actor["alias"]]:
                raise ValueError("The backend token cannot read exactly the owned actor")
            detail = self._api("GET", f"/api/v3/core/users/{self.users[actor['alias']]}/", token=self._service_token)
            if not isinstance(detail, dict) or detail.get("pk") != self.users[actor["alias"]]:
                raise ValueError("The backend token cannot read the owned actor detail")
            self._verify_user(detail, actor, [self.groups[actor["role_group"]]], [])
        for group, pk in self.groups.items():
            rows = self._list("/api/v3/core/groups/", {"search": group}, token=self._service_token)
            if len(rows) != 1 or rows[0].get("pk") != pk or rows[0].get("name") != group:
                raise ValueError("The backend token cannot resolve exactly the seeded role group")
        for name, path, verifier in (
            ("signing-key", "/crypto/certificatekeypairs/" + self.created["signing-key"] + "/", self._verify_signing_key),
            ("application", "/core/applications/" + LAB + "/", self._verify_application),
        ):
            row = self._api("GET", "/api/v3" + path)
            if not isinstance(row, dict) or row.get("pk") != self.created[name]:
                raise ValueError("The independently read owned object ID differs")
            verifier(row)
        provider = self._api("GET", f"/api/v3/providers/oauth2/{self.provider_id}/")
        if not isinstance(provider, dict):
            raise TypeError("The independently read provider is malformed")
        self._verify_provider(provider, assigned=True)
        setup = self._api("GET", f"/api/v3/providers/oauth2/{self.provider_id}/setup_urls/")
        urls = {"issuer": ISSUER, "authorize": "http://" + API_HOST + "/application/o/authorize/",
                "token": "http://" + API_HOST + "/application/o/token/",
                "user_info": "http://" + API_HOST + "/application/o/userinfo/",
                "provider_info": ISSUER + ".well-known/openid-configuration",
                "logout": ISSUER + "end-session/", "jwks": ISSUER + "jwks/",
                "dcr_registration": ISSUER + "register/"}
        if setup != urls:
            raise ValueError("The independently read issuer/JWKS/setup URLs differ")
        result = {"configuration_proved": True, "login_proved": False, "jwt_proved": False,
                  "scoped_backend_permissions_proved": True, "private_credentials_written": True,
                  "phase_b_id": self.phase, "proposal_digest": self.approval_digest,
                  "accepted_ids": dict(self.created), "setup_urls_sha256": canonical_digest(urls),
                  "actor_identity_sha256": {a["alias"]: canonical_digest(a) for a in ACTORS}}
        self._record("configuration_proved", **result)
        return result

    def run(self) -> dict:
        if self.started:
            raise BootstrapError("single_use_guard", self.created)
        self.started = True
        try:
            self._preflight()
            self._absent()
            self._defaults()
            self._configure()
            return self._readback()
        except Exception:  # noqa: BLE001 - important: private upstream errors must never enter evidence.
            try:
                self._record("stopped", accepted_ids=dict(self.created), configuration_proved=False,
                             automatic_retry=False, automatic_cleanup=False)
            except (OSError, ValueError):
                # important: a failed ledger must still prevent private exception disclosure and all dependent calls.
                pass
            raise BootstrapError(self.stage, self.created) from None
        finally:
            if self.ledger is not None:
                self.ledger.close()
            if self.directory is not None:
                os.close(self.directory)
            self._bootstrap_token, self._service_token, self._client_secret = "", "", ""


def bootstrap_idp(proposal: dict, approval_digest: str, approval_reference: str, *, private_state_root: Path,
                  client: PrivateHTTPClient, container_attestation: dict, bootstrap_token: str) -> dict:
    """Run inside the trusted caller's immediately approved owned guest operation."""
    return IdPBootstrap(proposal, approval_digest, approval_reference, private_state_root=private_state_root,
                        client=client, container_attestation=container_attestation, bootstrap_token=bootstrap_token).run()
