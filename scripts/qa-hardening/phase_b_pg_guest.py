"""Execute one separately approved offline PostgreSQL gate in the owned QA guest."""

from __future__ import annotations

import argparse
import ast
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import tarfile
import time
import types
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode

LAB = "orcastra-qa-hardening"
ROOT = Path("/srv") / LAB
HEAD = "037_migration_unknown_phase"
DOWNGRADE = "023_a_schedule_orcastra_owns"
DATABASES = {"journal": "orcastra_journal_test", "migration": "orcastra_migration_scratch"}
OBJECTS = ("task_journal", "cluster_log", "task_follower_clusters", "task_journal_seq")
TEST_MODULE = "backend/tests/test_the_journal_sql_round_trips_on_postgres.py"
EXACT_SOURCE = {"backend/Dockerfile", "backend/.dockerignore", "backend/requirements.txt",
                "backend/constraints.txt", "backend/docker-entrypoint.sh", "backend/alembic.ini",
                TEST_MODULE, "backend/tests/conftest.py", "backend/pytest.ini", "backend/requirements-dev.txt"}
FORBIDDEN = {".git", ".worktrees", ".venv", "__pycache__", "uploads", "credentials", "certificates",
             "certs", "private", "state", "storage-state", "secrets", ".aws", ".ssh", "backup-files"}
IMAGE_PINS = {
    "postgres": ("aa90e97ee862e558111d34cfb8b2c4bec768c2b039fb791341686928560263b3",
                 "79bd7c99e923138f136f8009d6bffa66e21e9d4fda5c0c561b00fc9c90cfe537"),
    "python": ("44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b",
               "9e87977b867847e186d066f531ef783b006d582a985c341c269446088d90f2c4"),
}
BUDGET = {"postgres_memory_mib": 512, "runner_memory_mib": 1024, "postgres_cpus": 0.5,
          "runner_cpus": 2, "postgres_pids": 256, "runner_pids": 256, "minimum_guest_free_gib": 70}
TIMEOUTS = {"http_seconds": 30, "image_load_seconds": 300, "postgres_ready_seconds": 120,
            "runner_seconds": 1500, "total_seconds": 2400}
PG_CAPS = ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"]
MAX_JSON = 4 * 1024 ** 2
MAX_RESPONSE = 2 * 1024 ** 2


def canonical_digest(document: dict) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def checked_sha(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("An exact lowercase SHA256 is required")
    return value


def checked_uuid(value: str) -> str:
    parsed = uuid.UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("An exact canonical UUID version 4 is required")
    return value


def strict_json(body: bytes) -> object:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON keys are forbidden")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("Non-finite JSON numbers are forbidden")

    return json.loads(body, object_pairs_hook=pairs, parse_constant=reject_constant)


def allowed_source(relative: str) -> bool:
    path = PurePosixPath(relative)
    if (path.is_absolute() or path.as_posix() != relative or ".." in path.parts
        or FORBIDDEN.intersection(path.parts) or any(part.startswith(".env") for part in path.parts)
        or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in path.parts)):
        return False
    return (relative in EXACT_SOURCE or relative.startswith("backend/app/") and path.suffix == ".py"
            or relative.startswith("backend/alembic/") and path.suffix in {".py", ".mako"})


def safe_filename(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}", value):
        raise ValueError("A plain reviewed filename is required")
    return value


def archive_metadata(value: dict, maximum: int) -> None:
    safe_filename(value["filename"])
    checked_sha(value["sha256"])
    if type(value["size"]) is not int or not 0 < value["size"] <= maximum:
        raise ValueError("The reviewed payload size is outside its bound")


def requirements_lock(entries: list[dict]) -> bytes:
    if len(entries) != 65:
        raise ValueError("Exactly sixty-five reviewed deduplicated wheels are required")
    names = []
    filenames = []
    lines = []
    for entry in entries:
        name, version = entry["name"], entry["version"]
        if (not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
            or not re.fullmatch(r"[0-9][A-Za-z0-9.!+_-]{0,63}", version)):
            raise ValueError("The wheel identity must have a normalized name and exact version")
        filename = safe_filename(entry["filename"])
        if not filename.endswith(".whl"):
            raise ValueError("Only reviewed wheels are installable")
        checked_sha(entry["sha256"])
        if type(entry["size"]) is not int or not 0 < entry["size"] <= 64 * 1024 ** 2:
            raise ValueError("A wheel exceeds its reviewed size bound")
        names.append(name)
        filenames.append(filename)
        lines.append(f"{name}=={version} --hash=sha256:{entry['sha256']}\n")
    if names != sorted(set(names)) or len(set(filenames)) != len(filenames):
        raise ValueError("Wheel identities must be unique and sorted, never duplicated by package/version")
    if sum(entry["size"] for entry in entries) > 128 * 1024 ** 2:
        raise ValueError("The combined reviewed wheel payload exceeds its guest budget")
    if not {"alembic", "asyncpg", "pytest", "pytest-asyncio", "sqlalchemy"}.issubset(names):
        raise ValueError("The explicit persistence/test dependency closure is incomplete")
    return "".join(lines).encode()


