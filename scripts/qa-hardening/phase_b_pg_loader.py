"""Draft disk loader for the reviewed nuc2 PG wrapper; no final manifest is sealed."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import select
import stat
import sys
import tarfile
import time
import types
import uuid
from pathlib import Path

LAB = "orcastra-qa-hardening"
GATE_ID = "35c4d18f-cedf-4483-89f9-aa364038e3a2"
VM_RUN = "0f2a5b15-f5ec-464b-8785-0b4b88c3b358"
PHASE_A_DIGEST = "384bc2030c890ea26f054ece6725a0876fd95e95a6ccfdfe53368d17e3eb863e"
B1_ID = "cdf24503-772e-4ad0-a194-04e965c6e44d"
B1_DIGEST = "c70f6bbeb9de8c4fc97394be97e0ac408badcdd3743b6ce28f7d8dcd75a3a2ba"
B1_HELPER_SHA = "50684d551b719324ced78749631661987e3d7db7f32ca5282b790d691f1969ca"
TARGET = {"cluster_alias": "nuc2", "project": "default", "vm_name": LAB, "vm_run_id": VM_RUN,
          "phase_a_digest": PHASE_A_DIGEST, "b1_phase_id": B1_ID, "profiles": [], "outer_nic_count": 0}
FROZEN = {
    "nuc2-pg-wrapper.py": {"size": 33528, "sha256": "be8a4219d7a634caa394f2517b2c6a676a1d30e29ef83e7c023c5730b922fcaf"},
    "phase_a.py": {"size": 14684, "sha256": "3ae8ef7c4667a41fc41f56ec0aa090d546a895adf4f90733c64a1b290479d233"},
    "outer_plan.py": {"size": 6674, "sha256": "92f8d1acf3ac7924d7a2138253908bbf7768e023b86e177c3e19714786d96d9a"},
    "phase_b_pg_guest.py": {"size": 59077, "sha256": "4db12be76576f786e1170a68953d1b5285fb46f9a6b4b59064a5a42661476e87"},
    "phase_b_pg_runner.py": {"size": 12116, "sha256": "bb38df6645a22fc42a3863d0233bf2f26f1bd618775fcb1ec844ac3a38b39c00"},
}
SOURCE_ARCHIVE = {"filename": "backend-source-collector-final.tar", "size": 5181440,
                  "sha256": "7be4ff96a92d42a8aa68a62e296117b662bb313e90c20462331a6d86f487c104"}
SOURCE_TREE_SHA = "4382e2c55e16001ea60ba7a8bc58908bb72fbbe94ccca72ef3ad1ab5dca8396f"
CONTROLS = {"approved-plan.json", "b1-result.json", "pg-proposal.json"}
MAX_JSON = 4 * 1024 ** 2
MAX_BUNDLE = 2 * 1024 ** 3
BLOCK = 64 * 1024
FLUSH = 1024 ** 2
MEMORY_RESERVE = 128 * 1024 ** 2
DISK_RESERVE = 512 * 1024 ** 2
LOADER_SECONDS = 300
MAX_OUTPUT = 128 * 1024
BASE = Path("/var/tmp")
SEALED_MANIFEST = None
OUTER_STAGES = {"target_readback", "prepare_incoming", "transfer_reviewed_files", "execute_pg_gate", "outer_vm_readback"}
LEDGER_EVENTS = {"approved", "target_verified", "guest_command_attempt", "guest_command_accepted",
                 "guest_command_completed", "guest_gate_stopped", "guest_file_attempt", "guest_file_completed",
                 "guest_file_verified", "guest_gate_verified", "outer_vm_verified", "stopped"}
LEDGER_FIELDS = {"event", "stage", "utc", "gate_id", "proposal_digest", "approval_reference", "vm_run_id", "b1_phase_id",
                 "vm_configuration", "host_reserves", "command_id", "operation_id", "returncode", "filename", "size",
                 "sha256", "failure_type", "guest_stage", "resource_ids", "tests_passed", "persistence_verified",
                 "automatic_retry", "automatic_cleanup"}


def canonical(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def strict_json(body: bytes) -> dict:
    if len(body) > MAX_JSON:
        raise ValueError("A public control exceeds its byte bound")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate control keys are refused")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("Non-finite control values are refused")

    value = json.loads(body, object_pairs_hook=pairs, parse_constant=constant)
    if type(value) is not dict:
        raise TypeError("A public control object is required")
    return value


def checked_sha(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("One exact lowercase SHA256 is required")
    return value


def checked_filename(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", value):
        raise ValueError("One reviewed plain USTAR filename is required")
    return value


def metadata(value: object, maximum: int) -> None:
    if (type(value) is not dict or set(value) != {"size", "sha256"}
        or type(value["size"]) is not int or not 0 < value["size"] <= maximum):
        raise ValueError("An exact positive bounded member descriptor is required")
    checked_sha(value["sha256"])


def bundle_size(members: dict) -> int:
    size = sum(512 + ((item["size"] + 511) // 512) * 512 for item in members.values()) + 1024
    return ((size + 10239) // 10240) * 10240


def validate_manifest(document: dict, expected_digest: str) -> dict:
    fields = {"schema_version", "phase", "gate_id", "target", "proposal_digest", "payload_files", "members", "archive"}
    if (type(document) is not dict or set(document) != fields or type(document["schema_version"]) is not int
        or document["schema_version"] != 1 or document["phase"] != "B-PG" or document["gate_id"] != GATE_ID
        or canonical(document["target"]) != canonical(TARGET) or canonical(document) != checked_sha(expected_digest)):
        raise ValueError("The exact selected target and closed manifest binding are required")
    if str(uuid.UUID(document["gate_id"])) != GATE_ID or GATE_ID in {VM_RUN, B1_ID}:
        raise ValueError("The PG gate must retain its distinct canonical identity")
    checked_sha(document["proposal_digest"])
    payloads, members = document["payload_files"], document["members"]
    if (type(payloads) is not dict or set(payloads) != {"source", "wheels", "postgres", "python"}
        or type(members) is not dict or len(members) != 12):
        raise ValueError("The exact four payload roles and twelve outer members are required")
    names = [checked_filename(name) for name in payloads.values()]
    if len(set(names)) != 4 or set(members) != CONTROLS | set(FROZEN) | set(names):
        raise ValueError("The closed member set cannot widen or alias names")
    for name, item in members.items():
        checked_filename(name)
        metadata(item, MAX_BUNDLE)
    if any(members[name] != value for name, value in FROZEN.items()):
        raise ValueError("A reviewed wrapper or pure helper binding differs")
    for name in CONTROLS:
        metadata(members[name], MAX_JSON if name == "pg-proposal.json" else 128 * 1024)
    for role, limit in {"source": 32 * 1024 ** 2, "wheels": 128 * 1024 ** 2,
                        "postgres": 512 * 1024 ** 2, "python": 512 * 1024 ** 2}.items():
        metadata(members[payloads[role]], limit)
    if (payloads["source"] != SOURCE_ARCHIVE["filename"]
        or members[payloads["source"]] != {key: SOURCE_ARCHIVE[key] for key in ("size", "sha256")}):
        raise ValueError("Only the refreshed collector source archive is supported")
    archive = document["archive"]
    if type(archive) is not dict or set(archive) != {"filename", "size", "sha256"}:
        raise ValueError("The final outer archive descriptor is required")
    if archive["filename"] != "nuc2-pg-bundle-" + GATE_ID + ".tar":
        raise ValueError("The exact new PG bundle filename is required")
    metadata({key: archive[key] for key in ("size", "sha256")}, MAX_BUNDLE)
    if archive["size"] != bundle_size(members):
        raise ValueError("The outer archive must have the exact regular USTAR size")
    return document


class Deadline:
    def __init__(self, *, clock=time.monotonic, seconds=LOADER_SECONDS):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= LOADER_SECONDS:
            raise ValueError("One bounded loader deadline is required")
        self.clock, self.until = clock, clock() + seconds

    def remaining(self) -> float:
        value = self.until - self.clock()
        if not math.isfinite(value) or value <= 0:
            raise TimeoutError("The private input loader deadline expired")
        return value


def regular(descriptor: int) -> os.stat_result:
    value = os.fstat(descriptor)
    if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid() or value.st_gid != os.getegid()
        or value.st_nlink != 1 or stat.S_IMODE(value.st_mode) != 0o600):
        raise PermissionError("An owned private single-link regular file is required")
    return value


def new_file(directory: int, name: str) -> int:
    checked_filename(name)
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
    try:
        os.fchmod(descriptor, 0o600)
        regular(descriptor)
        os.fsync(directory)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def private_bytes(directory: int, name: str, expected: dict, maximum: int = MAX_JSON) -> bytes:
    checked_filename(name)
    metadata(expected, maximum)
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as source:
        if regular(source.fileno()).st_size != expected["size"]:
            raise ValueError("A reviewed control size changed")
        body = source.read(expected["size"] + 1)
    if len(body) != expected["size"] or hashlib.sha256(body).hexdigest() != expected["sha256"]:
        raise ValueError("A reviewed control digest changed")
    return body


def disk_reserve(descriptor: int, amount: int) -> None:
    usage = os.fstatvfs(descriptor)
    if usage.f_bavail * usage.f_frsize < amount + DISK_RESERVE:
        raise OSError("The private disk reserve is unavailable")


def write_all(descriptor: int, body: bytes) -> None:
    disk_reserve(descriptor, len(body))
    view = memoryview(body)
    while view:
        count = os.write(descriptor, view)
        if count <= 0:
            raise OSError("A private disk write did not advance")
        view = view[count:]


def bounded_read(reader, maximum: int, deadline: Deadline) -> bytes:
    budget = deadline.remaining()
    try:
        descriptor = reader.fileno()
    except (AttributeError, io.UnsupportedOperation):
        body = reader.read(maximum)
    else:
        poller = select.poll()
        poller.register(descriptor, select.POLLIN | select.POLLHUP | select.POLLERR)
        if not poller.poll(max(1, math.ceil(budget * 1000))):
            raise TimeoutError("The sealed input pipe did not advance")
        body = os.read(descriptor, maximum)
    deadline.remaining()
    if type(body) is not bytes or len(body) > maximum:
        raise ValueError("A sealed input read exceeds its byte bound")
    return body


def stream_exact(reader, directory: int, name: str, expected: dict, deadline: Deadline) -> None:
    metadata(expected, MAX_BUNDLE)
    descriptor = new_file(directory, name)
    size, flushed, hasher = 0, 0, hashlib.sha256()
    try:
        while True:
            body = bounded_read(reader, min(BLOCK, expected["size"] - size + 1), deadline)
            if not body:
                break
            if size + len(body) > expected["size"]:
                raise ValueError("The sealed input exceeds its exact size")
            write_all(descriptor, body)
            size += len(body)
            hasher.update(body)
            if size - flushed >= FLUSH:
                os.fsync(descriptor)
                os.posix_fadvise(descriptor, 0, 0, os.POSIX_FADV_DONTNEED)
                flushed = size
        os.fsync(descriptor)
        os.posix_fadvise(descriptor, 0, 0, os.POSIX_FADV_DONTNEED)
        if size != expected["size"] or hasher.hexdigest() != expected["sha256"]:
            raise ValueError("The sealed input size or SHA256 differs")
    finally:
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class MemberReader:
    def __init__(self, source, size: int):
        self.source, self.left = source, size

    def read(self, maximum: int) -> bytes:
        if self.left == 0:
            return b""
        body = self.source.read(min(maximum, self.left))
        self.left -= len(body)
        return body


def unpack_verified(directory: int, manifest: dict, deadline: Deadline) -> None:
    descriptor = os.open("sealed-input.tar", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    with os.fdopen(descriptor, "rb", buffering=0) as source:
        if regular(source.fileno()).st_size != manifest["archive"]["size"]:
            raise ValueError("The owned sealed archive size changed")
        seen = set()
        for name in sorted(manifest["members"]):
            deadline.remaining()
            header = source.read(512)
            if len(header) != 512 or header[257:265] != b"ustar\x0000":
                raise ValueError("Only exact ordinary USTAR headers are supported")
            # important: inspect the fixed header before reading any extension or member body.
            member = tarfile.TarInfo.frombuf(header, encoding="ascii", errors="strict")
            expected = manifest["members"][name]
            if (member.name != name or member.name in seen or member.type != tarfile.REGTYPE
                or member.size != expected["size"] or member.mode != 0o600
                or (member.uid, member.gid, member.mtime) != (0, 0, 0) or member.uname or member.gname
                or member.linkname or member.devmajor or member.devminor or member.pax_headers):
                raise ValueError("The outer regular member set or metadata differs")
            stream_exact(MemberReader(source, member.size), directory, name, expected, deadline)
            padding = source.read((-member.size) % 512)
            if any(padding) or len(padding) != (-member.size) % 512:
                raise ValueError("Nonzero or short member padding is refused")
            seen.add(name)
        tail_size = manifest["archive"]["size"] - source.tell()
        if not 1024 <= tail_size <= 10240:
            raise ValueError("The exact outer terminator padding is required")
        tail = source.read(tail_size + 1)
        if len(tail) != tail_size or any(tail) or seen != set(manifest["members"]):
            raise ValueError("Trailing data, extra members or incomplete outer input is refused")


def check_phase_a(phase_a: dict) -> None:
    if type(phase_a) is not dict or type(phase_a.get("target")) is not dict:
        raise ValueError("One exact approved Phase A object is required")
    if (canonical(phase_a) != PHASE_A_DIGEST or phase_a.get("run_id") != VM_RUN
        or phase_a.get("target", {}).get("cluster_alias") != "nuc2"
        or phase_a.get("target", {}).get("project") != "default" or phase_a.get("ready_for_execution") is not True):
        raise ValueError("The exact approved Phase A target proof is required")


def check_pg_proposal(proposal: dict, manifest: dict) -> None:
    if type(proposal) is not dict:
        raise ValueError("One exact complete PG proposal object is required")
    target = {key: value for key, value in TARGET.items() if key != "cluster_alias"}
    core = {key: value for key, value in proposal.items() if key != "proposal_digest"}
    if (proposal.get("proposal_digest") != manifest["proposal_digest"] or canonical(core) != manifest["proposal_digest"]
        or proposal.get("phase") != "B-PG" or proposal.get("gate_id") != GATE_ID
        or proposal.get("ready_for_execution") is not True or proposal.get("pending") != []
        or canonical(proposal.get("target")) != canonical(target)
        or proposal.get("b1_readback") != {"proposal_digest": B1_DIGEST, "helper_sha256": B1_HELPER_SHA}):
        raise ValueError("A complete newly approved PG proposal is required")
    files = sorted(["pg-proposal.json", "phase_b_pg_guest.py", "phase_b_pg_runner.py", *manifest["payload_files"].values()])
    if proposal.get("agent_transfers") != {"path": "/srv/" + LAB + "/incoming/" + GATE_ID, "files": files}:
        raise ValueError("The exact seven selected guest transfers are required")
    descriptors = {"source": proposal["source"]["archive"], "wheels": proposal["wheels"],
                   "postgres": proposal["images"]["postgres"]["archive"], "python": proposal["images"]["python"]["archive"]}
    for role, descriptor in descriptors.items():
        filename = manifest["payload_files"][role]
        if descriptor != {"filename": filename, **manifest["members"][filename]}:
            raise ValueError("The proposal mixes a different source, wheel or image payload")
    inventory = proposal["source"]["inventory"]
    if inventory.get("source_tree_sha256") != SOURCE_TREE_SHA or len(inventory.get("entries", [])) != 358:
        raise ValueError("Only the refreshed collector source inventory is supported")


def check_revised_b1(b1: dict) -> None:
    expected = {"phase": "B1", "phase_b_id": B1_ID, "vm_run_id": VM_RUN,
                "proposal_digest": B1_DIGEST, "failure_type": None}
    if (type(b1) is not dict or set(b1) != set(expected) | {"result", "ledger"}
        or any(b1.get(key) != value for key, value in expected.items())
        or type(b1["result"]) is not dict or b1["result"].get("status") != "runtime_verified"
        or b1["result"].get("phase_b_id") != B1_ID or b1["result"].get("vm_run_id") != VM_RUN
        or type(b1["result"].get("images")) is not int or b1["result"]["images"] != 0
        or type(b1["result"].get("containers")) is not int or b1["result"]["containers"] != 0
        or type(b1["ledger"]) is not list or not 1 <= len(b1["ledger"]) <= 128):
        raise ValueError("An actual successful revised B1 result and ledger are required")


def check_proofs(phase_a: dict, proposal: dict, b1: dict, manifest: dict) -> None:
    check_phase_a(phase_a)
    check_pg_proposal(proposal, manifest)
    check_revised_b1(b1)


def public_kernel_text(path: Path, maximum: int) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("A bounded public kernel accounting file is required")
        body = source.read(maximum + 1)
    if len(body) > maximum:
        raise ValueError("The public kernel accounting exceeds its bound")
    return body.decode("ascii")


def memory_available(root: Path = Path("/sys/fs/cgroup")) -> int:
    values = [public_kernel_text(root / name, 4096).strip() for name in ("memory.max", "memory.current")]
    if any(not value.isdecimal() for value in values) or int(values[1]) > int(values[0]):
        raise ValueError("Finite consistent cgroup memory accounting is required")
    fields = {}
    for row in public_kernel_text(root / "memory.stat", 64 * 1024).splitlines():
        name, value = row.split()
        if name in fields or not value.isdecimal():
            raise ValueError("The cgroup memory fields differ")
        fields[name] = int(value)
    required = {"inactive_file", "file", "shmem", "file_dirty", "file_writeback", "unevictable"}
    if not required <= set(fields):
        raise ValueError("Complete clean file accounting is required")
    excluded = sum(fields[name] for name in ("shmem", "file_dirty", "file_writeback", "unevictable"))
    reclaimable = max(0, min(fields["inactive_file"], fields["file"], int(values[1])) - excluded)
    return int(values[0]) - int(values[1]) + reclaimable


def disk_backed(base: Path, mounts: str) -> bool:
    found = []
    for row in mounts.splitlines():
        fields = row.split()
        if len(fields) < 3:
            raise ValueError("The public mount table differs")
        point = fields[1]
        if str(base) == point or str(base).startswith(point.rstrip("/") + "/"):
            found.append((len(point), fields[2]))
    return bool(found) and max(found)[1] not in {"tmpfs", "ramfs"}


def new_directory(parent: int, name: str) -> int:
    checked_filename(name)
    os.mkdir(name, 0o700, dir_fd=parent)
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        os.fchmod(descriptor, 0o700)
        value = os.fstat(descriptor)
        if (not stat.S_ISDIR(value.st_mode) or value.st_uid != os.geteuid() or value.st_gid != os.getegid()
            or stat.S_IMODE(value.st_mode) != 0o700):
            raise PermissionError("The new private owned stage directory differs")
        os.fsync(parent)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def new_stage(manifest: dict) -> tuple[Path, int]:
    if BASE != Path("/var/tmp") or BASE.resolve() != BASE:
        raise ValueError("Only the protected disk staging parent is supported")
    parent = os.open(BASE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        value = os.fstat(parent)
        mode = stat.S_IMODE(value.st_mode)
        if not (value.st_uid == 0 and value.st_gid == 0 and mode == 0o1777
                or value.st_uid == os.geteuid() and value.st_gid == os.getegid() and mode == 0o700):
            raise PermissionError("A protected owned private or root sticky disk parent is required")
        if (os.fstatvfs(parent).f_flag & os.ST_RDONLY
            or not disk_backed(BASE, public_kernel_text(Path("/proc/self/mounts"), 64 * 1024))
            or memory_available() < MEMORY_RESERVE):
            raise ValueError("The private disk/memory safeguards are unavailable")
        disk_reserve(parent, manifest["archive"]["size"] + sum(value["size"] for value in manifest["members"].values()))
        name = LAB + "-pg-loader-" + GATE_ID
        descriptor = new_directory(parent, name)
        return BASE / name, descriptor
    finally:
        os.close(parent)


def reviewed_namespace(directory: int, path: Path, manifest: dict) -> dict:
    names = ("outer_plan", "phase_a", "phase_b_pg_guest")
    if any(name in sys.modules for name in names):
        raise ValueError("The reviewed loader requires a fresh dedicated Python process")
    for name in names:
        module = types.ModuleType(name)
        module.__file__ = str(path / (name + ".py"))
        body = private_bytes(directory, name + ".py", manifest["members"][name + ".py"], 256 * 1024)
        sys.modules[name] = module
        exec(compile(body, module.__file__, "exec"), module.__dict__)  # noqa: S102
    body = private_bytes(directory, "nuc2-pg-wrapper.py", FROZEN["nuc2-pg-wrapper.py"], 256 * 1024)
    namespace = {"__name__": "qa_reviewed_pg", "__file__": str(path / "nuc2-pg-wrapper.py")}
    exec(compile(body, namespace["__file__"], "exec"), namespace)  # noqa: S102
    return namespace


def resource_ids(value: object, *, complete: bool) -> dict:
    names = {"network", "postgres", "runner"}
    if (type(value) is not dict or (set(value) != names if complete else not set(value) <= names)
        or any(checked_sha(item) != item for item in value.values()) or len(set(value.values())) != len(value)):
        raise ValueError("Only distinct accepted owned resource IDs are exportable")
    return dict(value)


def bounded_output(value: dict, proposal_digest: str) -> bytes:
    fields = {"phase", "gate_id", "vm_run_id", "proposal_digest", "result", "resource_ids", "failure_type", "stage", "ledger"}
    if (type(value) is not dict or set(value) != fields or value["phase"] != "B-PG" or value["gate_id"] != GATE_ID
        or value["vm_run_id"] != VM_RUN or value["proposal_digest"] != proposal_digest
        or value["stage"] not in OUTER_STAGES or type(value["ledger"]) is not list or len(value["ledger"]) > 128):
        raise ValueError("Only the reviewed wrapper's selected summary is exportable")
    checked_sha(proposal_digest)
    if len(json.dumps(value, sort_keys=True, allow_nan=False).encode()) > MAX_OUTPUT:
        raise ValueError("The selected summary exceeds its output bound")
    operations, counts = [], {}
    for row in value["ledger"]:
        if (type(row) is not dict or not set(row) <= LEDGER_FIELDS or row.get("event") not in LEDGER_EVENTS
            or row.get("stage") not in OUTER_STAGES):
            raise ValueError("Only the reviewed event fields and stages are accepted")
        event = row["event"]
        counts[event] = counts.get(event, 0) + 1
        if "operation_id" in row:
            operation = row["operation_id"]
            if not isinstance(operation, str) or not re.fullmatch(
                r"/1\.0/operations/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", operation
            ):
                raise ValueError("Only exact accepted operation IDs are exportable")
            if event == "guest_command_accepted":
                if operation in operations:
                    raise ValueError("An accepted operation cannot be adopted twice")
                operations.append(operation)
            elif event != "guest_command_completed" or operation not in operations:
                raise ValueError("A dependent operation must follow its recorded acceptance")
    success = value["failure_type"] is None
    ids = resource_ids(value["resource_ids"], complete=success)
    summary = {"phase": "B-PG", "gate_id": GATE_ID, "vm_run_id": VM_RUN, "proposal_digest": proposal_digest,
               "status": "verified" if success else "stopped", "failure_type": None if success else "WrapperStopped",
               "stage": value["stage"], "resource_ids": ids, "accepted_operation_ids": operations,
               "ledger_event_counts": counts, "automatic_retry": False, "automatic_cleanup": False}
    if not success:
        if value["result"] is not None or not isinstance(value["failure_type"], str):
            raise ValueError("A stopped wrapper cannot report a successful result")
    else:
        result = value["result"]
        expected = {"phase": "B-PG", "gate_id": GATE_ID, "status": "journal_and_migration_verified",
                    "proposal_digest": proposal_digest, "no_outer_nic": True, "container_kernel_limits_verified": True,
                    "source_tree_sha256": SOURCE_TREE_SHA, "journal_fixture_clean": True,
                    "migration_column_schema_restored": True, "migration_schema_contract_restored": True,
                    "migration_down_objects_absent": True}
        extra = {"wheel_requirements_sha256", "resource_ids", "tests", "column_schema_sha256", "schema_contract_sha256"}
        if (type(result) is not dict or set(result) != set(expected) | extra
            or any(type(result[key]) is not type(item) or result[key] != item for key, item in expected.items())
            or resource_ids(result["resource_ids"], complete=True) != ids or value["stage"] != "outer_vm_readback"
            or counts.get("outer_vm_verified") != 1 or counts.get("guest_gate_verified") != 1):
            raise ValueError("The independently reviewed guest and outer success contract is required")
        hashes = {key: checked_sha(result[key]) for key in extra if key.endswith("sha256")}
        tests, expected_counts = result["tests"], {"collected": 14, "passed": 14, "failure": 0, "error": 0, "skipped": 0}
        if (type(tests) is not dict or set(tests) != set(expected_counts) | {"test_names", "duration_seconds"}
            or any(type(tests[key]) is not int or tests[key] != item for key, item in expected_counts.items())
            or type(tests["test_names"]) is not list or len(tests["test_names"]) != 14
            or any(not isinstance(name, str) or len(name) > 512 for name in tests["test_names"])
            or type(tests["duration_seconds"]) not in (int, float) or not math.isfinite(tests["duration_seconds"])
            or not 0 <= tests["duration_seconds"] <= 3600):
            raise ValueError("Fourteen passing cases and finite bounded timing are required")
        summary.update({**expected_counts, **hashes, "source_tree_sha256": SOURCE_TREE_SHA,
                        "no_outer_nic": True, "migration_verified": True, "journal_fixture_clean": True})
    return json.dumps(summary, sort_keys=True, allow_nan=False).encode()


def run(approval_reference: str, approved_digest: str, manifest_digest: str, *, reader=None) -> dict:
    # important: this draft cannot create files, import SDKs or consult configuration.
    if SEALED_MANIFEST is None:
        raise ValueError("Actual B1, ready proposal, final manifest and fresh approval remain required")
    if not isinstance(approval_reference, str) or not re.fullmatch(r"[A-Za-z0-9_\[\]/-]{1,256}", approval_reference):
        raise ValueError("One exact fresh owner approval reference is required")
    manifest = validate_manifest(SEALED_MANIFEST, manifest_digest)
    if manifest["proposal_digest"] != checked_sha(approved_digest):
        raise ValueError("Fresh approval must bind the complete actual PG proposal")
    os.umask(0o077)
    sys.dont_write_bytecode = True
    deadline = Deadline()
    path, directory = new_stage(manifest)
    try:
        stream_exact(sys.stdin.buffer if reader is None else reader, directory, "sealed-input.tar",
                     {key: manifest["archive"][key] for key in ("size", "sha256")}, deadline)
        unpack_verified(directory, manifest, deadline)
        controls = {name: strict_json(private_bytes(directory, name, manifest["members"][name])) for name in CONTROLS}
        check_proofs(controls["approved-plan.json"], controls["pg-proposal.json"], controls["b1-result.json"], manifest)
        namespace = reviewed_namespace(directory, path, manifest)
        namespace["load_context"](path, approved_digest, approval_reference)
        deadline.remaining()
        result = namespace["run"](path, approval_reference, approved_digest)
        body = bounded_output(result, approved_digest)
        descriptor = new_file(directory, "pg-loader-result.json")
        try:
            write_all(descriptor, body + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return strict_json(body)
    finally:
        os.close(directory)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approval-reference")
    parser.add_argument("--approved-digest")
    parser.add_argument("--manifest-digest")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"phase": "B-PG-loader-draft", "gate_id": GATE_ID, "ready_for_execution": False,
                          "sealed_manifest": None, "credential_access_possible": False, "target": TARGET}, sort_keys=True))
        return
    try:
        result = run(args.approval_reference, args.approved_digest, args.manifest_digest)
    # important: no arbitrary paths, input bodies, SDK exceptions or credential output.
    except Exception:  # noqa: BLE001
        print(json.dumps({"phase": "B-PG", "gate_id": GATE_ID, "ready_for_execution": False,
                          "status": "stopped", "failure_type": "LoaderStopped"}, sort_keys=True))
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    if result["status"] != "verified":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
