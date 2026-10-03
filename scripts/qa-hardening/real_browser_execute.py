"""Execute one frozen browser run in the owned, disconnected B2 guest.

Default invocation is inert. The root-generated manifest must bind the actual
controller summary and canonical accepted outer-operation evidence. No proof
of SSO or JWT acceptance is inferred from controller readiness.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import stat
import sys
import tarfile
import time
import types
import uuid
from pathlib import Path

LAB = "orcastra-qa-hardening"
ROOT = Path("/srv") / LAB
VM_RUN = "0f2a5b15-f5ec-464b-8785-0b4b88c3b358"
B1_ID = "86bee809-561c-4a19-8a69-ea738ae640bb"
B1_DIGEST = "b907a6b4a4e9fe6e58cf28c7a9d1c3266e8b8b5e90eacf56323275e9f63cacbb"
CONTROL = "278afe9a2f430b61b9503108882b20a72b0680f7f3024a35a934460e1f7e8a47"
HARNESS = "42db599b60dc547df86cf8da4452a3e17c284b0d9724af38583ad09c5c4ae08c"
PLANNER = "2cccb15b59f93db1be25f27bc2ee73e68dabcb8da0080853e9e2824fd2d7c594"
B1_HELPER = "50684d551b719324ced78749631661987e3d7db7f32ca5282b790d691f1969ca"
PG_HELPER = "dd10d226cc3e38b1b6033919a80a57f009a9cb97541047092ac8cdcd0f53492a"
IMAGE = "mcr.microsoft.com/playwright:v1.51.1-noble"
IMAGE_MANIFEST = "sha256:daa1690ea366d2d6b52ea085a59a221a6e954cd9d9c13c89bd7eccb0673e8961"
IMAGE_CONFIG = "sha256:07625bb0f130786b63a5f8d0a01ae8366633f044fdd1d322f4e17d891e40eb38"
DIFF_IDS = (
    "sha256:4b7c01ed0534d4f9be9cf97d068da1598c6c20b26cb6134fad066defdb6d541d",
    "sha256:7517b392b563d21a4b9a7c481e86bdd47122ff8a1d32c716e11876bda3e9a32c",
    "sha256:91dd71e7ee8632b3678c7177c67a5ca7f788cd023aad294471a171b2e0bb78a1",
    "sha256:9b669bace04dc663b357af39358fb77360a900fb9bf08cf6489b1f8ed6eca650",
)
NPM = {
    "@playwright/test": "sha512-nM+kEaTSAoVlXmMPH10017vn3FSiFqr/bh4fKg9vmAdMfd9SDqRZNvPSiAHADc/itWak+qPvMPZQOPwCBW7k7Q==",
    "playwright": "sha512-kkx+MB2KQRkyxjYPc3a0wLZZoDczmppyGJIvQ43l+aZihkaVvmu/21kiyaHeHjiFxjxNNFnUncKmcGIyOojsaw==",
    "playwright-core": "sha512-/crRMj8+j/Nq5s8QcvegseuyeZPxpQCZb6HNk3Sos3BlZyAknRjoyJPFWkpNn8v0+P3WiwqFF8P+zQo4eqiNuw==",
}
SERVICES = ("postgres", "authentik-postgres", "redis", "vault", "authentik-server", "authentik-worker", "backend", "frontend")
UPSTREAMS = ("frontend", "backend", "authentik-server", "network")
HELPERS = {"real_browser_guest.py", "phase_b_b1_guest.py", "phase_b_pg_guest.py", "real_browser_runtime.py", "real_browser_launch.cjs", "real_browser_execute.py"}
MAX_METADATA = 128 * 1024
MAX_NPM = 32 * 1024**2
DEADLINE = 1800


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def exact(value: object, keys: set[str]) -> dict:
    if type(value) is not dict or set(value) != keys:
        raise ValueError("Closed proof schema differs")
    return value


def checked_sha(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("Exact SHA256 required")
    return value


def checked_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Canonical UUIDv4 required")  # noqa: TRY004 - Boundary values share the validation failure contract.
    parsed = uuid.UUID(value)
    if str(parsed) != value or parsed.version != 4:
        raise ValueError("Canonical UUIDv4 required")
    return value


def checked_image(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError("Immutable image ID required")
    checked_sha(value[7:])
    return value


def strict_json(body: bytes) -> dict:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("Duplicate JSON key")
            value[key] = item
        return value

    def finite(_value):
        raise ValueError("Non-finite JSON value")

    return json.loads(body, object_pairs_hook=pairs, parse_constant=finite)


def filename(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value) or ".." in value:
        raise ValueError("Plain fixed input filename required")
    return value


def labels(phase: str) -> dict:
    return {"orcastra.qa.lab": LAB, "orcastra.qa.vm_run": VM_RUN, "orcastra.qa.phase_b": checked_uuid(phase)}


def adapter_from_result(result: dict, outer: dict) -> dict:
    """Read-only adapter: actual B2 result plus root's accepted transport evidence.

    The outer UUID remains explicit. Its SHA256 reference hashes the complete
    canonical operation evidence, rather than pretending it is a Docker ID.
    Unknown controller/envelope fields are refused and require source review.
    """
    exact(result, {"phase", "phase_b_id", "status", "proposal_digest", "resource_ids", "image_ids", "source_tree_sha256",
                   "vault_configuration_verified", "idp_configuration_verified", "readiness_verified", "kernel_limits_verified",
                   "sso_login_verified", "jwt_runtime_verified", "production_dockerfile_parity", "remaining_acceptance"})
    exact(outer, {"schema_version", "closed", "operation_id", "exit_code", "target", "b2_id", "proposal_digest",
                  "result_sha256", "source_tree_sha256", "controller_helper_sha256", "authorization_reference_sha256",
                  "resource_ids", "network", "image_ids"})
    phase = checked_uuid(result["phase_b_id"])
    if (type(outer["schema_version"]) is not int or outer["schema_version"] != 1 or outer["closed"] is not True
            or type(outer["exit_code"]) is not int or outer["exit_code"] != 0
            or outer["target"] != {"vm": LAB, "project": "default", "vm_run_id": VM_RUN}
            or result["phase"] != "B2" or result["status"] != "controller_configured_readiness_verified"
            or phase != outer["b2_id"] or result["proposal_digest"] != outer["proposal_digest"]
            or result["source_tree_sha256"] != outer["source_tree_sha256"]):
        raise ValueError("Actual successful owned B2 evidence required")
    checked_uuid(outer["operation_id"])
    for field in ("proposal_digest", "result_sha256", "source_tree_sha256", "controller_helper_sha256", "authorization_reference_sha256"):
        checked_sha(outer[field])
    for field in ("vault_configuration_verified", "idp_configuration_verified", "readiness_verified", "kernel_limits_verified"):
        if result[field] is not True:
            raise ValueError("Actual controller postcondition absent")
    for field in ("sso_login_verified", "jwt_runtime_verified", "production_dockerfile_parity"):
        if result[field] is not False:
            raise ValueError("A fresh pre-browser B2 result required")
    if result["remaining_acceptance"] != ["Per-role browser SSO login/logout and JWT runtime acceptance", "Real hypervisor resource behavior"]:
        raise ValueError("Controller acceptance contract differs")
    resources, images = result["resource_ids"], result["image_ids"]
    if type(resources) is not dict or not 9 <= len(resources) <= 128 or type(images) is not dict or len(images) > 32:
        raise ValueError("Bounded actual resource inventory required")
    selected = exact(outer["resource_ids"], set(UPSTREAMS))
    for role in (*SERVICES, "network"):
        checked_sha(resources.get(role))
    if len({resources[role] for role in (*SERVICES, "network")}) != 9 or selected != {role: resources[role] for role in UPSTREAMS}:
        raise ValueError("Exact distinct B2 upstream IDs required")
    expected_images = {role: checked_image(images.get(component)) for role, component in
                       (("frontend", "frontend"), ("backend", "backend"), ("authentik-server", "authentik"))}
    if outer["image_ids"] != expected_images:
        raise ValueError("Actual upstream image pins differ")
    network = exact(outer["network"], {"id", "name", "internal", "published_ports", "labels"})
    if canonical(network) != canonical({"id": resources["network"], "name": LAB + "-application-" + phase[:8],
                                        "internal": True, "published_ports": [], "labels": labels(phase)}):
        raise ValueError("Exact owned internal network required")
    return {"b2_id": phase, "labels": labels(phase), "resource_ids": selected,
            "service_ids": {role: resources[role] for role in SERVICES}, "image_ids": expected_images, "network": network,
            "operation_id": outer["operation_id"], "accepted_operation_reference_sha256": sha(canonical(outer)),
            "source_tree_sha256": result["source_tree_sha256"], "proposal_digest": result["proposal_digest"]}


def payload_contract(proof: dict) -> dict:
    exact(proof, {"schema_version", "closed", "control_sha256", "image", "npm"})
    image = exact(proof["image"], {"reference", "manifest_digest", "config_digest", "archive_sha256", "archive_verified", "config_verified", "diff_ids"})
    if (type(proof["schema_version"]) is not int or proof["schema_version"] != 1 or proof["closed"] is not True
            or proof["control_sha256"] != CONTROL or image["reference"] != IMAGE or image["manifest_digest"] != IMAGE_MANIFEST
            or image["config_digest"] != IMAGE_CONFIG or image["archive_verified"] is not True or image["config_verified"] is not True
            or type(image["diff_ids"]) is not list or tuple(image["diff_ids"]) != DIFF_IDS):
        raise ValueError("Pinned official browser image proof differs")
    checked_sha(image["archive_sha256"])
    if type(proof["npm"]) is not list or len(proof["npm"]) != 3:
        raise ValueError("Exact three-package offline closure required")
    observed = {}
    for row in proof["npm"]:
        exact(row, {"name", "version", "sha256", "integrity", "verified"})
        if row["name"] not in NPM or row["name"] in observed or row["version"] != "1.51.1" or row["verified"] is not True or row["integrity"] != NPM[row["name"]]:
            raise ValueError("Pinned offline npm proof differs")
        checked_sha(row["sha256"])
        observed[row["name"]] = row
    return {"image": image, "npm": observed}


def runtime_plan(adapter: dict, payload: dict, run: str) -> dict:
    return {"schema_version": 1, "run_id": checked_uuid(run), "b2_id": adapter["b2_id"], "upstream_ids": adapter["resource_ids"],
            "source_tree_sha256": adapter["source_tree_sha256"], "accepted_operation_reference_sha256": adapter["accepted_operation_reference_sha256"],
            "network_id": adapter["network"]["id"], "image_config": payload["image"]["config_digest"],
            "memory_bytes": 2 * 1024**3, "nano_cpus": 1_500_000_000, "pids": 1024, "shm_bytes": 1024**3,
            "user": "1000:1000", "cap_drop": ["ALL"], "read_only_rootfs": True, "no_new_privileges": True,
            "published_ports": [], "host_network": False, "outer_nic": False, "cgroup_slice_bytes": 14 * 1024**3,
            "proxies": [[32025, "frontend", 2025], [32405, "backend", 4050], [32090, "authentik-server", 32090]],
            "auth_alias": "auth.orcastra-qa.invalid:127.0.0.1", "deadline_seconds": DEADLINE,
            "npm": {name: row["sha256"] for name, row in payload["npm"].items()},
            "automatic_retry": False, "automatic_cleanup": False}


def fingerprint(status) -> tuple:
    return tuple(getattr(status, field) for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_uid", "st_gid", "st_nlink"))


def owned_open(path: Path, maximum: int, *, uid: int = 0, mode: int | None = None):
    if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(ROOT):
        raise ValueError("Absolute owned path required")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        current = Path("/")
        for part in path.parts[1:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
            current /= part
            status = os.fstat(directory)
            if (status.st_uid not in {0, uid} or status.st_gid not in {0, uid} or status.st_mode & 0o022
                    or current == ROOT and (status.st_uid != 0 or status.st_gid != 0 or stat.S_IMODE(status.st_mode) != 0o700)):
                raise ValueError("Private owned input ancestor required")
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        status = os.fstat(descriptor)
        if (not stat.S_ISREG(status.st_mode) or status.st_nlink != 1 or status.st_uid != uid or status.st_gid != uid
                or status.st_size > maximum or status.st_mode & 0o022 or mode is not None and stat.S_IMODE(status.st_mode) != mode):
            raise ValueError("Bounded owned regular single-link input required")
        return os.fdopen(descriptor, "rb"), fingerprint(status)
    except BaseException:
        os.close(descriptor)
        raise


def owned_bytes(path: Path, maximum: int, *, uid: int = 0, mode: int | None = None) -> bytes:
    source, original = owned_open(path, maximum, uid=uid, mode=mode)
    with source:
        body = source.read(maximum + 1)
        if len(body) > maximum or fingerprint(os.fstat(source.fileno())) != original:
            raise ValueError("Input changed while read")
        return body


def image_archive(source, check_time) -> dict:
    source.seek(0)
    seen, config, manifest, total = {}, None, None, 0
    config_name = IMAGE_CONFIG[7:] + ".json"
    wanted = {config_name, "manifest.json", *(value[7:] + "/layer.tar" for value in DIFF_IDS)}
    with tarfile.open(fileobj=source, mode="r|*") as archive:
        for member in archive:
            check_time()
            if (member.name not in wanted or member.name in seen or not member.isfile() or member.linkname or member.pax_headers
                    or member.size < 0 or member.size > 4 * 1024**3):
                raise ValueError("Closed browser archive member differs")
            total += member.size
            if total > 4 * 1024**3:
                raise ValueError("Browser archive expansion exceeds limit")
            hasher, small, count = hashlib.sha256(), bytearray(), 0
            with archive.extractfile(member) as item:
                for chunk in iter(lambda: item.read(1024**2), b""):
                    check_time()
                    hasher.update(chunk)
                    count += len(chunk)
                    if member.name in {config_name, "manifest.json"}:
                        small.extend(chunk)
                        if len(small) > MAX_METADATA:
                            raise ValueError("Bounded browser image metadata required")
            if count != member.size:
                raise ValueError("Truncated browser archive member")
            seen[member.name] = hasher.hexdigest()
            if member.name == config_name:
                config = strict_json(bytes(small))
            elif member.name == "manifest.json":
                manifest = strict_json(bytes(small))
    expected = [{"Config": config_name, "RepoTags": [IMAGE], "Layers": [value[7:] + "/layer.tar" for value in DIFF_IDS]}]
    if (set(seen) != wanted or seen.get(config_name) != IMAGE_CONFIG[7:] or type(config) is not dict
            or config.get("os") != "linux" or config.get("architecture") != "amd64"
            or config.get("rootfs", {}).get("diff_ids") != list(DIFF_IDS) or manifest != expected
            or any(seen[value[7:] + "/layer.tar"] != value[7:] for value in DIFF_IDS)):
        raise ValueError("Pinned image config, manifest or diff IDs differ")
    source.seek(0)
    return config


def extract_npm(payloads: dict[str, bytes], destination: Path, check_time) -> None:
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Never adopt an npm workdir")
    destination.mkdir(mode=0o755)
    destination.chmod(0o755)
    for name, body in payloads.items():
        target = destination / "node_modules" / name
        seen, total = set(), 0
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
            for member in archive:
                check_time()
                parts = Path(member.name).parts
                if (not member.isfile() or member.pax_headers or member.linkname or len(parts) < 2 or parts[0] != "package" or "/".join(parts) != member.name
                        or ".." in parts or member.name in seen or member.size < 0 or member.size > MAX_NPM):
                    raise ValueError("Unsafe offline npm member")
                seen.add(member.name)
                total += member.size
                if total > 128 * 1024**2 or len(seen) > 10000:
                    raise ValueError("Offline npm expansion exceeds budget")
                path = target.joinpath(*parts[1:])
                parent = destination
                for segment in path.parent.relative_to(destination).parts:
                    parent = parent / segment
                    parent.mkdir(exist_ok=True, mode=0o755)
                    parent.chmod(0o755)
                with archive.extractfile(member) as item:
                    data = item.read(MAX_NPM + 1)
                if len(data) != member.size:
                    raise ValueError("Truncated offline npm member")
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
                with os.fdopen(descriptor, "wb") as output:
                    os.fchmod(output.fileno(), 0o444)
                    output.write(data)
        if not seen:
            raise ValueError("Empty offline npm package")


def load_frozen(name: str, body: bytes, path: Path):
    module = types.ModuleType("_orcastra_browser_" + name)
    module.__file__ = str(path)
    exec(compile(body, str(path), "exec"), module.__dict__)  # noqa: S102
    return module


def execute(manifest_path: Path, expected_sha: str, authorization: str) -> dict:
    deadline = time.monotonic() + DEADLINE

    def remaining(seconds=30):
        value = min(seconds, deadline - time.monotonic())
        if value <= 0:
            raise TimeoutError("Browser overall deadline elapsed")
        return value

    raw = owned_bytes(manifest_path, MAX_METADATA)
    if sha(raw) != checked_sha(expected_sha):
        raise ValueError("Frozen root manifest changed")
    manifest = exact(strict_json(raw), {"schema_version", "run_id", "proposal_digest", "plan", "outer", "b2_result", "payload_proof", "files", "helpers"})
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("Execution schema differs")
    run = checked_uuid(manifest["run_id"])
    incoming = ROOT / "incoming" / run
    if manifest_path != incoming / "browser-manifest.json":
        raise ValueError("Exact new incoming manifest location required")
    result_metadata = exact(manifest["b2_result"], {"sha256", "size"})
    result_raw = owned_bytes(ROOT / "state" / checked_uuid(manifest["outer"]["b2_id"]) / "evidence/controller-summary.json", MAX_METADATA, mode=0o600)
    if sha(result_raw) != result_metadata["sha256"] or len(result_raw) != result_metadata["size"] or sha(result_raw) != manifest["outer"]["result_sha256"]:
        raise ValueError("Actual B2 result bytes changed")
    adapter = adapter_from_result(strict_json(result_raw), manifest["outer"])
    if not authorization or sha(authorization.encode()) != manifest["outer"]["authorization_reference_sha256"]:
        raise ValueError("Runtime authority reference differs")
    payload = payload_contract(manifest["payload_proof"])
    plan = runtime_plan(adapter, payload, run)
    if canonical(manifest["plan"]) != canonical(plan) or sha(canonical(plan)) != checked_sha(manifest["proposal_digest"]):
        raise ValueError("Canonical browser plan widened")
    helpers = exact(manifest["helpers"], HELPERS)
    reviewed = {}
    for name, expected in helpers.items():
        body = owned_bytes(incoming / name, 256 * 1024)
        if sha(body) != checked_sha(expected):
            raise ValueError("Frozen runtime helper bytes changed")
        reviewed[name] = body
    current_source = owned_bytes(Path(__file__), 256 * 1024)
    controller_source = owned_bytes(ROOT / "incoming" / adapter["b2_id"] / "phase_b_controller_guest.py", 256 * 1024)
    installed_pg = owned_bytes(ROOT / "incoming" / adapter["b2_id"] / "phase_b_pg_guest.py", 256 * 1024)
    if (helpers["real_browser_guest.py"] != PLANNER or helpers["phase_b_b1_guest.py"] != B1_HELPER
            or helpers["phase_b_pg_guest.py"] != PG_HELPER
            or Path(__file__) != incoming / "real_browser_execute.py"
            or sha(current_source) != helpers["real_browser_execute.py"]
            or sha(controller_source) != manifest["outer"]["controller_helper_sha256"]
            or sha(installed_pg) != helpers["phase_b_pg_guest.py"]):
        raise ValueError("Reviewed planner, B1, or executor source differs")
    files = exact(manifest["files"], {"control", "image", "harness", "npm"})
    buffers = {}
    for role in ("control", "harness"):
        metadata = exact(files[role], {"filename", "sha256", "size"})
        body = owned_bytes(incoming / filename(metadata["filename"]), MAX_METADATA)
        if sha(body) != metadata["sha256"] or len(body) != metadata["size"] or sha(body) != {"control": CONTROL, "harness": HARNESS}[role]:
            raise ValueError("Frozen browser control or harness changed")
        buffers[role] = body
    npm_files = exact(files["npm"], set(NPM))
    npm_payloads = {}
    for name, metadata in npm_files.items():
        exact(metadata, {"filename", "sha256", "size"})
        body = owned_bytes(incoming / filename(metadata["filename"]), MAX_NPM)
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(body).digest()).decode()
        if sha(body) != metadata["sha256"] or sha(body) != payload["npm"][name]["sha256"] or len(body) != metadata["size"] or integrity != NPM[name]:
            raise ValueError("Exact offline npm SHA256 and SHA512 required")
        npm_payloads[name] = body
    metadata = exact(files["image"], {"filename", "sha256", "size"})
    source, original = owned_open(incoming / filename(metadata["filename"]), 4 * 1024**3)
    try:
        hasher, count = hashlib.sha256(), 0
        for chunk in iter(lambda: source.read(1024**2), b""):
            remaining()
            hasher.update(chunk)
            count += len(chunk)
        if (count != metadata["size"] or hasher.hexdigest() != metadata["sha256"]
                or hasher.hexdigest() != payload["image"]["archive_sha256"] or fingerprint(os.fstat(source.fileno())) != original):
            raise ValueError("Frozen browser image archive changed")
        image_config = image_archive(source, remaining)
        # important: every helper/payload hash has passed before any reviewed
        # module is imported, archive extracted, or Docker mutation attempted.
        planner = load_frozen("planner", reviewed["real_browser_guest.py"], incoming / "real_browser_guest.py")
        b1 = load_frozen("b1", reviewed["phase_b_b1_guest.py"], incoming / "phase_b_b1_guest.py")
        pg = load_frozen("pg", reviewed["phase_b_pg_guest.py"], incoming / "phase_b_pg_guest.py")
        runtime = load_frozen("runtime", reviewed["real_browser_runtime.py"], incoming / "real_browser_runtime.py")
        return runtime.BrowserRun(manifest, adapter, reviewed, buffers, npm_payloads, source, original,
                                  image_config, remaining, planner, b1, pg, sys.modules[__name__]).execute()
    finally:
        source.close()


def main(argv=None) -> int:
    class ClosedParser(argparse.ArgumentParser):
        def error(self, _message):
            raise ValueError("Exact bounded executor arguments required")

    parser = ClosedParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--authorization-reference")
    args = parser.parse_args(argv)
    if not args.execute:
        print('{"status":"inert","live":false,"runtime_pending":true}')
        return 0
    if not all((args.manifest, args.manifest_sha256, args.authorization_reference)):
        raise ValueError("Closed runtime authority required")
    print(json.dumps(execute(args.manifest, args.manifest_sha256, args.authorization_reference), sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001 - The CLI must not expose untrusted exception details.
        print('{"status":"stopped","runtime_pending":true,"automatic_retry":false,"automatic_cleanup":false}', file=sys.stderr)
        raise SystemExit(1) from None