def validate_plan(plan: dict, approved_digest: str, approval_reference: str) -> dict:
    core = dict(plan)
    digest = core.pop("proposal_digest", None)
    if (not approval_reference or digest != checked_sha(approved_digest)
        or canonical_digest(core) != approved_digest):
        raise ValueError("New exact approval must bind the complete PostgreSQL proposal")
    if (core.get("schema_version") != 1 or core.get("phase") != "B-PG"
        or core.get("ready_for_execution") is not True or core.get("pending")):
        raise ValueError("A draft, incomplete, or different phase cannot execute")
    gate = checked_uuid(core["gate_id"])
    target = core["target"]
    checked_uuid(target["vm_run_id"])
    checked_uuid(target["b1_phase_id"])
    checked_sha(target["phase_a_digest"])
    if (target["vm_name"] != LAB or target["project"] != "default"
        or target["profiles"] != [] or target["outer_nic_count"] != 0
        or core["guest_root"] != f"{ROOT}/pg/{gate}"):
        raise ValueError("Only the exact owned root-disk-only VM is supported")
    if (core["databases"] != DATABASES or core["database_user"] != "orcastra_qa_journal"
        or core["migration"]["head"] != HEAD or core["migration"]["downgrade"] != DOWNGRADE
        or core["migration"]["journal_objects"] != list(OBJECTS)):
        raise ValueError("The two scratch database boundaries are immutable")
    expected_network = {"name": f"{LAB}-pg-{gate[:8]}", "bridge_name": "oqapg-" + gate[:8],
                        "subnet": "172.31.252.0/24", "internal": True, "published_ports": []}
    expected_labels = {"orcastra.qa.lab": LAB, "orcastra.qa.vm_run": target["vm_run_id"],
                       "orcastra.qa.b1": target["b1_phase_id"], "orcastra.qa.pg_gate": gate}
    if (core["network"] != expected_network or core["labels"] != expected_labels
        or core["containers"] != {"postgres": f"{LAB}-pg-db-{gate[:8]}", "runner": f"{LAB}-pg-runner-{gate[:8]}"}
        or core["resource_budget"] != BUDGET or core["timeouts"] != TIMEOUTS):
        raise ValueError("Network, labels, budgets, and finite timeouts cannot widen")
    for key in ("proposal_digest", "helper_sha256"):
        checked_sha(core["b1_readback"][key])
    checked_sha(core["executor_sha256"])
    checked_sha(core["runner_sha256"])
    if set(core["images"]) != set(IMAGE_PINS):
        raise ValueError("Only the two reviewed images are loadable")
    filenames = ["pg-proposal.json", "phase_b_pg_guest.py", "phase_b_pg_runner.py"]
    for component, (manifest, config) in IMAGE_PINS.items():
        image = core["images"][component]
        if (image["local_tag"] != f"{LAB}/{component}:{manifest[:16]}"
            or image["origin_manifest_digest"] != "sha256:" + manifest
            or image["expected_image_id"] != "sha256:" + config):
            raise ValueError("An image differs from the reviewed production-base/public pin")
        archive_metadata(image["archive"], 512 * 1024 ** 2)
        filenames.append(image["archive"]["filename"])
    archive_metadata(core["source"]["archive"], 32 * 1024 ** 2)
    archive_metadata(core["wheels"], 128 * 1024 ** 2)
    filenames.extend([core["source"]["archive"]["filename"], core["wheels"]["filename"]])
    if len(filenames) != len(set(filenames)):
        raise ValueError("Every incoming payload requires a distinct reviewed filename")
    lock = requirements_lock(core["wheel_inventory"])
    if hashlib.sha256(lock).hexdigest() != checked_sha(core["wheel_requirements_sha256"]):
        raise ValueError("The pinned hash-lock differs from the deduplicated wheel inventory")
    source = core["source"]["inventory"]
    tree = dict(source)
    tree_sha = tree.pop("source_tree_sha256")
    entries = source["entries"]
    paths = [entry["path"] for entry in entries]
    if (canonical_digest(tree) != checked_sha(tree_sha) or source["schema_version"] != 1
        or not 7 <= len(entries) <= 2000 or paths != sorted(set(paths))
        or not {TEST_MODULE, "backend/tests/conftest.py", "backend/pytest.ini", "backend/requirements-dev.txt",
                "backend/alembic.ini", "backend/requirements.txt", "backend/constraints.txt"}.issubset(paths)
        or any(not allowed_source(path) for path in paths)):
        raise ValueError("The source inventory widens or omits a reviewed input")
    for entry in entries:
        checked_sha(entry["sha256"])
        if type(entry["size"]) is not int or not 0 <= entry["size"] <= 8 * 1024 ** 2:
            raise ValueError("A source member exceeds its bound")
    if (source["total_bytes"] != sum(entry["size"] for entry in entries)
        or source["total_bytes"] > 32 * 1024 ** 2 or len(source["tests"]) != 14
        or len(set(source["tests"])) != 14
        or any(not re.fullmatch(r"test_[A-Za-z0-9_]+", name) for name in source["tests"])):
        raise ValueError("Source sizes or the fourteen explicit test identities differ")
    return core


def open_directory(path: Path, *, private: bool = False) -> int:
    if not path.is_absolute() or path.as_posix() != str(path) or ".." in path.parts:
        raise ValueError("A canonical absolute directory is required")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
            metadata = os.fstat(directory)
            if metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_mode & 0o022:
                raise ValueError("Guest paths must have root-owned non-writable ancestors")
        if private and stat.S_IMODE(os.fstat(directory).st_mode) != 0o700:
            raise ValueError("The owned guest directory must be root:root 0700")
        return directory
    except BaseException:
        os.close(directory)
        raise


def open_regular(path: Path, *, private: bool = True):
    directory = open_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    metadata = os.fstat(fd)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_gid != 0
        or metadata.st_nlink != 1 or private and stat.S_IMODE(metadata.st_mode) != 0o600):
        os.close(fd)
        raise ValueError("A protected root-owned regular single-link file is required")
    return os.fdopen(fd, "rb")


def regular_bytes(path: Path, maximum: int, *, private: bool = True) -> bytes:
    with open_regular(path, private=private) as source:
        body = source.read(maximum + 1)
    if len(body) > maximum:
        raise ValueError("A private input exceeded its size bound")
    return body


def verify_file(path: Path, metadata: dict) -> None:
    hasher = hashlib.sha256()
    size = 0
    with open_regular(path) as source:
        for chunk in iter(lambda: source.read(1024 ** 2), b""):
            size += len(chunk)
            if size > metadata["size"]:
                raise ValueError("A reviewed payload size differs")
            hasher.update(chunk)
    if size != metadata["size"] or hasher.hexdigest() != metadata["sha256"]:
        raise ValueError("A reviewed payload hash or size differs")


def new_file(path: Path, body: bytes, *, mode: int = 0o600) -> None:
    directory = open_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=directory)
        with os.fdopen(fd, "wb") as target:
            target.write(body)
            os.fchmod(target.fileno(), mode)
            target.flush()
            os.fsync(target.fileno())
    finally:
        os.close(directory)


def new_directory(path: Path) -> None:
    directory = open_directory(path.parent)
    try:
        os.mkdir(path.name, 0o700, dir_fd=directory)
    finally:
        os.close(directory)


def checked_member(member: tarfile.TarInfo) -> str:
    path = PurePosixPath(member.name)
    if (path.is_absolute() or path.as_posix() != member.name or ".." in path.parts
        or not member.isfile() or member.issparse() or member.mode != 0o644
        or member.uid != 0 or member.gid != 0 or member.mtime != 0
        or member.uname or member.gname or set(member.pax_headers) - {"path"}
        or member.pax_headers.get("path", member.name) != member.name):
        raise ValueError("Only exact ordinary reviewed tar members are accepted")
    return member.name


def inspect_files_archive(path: Path, entries: dict[str, dict], *, destination: Path | None = None) -> dict[str, bytes]:
    """No tar extraction API is used. Every name, size and content hash is bound."""
    seen = set()
    captured = {}
    with open_regular(path) as source, tarfile.open(fileobj=source, mode="r|*") as archive:
        for member in archive:
            name = checked_member(member)
            if name not in entries or name in seen or member.size != entries[name]["size"]:
                raise ValueError("Unexpected, duplicate, or differently sized tar member")
            seen.add(name)
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("An ordinary tar member is unavailable")
            body = stream.read(member.size + 1)
            if len(body) != member.size or hashlib.sha256(body).hexdigest() != entries[name]["sha256"]:
                raise ValueError("A tar member differs from its reviewed hash")
            if name in {TEST_MODULE, "wheels/requirements.lock"}:
                captured[name] = body
            if destination is not None:
                target = destination / name
                for parent in reversed(target.parent.parents):
                    if destination not in (parent, *parent.parents):
                        continue
                    try:
                        descriptor = open_directory(parent, private=True)
                    except FileNotFoundError:
                        new_directory(parent)
                    else:
                        os.close(descriptor)
                if target.parent != destination:
                    try:
                        descriptor = open_directory(target.parent, private=True)
                    except FileNotFoundError:
                        new_directory(target.parent)
                    else:
                        os.close(descriptor)
                new_file(target, body, mode=0o644)
    if seen != set(entries):
        raise ValueError("A reviewed tar member is missing")
    return captured


