"""Prepare a bounded PostgreSQL journal and migration gate without live access."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import stat
import tarfile
import uuid
from pathlib import Path

from phase_b_b1_plan import render_b1
from phase_b_source import allowed_source, git_source_paths, read_source

LAB = "orcastra-qa-hardening"
SCRIPTS = Path(__file__).parent
TEST_MODULE = "backend/tests/test_the_journal_sql_round_trips_on_postgres.py"
TEST_INPUTS = {TEST_MODULE, "backend/tests/conftest.py", "backend/pytest.ini", "backend/requirements-dev.txt"}
HEAD = "037_migration_unknown_phase"
DOWNGRADE = "023_a_schedule_orcastra_owns"
DATABASES = {"journal": "orcastra_journal_test", "migration": "orcastra_migration_scratch"}
JOURNAL_OBJECTS = ["task_journal", "cluster_log", "task_follower_clusters", "task_journal_seq"]
MAX_TEST_INPUT_BYTES = 1024 * 1024


def canonical_digest(document: dict) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def checked_uuid(value: str) -> str:
    parsed = uuid.UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("An exact canonical UUID version 4 is required")
    return value


def checked_sha(value: str) -> str:
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("An exact lowercase SHA256 digest is required")
    return value


def read_test_input(repository: Path, relative: str) -> bytes:
    if relative not in TEST_INPUTS:
        raise ValueError("Only the four reviewed PostgreSQL test inputs are exportable")
    # note: reuse the openat/no-follow reader without extending its production allowlist.
    if allowed_source(relative):
        return read_source(repository, relative)[0]
    directory = os.open(repository, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in Path(relative).parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
        fd = os.open(Path(relative).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as source:
            status = os.fstat(source.fileno())
            if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_TEST_INPUT_BYTES:
                raise ValueError("A reviewed regular test input is required")
            body = source.read(MAX_TEST_INPUT_BYTES + 1)
            if len(body) > MAX_TEST_INPUT_BYTES:
                raise ValueError("A reviewed test input exceeded its size bound")
            return body
    finally:
        os.close(directory)


def source_inventory(repository: Path) -> dict:
    entries = []
    selected = {path for path in git_source_paths(repository)
                if path.startswith("backend/") and allowed_source(path)} | TEST_INPUTS
    for relative in sorted(selected):
        if relative in TEST_INPUTS:
            body = read_test_input(repository, relative)
        else:
            body = read_source(repository, relative)[0]
        entries.append({"path": relative, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()})
    module = read_test_input(repository, TEST_MODULE)
    tests = [node.name for node in ast.parse(module).body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")]
    if len(tests) != 14 or len(set(tests)) != 14:
        raise ValueError("Re-review the journal gate when its fourteen explicit cases change")
    required = {"backend/alembic.ini", "backend/requirements.txt", "backend/constraints.txt", *TEST_INPUTS}
    if not required.issubset({entry["path"] for entry in entries}):
        raise ValueError("A required migration or test input is absent")
    inventory = {"schema_version": 1, "scope": "Backend runtime/migrations and four explicit test inputs only",
                 "entries": entries, "tests": tests, "total_bytes": sum(e["size"] for e in entries)}
    inventory["source_tree_sha256"] = canonical_digest(inventory)
    return inventory


def export_source(repository: Path, inventory: dict, destination: Path) -> dict:
    if not destination.is_absolute() or destination.suffix != ".tar":
        raise ValueError("An exact new absolute .tar destination is required")
    paths = [entry["path"] for entry in inventory["entries"]]
    if paths != sorted(set(paths)):
        raise ValueError("A reviewed source inventory must have unique sorted paths")
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as target, tarfile.open(fileobj=target, mode="w") as archive:
        for entry in inventory["entries"]:
            relative = entry["path"]
            if not relative.startswith("backend/") or not (allowed_source(relative) or relative in TEST_INPUTS):
                raise ValueError("The inventory widens the reviewed backend/test boundary")
            body = read_test_input(repository, relative) if relative in TEST_INPUTS else read_source(repository, relative)[0]
            if len(body) != entry["size"] or hashlib.sha256(body).hexdigest() != entry["sha256"]:
                raise ValueError("A reviewed source input changed; never export a mixed tree")
            member = tarfile.TarInfo(relative)
            member.size, member.mode = len(body), 0o644
            member.uid = member.gid = member.mtime = 0
            member.uname = member.gname = ""
            archive.addfile(member, io.BytesIO(body))
    return {"filename": destination.name, "size": destination.stat().st_size,
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


def render_plan(*, vm_run_id: str, phase_a_digest: str, b1_id: str, gate_id: str,
                inventory: dict, image_archives: dict | None = None, wheel_archive: dict | None = None,
                source_archive: dict | None = None, wheel_inventory: list[dict] | None = None,
                wheel_requirements_sha256: str | None = None) -> dict:
    for value in (vm_run_id, b1_id, gate_id):
        checked_uuid(value)
    checked_sha(phase_a_digest)
    if wheel_requirements_sha256 is not None:
        checked_sha(wheel_requirements_sha256)
    b1 = render_b1(vm_run_id=vm_run_id, phase_a_digest=phase_a_digest, phase_b_id=b1_id)
    pins = json.loads((SCRIPTS / "phase_b_public_pins.json").read_text())["images"]
    short = gate_id[:8]
    images = {}
    for component, key in (("postgres", "postgres"), ("python", "backend_base")):
        image = pins[key]
        images[component] = {"local_tag": f"{LAB}/{component}:{image['manifest_digest'][7:23]}",
                             "expected_image_id": image["config_digest"],
                             "origin_manifest_digest": image["manifest_digest"],
                             "archive": (image_archives or {}).get(component)}
    transfer_files = ["pg-proposal.json", "phase_b_pg_guest.py", "phase_b_pg_runner.py",
                      (source_archive or {}).get("filename", "backend-source.tar"),
                      (wheel_archive or {}).get("filename", "wheels.tar")]
    transfer_files.extend((image["archive"] or {}).get("filename", component + "-docker-load.tar.gz")
                          for component, image in images.items())
    if (len(set(transfer_files)) != 7
        or any(not isinstance(name, str) or Path(name).name != name or name in {".", ".."}
               for name in transfer_files)):
        raise ValueError("The seven agent transfers must have unique plain filenames")
    plan = {
        "schema_version": 1, "phase": "B-PG", "gate_id": gate_id,
        "scope": "Fresh scratch PostgreSQL journal persistence and bounded migration reversal only",
        "target": {"vm_name": LAB, "vm_run_id": vm_run_id, "phase_a_digest": phase_a_digest,
                   "b1_phase_id": b1_id, "project": "default", "profiles": [], "outer_nic_count": 0},
        "b1_readback": {"proposal_digest": b1["proposal_digest"], "helper_sha256": b1["guest_script_sha256"]},
        "guest_root": f"/srv/{LAB}/pg/{gate_id}",
        "agent_transfers": {"path": f"/srv/{LAB}/incoming/{gate_id}", "files": sorted(transfer_files)},
        "network": {"name": f"{LAB}-pg-{short}", "subnet": "172.31.252.0/24", "internal": True,
                    "bridge_name": "oqapg-" + short, "published_ports": []},
        "containers": {"postgres": f"{LAB}-pg-db-{short}", "runner": f"{LAB}-pg-runner-{short}"},
        "labels": {"orcastra.qa.lab": LAB, "orcastra.qa.vm_run": vm_run_id,
                   "orcastra.qa.b1": b1_id, "orcastra.qa.pg_gate": gate_id},
        "databases": dict(DATABASES), "database_user": "orcastra_qa_journal", "images": images,
        "source": {"inventory": inventory, "archive": source_archive}, "wheels": wheel_archive,
        "wheel_inventory": wheel_inventory or [], "wheel_requirements_sha256": wheel_requirements_sha256,
        "runner_sha256": hashlib.sha256((SCRIPTS / "phase_b_pg_runner.py").read_bytes()).hexdigest(),
        "executor_sha256": (hashlib.sha256((SCRIPTS / "phase_b_pg_guest.py").read_bytes()).hexdigest()
                            if (SCRIPTS / "phase_b_pg_guest.py").is_file() else None),
        "resource_budget": {"postgres_memory_mib": 512, "runner_memory_mib": 1024,
                            "postgres_cpus": 0.5, "runner_cpus": 2, "postgres_pids": 256,
                            "runner_pids": 256, "minimum_guest_free_gib": 70},
        "timeouts": {"http_seconds": 30, "image_load_seconds": 300, "postgres_ready_seconds": 120,
                     "runner_seconds": 1500, "total_seconds": 2400},
        "steps": ["Recheck outer VM identity, markers and B1 runtime; refuse existing IDs/paths or overlapping route",
                  "Verify every transferred payload hash before unpacking reviewed regular source/wheel files",
                  "Load two reviewed public image archives; verify exact config IDs without pull or build",
                  "Create the owned internal bridge and fresh private PG state/password file",
                  "Start only the owned PostgreSQL container; initialize the two exact scratch databases",
                  "Run the pinned Python QA container with read-only source/wheels and fresh tooling installed offline",
                  "Upgrade journal-test DB to head and execute exactly fourteen cases; require no skip/failure",
                  "Upgrade separate empty migration DB to head, downgrade to 023, then upgrade again",
                  "Verify versions, journal objects, public column/default/constraint/index/sequence contracts and fixture cleanup independently through asyncpg"],
        "migration": {"head": HEAD, "downgrade": DOWNGRADE, "journal_objects": JOURNAL_OBJECTS,
                      "destructive_scope": "Only the separate newly created empty migration-scratch database"},
        "test_mutation": "The fourteen-case fixture TRUNCATEs three journal tables before and after tests in the fresh journal-test DB",
        "secrets": "Generate a fresh guest-only password; never use existing env/credentials or print URL/password/output bodies",
        "evidence": "Retain private command/JUnit output in guest; export only allowlisted counts, test names, object checks and digests",
        "failure": "Stop, retain owned resources and private artifacts; no implicit retry, removal or broad cleanup",
        "cleanup": "Separate exact approval using created container/network IDs and owned guest paths",
        "excluded": ["Host Docker or database", "SSO/Vault/app startup", "Organization/role changes", "Hypervisor mutation",
                     "Production database or credentials", "Outer network or published port", "Node runtime decision"],
        "ready_for_execution": False,
        "pending": ["Verified source/wheel/image archives", "Guest provisioning executor and private target wrapper review",
                    "Successful B1 readback", "New exact owner approval"],
    }
    plan["proposal_digest"] = canonical_digest(plan)
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--vm-run-id", required=True)
    parser.add_argument("--phase-a-digest", required=True)
    parser.add_argument("--b1-id", required=True)
    parser.add_argument("--gate-id", required=True)
    args = parser.parse_args()
    print(json.dumps(render_plan(vm_run_id=args.vm_run_id, phase_a_digest=args.phase_a_digest,
                                 b1_id=args.b1_id, gate_id=args.gate_id,
                                 inventory=source_inventory(args.repository)), indent=2))


if __name__ == "__main__":
    main()
