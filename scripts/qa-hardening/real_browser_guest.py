"""Prepare, but never run, the isolated B2 real-browser guest contract.

The default command is intentionally inert: it does not open proofs, secrets,
Docker, a network socket, or a guest agent.  ``--prepare`` validates only
closed, public proof descriptors and writes one new private plan.  A later
transport owner must separately execute that plan after a fresh authorization.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tarfile
import uuid
from pathlib import Path

LAB = "orcastra-qa-hardening"
VM_RUN_ID = "0f2a5b15-f5ec-464b-8785-0b4b88c3b358"
IMAGE = "mcr.microsoft.com/playwright:v1.51.1-noble"
MANIFEST = "sha256:daa1690ea366d2d6b52ea085a59a221a6e954cd9d9c13c89bd7eccb0673e8961"
CONFIG = "sha256:07625bb0f130786b63a5f8d0a01ae8366633f044fdd1d322f4e17d891e40eb38"
PUBLIC_CONTROL_SHA256 = "278afe9a2f430b61b9503108882b20a72b0680f7f3024a35a934460e1f7e8a47"
DIFF_IDS = (
    "sha256:4b7c01ed0534d4f9be9cf97d068da1598c6c20b26cb6134fad066defdb6d541d",
    "sha256:7517b392b563d21a4b9a7c481e86bdd47122ff8a1d32c716e11876bda3e9a32c",
    "sha256:91dd71e7ee8632b3678c7177c67a5ca7f788cd023aad294471a171b2e0bb78a1",
    "sha256:9b669bace04dc663b357af39358fb77360a900fb9bf08cf6489b1f8ed6eca650",
)
NPM_INTEGRITIES = {
    "@playwright/test": "sha512-nM+kEaTSAoVlXmMPH10017vn3FSiFqr/bh4fKg9vmAdMfd9SDqRZNvPSiAHADc/itWak+qPvMPZQOPwCBW7k7Q==",
    "playwright": "sha512-kkx+MB2KQRkyxjYPc3a0wLZZoDczmppyGJIvQ43l+aZihkaVvmu/21kiyaHeHjiFxjxNNFnUncKmcGIyOojsaw==",
    "playwright-core": "sha512-/crRMj8+j/Nq5s8QcvegseuyeZPxpQCZb6HNk3Sos3BlZyAknRjoyJPFWkpNn8v0+P3WiwqFF8P+zQo4eqiNuw==",
}
ACTORS = ("admin", "partner-a", "partner-b", "tenant-a1", "tenant-a2", "tenant-b1")
MAX_PROOF_BYTES = 128 * 1024
MAX_SECRET_BYTES = 16 * 1024


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("an exact lowercase SHA256 is required")
    return value


def identifier(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("an immutable Docker identifier is required")
    return value


def exact(value: object, keys: set[str], label: str) -> dict:
    if type(value) is not dict or set(value) != keys:
        raise ValueError(f"{label} has an unexpected shape")
    return value


def json_bytes(path: Path, limit: int) -> dict:
    status = path.lstat()
    if not stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode) or status.st_size > limit:
        raise ValueError("a bounded regular proof file is required")
    body = path.read_bytes()
    try:
        value = json.loads(body, object_pairs_hook=lambda rows: reject_duplicates(rows), parse_constant=reject_nonfinite)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("a finite JSON proof is required") from error
    if type(value) is not dict:
        raise ValueError("a JSON proof object is required")
    return value


def reject_duplicates(rows: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in rows:
        if key in result:
            raise ValueError("duplicate proof key")
        result[key] = value
    return result


def reject_nonfinite(_value: str):
    raise ValueError("non-finite proof value")


def verify_b2(proof: dict) -> dict:
    exact(proof, {"schema_version", "phase", "status", "b2_id", "runtime_authority", "labels", "resource_ids", "network"}, "B2 proof")
    if proof["schema_version"] != 1 or proof["phase"] != "B2" or proof["status"] != "controller_configured_readiness_verified":
        raise ValueError("a closed successful B2 proof is required")
    b2_id = str(uuid.UUID(proof["b2_id"]))
    authority = exact(proof["runtime_authority"], {"authorization_reference_sha256", "controller_helper_sha256", "accepted_operation_id", "closed"}, "runtime authority")
    if authority["closed"] is not True:
        raise ValueError("runtime authority is not closed")
    for name in ("authorization_reference_sha256", "controller_helper_sha256"):
        sha256_text(authority[name])
    identifier(authority["accepted_operation_id"])
    labels = exact(proof["labels"], {"orcastra.qa.lab", "orcastra.qa.vm_run", "orcastra.qa.phase_b"}, "B2 labels")
    if labels != {"orcastra.qa.lab": LAB, "orcastra.qa.vm_run": VM_RUN_ID, "orcastra.qa.phase_b": b2_id}:
        raise ValueError("B2 labels do not bind this lab run")
    resource_ids = proof["resource_ids"]
    if type(resource_ids) is not dict or set(resource_ids) != {"frontend", "backend", "authentik-server", "network"}:
        raise ValueError("B2 proof lacks the fixed browser upstream IDs")
    if len({identifier(value) for value in resource_ids.values()}) != len(resource_ids):
        raise ValueError("B2 resource IDs must be distinct")
    network = exact(proof["network"], {"id", "name", "internal", "published_ports", "labels"}, "B2 browser network")
    if network["id"] != resource_ids["network"] or not isinstance(network["name"], str) or not network["name"].startswith(LAB + "-application-") or network["internal"] is not True or network["published_ports"] != [] or network["labels"] != labels:
        raise ValueError("B2 network proof widened beyond the isolated application network")
    return {"b2_id": b2_id, "labels": labels, "resource_ids": resource_ids, "network": network, "authority": authority}


def verify_payload(proof: dict) -> dict:
    exact(proof, {"schema_version", "closed", "control_sha256", "image", "npm"}, "browser payload proof")
    image = exact(proof["image"], {"reference", "manifest_digest", "config_digest", "archive_sha256", "archive_verified", "config_verified", "diff_ids"}, "browser image proof")
    if proof["schema_version"] != 1 or proof["closed"] is not True or proof["control_sha256"] != PUBLIC_CONTROL_SHA256 or image["reference"] != IMAGE or image["manifest_digest"] != MANIFEST or image["config_digest"] != CONFIG or image["archive_verified"] is not True or image["config_verified"] is not True or tuple(image["diff_ids"]) != DIFF_IDS:
        raise ValueError("the closed Playwright image archive/config/diff proof differs")
    sha256_text(image["archive_sha256"])
    npm = proof["npm"]
    if type(npm) is not list or len(npm) != 3:
        raise ValueError("exactly three offline Playwright npm tarballs are required")
    observed = {}
    for row in npm:
        exact(row, {"name", "version", "sha256", "integrity", "verified"}, "npm tarball proof")
        if row["name"] in observed or row["version"] != "1.51.1" or row["integrity"] != NPM_INTEGRITIES.get(row["name"]) or row["verified"] is not True:
            raise ValueError("the closed npm tarball proof differs")
        sha256_text(row["sha256"])
        observed[row["name"]] = row
    if set(observed) != set(NPM_INTEGRITIES):
        raise ValueError("the exact Playwright npm closure is required")
    return {"image": image, "npm": observed}


def private_new_uuid_path(path: Path) -> None:
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise ValueError("a fresh absolute private output path is required")
    try:
        uuid.UUID(path.name)
    except (ValueError, AttributeError) as error:
        raise ValueError("the private output directory name must be a fresh UUID") from error


def private_secret_bytes(path: Path) -> bytes:
    if not path.is_absolute():
        raise ValueError("an absolute guest-local bootstrap password file is required")
    parent = path.parent
    while True:
        status = parent.lstat()
        if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
            raise ValueError("the bootstrap password path has an unsafe ancestor")
        if parent.parent == parent:
            break
        parent = parent.parent
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600 or before.st_uid != os.geteuid() or before.st_gid != os.getegid() or before.st_size > MAX_SECRET_BYTES:
            raise ValueError("a private owned 0600 bootstrap password file is required")
        body = os.read(descriptor, MAX_SECRET_BYTES + 1)
        after = os.fstat(descriptor)
        if len(body) > MAX_SECRET_BYTES or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("the bootstrap password file changed while being read")
        return body
    finally:
        os.close(descriptor)


def runtime_plan(b2: dict, payload: dict) -> dict:
    return {
        "schema_version": 1,
        "status": "prepared_not_executed",
        "runtime_authority_required": True,
        "network": {"id": b2["network"]["id"], "name": b2["network"]["name"], "internal": True, "published_ports": []},
        "image": {"reference": IMAGE, "manifest_digest": MANIFEST, "config_digest": CONFIG, "archive_sha256": payload["image"]["archive_sha256"], "diff_ids": list(DIFF_IDS)},
        "offline_npm": {"version": "1.51.1", "packages": {name: row["sha256"] for name, row in payload["npm"].items()}, "ignore_scripts": True, "workdir_only": True},
        "container": {"memory_bytes": 4 * 1024 ** 3, "nano_cpus": 1_500_000_000, "pids_limit": 1024, "shm_bytes": 1024 ** 3, "readonly_rootfs": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "network_mode": b2["network"]["name"], "published_ports": [], "host_network": False, "outer_nic": False, "cgroup_slice_max_bytes": 14 * 1024 ** 3, "mounts": [{"role": "payload", "target": "/input", "read_only": True}, {"role": "credentials", "target": "/run/orcastra-qa", "read_only": True}, {"role": "work", "target": "/qa", "read_only": False}], "user_policy": "prefer_pwuser_only_if_uid_gid_match_0600_credentials"},
        "proxies": {"frontend": {"listen": "127.0.0.1:32025", "upstream": "frontend:3000"}, "backend": {"listen": "127.0.0.1:32405", "upstream": "backend:4050"}, "auth": {"listen": "127.0.0.1:32090", "upstream": "authentik-server:32090", "host_alias": "auth.orcastra-qa.invalid"}},
        "credentials": {"adapter": "six_bootstrap_passwords_only", "mode": "0600", "stdout": "forbidden", "mount": "read_only_private"},
        "deadline_seconds": 1800,
        "cleanup": "forbidden_without_separate_exact_authorization",
    }


def read_six_passwords(secret: Path) -> dict[str, str]:
    """Select only the six plaintext actor passwords from IdP credentials.json."""
    try:
        document = json.loads(private_secret_bytes(secret), object_pairs_hook=lambda rows: reject_duplicates(rows), parse_constant=reject_nonfinite)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("a finite private bootstrap password envelope is required") from error
    envelope = exact(document, {"actors", "client_secret"}, "IdP bootstrap credentials")
    actors = exact(envelope["actors"], set(ACTORS), "IdP actor credentials")
    passwords = {}
    for alias in ACTORS:
        row = exact(actors[alias], {"password", "password_hash"}, "IdP actor password")
        passwords[alias] = row["password"]
    if any(not isinstance(value, str) or not 12 <= len(value) <= 512 or any(char in value for char in "\r\n\0") for value in passwords.values()):
        raise ValueError("invalid synthetic bootstrap password")
    return {actor: passwords[actor] for actor in ACTORS}


def materialize_npm(*, tarballs: dict[str, Path], proof: dict, workdir: Path) -> dict[str, Path]:
    """Extract the already-verified npm closure without invoking npm or scripts."""
    payload = verify_payload(proof)
    if workdir.exists() or workdir.is_symlink():
        raise FileExistsError("offline node workdir must be fresh")
    if set(tarballs) != set(NPM_INTEGRITIES):
        raise ValueError("the exact three npm tarballs are required")
    workdir.mkdir(mode=0o700)
    result = {}
    for name, row in payload["npm"].items():
        source = tarballs[name]
        status = source.lstat()
        if not stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode) or digest(source.read_bytes()) != row["sha256"]:
            raise ValueError("offline npm tarball hash differs")
        target = workdir / "node_modules" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(source, "r:gz") as archive:
            members = archive.getmembers()
            if not members or any(not item.isfile() or not item.name.startswith("package/") or ".." in Path(item.name).parts for item in members):
                raise ValueError("npm tarball contains a non-regular or traversing member")
            for item in members:
                relative = Path(*Path(item.name).parts[1:])
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(item) as source_file:
                    if source_file is None:
                        raise ValueError("npm tarball member is unreadable")
                    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(descriptor, "wb") as output:
                        output.write(source_file.read())
        result[name] = target
    return result


def prepare(*, b2_proof: Path, payload_proof: Path, output: Path, expected_helper_sha256: str) -> dict:
    if digest(Path(__file__).read_bytes()) != sha256_text(expected_helper_sha256):
        raise ValueError("the browser guest helper is not frozen")
    private_new_uuid_path(output)
    b2 = verify_b2(json_bytes(b2_proof, MAX_PROOF_BYTES))
    payload = verify_payload(json_bytes(payload_proof, MAX_PROOF_BYTES))
    output.mkdir(mode=0o700)
    plan = runtime_plan(b2, payload)
    descriptor = os.open(output / "plan.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as target:
        target.write(canonical(plan) + b"\n")
        target.flush(); os.fsync(target.fileno())
    return {"status": "prepared_not_executed", "plan_sha256": digest(canonical(plan)), "output": str(output)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--b2-proof")
    parser.add_argument("--payload-proof")
    parser.add_argument("--output")
    parser.add_argument("--helper-sha256")
    args = parser.parse_args(argv)
    if not args.prepare:
        print(json.dumps({"status": "inert", "live": False, "ready": False, "reason": "closed B2 and payload proofs plus runtime authority are required"}, sort_keys=True))
        return 0
    if not all((args.b2_proof, args.payload_proof, args.output, args.helper_sha256)):
        raise ValueError("prepare requires exact proof paths, a new output UUID path, and frozen helper hash")
    print(json.dumps(prepare(b2_proof=Path(args.b2_proof), payload_proof=Path(args.payload_proof), output=Path(args.output), expected_helper_sha256=args.helper_sha256), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001 - The CLI must not expose untrusted exception details.
        print('{"status":"refused","live":false}', file=sys.stderr)
        raise SystemExit(1)