def inspect_image_archive(path: Path, image: dict) -> dict:
    seen = {}
    config = None
    manifest = None
    config_name = image["expected_image_id"][7:] + ".json"
    total = 0
    with open_regular(path) as source, tarfile.open(fileobj=source, mode="r|*") as archive:
        for member in archive:
            name = checked_member(member)
            if name in seen or not (name in {"manifest.json", config_name} or re.fullmatch(r"[0-9a-f]{64}/layer.tar", name)):
                raise ValueError("An image archive contains an unreviewed or duplicate member")
            total += member.size
            if total > 4 * 1024 ** 3 or member.size < 0:
                raise ValueError("The image expansion exceeds its bounded budget")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("An image archive member is unavailable")
            hasher = hashlib.sha256()
            small = bytearray()
            size = 0
            for chunk in iter(lambda source=stream: source.read(1024 ** 2), b""):
                hasher.update(chunk)
                size += len(chunk)
                if name in {"manifest.json", config_name}:
                    small.extend(chunk)
                    if len(small) > 64 * 1024:
                        raise ValueError("An image config/manifest exceeds its bound")
            if size != member.size:
                raise ValueError("An image member was truncated")
            seen[name] = hasher.hexdigest()
            if name == config_name:
                config = strict_json(bytes(small))
            elif name == "manifest.json":
                manifest = strict_json(bytes(small))
    if (seen.get(config_name) != image["expected_image_id"][7:] or not isinstance(config, dict)
        or config.get("os") != "linux" or config.get("architecture") != "amd64"):
        raise ValueError("An image config differs from the pinned linux/amd64 identity")
    layers = config.get("rootfs", {}).get("diff_ids", [])
    if not layers or len(layers) > 20 or any(not re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in layers):
        raise ValueError("An image config has unreviewed layer identities")
    layer_names = [value[7:] + "/layer.tar" for value in layers]
    expected = [{"Config": config_name, "RepoTags": [image["local_tag"]], "Layers": layer_names}]
    if (manifest != expected or set(seen) != {config_name, "manifest.json", *layer_names}
        or any(seen.get(name) != diff[7:] for name, diff in zip(layer_names, layers, strict=True))):
        raise ValueError("Only the exact image tag and config-bound layers may be loaded")
    return config


class UnixConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(ROOT / "docker.sock"))


@contextmanager
def request_deadline(seconds: float):
    # important: socket timeouts alone restart on each progress chunk. This CLI
    # has one thread; a real-time alarm bounds the complete local API operation.
    if not 0 < seconds <= TIMEOUTS["image_load_seconds"] or signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise ValueError("Only a fresh finite bounded guest API deadline is supported")
    previous = signal.getsignal(signal.SIGALRM)

    def expired(_signal, _frame):
        raise TimeoutError("The complete owned Docker API request timed out")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class UnixDocker:
    """Fixed protected guest socket; no TCP, proxy, host socket, redirects or pull."""

    def request(self, method: str, path: str, *, body=None, size: int | None = None, timeout: int = 30) -> tuple[int, bytes]:
        if method not in {"GET", "POST"} or not path.startswith("/") or "\r" in path or "\n" in path:
            raise ValueError("Unexpected bounded Docker API request")
        connection = UnixConnection("localhost", timeout=timeout)
        headers = {"Content-Type": "application/json"}
        if size is not None:
            headers = {"Content-Type": "application/x-tar", "Content-Length": str(size)}
        elif body is not None:
            body = json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
        try:
            with request_deadline(timeout):
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                data = response.read(MAX_RESPONSE + 1)
                if len(data) > MAX_RESPONSE:
                    raise ValueError("A private Docker response exceeded its bound")
                return response.status, data
        finally:
            connection.close()


def host_config_value_matches(key: str, actual: object, expected: object) -> bool:
    # note: Docker discards oom-kill-disable on cgroup v2; the approved request keeps it false.
    if key == "OomKillDisable":
        return expected is False and (actual is False or actual is None)
    return type(actual) is type(expected) and actual == expected


def network_payload(plan: dict) -> dict:
    network = plan["network"]
    return {"Name": network["name"], "CheckDuplicate": True, "Driver": "bridge", "Internal": True,
            "Attachable": False, "EnableIPv6": False,
            "IPAM": {"Driver": "default", "Config": [{"Subnet": network["subnet"], "Gateway": "172.31.252.1"}]},
            "Options": {"com.docker.network.bridge.name": network["bridge_name"],
                        "com.docker.network.bridge.enable_ip_masquerade": "false"}, "Labels": plan["labels"]}


