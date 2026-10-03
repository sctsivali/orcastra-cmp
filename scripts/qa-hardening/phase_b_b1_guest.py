"""Install an explicitly approved offline Docker runtime in the owned QA guest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

LAB = "orcastra-qa-hardening"
ROOT = Path("/srv") / LAB
BINARIES = {"docker", "dockerd", "containerd", "containerd-shim-runc-v2", "ctr", "runc", "docker-init", "docker-proxy"}
TOOLS = {"docker-29.8.1.tgz", "docker-compose", "docker-buildx"}
UNIT_DIRECTORY = Path("/etc/systemd/system")
CGROUP_DIRECTORY = Path("/sys/fs/cgroup")
MEMORY_MAX = 14 * 1024 ** 3
TASKS_MAX = 8192


def protected_socket(socket_status, parent_status) -> bool:
    if (not stat.S_ISSOCK(socket_status.st_mode) or socket_status.st_uid != 0 or socket_status.st_gid != 0
        or stat.S_IMODE(socket_status.st_mode) != 0o660):
        raise ValueError("The owned Docker Unix socket must be root:root 0660")
    if (not stat.S_ISDIR(parent_status.st_mode) or parent_status.st_uid != 0 or parent_status.st_gid != 0
        or stat.S_IMODE(parent_status.st_mode) != 0o700):
        raise ValueError("The Docker socket parent must be root:root 0700")
    return True


def validated_cgroup(value: str, unit: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (path.anchor != "/" or path.as_posix() != value or path.name != unit
        or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."} for part in path.parts[1:])):
        raise ValueError("The loaded owned unit has an unexpected cgroup path")
    return path


def read_cgroup_file(group: PurePosixPath, filename: str) -> str:
    if (group.anchor != "/" or any(part in {".", ".."} for part in group.parts[1:])
        or filename not in {"memory.max", "cpu.max", "pids.max"}):
        raise ValueError("Unexpected cgroup controller file")
    directory = os.open(CGROUP_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in group.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("A cgroup controller must be a regular kernel file")
            body = source.read(129)
        if len(body) > 128:
            raise ValueError("A cgroup controller value exceeded its bound")
        return body.decode("ascii").strip()
    finally:
        os.close(directory)


def controller_limits(group: PurePosixPath) -> dict:
    memory = read_cgroup_file(group, "memory.max")
    tasks = read_cgroup_file(group, "pids.max")
    cpu = read_cgroup_file(group, "cpu.max").split()
    if (memory != "max" and not re.fullmatch(r"[0-9]+", memory)
        or tasks != "max" and not re.fullmatch(r"[0-9]+", tasks)
        or len(cpu) != 2 or not re.fullmatch(r"[0-9]+", cpu[1])
        or cpu[0] != "max" and not re.fullmatch(r"[0-9]+", cpu[0])):
        raise ValueError("Unexpected cgroup controller values")
    period = int(cpu[1])
    if not 1000 <= period <= 1000000:
        raise ValueError("The cgroup CPU period is invalid")
    return {"memory": None if memory == "max" else int(memory),
            "tasks": None if tasks == "max" else int(tasks),
            "quota": None if cpu[0] == "max" else int(cpu[0]), "period": period}


def bounded_controller_limits(slice_limits: dict, service_limits: dict) -> dict:
    if (slice_limits["memory"] != MEMORY_MAX or slice_limits["tasks"] != TASKS_MAX
        or slice_limits["quota"] != 3 * slice_limits["period"]):
        raise ValueError("The owned slice does not enforce the approved memory/CPU/tasks limits")
    if (service_limits["memory"] not in (None, MEMORY_MAX) or service_limits["tasks"] != TASKS_MAX
        or service_limits["quota"] is not None and service_limits["quota"] != 3 * service_limits["period"]):
        raise ValueError("The Docker service does not inherit the approved effective slice limits")
    return {"memory_max_bytes": MEMORY_MAX, "cpu_quota_ratio": 3, "tasks_max": TASKS_MAX,
            "cpu_quota_us": slice_limits["quota"], "cpu_period_us": slice_limits["period"]}


def validate_ancestor_limits(limits: dict) -> None:
    if (limits["memory"] is not None and limits["memory"] < MEMORY_MAX
        or limits["tasks"] is not None and limits["tasks"] < TASKS_MAX
        or limits["quota"] is not None and limits["quota"] < 3 * limits["period"]):
        raise ValueError("An ancestor cgroup changes the approved effective runtime limits")


def runtime_policy() -> dict:
    prefix = str(ROOT)
    return {"hosts": ["unix://" + prefix + "/docker.sock"], "group": "root",
            "data-root": prefix + "/docker-data", "exec-root": prefix + "/docker-exec",
            "pidfile": prefix + "/docker.pid", "bridge": "none", "iptables": True,
            "ip6tables": False, "ipv6": False, "userland-proxy": False, "no-new-privileges": True,
            "exec-opts": ["native.cgroupdriver=systemd"], "cgroup-parent": LAB + ".slice",
            "features": {"containerd-snapshotter": False}, "live-restore": False, "shutdown-timeout": 20,
            "storage-driver": "overlay2",
            "max-concurrent-downloads": 1, "log-driver": "local", "log-opts": {"max-size": "10m", "max-file": "2"}}


def systemd_units(vm_run_id: str, phase_b_id: str) -> dict:
    prefix = str(ROOT)
    return {
        LAB + ".slice": f"# QA VM run {vm_run_id}; Phase B {phase_b_id}\n[Unit]\nDescription=Isolated QA runtime budget\n[Slice]\nMemoryMax=14G\nCPUQuota=300%\nTasksMax=8192\n",
        LAB + "-docker.service": f"# QA VM run {vm_run_id}; Phase B {phase_b_id}\n[Unit]\nDescription=Isolated QA Docker runtime\nAfter=local-fs.target\n[Service]\nType=notify\nNotifyAccess=all\nSlice={LAB}.slice\nEnvironment=PATH={prefix}/bin:/usr/sbin:/usr/bin:/sbin:/bin\nExecStart={prefix}/bin/dockerd --config-file={prefix}/runtime-config/daemon.json\nRestart=no\nDelegate=yes\nKillMode=process\nTimeoutStartSec=60\nTimeoutStopSec=30\nTasksMax=8192\nLimitNOFILE=1048576\n",
    }


def canonical_digest(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def valid_uuid(value: str) -> bool:
    parsed = uuid.UUID(value)
    return parsed.version == 4 and str(parsed) == value


def regular_bytes(path: Path, maximum: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("A reviewed regular file is required")
        body = source.read(maximum + 1)
    if len(body) > maximum:
        raise ValueError("The reviewed file-size bound was exceeded")
    return body


def write_new(path: Path, body: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as target:
        target.write(body)


class RuntimeBootstrap:
    """No application containers, accounts, certificates, or databases are started."""

    def __init__(self, plan: dict, approved_digest: str, approval_reference: str):
        document = dict(plan)
        recorded_digest = document.pop("proposal_digest", None)
        if canonical_digest(document) != approved_digest or recorded_digest != approved_digest or not approval_reference:
            raise ValueError("Fresh approval must bind this exact B1 proposal")
        if (document.get("schema_version") != 1 or document.get("phase") != "B1"
            or not valid_uuid(document["phase_b_id"]) or not valid_uuid(document["target"]["vm_run_id"])):
            raise ValueError("Unexpected B1 phase or ownership identity")
        if document["target"]["vm_name"] != LAB or document["target"]["project"] != "default":
            raise ValueError("Only the approved disconnected QA VM is supported")
        if (document["target"]["expanded_devices"] != ["root"] or document["target"]["profiles"]
            or document["target"]["outer_nic_count"] != 0):
            raise ValueError("Only the root-disk-only VM contract is supported")
        if {item["filename"] for item in document["tools"]} != TOOLS or len(document["tools"]) != 3:
            raise ValueError("Only the three reviewed static runtime artifacts are supported")
        if (document["docker_policy"] != runtime_policy()
            or document["systemd_units"] != systemd_units(document["target"]["vm_run_id"], document["phase_b_id"])):
            raise ValueError("B1 cannot widen the owned daemon configuration or systemd commands")
        self.plan = document
        self.phase_b_id = document["phase_b_id"]
        self.incoming = ROOT / "incoming" / self.phase_b_id
        self.stage = "approval"
        self.ledger = None

    def record(self, event: str, **fields) -> None:
        if self.ledger is None:
            return
        item = {"utc": datetime.now(timezone.utc).isoformat(), "phase_b_id": self.phase_b_id,
                "vm_run_id": self.plan["target"]["vm_run_id"], "stage": self.stage, "event": event, **fields}
        self.ledger.write(json.dumps(item, sort_keys=True) + "\n")
        self.ledger.flush()
        os.fsync(self.ledger.fileno())

    def command(self, command: list[str], *, timeout: int = 60, read_only: bool = False) -> str:
        self.record("command_attempt", command_id=self.stage, read_only=read_only)
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=timeout,
                                env={"PATH": str(ROOT / "bin") + ":/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
        self.record("command_completed", command_id=self.stage, returncode=result.returncode)
        if result.returncode:
            raise RuntimeError("An approved guest runtime command failed")
        return result.stdout.decode()

    def effective_boundaries(self) -> dict:
        protected_socket((ROOT / "docker.sock").lstat(), ROOT.lstat())
        properties = "Id,LoadState,ActiveState,FragmentPath,DropInPaths,ControlGroup,Slice,MemoryMax,TasksMax,MainPID,Delegate"
        loaded = {}
        for unit, expected_contents in self.plan["systemd_units"].items():
            if regular_bytes(UNIT_DIRECTORY / unit, 8192) != expected_contents.encode():
                raise ValueError("The owned unit file differs from the approved proposal")
            output = self.command(["systemctl", "show", unit, "--property=" + properties], read_only=True)
            pairs = [line.split("=", 1) for line in output.splitlines()]
            if any(len(pair) != 2 for pair in pairs) or len({pair[0] for pair in pairs}) != len(pairs):
                raise ValueError("Unexpected owned unit property response")
            values = dict(pairs)
            if (values.get("Id") != unit or values.get("LoadState") != "loaded" or values.get("ActiveState") != "active"
                or values.get("FragmentPath") != str(UNIT_DIRECTORY / unit) or values.get("DropInPaths") != ""):
                raise ValueError("The expected owned systemd unit is not loaded and active without overrides")
            values["validated_group"] = validated_cgroup(values["ControlGroup"], unit)
            loaded[unit] = values
        slice_name = LAB + ".slice"
        service_name = LAB + "-docker.service"
        slice_values = loaded[slice_name]
        service_values = loaded[service_name]
        slice_group = slice_values["validated_group"]
        service_group = service_values["validated_group"]
        if (slice_values.get("MemoryMax") != str(MEMORY_MAX) or slice_values.get("TasksMax") != str(TASKS_MAX)
            or service_values.get("Slice") != slice_name or service_values.get("Delegate") != "yes"
            or service_values.get("TasksMax") != str(TASKS_MAX)
            or not re.fullmatch(r"[1-9][0-9]*", service_values.get("MainPID", ""))
            or service_group.parent != slice_group):
            raise ValueError("The owned Docker service does not use the approved active slice")
        limits = bounded_controller_limits(controller_limits(slice_group), controller_limits(service_group))
        # note: systemd nests hyphenated slices. Use the loaded unit's validated
        # ControlGroup and inspect every actual ancestor, never a guessed path.
        ancestor = slice_group.parent
        while ancestor != PurePosixPath("/"):
            validate_ancestor_limits(controller_limits(ancestor))
            ancestor = ancestor.parent
        return {"socket_protected": True, "systemd_units_verified": True, "cgroup_limits_verified": True, **limits}

    def preflight(self) -> None:
        self.stage = "read_only_preflight"
        if os.geteuid() != 0:
            raise PermissionError("The approved guest bootstrap requires guest root")
        if regular_bytes(Path("/etc") / LAB, 128).strip() != LAB.encode():
            raise ValueError("The guest lab marker differs")
        if regular_bytes(Path("/etc") / (LAB + "-run"), 128).strip() != self.plan["target"]["vm_run_id"].encode():
            raise ValueError("The guest run marker differs")
        if {path.name for path in Path("/sys/class/net").iterdir()} != {"lo"}:
            raise ValueError("Only loopback may exist before B1")
        if os.uname().machine != "x86_64" or not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
            raise ValueError("x86_64 and cgroup v2 are required")
        if regular_bytes(Path("/sys/module/apparmor/parameters/enabled"), 64).strip() != b"Y":
            raise ValueError("Enabled AppArmor is required; security modules are never disabled")
        for name in ["systemctl", "iptables", "ip", "ps", "xz", "apparmor_parser", "modinfo", "modprobe"]:
            if shutil.which(name, path="/usr/sbin:/usr/bin:/sbin:/bin") is None:
                raise ValueError("A pinned-image runtime prerequisite is absent")
        # note: a fresh guest may not have loaded OverlayFS yet. Docker initializes
        # its normal storage driver; this preflight only verifies the guest module.
        if "overlay" not in Path("/proc/filesystems").read_text():
            self.command(["modinfo", "overlay"], read_only=True)
        if shutil.disk_usage(ROOT).free < 80 * 1024 ** 3:
            raise ValueError("The guest must retain at least 80 GiB free before B1")
        for directory in ["bin", "docker-config", "docker-data", "docker-exec", "runtime-config"]:
            if (ROOT / directory).exists() or (ROOT / directory).is_symlink():
                raise FileExistsError("An exact B1 runtime path already exists; do not adopt or overwrite it")
        for unit in [LAB + ".slice", LAB + "-docker.service"]:
            if (UNIT_DIRECTORY / unit).exists() or (UNIT_DIRECTORY / unit).is_symlink():
                raise FileExistsError("A B1 systemd ID already exists")
        if Path("/var/run/docker.sock").exists() or (ROOT / "docker.sock").exists():
            raise FileExistsError("An existing guest Docker socket is outside B1")
        for item in self.plan["tools"]:
            body = regular_bytes(self.incoming / item["filename"], item["size"])
            if len(body) != item["size"] or hashlib.sha256(body).hexdigest() != item["sha256"]:
                raise ValueError("A reviewed runtime artifact differs")
        guest_sha = hashlib.sha256(regular_bytes(Path(__file__), 128 * 1024)).hexdigest()
        if guest_sha != self.plan["guest_script_sha256"]:
            raise ValueError("The guest executor differs from the approved proposal")
        self.record("preflight_verified", no_nic=True, free_disk_gib=shutil.disk_usage(ROOT).free // 1024 ** 3)

    def install(self) -> dict:
        self.preflight()
        ledger_path = ROOT / "artifacts" / ("b1-" + self.phase_b_id + ".jsonl")
        fd = os.open(ledger_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.ledger = os.fdopen(fd, "w")
        try:
            self.record("approved", proposal_digest=canonical_digest(self.plan))
            self.record("preflight_verified", no_nic=True, free_disk_gib=shutil.disk_usage(ROOT).free // 1024 ** 3)
            self.stage = "install_owned_runtime_files"
            self.record("write_attempt", resource="owned_runtime_directories")
            for directory in ["bin", "docker-config", "docker-data", "docker-exec", "runtime-config"]:
                (ROOT / directory).mkdir(mode=0o700)
            plugins = ROOT / "docker-config/cli-plugins"
            plugins.mkdir(mode=0o700)
            with tarfile.open(self.incoming / "docker-29.8.1.tgz", mode="r:gz") as archive:
                members = {member.name: member for member in archive.getmembers() if member.isfile()}
                if set(members) != {"docker/" + name for name in BINARIES}:
                    raise ValueError("The Docker archive differs from the reviewed binary inventory")
                for name in sorted(BINARIES):
                    member = members["docker/" + name]
                    if member.size > 128 * 1024 * 1024:
                        raise ValueError("A Docker binary exceeds its bounded size")
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError("A Docker archive member is unavailable")
                    write_new(ROOT / "bin" / name, source.read(), 0o755)
            for filename in ["docker-compose", "docker-buildx"]:
                item = next(item for item in self.plan["tools"] if item["filename"] == filename)
                write_new(plugins / filename, regular_bytes(self.incoming / filename, item["size"]), 0o755)
            write_new(ROOT / "runtime-config/daemon.json", json.dumps(self.plan["docker_policy"], sort_keys=True).encode())
            for name, contents in self.plan["systemd_units"].items():
                if name not in {LAB + ".slice", LAB + "-docker.service"}:
                    raise ValueError("Unexpected guest systemd resource")
                write_new(UNIT_DIRECTORY / name, contents.encode(), 0o644)
            self.record("write_completed", resource="owned_runtime_files")
            self.stage = "validate_owned_daemon_config"
            self.command([str(ROOT / "bin/dockerd"), "--validate", "--config-file", str(ROOT / "runtime-config/daemon.json")])
            self.stage = "systemd_daemon_reload"
            self.command(["systemctl", "daemon-reload"])
            self.stage = "start_owned_docker"
            self.command(["systemctl", "start", LAB + "-docker.service"], timeout=90)
            self.stage = "runtime_readback"
            docker = [str(ROOT / "bin/docker"), "--host", "unix://" + str(ROOT / "docker.sock"),
                      "--config", str(ROOT / "docker-config")]
            version = json.loads(self.command([*docker, "version", "--format", "{{json .}}"], read_only=True))
            info = json.loads(self.command([*docker, "info", "--format", "{{json .}}"], read_only=True))
            if version["Client"]["Version"] != "29.8.1" or version["Server"]["Version"] != "29.8.1":
                raise ValueError("The guest Docker version differs from the approved pin")
            if (info["DockerRootDir"] != str(ROOT / "docker-data") or info["Driver"] != "overlay2"
                or info["Images"] != 0 or info["Containers"] != 0):
                raise ValueError("B1 must have the exact new runtime root and zero images/containers")
            if {path.name for path in Path("/sys/class/net").iterdir()} != {"lo"}:
                raise ValueError("B1 must not create a bridge or outer NIC")
            compose = self.command([*docker, "compose", "version", "--short"], read_only=True).strip().removeprefix("v")
            buildx = self.command([*docker, "buildx", "version"], read_only=True)
            if compose != "5.5.1" or " v0.37.1 " not in buildx:
                raise ValueError("A Docker CLI plugin version differs from the approved pin")
            self.stage = "effective_runtime_boundaries"
            boundaries = self.effective_boundaries()
            result = {"phase": "B1", "status": "runtime_verified", "phase_b_id": self.phase_b_id,
                      "vm_run_id": self.plan["target"]["vm_run_id"], "docker": "29.8.1", "compose": compose,
                      "buildx": "0.37.1", "no_nic": True, "images": 0, "containers": 0, **boundaries}
            self.record("runtime_verified", **{key: value for key, value in result.items() if key not in {"phase_b_id", "vm_run_id"}})
            return result
        except Exception as error:
            self.record("stopped", failure_type=type(error).__name__)
            raise
        finally:
            self.ledger.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--approved-digest", required=True)
    parser.add_argument("--approval-reference", required=True)
    args = parser.parse_args()
    bootstrap = None
    try:
        plan = json.loads(regular_bytes(args.proposal, 128 * 1024))
        bootstrap = RuntimeBootstrap(plan, args.approved_digest, args.approval_reference)
        expected_path = bootstrap.incoming / "b1-proposal.json"
        if args.proposal != expected_path:
            raise ValueError("Only the exact approved incoming B1 proposal path is supported")
        print(json.dumps(bootstrap.install(), sort_keys=True))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(json.dumps({"phase": "B1", "status": "stopped", "failure_type": type(error).__name__,
                          "stage": bootstrap.stage if bootstrap else "approval"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
