"""Pure B2 draft renderer. Public bytes and proof attestations are explicit inputs.

This module performs no discovery, I/O, execution, or approval. Proof digests are
caller-supplied evidence references, never evidence produced by this renderer.
The proofs contain pre-execution evidence only. Runtime acceptance gates are
pending postconditions of a separately approved B2 execution.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from pathlib import PurePosixPath

from phase_b_idp_bootstrap import expected_idp_spec
from phase_b_vault_bootstrap import ownership_marker, vault_contract

LAB = "orcastra-qa-hardening"
ROOT = "/srv/" + LAB
VM_RUN_ID = "0f2a5b15-f5ec-464b-8785-0b4b88c3b358"
PHASE_A_DIGEST = "384bc2030c890ea26f054ece6725a0876fd95e95a6ccfdfe53368d17e3eb863e"
B1_ID = "86bee809-561c-4a19-8a69-ea738ae640bb"
B1_DIGEST = "b907a6b4a4e9fe6e58cf28c7a9d1c3266e8b8b5e90eacf56323275e9f63cacbb"
B1_HELPER_SHA256 = "50684d551b719324ced78749631661987e3d7db7f32ca5282b790d691f1969ca"
IDP_HELPER_SHA256 = "0ffa7e3d7db0e4d8386d73bd31b41408f4f991381d423149fd065465c049565e"
PG_GATE_ID = "e2a34c63-1f6f-4e6e-8c61-935cd5ec1789"
PG_PROPOSAL_DIGEST = "1f717582d99c5f3d8fc5a817326310c9d2614036450ac22f383f44915b80eb45"
PG_HELPER_SHA256 = "dd10d226cc3e38b1b6033919a80a57f009a9cb97541047092ac8cdcd0f53492a"
PUBLIC_PINS_SHA256 = "5c9ef077e3710c4c653284ca30ea908e2a226b0e7423380ad91a6792b8919663"
MAX_PUBLIC_BYTES = 2 * 1024 ** 2
MAX_PROPOSAL_BYTES = 1024 ** 2
MAX_ARCHIVE_BYTES = 4 * 1024 ** 3 + 16 * 1024 ** 2
IMAGE_REPOSITORIES = {
    "postgres": ("docker.io", "library/postgres"), "redis": ("docker.io", "library/redis"),
    "vault": ("docker.io", "hashicorp/vault"), "authentik": ("ghcr.io", "goauthentik/server"),
    "backend_base": ("docker.io", "library/python"),
}
NODE_COMPONENT = "frontend_supported_candidate"
PROOF_KEYS = {"source", "images", "packages", "node_selection", "b1_success", "pg_success",
              "executor_review", "outer_transport_review"}
SERVICE_LIMITS = {
    "postgres": (512, 500, 256), "authentik-postgres": (512, 500, 256),
    "redis": (128, 250, 128), "vault": (384, 250, 256),
    "authentik-server": (1024, 500, 512), "authentik-worker": (1024, 500, 512),
    "backend": (1536, 1000, 512), "frontend": (512, 500, 256),
}
VOLUME_ROLES = ("app-postgres", "auth-postgres", "redis-data", "vault-data", "auth-data", "backend-config", "uploads")
SOCKET_EXCLUSIONS = (ROOT + "/docker.sock", "/var/run/docker.sock", "/run/docker.sock",
                     "/run/containerd/containerd.sock", "/var/lib/docker", "/var/lib/lxd", "/var/lib/incus")


def canonical_digest(document: dict) -> str:
    body = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(body) > MAX_PROPOSAL_BYTES:
        raise ValueError("The canonical public proposal exceeds its byte bound")
    return hashlib.sha256(body).hexdigest()


def _closed(document: object, fields: set[str]) -> dict:
    if type(document) is not dict or set(document) != fields:
        raise ValueError("Only the closed public evidence schema is accepted")
    return document


def _sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("An exact lowercase SHA256 evidence digest is required")
    return value


def _oci(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError("A complete SHA256 image digest is required")
    _sha(value[7:])
    return value


def _integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("A bounded integer evidence value is required")
    return value


def _phase_id(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("A fresh canonical UUIDv4 is required")
    parsed = uuid.UUID(value)
    if parsed.version != 4 or str(parsed) != value or value in {VM_RUN_ID, B1_ID, PG_GATE_ID}:
        raise ValueError("B2 requires a new canonical UUIDv4")
    return value


def _filename(value: object, suffix: str) -> str:
    if (not isinstance(value, str) or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}", value) is None
            or not value.endswith(suffix) or PurePosixPath(value).name != value or ".." in value):
        raise ValueError("A public archive must have a unique plain filename")
    return value


def _strict_json(body: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate public JSON keys are refused")
            result[key] = value
        return result

    def reject(_value):
        raise ValueError("Non-finite public JSON values are refused")

    document = json.loads(body, object_pairs_hook=pairs, parse_constant=reject)
    if type(document) is not dict:
        raise TypeError("A public JSON object is required")
    return document


def _image(pin: dict, component: str) -> dict:
    repository = IMAGE_REPOSITORIES.get(component, ("docker.io", "library/node") if component == NODE_COMPONENT else None)
    if (type(pin) is not dict or repository is None or (pin.get("registry"), pin.get("repository")) != repository
            or pin.get("platform") != "linux/amd64"):
        raise ValueError("An exact reviewed public linux/amd64 repository is required")
    for name in ("index_digest", "manifest_digest", "config_digest"):
        _oci(pin[name])
    for name in ("manifest_bytes", "config_bytes"):
        _integer(pin[name], 1, 1024 ** 2)
    layers, diff_ids = pin["layers"], pin["uncompressed_layer_digests"]
    if (type(layers) is not list or type(diff_ids) is not list or not 0 < len(layers) <= 64
            or len(layers) != len(diff_ids)):
        raise ValueError("Every reviewed image layer must bind its config diff_id")
    for layer, diff_id in zip(layers, diff_ids, strict=True):
        _closed(layer, {"digest", "size", "media_type"})
        _oci(layer["digest"])
        _oci(diff_id)
        _integer(layer["size"], 1, 512 * 1024 ** 2)
        if layer["media_type"] not in {"application/vnd.oci.image.layer.v1.tar+gzip", "application/vnd.docker.image.rootfs.diff.tar.gzip"}:
            raise ValueError("Only bounded reviewed gzip image layers are supported")
    total = _integer(pin["compressed_layer_bytes"], 1, 512 * 1024 ** 2)
    if (total != sum(layer["size"] for layer in layers) or len({layer["digest"] for layer in layers}) != len(layers)
            or len(set(diff_ids)) != len(diff_ids)):
        raise ValueError("The reviewed image layers or byte total are inconsistent")
    fields = ("registry", "repository", "platform", "index_digest", "manifest_digest", "config_digest",
              "manifest_bytes", "config_bytes", "layers", "uncompressed_layer_digests", "compressed_layer_bytes")
    return copy.deepcopy({name: pin[name] for name in fields})


def public_images(public_pins_bytes: bytes, node_selection: dict | None = None) -> dict:
    if not isinstance(public_pins_bytes, bytes):
        raise TypeError("Explicit frozen public pin bytes are required")
    if len(public_pins_bytes) > MAX_PUBLIC_BYTES or hashlib.sha256(public_pins_bytes).hexdigest() != PUBLIC_PINS_SHA256:
        raise ValueError("The exact frozen public image pins are required")
    document = _strict_json(public_pins_bytes)
    if document.get("schema_version") != 1 or type(document.get("images")) is not dict:
        raise ValueError("The frozen public image pin schema is required")
    selected = {name: _image(document["images"][name], name) for name in IMAGE_REPOSITORIES}
    if node_selection is not None:
        _closed(node_selection, {"component", "owner_decision_sha256", "index_digest", "manifest_digest", "config_digest"})
        _sha(node_selection["owner_decision_sha256"])
        if node_selection["component"] != NODE_COMPONENT:
            raise ValueError("The unapproved Node baseline cannot be selected")
        node = _image(document["images"][NODE_COMPONENT], NODE_COMPONENT)
        if any(node_selection[field] != node[field] for field in ("index_digest", "manifest_digest", "config_digest")):
            raise ValueError("The owner runtime decision must bind the complete supported public Node pin")
        selected[NODE_COMPONENT] = node
    return selected


def target() -> dict:
    return {"vm_name": LAB, "vm_run_id": VM_RUN_ID, "phase_a_digest": PHASE_A_DIGEST,
            "b1_phase_id": B1_ID, "project": "default", "profiles": [], "outer_nic_count": 0}


def labels(phase_b_id: str) -> dict:
    return {"orcastra.qa.lab": LAB, "orcastra.qa.vm_run": VM_RUN_ID, "orcastra.qa.phase_b": _phase_id(phase_b_id)}


def resource_budget() -> dict:
    return {
        "vm_memory_mib": 22 * 1024, "vm_vcpu": 4, "vm_root_gib": 120,
        "docker_slice_memory_mib": 14 * 1024, "docker_slice_cpu_millicores": 3000, "docker_slice_tasks": 8192,
        "service_limits": {name: {"memory_mib": memory, "cpu_millicores": cpu, "pids": pids} for name, (memory, cpu, pids) in SERVICE_LIMITS.items()},
        "retained_pg_limits": {"postgres_memory_mib": 512, "runner_memory_mib": 1024, "total_memory_mib": 1536, "total_pids": 512},
        "single_build_worker": {"count": 1, "memory_mib": 6144, "cpu_millicores": 2000, "pids": 1024},
        "slice_memory_reserve_mib": 1024, "os_browser_budget_mib": 4096, "outer_memory_reserve_mib": 4096,
        "minimum_guest_free_gib": 60,
        "cpu_policy": "Per-service limits may overlap; the inherited slice enforces an aggregate three-core quota",
        "nested_engines": "Excluded from B2 and absent during the build",
    }


def _source(proof: dict) -> None:
    _closed(proof, {"source_tree_sha256", "archive_sha256", "archive_filename", "archive_size", "file_count", "public_only_verified"})
    for field in ("source_tree_sha256", "archive_sha256"):
        _sha(proof[field])
    _filename(proof["archive_filename"], ".tar")
    _integer(proof["archive_size"], 1, 64 * 1024 ** 2)
    _integer(proof["file_count"], 1, 8192)
    if proof["public_only_verified"] is not True:
        raise ValueError("The source proof must exclude deployment, authentication, and private state")


def _archives(proof: dict, images: dict) -> None:
    _closed(proof, set(images))
    filenames = []
    for component, image in images.items():
        item = _closed(proof[component], {"archive_sha256", "archive_filename", "archive_size", "archive_verified", "expected_image_id", "origin_manifest_digest"})
        _sha(item["archive_sha256"])
        filenames.append(_filename(item["archive_filename"], ".tar.gz"))
        _integer(item["archive_size"], 1, MAX_ARCHIVE_BYTES)
        if (item["archive_verified"] is not True or item["expected_image_id"] != image["config_digest"]
                or item["origin_manifest_digest"] != image["manifest_digest"]):
            raise ValueError("An image archive must bind the independently verified official config and manifest")
    if len(set(filenames)) != len(filenames):
        raise ValueError("Image archives require distinct filenames")


def _packages(proof: dict, source: dict | None, node: dict | None) -> None:
    fields = {"source_tree_sha256", "node_config_digest", "closure_sha256", "wheel_archive_sha256",
              "wheel_requirements_sha256", "npm_tarballs_archive_sha256", "apk_archive_sha256", "issues"}
    _closed(proof, fields)
    for field in fields - {"node_config_digest", "issues"}:
        _sha(proof[field])
    if (source is None or node is None or proof["source_tree_sha256"] != source["source_tree_sha256"]
            or proof["node_config_digest"] != node["config_digest"] or proof["issues"] != []):
        raise ValueError("The complete package closure must bind the source and selected Node runtime")


def _b1(proof: dict) -> None:
    fixed = {"phase": "B1", "status": "runtime_verified", "phase_b_id": B1_ID, "vm_run_id": VM_RUN_ID,
             "proposal_digest": B1_DIGEST, "helper_sha256": B1_HELPER_SHA256, "no_nic": True,
             "socket_protected": True, "systemd_units_verified": True, "cgroup_limits_verified": True,
             "memory_max_bytes": 14 * 1024 ** 3, "cpu_quota_ratio": 3, "tasks_max": 8192}
    _closed(proof, set(fixed) | {"result_sha256"})
    _sha(proof["result_sha256"])
    for field in ("memory_max_bytes", "cpu_quota_ratio", "tasks_max"):
        _integer(proof[field], fixed[field], fixed[field])
    if any(proof[field] != expected or type(proof[field]) is not type(expected) for field, expected in fixed.items()):
        raise ValueError("Actual B1 success must bind the revised approved runtime and effective boundaries")


def _pg(proof: dict, expected_pg_digest: str | None) -> None:
    fields = {"phase", "status", "gate_id", "proposal_digest", "result_sha256", "target", "labels", "resource_ids",
              "no_outer_nic", "container_kernel_limits_verified", "postgres_running_verified", "runner_completed_verified"}
    _closed(proof, fields)
    _sha(proof["result_sha256"])
    expected_labels = {"orcastra.qa.lab": LAB, "orcastra.qa.vm_run": VM_RUN_ID, "orcastra.qa.b1": B1_ID, "orcastra.qa.pg_gate": PG_GATE_ID}
    identifiers = _closed(proof["resource_ids"], {"network", "postgres", "runner"})
    _closed(proof["target"], set(target()))
    _integer(proof["target"]["outer_nic_count"], 0, 0)
    for value in identifiers.values():
        _sha(value)
    if (expected_pg_digest is None or proof["phase"] != "B-PG" or proof["status"] != "journal_and_migration_verified"
            or proof["gate_id"] != PG_GATE_ID or proof["proposal_digest"] != expected_pg_digest
            or proof["target"] != target() or proof["labels"] != expected_labels or len(set(identifiers.values())) != 3
            or any(proof[field] is not True for field in fields if field.endswith("verified") or field == "no_outer_nic")):
        raise ValueError("Actual PG success must bind the exact gate, target, ownership labels, and three distinct immutable IDs")


def _reviews(proofs: dict) -> None:
    executor = proofs.get("executor_review")
    if executor is not None:
        _closed(executor, {"executor_sha256", "vault_helper_sha256", "idp_helper_sha256", "review_sha256", "full_executor_reviewed"})
        for field in ("executor_sha256", "vault_helper_sha256", "idp_helper_sha256", "review_sha256"):
            _sha(executor[field])
        if executor["full_executor_reviewed"] is not True or executor["idp_helper_sha256"] != IDP_HELPER_SHA256:
            raise ValueError("The complete frozen B2 executor requires independent review")
    transport = proofs.get("outer_transport_review")
    if transport is not None:
        _closed(transport, {"helper_sha256", "review_sha256", "cluster_alias", "project", "vm_name", "vm_run_id", "vsock_only", "no_environment_discovery", "no_retries"})
        for field in ("helper_sha256", "review_sha256"):
            _sha(transport[field])
        if (transport["cluster_alias"] != "nuc2" or transport["project"] != "default" or transport["vm_name"] != LAB
                or transport["vm_run_id"] != VM_RUN_ID or any(transport[field] is not True for field in ("vsock_only", "no_environment_discovery", "no_retries"))):
            raise ValueError("The exact owned agent transport requires separate frozen review")


def _proofs(proofs: dict | None, images: dict, expected_pg_digest: str | None) -> dict:
    if proofs is None:
        proofs = {}
    if type(proofs) is not dict or set(proofs) - PROOF_KEYS:
        raise ValueError("Only the closed public B2 pre-execution evidence families are accepted")
    result = copy.deepcopy(proofs)
    source, packages = [result.get(name) for name in ("source", "packages")]
    if source is not None:
        _source(source)
    if result.get("images") is not None:
        _archives(result["images"], images)
    if packages is not None:
        _packages(packages, source, images.get(NODE_COMPONENT))
    if result.get("b1_success") is not None:
        _b1(result["b1_success"])
    if result.get("pg_success") is not None:
        _pg(result["pg_success"], expected_pg_digest)
    _reviews(result)
    return {name: result.get(name) for name in sorted(PROOF_KEYS)}


def _runtime_acceptance(phase: str, proofs: dict, images: dict) -> dict:
    assertions = {
        "npm_cache_materialization": [
            "Offline, deterministically materialize a fresh private npm cache from only the verified npm tarballs archive and approved package closure beneath protected B2 phase paths in bounded tooling",
            "Verify cache coverage against the lockfile resolved packages and integrity with the selected Node/npm runtime; record sanitized phase, source, closure, and archive-bound evidence without public downloads or package script execution",
        ],
        "installed_dependencies": [
            "Install backend and frontend dependencies in fresh bounded tooling from only the approved package closure",
            "Use only the verified npm cache materialized for this phase for frontend dependency installation",
            "Verify installed dependencies and record sanitized evidence bound to this B2 phase, source, closure, and both runtime image IDs",
        ],
        "build": [
            "Build fresh backend and frontend images one at a time with network none, pulls disabled, and the fixed inherited slice and worker limits",
            "Verify both image IDs and archive hashes against the source, closure, and fresh installed dependency evidence before starting application containers",
        ],
        "application_databases": [
            "Apply migrations only to the new private application database and verify the expected schema and fresh lab data",
            "Preserve the independently attested PG gate IDs and private state without restart, adoption, or cleanup",
        ],
        "authentik_runtime": [
            "Independently verify the pinned server and worker HTTP listen at 0.0.0.0:32090 with HTTPS disabled before IdP bootstrap",
            "Verify the server container port 32090/tcp, loopback publication, and internal auth.orcastra-qa.invalid:32090 issuer reachability",
        ],
        "vault": [
            "Verify fresh private Vault initialization, ownership, least-privilege policy and role, backend token confinement, and the exact vault_bootstrap contract",
        ],
        "idp_configuration": [
            "Verify the exact idp_bootstrap configuration and private material ownership without granting extra roles or permissions",
        ],
        "sso_jwt": [
            "Verify fresh-lab SSO login, logout, and backend JWT integration with the configured issuer, audience, signing key, expiry, and role claims",
            "Reject invalid or elevated JWTs and export no tokens, credentials, private keys, or customer identities",
        ],
        "readiness": [
            "Verify all eight service health and application readiness checks after migrations and both bootstrap contracts succeed",
            "Export finite sanitized acceptance readbacks without treating a started container or responsive API as full runtime success",
        ],
        "isolation_and_limits": [
            "Verify the exact guest target remains root-only with no NIC, new immutable resource IDs and ownership labels, private paths, internal network, loopback ports, and socket exclusions",
            "Verify effective memory, CPU, and PID limits for all eight services, the retained PG gate, and the single build worker within the fixed budgets",
        ],
    }
    return {
        "timing": "Postconditions of separately approved B2 execution; never prerequisites or authority for its initial execution approval",
        "status": "pending_execution", "required_for_runtime_success": True,
        "expected_bindings": {"phase_b_id": phase,
                              "source_tree_sha256": proofs["source"]["source_tree_sha256"] if proofs["source"] is not None else None,
                              "package_closure_sha256": proofs["packages"]["closure_sha256"] if proofs["packages"] is not None else None,
                              "npm_tarballs_archive_sha256": proofs["packages"]["npm_tarballs_archive_sha256"] if proofs["packages"] is not None else None,
                              "python_config_digest": images["backend_base"]["config_digest"],
                              "node_config_digest": images[NODE_COMPONENT]["config_digest"] if NODE_COMPONENT in images else None},
        "gates": {name: {"status": "pending_execution", "assertions": items} for name, items in assertions.items()},
    }


def _resources(phase: str, proofs: dict) -> dict:
    ownership = labels(phase)
    short = phase[:8]
    names = {name: f"{LAB}-{name}-{short}" for name in SERVICE_LIMITS}
    volumes = {name: {"name": f"{LAB}-{name}-{short}", "role": name, "labels": dict(ownership),
                      "private_path": f"{ROOT}/state/{phase}/{name}", "parent_owner": "root:root", "parent_mode": "0700"} for name in VOLUME_ROLES}
    image_roles = {"postgres": "postgres", "authentik-postgres": "postgres", "redis": "redis", "vault": "vault",
                   "authentik-server": "authentik", "authentik-worker": "authentik", "backend": "backend", "frontend": "frontend"}
    ports = {"frontend": "127.0.0.1:32025:2025", "backend": "127.0.0.1:32405:4050",
             "authentik-server": "127.0.0.1:32090:32090", "vault": "127.0.0.1:32820:8200"}
    containers = {name: {"name": names[name], "role": name, "labels": dict(ownership), "created_id": None,
                         "image_component": image_roles[name], "limits": resource_budget()["service_limits"][name],
                         "published_ports": [ports[name]] if name in ports else [], "restart": "no", "pull_policy": "never",
                         "privileged": False, "host_network": False, "host_pid": False, "no_new_privileges": True,
                         "required_public_environment": {"AUTHENTIK_LISTEN__HTTP": "0.0.0.0:32090", "AUTHENTIK_LISTEN__HTTPS": ""}
                         if name in {"authentik-server", "authentik-worker"} else {},
                         "socket_mounts": []} for name in SERVICE_LIMITS}
    network = {"name": f"{LAB}-application-{short}", "created_id": None, "bridge_name": "oqab2-" + short,
               "subnet": "172.28.66.0/24", "internal": True, "labels": dict(ownership), "published_ports": [],
               "external_connections": [], "members": list(SERVICE_LIMITS)}
    retained = None
    if proofs["pg_success"] is not None:
        retained = {"gate_id": PG_GATE_ID, "proposal_digest": proofs["pg_success"]["proposal_digest"],
                    "resource_ids": copy.deepcopy(proofs["pg_success"]["resource_ids"]),
                    "labels": copy.deepcopy(proofs["pg_success"]["labels"]), "effect": "Preserve exact PG IDs and private paths without restart, reuse, adoption, or cleanup",
                    "network_name": f"{LAB}-pg-{PG_GATE_ID[:8]}", "bridge_name": "oqapg-" + PG_GATE_ID[:8],
                    "containers": {"postgres": f"{LAB}-pg-db-{PG_GATE_ID[:8]}", "runner": f"{LAB}-pg-runner-{PG_GATE_ID[:8]}"}}
    return {"containers": containers, "volumes": volumes, "network": network, "retained_pg": retained,
            "preflight": "Every new name and phase path must be absent; only independently read back PG IDs may already exist",
            "created_id_policy": "Record immutable IDs after creation; never substitute a matching name or adopt an existing object"}


def render_b2(*, phase_b_id: str, public_pins_bytes: bytes, expected_pg_digest: str | None = PG_PROPOSAL_DIGEST, proofs: dict | None = None) -> dict:
    phase = _phase_id(phase_b_id)
    if expected_pg_digest is not None:
        _sha(expected_pg_digest)
        if expected_pg_digest != PG_PROPOSAL_DIGEST:
            raise ValueError("Only the exact current PostgreSQL gate proposal may supply planning provenance")
    if proofs is not None and type(proofs) is not dict:
        raise TypeError("Explicit public proof objects are required")
    images = public_images(public_pins_bytes, (proofs or {}).get("node_selection"))
    evidence = _proofs(proofs, images, expected_pg_digest)
    gates = []
    missing = {
        "node_selection": "The owner Node runtime selection and exact public pin decision remain pending",
        "source": "A verified current curated source tree and source archive proof are required",
        "images": "Independent verification of every selected public Docker archive is required",
        "packages": "The complete wheel, npm, and APK closure for the selected runtime must be frozen",
        "b1_success": "Actual successful revised B1 execution and effective runtime boundary readback are required",
        "pg_success": "Actual successful PostgreSQL journal and migration gate outcomes with retained immutable IDs are required",
        "executor_review": "The complete B2 guest executor and both bootstrap helpers must be frozen and independently reviewed",
        "outer_transport_review": "The exact outer agent transport must be frozen and independently reviewed",
    }
    for name, reason in missing.items():
        if evidence[name] is None:
            gates.append(reason)
    if expected_pg_digest is None:
        gates.append("The current PostgreSQL revision digest must be supplied as planning provenance")
    gates.append("A new exact owner execution approval must bind the completed frozen B2 proposal; this renderer cannot grant execution readiness")
    state = f"{ROOT}/state/{phase}"
    plan = {
        "schema_version": 1, "phase": "B2", "phase_b_id": phase,
        "scope": "Prepare a fresh eight-service Orcastra application in the owned disconnected QA guest after revised B1 and the PostgreSQL gate; initialize only new private application databases, Vault, and IdP state",
        "ready_for_guest_execution": False, "remaining_gates": gates,
        "target": target(), "b1_readback": {"proposal_digest": B1_DIGEST, "helper_sha256": B1_HELPER_SHA256},
        "outer_binding": {"cluster_alias": "nuc2", "project": "default", "vm_name": LAB, "vm_run_id": VM_RUN_ID,
                          "phase_a_digest": PHASE_A_DIGEST, "profiles": [], "outer_nic_count": 0,
                          "expanded_devices": {"root": {"type": "disk", "path": "/", "pool": "default", "size": "120GiB"}},
                          "transport": "Approved private daemon agent over vsock only"},
        "pg_planning_provenance": {"gate_id": PG_GATE_ID, "expected_proposal_digest": expected_pg_digest,
                                   "helper_sha256": PG_HELPER_SHA256,
                                   "authority": "Planning provenance only; a draft digest is not execution approval or success"},
        "public_pins_sha256": PUBLIC_PINS_SHA256, "images": images, "proofs": evidence,
        "runtime_acceptance": _runtime_acceptance(phase, evidence, images),
        "bootstrap_helper_pins": {"idp_helper_sha256": IDP_HELPER_SHA256},
        "resource_budget": resource_budget(), "resources": _resources(phase, evidence),
        "private_paths": {"phase_root": state, "phase_owner": "root:root", "directory_mode": "0700", "file_mode": "0600",
                          "owner_marker": "owner.json", "incoming": f"{ROOT}/incoming/{phase}",
                          "configuration": f"{ROOT}/config/{phase}", "source": f"{ROOT}/source/{phase}",
                          "databases": [state + "/app-postgres", state + "/auth-postgres"],
                          "idp": state + "/idp-bootstrap", "vault": state + "/vault-bootstrap", "uploads": state + "/uploads",
                          "artifacts": f"{ROOT}/artifacts/b2-{phase}",
                          "secret_generation": "Fresh guest-only material beneath protected phase paths; no secret values in proposals, bundles, arguments, logs, or exported evidence"},
        "vault_bootstrap": vault_contract(VM_RUN_ID, phase), "idp_bootstrap": expected_idp_spec(),
        "execution_policy": {"fresh_only": True, "automatic_restart": False, "automatic_retry": False, "automatic_cleanup": False,
                             "overwrite": False, "adopt_existing": False, "install_b1_again": False,
                             "public_pull": False, "build_network": "none", "build_pull": False,
                             "migrations": "Only the newly created private application PostgreSQL database",
                             "socket_exclusions": list(SOCKET_EXCLUSIONS)},
        "stages": ["Recheck the exact outer root-only VM, revised B1 runtime, full slice boundaries, and retained PG IDs",
                   "Verify current source, all selected image archives, package closure, and exact new incoming files before any guest writes",
                   "Load only verified pinned archives without pulls, materialize the fresh private npm cache from verified tarballs, and prove installed dependencies in fresh bounded tooling",
                   "Build one source image at a time offline under the inherited slice; retain the PG gate without restart or cleanup",
                   "Create only the named new internal application network, protected phase paths, ownership marker, and labeled volumes",
                   "Generate only fresh guest-private material and start the eight exact bounded labeled containers without restart",
                   "Apply migrations only to fresh application data and invoke the exact reviewed Vault and IdP bootstrap contracts",
                   "Verify the planned fresh-lab SSO, JWT, readiness, isolation, and runtime acceptance gates without exporting private material",
                   "Export finite sanitized image, health, configuration, resource-limit, and target-isolation readback; retain private and partial artifacts"],
        "excluded": ["Outer host resources or NIC, bridge, ACL, or trust changes", "B1 reinstall, daemon replacement, or service restart",
                     "Existing PG gate adoption, mutation, stop, prune, or deletion", "Shared databases, Vault, IdP, credentials, or customer identities",
                     "Host or guest Docker/containerd socket mounts into application or IdP containers", "Nested LXD/Incus engines or registered fleet targets",
                     "Browser role QA beyond the fresh-lab SSO/JWT acceptance checks", "Publication, pushes, retries, overwrite, and automatic cleanup"],
        "failure": "Stop at the recorded stage and retain exact accepted IDs and partial artifacts; cleanup needs a separate exact proposal and approval",
    }
    plan["proposal_digest"] = canonical_digest(plan)
    return plan


def validate_manifest(proposal: dict, public_pins_bytes: bytes) -> dict:
    if type(proposal) is not dict:
        raise TypeError("A complete public B2 draft proposal is required")
    core = copy.deepcopy(proposal)
    recorded = core.pop("proposal_digest", None)
    if _sha(recorded) != canonical_digest(core):
        raise ValueError("The full canonical B2 proposal digest differs")
    expected = render_b2(phase_b_id=core["phase_b_id"], public_pins_bytes=public_pins_bytes,
                         expected_pg_digest=core["pg_planning_provenance"]["expected_proposal_digest"], proofs=core["proofs"])
    if proposal != expected:
        raise ValueError("The target, ownership, resource budget, helper contract, or public draft boundary differs")
    return copy.deepcopy(proposal)


def phase_marker(proposal: dict, public_pins_bytes: bytes) -> dict:
    """Return the separately written marker, avoiding a self-referential proposal digest."""
    validated = validate_manifest(proposal, public_pins_bytes)
    return ownership_marker(VM_RUN_ID, validated["phase_b_id"], validated["proposal_digest"])