def container_payload(plan: dict, role: str, network_id: str) -> dict:
    checked_sha(network_id)
    if role not in {"postgres", "runner"}:
        raise ValueError("Only the two approved container roles are supported")
    root = plan["guest_root"]
    budget = plan["resource_budget"]
    memory = budget[role + "_memory_mib"] * 1024 ** 2
    host = {"NetworkMode": network_id, "ReadonlyRootfs": True, "Privileged": False,
            "AutoRemove": False, "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "CapDrop": ["ALL"], "CapAdd": PG_CAPS if role == "postgres" else [],
            "SecurityOpt": ["no-new-privileges:true"], "CgroupParent": LAB + ".slice",
            "CgroupnsMode": "private", "Memory": memory, "MemorySwap": memory,
            "NanoCpus": int(budget[role + "_cpus"] * 10 ** 9), "PidsLimit": budget[role + "_pids"],
            "OomKillDisable": False, "PublishAllPorts": False, "PortBindings": {},
            "ShmSize": 64 * 1024 ** 2, "LogConfig": {"Type": "local", "Config": {"max-size": "10m", "max-file": "2"}}}
    mounts = [{"Type": "bind", "Source": root + "/secrets", "Target": "/run/qa-gate", "ReadOnly": True,
               "BindOptions": {"Propagation": "rprivate"}}]
    payload = {"Image": plan["images"]["postgres" if role == "postgres" else "python"]["expected_image_id"],
               "Labels": plan["labels"], "HostConfig": host,
               "NetworkingConfig": {"EndpointsConfig": {plan["network"]["name"]: {
                   "NetworkID": network_id, "Aliases": [plan["containers"][role]]}}}}
    if role == "postgres":
        mounts.extend([{"Type": "bind", "Source": root + "/state", "Target": "/var/lib/postgresql/data", "ReadOnly": False,
                        "BindOptions": {"Propagation": "rprivate"}},
                       {"Type": "bind", "Source": root + "/initdb", "Target": "/docker-entrypoint-initdb.d", "ReadOnly": True,
                        "BindOptions": {"Propagation": "rprivate"}}])
        payload["Env"] = ["POSTGRES_USER=orcastra_qa_journal", "POSTGRES_DB=postgres",
                          "POSTGRES_PASSWORD_FILE=/run/qa-gate/password", "POSTGRES_HOST_AUTH_METHOD=scram-sha-256",
                          "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256", "PGDATA=/var/lib/postgresql/data"]
        payload["Healthcheck"] = {"Test": ["CMD", "pg_isready", "--host=127.0.0.1", "--port=5432",
                                               "--username=orcastra_qa_journal", "--dbname=postgres"],
                                  "Interval": 2 * 10 ** 9, "Timeout": 2 * 10 ** 9, "Retries": 60,
                                  "StartPeriod": 20 * 10 ** 9}
        host["Tmpfs"] = {"/var/run/postgresql": "rw,nosuid,nodev,size=16m,mode=3775",
                         "/tmp": "rw,nosuid,nodev,noexec,size=64m,mode=1777"}
    else:
        for source, target, readonly in [("source", "/source", True), ("wheels/wheels", "/wheels", True),
                                         ("runner", "/runner", True), ("evidence", "/evidence", False)]:
            mounts.append({"Type": "bind", "Source": root + "/" + source, "Target": target, "ReadOnly": readonly,
                           "BindOptions": {"Propagation": "rprivate"}})
        payload.update({"Entrypoint": ["python", "/runner/launch.py"], "Cmd": [], "User": "0:0",
                        "WorkingDir": "/source/backend", "Env": ["PYTHONDONTWRITEBYTECODE=1", "PYTHONNOUSERSITE=1",
                                                                   "PYTHONUNBUFFERED=1", "HOME=/tmp"]})
        # important: Docker otherwise defaults this mount to noexec, blocking native wheel imports.
        host["Tmpfs"] = {"/opt/qa-venv": "rw,exec,nosuid,nodev,size=512m,mode=0755",
                         "/tmp": "rw,nosuid,nodev,noexec,size=128m,mode=1777"}
    host["Mounts"] = mounts
    return payload


def launch_source(plan: dict, approved_digest: str) -> bytes:
    # important: the runner waits while guest kernel boundaries are inspected.
    return ('''"""Private offline bootstrap for the exact approved PostgreSQL runner."""
import hashlib
import os
import subprocess
import time
from pathlib import Path
os.umask(0o077)
gate = $GATE
approved = $APPROVED
runner_sha = $RUNNER_SHA
runner_deadline = time.monotonic() + 1500
if hashlib.sha256(Path('/runner/phase_b_pg_runner.py').read_bytes()).hexdigest() != runner_sha:
    raise ValueError('The approved runner changed')
deadline = time.monotonic() + 60
while not Path('/evidence/execute').exists():
    if time.monotonic() >= deadline:
        raise TimeoutError('Guest kernel verification did not complete')
    time.sleep(0.2)
if Path('/evidence/execute').read_bytes() != gate.encode():
    raise ValueError('The fresh execution marker differs')
environment = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1',
               'PYTHONNOUSERSITE': '1', 'HOME': '/tmp'}
def command(arguments, name, timeout):
    remaining = min(timeout, runner_deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError('The approved runner wall budget expired')
    with open('/evidence/' + name, 'xb') as private:
        result = subprocess.run(arguments, env=environment, stdout=private, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, timeout=remaining, check=False)
    if result.returncode:
        raise RuntimeError('Offline QA tooling failed; inspect the private guest artifact')
command(['python', '-m', 'venv', '/opt/qa-venv'], 'venv-private.log', 60)
python = '/opt/qa-venv/bin/python'
command([python, '-m', 'pip', 'install', '--no-index', '--no-cache-dir', '--disable-pip-version-check',
         '--only-binary=:all:', '--require-hashes', '--find-links=/wheels', '-r', '/wheels/requirements.lock'],
        'pip-private.log', 180)
command([python, '-m', 'pip', 'check'], 'pip-check-private.log', 30)
command([python, '-c', 'from asyncpg.protocol import protocol; from asyncpg.pgproto import pgproto; '
         'from importlib.machinery import EXTENSION_SUFFIXES; '
         'assert all(any(module.__file__.endswith(suffix) for suffix in EXTENSION_SUFFIXES) '
         'for module in (protocol, pgproto)); print("Native asyncpg imports verified")'],
        'native-import-private.log', 30)
command([python, '/runner/phase_b_pg_runner.py', '--proposal', '/run/qa-gate/pg-proposal.json',
         '--approved-digest', approved], 'runner-summary-private.json', 1200)
''').replace("$GATE", repr(plan["gate_id"])).replace("$APPROVED", repr(approved_digest)).replace("$RUNNER_SHA", repr(plan["runner_sha256"])).encode()


def require_runner_mounts(body: str) -> None:
    """Verify effective kernel flags, where executable mounts omit noexec."""
    if (type(body) is not str or len(body) > 64 * 1024 or not body.isascii()
            or "\x00" in body or len(body.splitlines()) > 256):
        raise ValueError("The owned runner mount table exceeds its reviewed bound")
    expected = {"/opt/qa-venv": False, "/tmp": True}
    seen = set()
    for line in body.splitlines():
        if len(line) > 4096:
            raise ValueError("An owned runner mount row exceeds its reviewed bound")
        fields = line.split(" ")
        try:
            separator = fields.index("-")
        except ValueError:
            raise ValueError("An owned runner mount row is malformed") from None
        if (separator < 6 or len(fields) != separator + 4 or any(not field for field in fields)
                or not fields[0].isdigit() or not fields[1].isdigit()
                or not re.fullmatch(r"[0-9]+:[0-9]+", fields[2])):
            raise ValueError("An owned runner mount row is malformed")
        destination = fields[4]
        if any(destination.startswith(path + "/") for path in expected):
            raise ValueError("A nested mount obscures the owned runner execution boundary")
        if destination not in expected:
            continue
        flags = fields[5].split(",")
        if (destination in seen or fields[3] != "/" or fields[separator + 1:separator + 3] != ["tmpfs", "tmpfs"]
                or len(flags) != len(set(flags)) or not {"rw", "nosuid", "nodev"}.issubset(flags)
                or {"ro", "suid", "dev"}.intersection(flags)
                or {"exec", "noexec"}.issubset(flags)
                or ("noexec" in flags) is not expected[destination]):
            raise ValueError("The owned runner temporary mount execution or security flags differ")
        seen.add(destination)
    if seen != set(expected):
        raise ValueError("An exact owned runner temporary mount is missing")


def sanitized_evidence(result: dict, plan: dict) -> dict:
    expected_top = {"phase", "gate_id", "status", "tests", "journal_before", "journal_after", "migration_before",
                    "migration_down", "migration_after", "scope_limit"}
    if (set(result) != expected_top or result["phase"] != "B-PG" or result["gate_id"] != plan["gate_id"]
        or result["status"] != "journal_and_migration_verified"
        or result["scope_limit"] != "Public column/default, constraint, index and sequence configuration equality plus journal objects; excludes physical state, sequence current values, grants and data restoration"):
        raise ValueError("The private runner result differs from the exact approved evidence contract")
    tests = result["tests"]
    if (set(tests) != {"collected", "passed", "failure", "error", "skipped", "test_names", "duration_seconds"}
        or tests["test_names"] != sorted(plan["source"]["inventory"]["tests"])
        or any(type(tests[key]) is not int or tests[key] != value
               for key, value in {"collected": 14, "passed": 14, "failure": 0, "error": 0, "skipped": 0}.items())
        or type(tests["duration_seconds"]) not in {int, float} or not math.isfinite(tests["duration_seconds"])
        or not 0 <= tests["duration_seconds"] <= TIMEOUTS["runner_seconds"]):
        raise ValueError("Every explicit journal case must pass without skips or invalid counts")
    for key in ("journal_before", "journal_after", "migration_before", "migration_down", "migration_after"):
        snapshot = result[key]
        down = key == "migration_down"
        if (set(snapshot) != {"version", "objects", "journal_row_counts", "column_schema_sha256", "schema_contract_sha256"}
            or snapshot["version"] != (DOWNGRADE if down else HEAD) or set(snapshot["objects"]) != set(OBJECTS)
            or any(value is not (not down) for value in snapshot["objects"].values())):
            raise ValueError("A migration schema/object readback differs")
        checked_sha(snapshot["column_schema_sha256"])
        checked_sha(snapshot["schema_contract_sha256"])
        counts = snapshot["journal_row_counts"]
        if down and counts or not down and (set(counts) != set(OBJECTS[:3])
            or any(type(value) is not int or value != 0 for value in counts.values())):
            raise ValueError("Fresh schema or journal fixture cleanup is incomplete")
    hashes = {result[key]["column_schema_sha256"] for key in
              ("journal_before", "journal_after", "migration_before", "migration_after")}
    if len(hashes) != 1:
        raise ValueError("The restored column schema or journal schema changed")
    contracts = {result[key]["schema_contract_sha256"] for key in
                 ("journal_before", "journal_after", "migration_before", "migration_after")}
    if len(contracts) != 1:
        raise ValueError("The restored public schema contract or journal schema changed")
    return {"tests": dict(tests), "journal_fixture_clean": True, "migration_column_schema_restored": True,
            "migration_schema_contract_restored": True, "column_schema_sha256": hashes.pop(),
            "schema_contract_sha256": contracts.pop(), "migration_down_objects_absent": True}


class ScratchGate:
    """Only accepted owned IDs are retained; failures never trigger retry or cleanup."""

    def __init__(self, plan: dict, approved_digest: str, approval_reference: str, *, transport=None,
                 clock=time.monotonic, sleeper=time.sleep):
        self.plan = validate_plan(plan, approved_digest, approval_reference)
        self.digest = approved_digest
        self.document = plan
        self.gate = self.plan["gate_id"]
        self.root = Path(self.plan["guest_root"])
        self.incoming = ROOT / "incoming" / self.gate
        self.transport = transport or UnixDocker()
        self.clock, self.sleeper = clock, sleeper
        self.deadline = clock() + TIMEOUTS["total_seconds"]
        self.stage = "approval"
        self.ledger = None
        self.sequence = 0
        self.created = {}
        self.runtime = None
        self.b1 = None
        self.image_configs = {}

    def record(self, event: str, **fields) -> None:
        if self.ledger is None:
            return
        item = {"utc": datetime.now(timezone.utc).isoformat(), "gate_id": self.gate,
                "vm_run_id": self.plan["target"]["vm_run_id"], "stage": self.stage, "event": event, **fields}
        self.ledger.write(json.dumps(item, sort_keys=True, allow_nan=False) + "\n")
        self.ledger.flush()
        os.fsync(self.ledger.fileno())

    def remaining(self, seconds: int) -> float:
        value = min(seconds, self.deadline - self.clock())
        if value <= 0:
            raise TimeoutError("The approved guest gate time budget expired")
        return value

    def api(self, method: str, path: str, *, body=None, expected=200, accept: str | None = None,
            upload=None, upload_size=None, timeout=30):
        self.sequence += 1
        sequence = self.sequence
        self.record("api_attempt", request_sequence=sequence, read_only=method == "GET")
        status_code, raw = self.transport.request(method, path, body=upload if upload is not None else body,
                                                  size=upload_size, timeout=self.remaining(timeout))
        try:
            result = None if not raw or upload is not None else strict_json(raw)
            if status_code == expected and accept:
                resource_id = checked_sha(result["Id"])
                self.created[accept] = resource_id
                # important: preserve the accepted ID before any readback/wait/artifact can fail.
                self.record("resource_accepted", resource=accept, resource_id=resource_id, request_sequence=sequence)
        finally:
            artifact = self.root / "evidence" / f"api-{sequence:04d}-private.log"
            if artifact.parent.exists():
                new_file(artifact, raw)
            self.record("api_completed", request_sequence=sequence, http_status=status_code)
        if status_code != expected:
            raise RuntimeError("An approved Docker API request failed; inspect its private guest artifact")
        if upload is not None:
            for line in raw.splitlines():
                item = strict_json(line)
                if not isinstance(item, dict) or "error" in item or "errorDetail" in item:
                    raise RuntimeError("An approved image load failed; inspect its private guest artifact")
        return result

    def read_only_command(self, command: list[str]) -> bytes:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, check=False,
                                timeout=self.remaining(10), env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
        if result.returncode or len(result.stdout) > 128 * 1024:
            raise RuntimeError("A bounded read-only guest preflight failed")
        return result.stdout

    def verify_b1(self) -> None:
        target = self.plan["target"]
        directory = ROOT / "incoming" / target["b1_phase_id"]
        body = regular_bytes(directory / "phase_b_b1_guest.py", 128 * 1024)
        if hashlib.sha256(body).hexdigest() != self.plan["b1_readback"]["helper_sha256"]:
            raise ValueError("The approved B1 helper changed")
        proposal = strict_json(regular_bytes(directory / "b1-proposal.json", 128 * 1024))
        if (proposal["target"]["vm_run_id"] != target["vm_run_id"]
            or proposal["target"]["phase_a_digest"] != target["phase_a_digest"]
            or proposal["phase_b_id"] != target["b1_phase_id"]
            or proposal["guest_script_sha256"] != self.plan["b1_readback"]["helper_sha256"]):
            raise ValueError("The previous successful runtime has different ownership/provenance")
        module = types.ModuleType("_approved_qa_b1_readback")
        module.__file__ = str(directory / "phase_b_b1_guest.py")
        exec(compile(body, module.__file__, "exec"), module.__dict__)  # noqa: S102
        runtime = module.RuntimeBootstrap(proposal, self.plan["b1_readback"]["proposal_digest"], "PG read-only preflight")
        rows = [strict_json(line) for line in regular_bytes(ROOT / "artifacts" / ("b1-" + target["b1_phase_id"] + ".jsonl"),
                                                         2 * 1024 ** 2).splitlines()]
        if (not rows or rows[0].get("event") != "approved"
            or rows[0].get("proposal_digest") != self.plan["b1_readback"]["proposal_digest"]
            or rows[-1].get("event") != "runtime_verified"
            or any(row.get("phase_b_id") != target["b1_phase_id"] or row.get("vm_run_id") != target["vm_run_id"] for row in rows)
            or rows[-1].get("socket_protected") is not True or rows[-1].get("cgroup_limits_verified") is not True):
            raise ValueError("The owned B1 ledger does not end in the reviewed successful runtime")
        self.b1, self.runtime = module, runtime
        runtime.effective_boundaries()

    def preflight(self) -> None:
        self.stage = "read_only_preflight"
        if os.geteuid() != 0 or os.uname().machine != "x86_64":
            raise PermissionError("Only guest root on the approved x86_64 QA VM may execute")
        for path in [ROOT, ROOT / "artifacts", self.incoming]:
            os.close(open_directory(path, private=True))
        if (regular_bytes(Path("/etc") / LAB, 128).strip() != LAB.encode()
            or regular_bytes(Path("/etc") / (LAB + "-run"), 128).strip() != self.plan["target"]["vm_run_id"].encode()):
            raise ValueError("The private guest ownership markers differ")
        if {path.name for path in Path("/sys/class/net").iterdir()} != {"lo"}:
            raise ValueError("The initial PG gate requires the previous no-NIC/loopback-only baseline")
        if (self.root.exists() or self.root.is_symlink()
            or (ROOT / "artifacts" / ("pg-" + self.gate + ".jsonl")).exists()
            or (ROOT / "artifacts" / ("pg-" + self.gate + ".jsonl")).is_symlink()):
            raise FileExistsError("A gate ID/path already exists; never adopt, retry or overwrite it")
        if shutil.disk_usage(ROOT).free < BUDGET["minimum_guest_free_gib"] * 1024 ** 3:
            raise ValueError("The reviewed guest free-disk reserve is unavailable")
        self.verify_b1()
        info = self.api("GET", "/info")
        if (info.get("ServerVersion") != "29.8.1" or info.get("DockerRootDir") != str(ROOT / "docker-data")
            or info.get("Driver") != "overlay2" or info.get("CgroupDriver") != "systemd"
            or info.get("CgroupVersion") != "2" or info.get("Images") != 0 or info.get("Containers") != 0):
            raise ValueError("The fresh owned daemon baseline or cgroup mode changed")
        if self.api("GET", "/containers/json?all=1") or self.api("GET", "/images/json?all=1"):
            raise FileExistsError("Refusing to adopt or overwrite any existing image/container")
        networks = self.api("GET", "/networks")
        if (len(networks) != 2 or {item.get("Name") for item in networks} != {"host", "none"}
            or any(item.get("Driver") not in {"host", "null"} for item in networks)):
            raise FileExistsError("The initial owned daemon already has a network outside the empty baseline")
        wanted = ipaddress.ip_network(self.plan["network"]["subnet"])
        routes = strict_json(self.read_only_command(["ip", "-j", "-4", "route", "show", "table", "all"]))
        for route in routes:
            destination = route.get("dst", "default")
            if destination == "default" or wanted.overlaps(ipaddress.ip_network(destination, strict=False)):
                raise ValueError("A guest route overlaps or escapes the owned internal subnet")
        executor = regular_bytes(self.incoming / "phase_b_pg_guest.py", 256 * 1024)
        runner = regular_bytes(self.incoming / "phase_b_pg_runner.py", 128 * 1024)
        if (hashlib.sha256(executor).hexdigest() != self.plan["executor_sha256"]
            or hashlib.sha256(runner).hexdigest() != self.plan["runner_sha256"]
            or Path(__file__) != self.incoming / "phase_b_pg_guest.py"):
            raise ValueError("Only the exact approved incoming executor/runner may execute")

    def payload_entries(self) -> tuple[dict, dict]:
        source = {entry["path"]: entry for entry in self.plan["source"]["inventory"]["entries"]}
        wheels = {"wheels/" + entry["filename"]: entry for entry in self.plan["wheel_inventory"]}
        lock = requirements_lock(self.plan["wheel_inventory"])
        wheels["wheels/requirements.lock"] = {"size": len(lock), "sha256": self.plan["wheel_requirements_sha256"]}
        return source, wheels

    def verify_payloads(self) -> None:
        self.stage = "verify_transferred_payloads"
        for metadata in [self.plan["source"]["archive"], self.plan["wheels"],
                         *(self.plan["images"][role]["archive"] for role in IMAGE_PINS)]:
            verify_file(self.incoming / metadata["filename"], metadata)
        source, wheels = self.payload_entries()
        captured = inspect_files_archive(self.incoming / self.plan["source"]["archive"]["filename"], source)
        tests = [node.name for node in ast.parse(captured[TEST_MODULE]).body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")]
        if tests != self.plan["source"]["inventory"]["tests"]:
            raise ValueError("The archive test identities changed")
        captured = inspect_files_archive(self.incoming / self.plan["wheels"]["filename"], wheels)
        if captured["wheels/requirements.lock"] != requirements_lock(self.plan["wheel_inventory"]):
            raise ValueError("The transferred offline hash-lock changed")
        for role, image in self.plan["images"].items():
            self.image_configs[role] = inspect_image_archive(self.incoming / image["archive"]["filename"], image)
        self.record("payloads_verified", source_tree_sha256=self.plan["source"]["inventory"]["source_tree_sha256"],
                    wheel_requirements_sha256=self.plan["wheel_requirements_sha256"], wheel_count=65)

    def prepare_paths(self) -> None:
        self.stage = "create_owned_gate_paths"
        self.record("write_attempt", resource="owned_gate_directories")
        try:
            descriptor = open_directory(ROOT / "pg", private=True)
        except FileNotFoundError:
            new_directory(ROOT / "pg")
        else:
            os.close(descriptor)
        new_directory(self.root)
        for name in ("source", "wheels", "evidence", "runner", "secrets", "initdb", "state"):
            new_directory(self.root / name)
        source, wheels = self.payload_entries()
        inspect_files_archive(self.incoming / self.plan["source"]["archive"]["filename"], source,
                              destination=self.root / "source")
        inspect_files_archive(self.incoming / self.plan["wheels"]["filename"], wheels, destination=self.root / "wheels")
        new_file(self.root / "secrets" / "owner", self.gate.encode())
        new_file(self.root / "secrets" / "password", secrets.token_hex(32).encode())
        new_file(self.root / "secrets" / "pg-proposal.json", json.dumps(self.document, sort_keys=True, allow_nan=False).encode())
        new_file(self.root / "runner" / "phase_b_pg_runner.py", regular_bytes(self.incoming / "phase_b_pg_runner.py", 128 * 1024))
        new_file(self.root / "runner" / "launch.py", launch_source(self.plan, self.digest))
        sql = b"CREATE DATABASE orcastra_journal_test OWNER orcastra_qa_journal;\nCREATE DATABASE orcastra_migration_scratch OWNER orcastra_qa_journal;\n"
        new_file(self.root / "initdb" / "00-create-scratch.sql", sql, mode=0o644)
        directory = open_directory(self.root / "initdb", private=True)
        try:
            os.fchmod(directory, 0o755)
        finally:
            os.close(directory)
        self.record("write_completed", resource="owned_gate_directories")

    def load_images(self) -> None:
        for role in IMAGE_PINS:
            self.stage = "load_owned_image_" + role
            image = self.plan["images"][role]
            existing = self.api("GET", "/images/json?" + urlencode({"filters": json.dumps({"reference": [image["local_tag"]]})}))
            if existing:
                raise FileExistsError("The exact image tag already exists; never overwrite it")
            metadata = image["archive"]
            verify_file(self.incoming / metadata["filename"], metadata)
            with open_regular(self.incoming / metadata["filename"]) as upload:
                self.api("POST", "/images/load?quiet=1", upload=upload, upload_size=metadata["size"],
                         timeout=TIMEOUTS["image_load_seconds"])
            details = self.api("GET", "/images/" + image["expected_image_id"] + "/json")
            if (details.get("Id") != image["expected_image_id"] or details.get("Os") != "linux"
                or details.get("Architecture") != "amd64" or details.get("RepoTags") != [image["local_tag"]]):
                raise ValueError("The accepted image does not have the exact owned tag/config identity")
            self.record("image_verified", component=role, image_id=image["expected_image_id"], archive_sha256=metadata["sha256"])

    def create_network(self) -> None:
        self.stage = "create_owned_internal_network"
        self.api("POST", "/networks/create", body=network_payload(self.plan), expected=201, accept="network")
        self.stage = "verify_owned_internal_network"
        network = self.api("GET", "/networks/" + self.created["network"])
        expected = network_payload(self.plan)
        if (network.get("Id") != self.created["network"] or network.get("Name") != expected["Name"]
            or network.get("Driver") != "bridge" or network.get("Internal") is not True
            or network.get("EnableIPv6") is not False or network.get("Attachable") is not False
            or network.get("Labels") != expected["Labels"] or network.get("Options") != expected["Options"]
            or network.get("IPAM", {}).get("Driver") != "default"
            or network.get("IPAM", {}).get("Config") != expected["IPAM"]["Config"] or network.get("Containers")):
            raise ValueError("The accepted network differs from the exact isolated empty bridge")
        self.record("network_verified", resource_id=self.created["network"], internal=True, published_ports=0)

    def create_container(self, role: str) -> None:
        self.stage = "create_owned_" + role
        self.api("POST", "/containers/create?" + urlencode({"name": self.plan["containers"][role]}),
                 body=container_payload(self.plan, role, self.created["network"]), expected=201, accept=role)
        self.inspect_container(role, running=False)
        self.stage = "start_owned_" + role
        self.record("start_attempt", resource=role, resource_id=self.created[role])
        self.api("POST", "/containers/" + self.created[role] + "/start", expected=204)
        self.record("start_completed", resource=role, resource_id=self.created[role])

    def inspect_container(self, role: str, *, running: bool | None = None) -> dict:
        item = self.api("GET", "/containers/" + self.created[role] + "/json")
        expected = container_payload(self.plan, role, self.created["network"])
        config, host = item.get("Config", {}), item.get("HostConfig", {})
        state = item.get("State", {})
        prestart = running is False
        if (type(state) is not dict or type(state.get("Running")) is not bool
            or prestart and (state.get("Status") != "created" or state["Running"] is not False
                             or type(state.get("Pid")) is not int or state["Pid"] != 0)
            or not prestart and not (state.get("Status") == "running" and state["Running"] is True
                                     or state.get("Status") == "exited" and state["Running"] is False)):
            raise ValueError("The accepted container lifecycle state is unknown or unexpected")
        # important: only the freshly created prestart state may lack AppArmor.
        profiles = {"", "docker-default"} if prestart else {"docker-default"}
        if (item.get("Id") != self.created[role] or item.get("Name") != "/" + self.plan["containers"][role]
            or item.get("Image") != expected["Image"] or config.get("Labels") != self.plan["labels"]
            or any(key not in host or not host_config_value_matches(key, host[key], value)
                   for key, value in expected["HostConfig"].items() if key != "Mounts")
            or config.get("Env") is None or not set(expected.get("Env", [])).issubset(config["Env"])
            or type(item.get("AppArmorProfile")) is not str or item["AppArmorProfile"] not in profiles):
            raise ValueError("The accepted container identity/security/budget differs")
        mounts = item.get("Mounts", [])
        expected_mounts = expected["HostConfig"]["Mounts"]
        binds = [mount for mount in mounts if mount.get("Type") == "bind"]
        tmpfs = [mount for mount in mounts if mount.get("Type") == "tmpfs"]
        if (len(binds) != len(expected_mounts) or len(tmpfs) + len(binds) != len(mounts)
            or len({mount.get("Destination") for mount in mounts}) != len(mounts)
            or any(mount.get("Destination") not in expected["HostConfig"]["Tmpfs"] or mount.get("RW") is not True for mount in tmpfs)
            or any(not any(m.get("Type") == "bind" and m.get("Source") == e["Source"]
                          and m.get("Destination") == e["Target"] and m.get("RW") is (not e["ReadOnly"])
                          and m.get("Propagation") == "rprivate" for m in mounts) for e in expected_mounts)):
            raise ValueError("A container mount is unapproved or writable beyond its owned boundary")
        image_config = self.image_configs["postgres" if role == "postgres" else "python"]["config"]
        environment = {}
        for value in [*image_config.get("Env", []), *expected.get("Env", [])]:
            key, separator, value = value.partition("=")
            if not separator:
                raise ValueError("A reviewed image environment value is invalid")
            environment[key] = value
        actual_environment = [value.partition("=") for value in config.get("Env", [])]
        image_user = image_config.get("User")
        approved_user = expected.get("User", "" if image_user is None else image_user)
        if (any(not separator for _, separator, _ in actual_environment)
            or len({key for key, _, _ in actual_environment}) != len(actual_environment)
            or {key: value for key, _, value in actual_environment} != environment
            or type(approved_user) is not str or type(config.get("User")) is not str or config["User"] != approved_user
            or any(config.get(key) != expected.get(key, image_config.get(key))
                   for key in ("Entrypoint", "Cmd", "WorkingDir"))):
            raise ValueError("The accepted container entrypoint/user/environment differs from the exact approved image/command")
        networks = item.get("NetworkSettings", {}).get("Networks", {})
        if (set(networks) != {self.plan["network"]["name"]}
            or networks[self.plan["network"]["name"]].get("NetworkID") != self.created["network"]):
            raise ValueError("The container attached to an unapproved network")
        if running is not None and state.get("Running") is not running:
            raise ValueError("The accepted container is in an unexpected running state")
        if state.get("OOMKilled") is True or state.get("Dead") is True or state.get("Error"):
            raise RuntimeError("The owned container failed; inspect its private guest state")
        return item

    def wait_container(self, role: str) -> dict:
        seconds = TIMEOUTS["postgres_ready_seconds" if role == "postgres" else "runner_seconds"]
        deadline = min(self.deadline, self.clock() + seconds)
        self.stage = "wait_owned_" + role
        while self.clock() < deadline:
            item = self.inspect_container(role)
            state = item["State"]
            if role == "postgres" and state.get("Running") is True and state.get("Health", {}).get("Status") == "healthy":
                self.record("container_ready", resource=role, resource_id=self.created[role])
                return item
            if state.get("Running") is False:
                self.record("container_completed", resource=role, resource_id=self.created[role], returncode=state.get("ExitCode"))
                if role != "runner" or type(state.get("ExitCode")) is not int or state["ExitCode"] != 0:
                    raise RuntimeError("The owned container exited unsuccessfully")
                return item
            self.sleeper(min(1, max(0, deadline - self.clock())))
        raise TimeoutError("An owned container did not reach its approved final state")

    @staticmethod
    def process_file(pid: int, name: str) -> str:
        if type(pid) is not int or pid <= 1 or name not in {"cgroup", "status", "attr/current", "mountinfo"}:
            raise ValueError("Only bounded owned-process security readback is supported")
        descriptor = os.open("/proc", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in (str(pid), *PurePosixPath(name).parts[:-1]):
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = following
            fd = os.open(PurePosixPath(name).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("An owned-process security file is not regular")
                maximum = 64 * 1024 if name == "mountinfo" else 8192
                body = source.read(maximum + 1)
            if len(body) > maximum:
                raise ValueError("An owned-process security file exceeds its bound")
            return body.decode("ascii").strip()
        finally:
            os.close(descriptor)

    def verify_container_kernel(self, role: str) -> None:
        self.stage = "verify_kernel_" + role
        item = self.inspect_container(role, running=True)
        pid = item["State"]["Pid"]
        group_row = self.process_file(pid, "cgroup").splitlines()
        if len(group_row) != 1 or not group_row[0].startswith("0::"):
            raise ValueError("The owned container must use cgroup v2")
        group = self.b1.validated_cgroup(group_row[0][3:], "docker-" + self.created[role] + ".scope")
        output = self.runtime.command(["systemctl", "show", LAB + ".slice", "--property=ControlGroup"], read_only=True)
        if not output.startswith("ControlGroup=") or len(output.splitlines()) != 1:
            raise ValueError("The approved runtime slice readback differs")
        parent = self.b1.validated_cgroup(output.strip().split("=", 1)[1], LAB + ".slice")
        limits = self.b1.controller_limits(group)
        budget = self.plan["resource_budget"]
        if (group.parent != parent or limits["memory"] != budget[role + "_memory_mib"] * 1024 ** 2
            or limits["tasks"] != budget[role + "_pids"] or limits["quota"] is None
            or limits["quota"] * 10 ** 9 != limits["period"] * int(budget[role + "_cpus"] * 10 ** 9)):
            raise ValueError("The owned container kernel resource boundary differs")
        properties = dict(line.split(":", 1) for line in self.process_file(pid, "status").splitlines() if ":" in line)
        if (properties.get("NoNewPrivs", "").strip() != "1" or properties.get("Seccomp", "").strip() != "2"
            or not re.fullmatch(r"0+", properties.get("CapEff", "").strip())
            or self.process_file(pid, "attr/current") != "docker-default (enforce)"):
            raise ValueError("The running owned process security controls differ")
        if role == "runner":
            require_runner_mounts(self.process_file(pid, "mountinfo"))
            if self.inspect_container(role, running=True)["State"]["Pid"] != pid:
                raise ValueError("The owned runner PID changed during kernel mount verification")
        self.record("container_kernel_verified", resource=role, resource_id=self.created[role],
                    memory_max_bytes=limits["memory"], pids_max=limits["tasks"], cpu_quota_us=limits["quota"],
                    cpu_period_us=limits["period"], seccomp_enforced=True, no_new_privileges=True, apparmor_enforced=True)

    def execute(self) -> dict:
        self.preflight()
        path = ROOT / "artifacts" / ("pg-" + self.gate + ".jsonl")
        directory = open_directory(path.parent, private=True)
        try:
            fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        finally:
            os.close(directory)
        self.ledger = os.fdopen(fd, "w")
        try:
            self.record("approved", proposal_digest=self.digest)
            self.record("preflight_verified", no_nic=True, fresh_daemon=True)
            self.verify_payloads()
            self.prepare_paths()
            self.load_images()
            self.create_network()
            self.create_container("postgres")
            self.wait_container("postgres")
            self.verify_container_kernel("postgres")
            self.create_container("runner")
            self.verify_container_kernel("runner")
            self.stage = "release_verified_runner"
            self.record("write_attempt", resource="runner_execute_marker")
            new_file(self.root / "evidence" / "execute", self.gate.encode())
            self.record("write_completed", resource="runner_execute_marker")
            self.wait_container("runner")
            self.stage = "verify_persistence_and_migration_evidence"
            result = strict_json(regular_bytes(self.root / "evidence" / "runner-summary-private.json", MAX_JSON))
            summary = sanitized_evidence(result, self.plan)
            self.runtime.effective_boundaries()
            self.inspect_container("postgres", running=True)
            result = {"phase": "B-PG", "gate_id": self.gate, "status": "journal_and_migration_verified",
                      "proposal_digest": self.digest, "resource_ids": dict(self.created), "no_outer_nic": True,
                      "container_kernel_limits_verified": True, "source_tree_sha256": self.plan["source"]["inventory"]["source_tree_sha256"],
                      "wheel_requirements_sha256": self.plan["wheel_requirements_sha256"], **summary}
            self.record("gate_verified", **{key: value for key, value in result.items() if key != "gate_id"})
            return result
        except Exception as error:
            self.record("stopped", failure_type=type(error).__name__, resource_ids=dict(self.created))
            raise
        finally:
            self.ledger.close()


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--approved-digest", required=True)
    parser.add_argument("--approval-reference", required=True)
    args = parser.parse_args()
    gate = None
    try:
        plan = strict_json(regular_bytes(args.proposal, MAX_JSON))
        gate = ScratchGate(plan, args.approved_digest, args.approval_reference)
        if args.proposal != gate.incoming / "pg-proposal.json":
            raise ValueError("Only the exact approved incoming PostgreSQL proposal path is supported")
        print(json.dumps(gate.execute(), sort_keys=True, allow_nan=False))
    # important: driver/Docker exceptions and raw output may contain private data.
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"phase": "B-PG", "status": "stopped", "failure_type": type(error).__name__,
                          "stage": gate.stage if gate else "approval", "resource_ids": dict(gate.created) if gate else {}}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
