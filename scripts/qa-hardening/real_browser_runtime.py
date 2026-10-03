"""Bounded guest-only runtime, imported after the executor's complete byte gate."""

from __future__ import annotations

import contextlib
import http.client
import os
import platform
import re
import shutil
import signal
import socket
import stat
import time
from pathlib import Path
from urllib.parse import urlencode

LAB = "orcastra-qa-hardening"
ROOT = Path("/srv") / LAB
ACTORS = ("admin", "partner-a", "partner-b", "tenant-a1", "tenant-a2", "tenant-b1")
CASES = {
    **{"auth." + actor: ("GET", "/api/v1/system/settings", 200) for actor in ACTORS},
    "auth.invalid": ("GET", "/api/v1/system/settings", 401),
    "organization.setup": ("POST", "/api/v1/organizations/", 201),
    "organization.neighbor-denial": ("GET", "/api/v1/organizations/:neighbor/preferences", 403),
    "organization.branding": ("PUT", "/api/v1/organizations/:owned/preferences", 200),
    "organization.revoke": ("DELETE", "/api/v1/organizations/:owned/members/:synthetic", 204),
    "settings.urls": ("UI", "/settings", 200),
    "settings.tenant-denial": ("UI", "/settings/integrations", 200),
    "settings.personal-theme-cache": ("PUT", "/api/v1/user-preferences", 200),
    "auth.logout": ("GET", "/api/v1/system/settings", 401),
}


@contextlib.contextmanager
def request_deadline(seconds):
    """Bound the complete HTTP operation, including streaming and response reads."""
    def expired(_signum, _frame):
        raise TimeoutError("The bounded Docker operation elapsed")

    previous = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    started = time.monotonic()
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(0.001, previous_timer[0] - (time.monotonic() - started)), previous_timer[1])


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(ROOT / "docker.sock"))


class Docker:
    def __init__(self, core, remaining):
        self.core, self.remaining = core, remaining

    def request(self, method, path, *, body=None, upload=None, size=None, expected=200, seconds=30):
        if (method not in {"GET", "POST"} or not isinstance(path, str) or not path.startswith("/")
                or any(character in path for character in "\r\n\0") or upload is not None and body is not None):
            raise ValueError("Only bounded owned Unix Docker requests are supported")
        headers, payload = {}, None
        if body is not None:
            payload = self.core.canonical(body)
            headers["Content-Type"] = "application/json"
        if upload is not None:
            if type(size) is not int or not 0 < size <= 4 * 1024**3:
                raise ValueError("Exact bounded image upload size required")
            payload = upload
            headers.update({"Content-Type": "application/x-tar", "Content-Length": str(size)})
        connection = UnixConnection("localhost", timeout=self.remaining(seconds))
        try:
            with request_deadline(self.remaining(seconds)):
                connection.request(method, path, body=payload, headers=headers)
                response = connection.getresponse()
                data = response.read(2 * 1024**2 + 1)
                if len(data) > 2 * 1024**2 or response.status != expected:
                    raise RuntimeError("The exact owned Docker request failed")
            if not data:
                return {}
            # Docker's load response is JSON lines; immutable image readback is
            # the acceptance gate, never an untrusted progress/error string.
            if upload is not None:
                return {}
            return self.core.strict_json(data)
        finally:
            connection.close()


def new_file(path: Path, body: bytes, *, mode=0o600, uid=0):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), mode)
        os.fchown(output.fileno(), uid, uid)
        output.write(body)
        output.flush()
        os.fsync(output.fileno())
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def private_directory(path: Path, *, uid=0):
    path.mkdir(mode=0o700)
    os.chown(path, uid, uid)
    status = path.lstat()
    if (not stat.S_ISDIR(status.st_mode) or status.st_uid != uid or status.st_gid != uid
            or stat.S_IMODE(status.st_mode) != 0o700):
        raise ValueError("Exact private new directory required")


def process_file(pid: int, name: str) -> str:
    if type(pid) is not int or pid <= 0 or name not in {"status", "cgroup", "attr/current"}:
        raise ValueError("Exact running process proof required")
    directory = os.open("/proc", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in (str(pid), *name.split("/")[:-1]):
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
        descriptor = os.open(name.split("/")[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as source:
            body = source.read(65537)
        if len(body) > 65536:
            raise ValueError("Process evidence exceeds its bound")
        return body.decode("ascii").strip()
    finally:
        os.close(directory)


def kernel_current_memory(group):
    if group.anchor != "/" or any(re.fullmatch(r"[A-Za-z0-9_.-]+", part) is None or part in {".", ".."} for part in group.parts[1:]):
        raise ValueError("Exact validated cgroup path required")
    directory = os.open("/sys/fs/cgroup", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in group.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
        descriptor = os.open("memory.current", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as source:
            body = source.read(129).strip()
        if len(body) > 128 or re.fullmatch(rb"[0-9]+", body) is None:
            raise ValueError("The actual slice memory counter differs")
        return int(body)
    finally:
        os.close(directory)


def result_contract(core, result: dict, ledger: bytes) -> dict:
    core.exact(result, {"schema_version", "lab", "results"})
    if type(result["schema_version"]) is not int or result["schema_version"] != 1 or result["lab"] != LAB:
        raise ValueError("Sanitized harness result identity differs")
    if type(result["results"]) is not list or len(result["results"]) != 15:
        raise ValueError("Every frozen browser case must pass")
    seen = set()
    for row in result["results"]:
        core.exact(row, {"case_id", "method", "path", "status", "assertion"})
        case = row["case_id"]
        if (not isinstance(case, str) or case not in CASES or case in seen or row["assertion"] is not True
                or type(row["status"]) is not int or (row["method"], row["path"], row["status"]) != CASES[case]):
            raise ValueError("Unique sanitized passed assertions required")
        seen.add(case)
    if seen != set(CASES) or len(ledger) > 65536:
        raise ValueError("Frozen browser case or private ledger closure differs")
    rows = [core.strict_json(line) for line in ledger.splitlines()]
    if not rows or rows[0] != {"event": "ledger_started"} or len(rows) != 6:
        raise ValueError("Complete preserved private resource ledger required")
    organizations, members = set(), set()
    for row in rows[1:]:
        if row.get("event") == "organization_created":
            core.exact(row, {"event", "organization_id"})
            identifier = row["organization_id"]
            if type(identifier) is not int or identifier <= 0 or identifier in organizations:
                raise ValueError("Distinct accepted organization IDs required")
            organizations.add(identifier)
        elif row.get("event") == "member_created":
            core.exact(row, {"event", "member_id", "organization_id"})
            identifier = row["member_id"]
            if (type(identifier) is not int or identifier <= 0 or identifier in members
                    or type(row["organization_id"]) is not int or row["organization_id"] not in organizations):
                raise ValueError("Accepted membership must bind an owned organization")
            members.add(identifier)
        else:
            raise ValueError("Unknown private ledger event")
    if len(organizations) != 2 or len(members) != 3:
        raise ValueError("Accepted resource ledger is incomplete")
    return {"passed": 15, "failed": 0, "skipped": 0, "private_ledger_sha256": core.sha(ledger),
            "accepted_organization_count": 2, "accepted_member_count": 3}


def six_passwords(core, body: bytes) -> dict:
    """Parse the frozen IdP schema from one bounded guest-private read."""
    envelope = core.exact(core.strict_json(body), {"actors", "client_secret"})
    actors = core.exact(envelope["actors"], set(ACTORS))
    passwords = {}
    for name in ACTORS:
        row = core.exact(actors[name], {"password", "password_hash"})
        password = row["password"]
        if not isinstance(password, str) or not 12 <= len(password) <= 512 or any(character in password for character in "\r\n\0"):
            raise ValueError("Only valid synthetic actor passwords are supported")
        passwords[name] = password
    return passwords


def image_configuration_matches(actual, original):
    """Compare every image configuration field used by this fixed launch.

    Docker's immutable image ID binds the complete raw config hash. Its inspect
    response can also include default fields absent from that raw JSON.
    """
    if type(actual) is not dict or type(original) is not dict:
        return False
    defaults = {"Env": [], "Labels": {}, "Entrypoint": [], "Cmd": [], "User": "", "WorkingDir": "", "Volumes": {}, "ExposedPorts": {}}
    for key, default in defaults.items():
        observed, expected = actual.get(key), original.get(key)
        observed = default if observed is None else observed
        expected = default if expected is None else expected
        if type(observed) is not type(expected) or observed != expected:
            return False
    return True


class BrowserRun:
    def __init__(self, manifest, adapter, helpers, buffers, npm, archive, fingerprint, image_config, remaining, planner, b1, pg, core):
        self.manifest, self.adapter, self.helpers, self.buffers, self.npm = manifest, adapter, helpers, buffers, npm
        self.archive, self.archive_fingerprint, self.image_config = archive, fingerprint, image_config
        self.remaining, self.planner, self.b1, self.pg, self.core = remaining, planner, b1, pg, core
        self.run = core.checked_uuid(manifest["run_id"])
        self.phase = core.checked_uuid(adapter["b2_id"])
        self.state = ROOT / "state" / self.run
        self.evidence = self.state / "evidence"
        self.api = Docker(core, remaining)
        self.ledger = None
        self.identifier = None
        self.runtime = None
        self.payload = None

    def record(self, event, **fields):
        if self.ledger is None:
            raise RuntimeError("The private durable ledger must exist before mutation")
        self.ledger.write(self.core.canonical({"event": event, "run_id": self.run, "b2_id": self.phase, **fields}) + b"\n")
        self.ledger.flush()
        os.fsync(self.ledger.fileno())

    def preflight(self):
        if os.geteuid() != 0 or os.getegid() != 0 or platform.machine() != "x86_64":
            raise PermissionError("Only guest root on the pinned amd64 lab is supported")
        for path, expected in ((Path("/etc") / LAB, LAB), (Path("/etc") / (LAB + "-run"), self.core.VM_RUN)):
            status = path.lstat()
            if not stat.S_ISREG(status.st_mode) or status.st_uid != 0 or status.st_gid != 0 or status.st_mode & 0o022:
                raise ValueError("Owned lab markers required")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as source:
                if source.read(129).strip() != expected.encode():
                    raise ValueError("The guest identity marker differs")
        if any((item / "device").exists() for item in Path("/sys/class/net").iterdir()):
            raise ValueError("The owned lab must have no physical outer NIC")
        if (Path("/sys/module/apparmor/parameters/enabled").read_text().strip() != "Y"
                or not Path("/sys/fs/cgroup/cgroup.controllers").is_file()):
            raise ValueError("Enforcing AppArmor and cgroup v2 are required")
        if shutil.disk_usage(ROOT).free < 8 * 1024**3:
            raise ValueError("The private browser expansion reserve is unavailable")
        incoming = ROOT / "incoming" / self.core.B1_ID
        plan = self.core.strict_json(self.core.owned_bytes(incoming / "b1-proposal.json", self.core.MAX_METADATA))
        if plan.get("phase_b_id") != self.core.B1_ID or plan.get("target", {}).get("vm_run_id") != self.core.VM_RUN:
            raise ValueError("The installed runtime is not the accepted owned B1 run")
        self.runtime = self.b1.RuntimeBootstrap(plan, self.core.B1_DIGEST, "browser-owned-runtime-readback")
        b1_log = self.core.owned_bytes(ROOT / "artifacts" / ("b1-" + self.core.B1_ID + ".jsonl"), 2 * 1024**2, mode=0o600)
        last = self.core.strict_json(b1_log.splitlines()[-1])
        expected_b1 = {"event": "runtime_verified", "phase": "B1", "status": "runtime_verified", "phase_b_id": self.core.B1_ID,
                       "vm_run_id": self.core.VM_RUN, "docker": "29.8.1", "compose": "5.5.1", "buildx": "0.37.1", "no_nic": True,
                       "socket_protected": True, "systemd_units_verified": True, "cgroup_limits_verified": True,
                       "memory_max_bytes": 14 * 1024**3, "tasks_max": 8192, "cpu_quota_ratio": 3}
        if any(last.get(key) != value or type(last.get(key)) is not type(value) for key, value in expected_b1.items()):
            raise ValueError("Actual successful B1 acceptance is required")
        with request_deadline(self.remaining(180)):
            boundaries = self.runtime.effective_boundaries()
        if boundaries.get("memory_max_bytes") != 14 * 1024**3 or boundaries.get("tasks_max") != 8192:
            raise ValueError("The accepted runtime slice changed")
        self.memory_budget()
        info = self.api.request("GET", "/info")
        if (info.get("ServerVersion") != "29.8.1" or info.get("DockerRootDir") != str(ROOT / "docker-data")
                or info.get("Driver") != "overlay2" or info.get("CgroupDriver") != "systemd" or info.get("CgroupVersion") != "2"):
            raise ValueError("Only the exact isolated accepted Docker daemon is supported")
        self.verify_network()
        for role in ("frontend", "backend", "authentik-server"):
            self.verify_upstream(role)

    def memory_budget(self):
        fields = {}
        descriptor = os.open("/proc/meminfo", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            body = source.read(65537)
        if len(body) > 65536:
            raise ValueError("The guest memory evidence exceeds its bound")
        for line in body.decode("ascii").splitlines():
            if line.startswith("MemAvailable:"):
                fields = line.split()
        if len(fields) != 3 or fields[2] != "kB" or not fields[1].isdecimal() or int(fields[1]) * 1024 < 6 * 1024**3:
            raise ValueError("The browser cap plus 4 GiB guest reserve is unavailable")
        value = self.runtime.command(["systemctl", "show", LAB + ".slice", "--property=ControlGroup"], read_only=True).strip()
        if not value.startswith("ControlGroup="):
            raise ValueError("The loaded slice identity is absent")
        group = self.b1.validated_cgroup(value.split("=", 1)[1], LAB + ".slice")
        if kernel_current_memory(group) + 2 * 1024**3 > 14 * 1024**3:
            raise ValueError("The browser would exceed the accepted aggregate slice budget")

    def verify_network(self, extra=None):
        expected = self.adapter["network"]
        item = self.api.request("GET", "/networks/" + expected["id"])
        wanted = set(self.adapter["service_ids"].values())
        if extra is not None:
            wanted.add(extra)
        if (item.get("Id") != expected["id"] or item.get("Name") != expected["name"] or item.get("Driver") != "bridge"
                or item.get("Scope") != "local" or item.get("Internal") is not True or item.get("Ingress") is not False
                or item.get("Attachable") is not False or item.get("EnableIPv6") is not False or item.get("Labels") != expected["labels"]
                or item.get("Options") != {"com.docker.network.bridge.enable_ip_masquerade": "false", "com.docker.network.bridge.name": "oqab2-" + self.phase[:8]}
                or item.get("IPAM", {}).get("Driver") != "default"
                or item.get("IPAM", {}).get("Config") != [{"Subnet": "172.28.66.0/24", "Gateway": "172.28.66.1"}]
                or set(item.get("Containers", {})) != wanted):
            raise ValueError("The exact isolated B2 network or its membership changed")

    def verify_upstream(self, role):
        identifier = self.adapter["resource_ids"][role]
        item = self.api.request("GET", "/containers/" + identifier + "/json")
        host, config, state = item.get("HostConfig", {}), item.get("Config", {}), item.get("State", {})
        image = self.api.request("GET", "/images/" + self.adapter["image_ids"][role] + "/json")
        image_labels = image.get("Config", {}).get("Labels") or {}
        if (item.get("Id") != identifier or item.get("Image") != self.adapter["image_ids"][role]
                or image.get("Id") != self.adapter["image_ids"][role]
                or config.get("Labels") != {**image_labels, **self.adapter["labels"]}
                or state.get("Running") is not True or type(state.get("Pid")) is not int or state["Pid"] <= 0
                or state.get("OOMKilled") or state.get("Dead") or state.get("Error")
                or state.get("Health", {}).get("Status", "healthy") != "healthy"
                or item.get("AppArmorProfile") != "docker-default" or host.get("NetworkMode") != self.adapter["network"]["id"]):
            raise ValueError("An exact owned running B2 upstream changed")
        networks = item.get("NetworkSettings", {}).get("Networks", {})
        if set(networks) != {self.adapter["network"]["name"]}:
            raise ValueError("An upstream acquired an unapproved network")
        endpoint = networks[self.adapter["network"]["name"]]
        aliases = endpoint.get("Aliases") or []
        required = {role} | ({"auth.orcastra-qa.invalid"} if role == "authentik-server" else set())
        permitted = required | {identifier, identifier[:12], item.get("Name", "").lstrip("/")}
        if endpoint.get("NetworkID") != self.adapter["network"]["id"] or not required.issubset(aliases) or not set(aliases).issubset(permitted):
            raise ValueError("The exact upstream network aliases differ")
        port, outside = {"frontend": (2025, 32025), "backend": (4050, 32405), "authentik-server": (32090, 32090)}[role]
        bindings = {str(port) + "/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(outside)}]}
        actual = {key: value for key, value in item.get("NetworkSettings", {}).get("Ports", {}).items() if value is not None}
        if host.get("PortBindings") != bindings or actual != bindings:
            raise ValueError("The source-proven B2 endpoint ports changed")

    def begin(self):
        # important: one exclusive attempt per B2 prevents a new UUID from
        # silently retrying already accepted browser mutations after failure.
        new_file(ROOT / "state" / self.phase / "browser-attempt.json", self.core.canonical({
            "run_id": self.run, "proposal_digest": self.manifest["proposal_digest"]}))
        private_directory(self.state)
        descriptor = os.open(ROOT / "artifacts" / ("browser-" + self.run + ".jsonl"),
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.ledger = os.fdopen(descriptor, "wb")
        self.record("attempt_started", accepted_operation_reference_sha256=self.adapter["accepted_operation_reference_sha256"])

    def load_image(self):
        if self.core.fingerprint(os.fstat(self.archive.fileno())) != self.archive_fingerprint:
            raise ValueError("The pinned image changed before load")
        images = self.api.request("GET", "/images/json?all=1")
        if type(images) is not list or any(row.get("Id") == self.core.IMAGE_CONFIG for row in images):
            raise ValueError("Do not adopt a browser image from an earlier run")
        self.archive.seek(0)
        self.record("pinned_image_load_started", archive_sha256=self.manifest["files"]["image"]["sha256"])
        self.api.request("POST", "/images/load?quiet=1", upload=self.archive, size=self.manifest["files"]["image"]["size"], seconds=300)
        if self.core.fingerprint(os.fstat(self.archive.fileno())) != self.archive_fingerprint:
            raise ValueError("The pinned image changed during load")
        image = self.api.request("GET", "/images/" + self.core.IMAGE_CONFIG + "/json")
        if (image.get("Id") != self.core.IMAGE_CONFIG or image.get("Architecture") != "amd64" or image.get("Os") != "linux"
                or image.get("RootFS", {}).get("Layers") != list(self.core.DIFF_IDS)
                or image.get("RootFS", {}).get("Type") != "layers"
                or not image_configuration_matches(image.get("Config"), self.image_config.get("config"))):
            raise ValueError("The loaded image is not the exact pinned official browser image")
        self.record("pinned_image_verified", image_id=self.core.IMAGE_CONFIG)

    def materialize(self):
        inputs = self.state / "input"
        inputs.mkdir(mode=0o755)
        inputs.chmod(0o755)
        new_file(inputs / "real_role_harness.cjs", self.buffers["harness"], mode=0o444)
        new_file(inputs / "real_browser_launch.cjs", self.helpers["real_browser_launch.cjs"], mode=0o444)
        configuration = {"schema_version": 1, "run_id": self.run, "proposal_digest": self.manifest["proposal_digest"],
                         "proxies": self.manifest["plan"]["proxies"], "deadline_seconds": int(self.remaining(self.core.DEADLINE)),
                         "deadline_epoch_ms": int((time.time() + self.remaining(self.core.DEADLINE)) * 1000)}
        new_file(inputs / "launch.json", self.core.canonical(configuration), mode=0o444)
        self.core.extract_npm(self.npm, self.state / "node", self.remaining)
        private_directory(self.evidence, uid=1000)
        credentials = self.state / "credentials"
        private_directory(credentials, uid=1000)
        source = ROOT / "state" / self.phase / "idp-bootstrap/credentials.json"
        # The boundary verifies the exact B2-owned secret path first. The
        # reviewed adapter exports only six passwords, never hashes or client secrets.
        passwords = six_passwords(self.core, self.core.owned_bytes(source, 16384, mode=0o600))
        body = self.core.canonical({"schema_version": 1, "actors": {name: {"password": value} for name, value in passwords.items()}})
        new_file(credentials / "actors.json", body, uid=1000)
        passwords.clear()
        self.record("private_inputs_materialized", helper_sha256=self.manifest["helpers"]["real_browser_launch.cjs"])

    def container_payload(self):
        def bind(source, target, readonly=True):
            return {"Type": "bind", "Source": str(source), "Target": target, "ReadOnly": readonly,
                    "BindOptions": {"Propagation": "rprivate"}}

        network = self.adapter["network"]
        host = {"NetworkMode": network["id"], "ReadonlyRootfs": True, "Privileged": False, "CapDrop": ["ALL"], "CapAdd": [],
                "SecurityOpt": ["no-new-privileges:true"], "Memory": 2 * 1024**3, "MemorySwap": 2 * 1024**3,
                "NanoCpus": 1_500_000_000, "PidsLimit": 1024, "ShmSize": 1024**3, "CgroupParent": LAB + ".slice",
                "CgroupnsMode": "private", "PidMode": "", "IpcMode": "private", "UTSMode": "", "UsernsMode": "",
                "AutoRemove": False, "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                "OomKillDisable": False,
                "PublishAllPorts": False, "PortBindings": {}, "Devices": [], "DeviceRequests": [], "DeviceCgroupRules": [],
                "Binds": [], "Links": [], "VolumesFrom": [], "GroupAdd": [], "ExtraHosts": ["auth.orcastra-qa.invalid:127.0.0.1"],
                "Tmpfs": {"/tmp": "rw,nosuid,nodev,noexec,size=1073741824,mode=1777"},
                "LogConfig": {"Type": "local", "Config": {"max-size": "10m", "max-file": "2"}},
                "Mounts": [bind(self.state / "input", "/input"), bind(self.state / "node", "/node"),
                           bind(self.state / "credentials", "/run/orcastra-qa"), bind(self.evidence, "/qa", False)]}
        return {"Image": self.core.IMAGE_CONFIG, "Labels": {**self.adapter["labels"], "orcastra.qa.browser_run": self.run},
                "User": "1000:1000", "WorkingDir": "/qa", "Entrypoint": ["node"], "Cmd": ["/input/real_browser_launch.cjs", "--execute"],
                "Env": ["NODE_PATH=/node/node_modules", "PLAYWRIGHT_BROWSERS_PATH=/ms-playwright", "NODE_OPTIONS=--max-old-space-size=512",
                        "HOME=/tmp/orcastra-qa-home", "LANG=C.UTF-8", "CI=1"], "HostConfig": host,
                "NetworkingConfig": {"EndpointsConfig": {network["name"]: {"NetworkID": network["id"], "Aliases": ["browser-" + self.run]}}}}

    def inspect(self, *, running=None):
        item = self.api.request("GET", "/containers/" + self.identifier + "/json")
        expected = self.payload
        host, config, state = item.get("HostConfig", {}), item.get("Config", {}), item.get("State", {})
        image_config = self.image_config.get("config", {})
        if (item.get("Id") != self.identifier or item.get("Image") != self.core.IMAGE_CONFIG
                or config.get("Labels") != {**(image_config.get("Labels") or {}), **expected["Labels"]}
                or state.get("OOMKilled") or state.get("Dead") or state.get("Error")
                or running is not None and state.get("Running") is not running):
            raise ValueError("The accepted browser identity or state changed")
        profile = item.get("AppArmorProfile")
        if state.get("Running") is True:
            if profile != "docker-default" or type(state.get("Pid")) is not int or state["Pid"] <= 0:
                raise ValueError("Running browser confinement is absent")
        elif state.get("Status") == "created" and state.get("Running") is False and type(state.get("Pid")) is int and state["Pid"] == 0:
            if profile not in {"", "docker-default"}:
                raise ValueError("The prestart browser AppArmor profile differs")
        elif profile != "docker-default":
            raise ValueError("The accepted browser AppArmor profile changed")
        if running is False and (state.get("Status") != "created" or type(state.get("Pid")) is not int or state["Pid"] != 0):
            raise ValueError("Only a fresh unstarted created browser may be inspected before start")
        for key, desired in expected["HostConfig"].items():
            if key == "Mounts":
                continue
            actual = host.get(key)
            if key == "OomKillDisable" and key not in host:
                raise ValueError("The explicit Docker OOM setting is absent")
            if desired in ([], {}) and actual is None:
                actual = desired
            if not self.pg.host_config_value_matches(key, actual, desired):
                raise ValueError("The exact browser limits or namespace configuration changed")
        for key in ("Entrypoint", "Cmd", "User", "WorkingDir"):
            if config.get(key) != expected[key]:
                raise ValueError("The browser launch command or user changed")
        environment = {}
        for row in [*(image_config.get("Env") or []), *expected["Env"]]:
            name, separator, value = row.partition("=")
            if not separator:
                raise ValueError("Malformed pinned image environment")
            environment[name] = value
        rows = [row.partition("=") for row in config.get("Env", [])]
        if any(not separator for _, separator, _ in rows) or len({name for name, _, _ in rows}) != len(rows) or {name: value for name, _, value in rows} != environment:
            raise ValueError("The browser complete environment changed")
        mounts = item.get("Mounts", [])
        wanted = expected["HostConfig"]["Mounts"]
        if type(mounts) is not list or any(type(row) is not dict for row in mounts):
            raise ValueError("The browser mount inventory is malformed")
        binds = [row for row in mounts if row.get("Type") == "bind"]
        tmpfs = [row for row in mounts if row.get("Type") == "tmpfs"]
        # Docker may omit HostConfig.Tmpfs from this inventory. If reported,
        # it must still name only the declared scratch mount without a source.
        if (len(binds) != len(wanted) or len(binds) + len(tmpfs) != len(mounts)
                or any(type(row.get("Destination")) is not str for row in mounts)
                or len({row["Destination"] for row in mounts}) != len(mounts)
                or any(row["Destination"] not in expected["HostConfig"]["Tmpfs"] or row.get("RW") is not True
                       or row.get("Source") not in (None, "") or row.get("Propagation") not in (None, "") for row in tmpfs)):
            raise ValueError("An extra mount widened browser access")
        for desired in wanted:
            matches = [row for row in binds if row.get("Destination") == desired["Target"]]
            if (len(matches) != 1 or matches[0].get("Type") != "bind" or matches[0].get("Source") != desired["Source"]
                    or matches[0].get("RW") is not (not desired["ReadOnly"]) or matches[0].get("Propagation") != "rprivate"):
                raise ValueError("The browser mounted an unapproved source")
        networks = item.get("NetworkSettings", {}).get("Networks", {})
        network = self.adapter["network"]
        permitted = {network["id"]} if state.get("Running") else {None, "", network["id"]}
        if set(networks) != {network["name"]} or networks[network["name"]].get("NetworkID") not in permitted:
            raise ValueError("The browser joined an unapproved network")
        if state.get("Running"):
            aliases = networks[network["name"]].get("Aliases") or []
            required_alias = "browser-" + self.run
            allowed_aliases = {required_alias, self.identifier, self.identifier[:12], item.get("Name", "").lstrip("/")}
            if required_alias not in aliases or not set(aliases).issubset(allowed_aliases):
                raise ValueError("The browser network aliases changed")
        if any(value is not None for value in item.get("NetworkSettings", {}).get("Ports", {}).values()):
            raise ValueError("The browser published an unapproved port")
        return item

    def kernel_boundary(self):
        item = self.inspect(running=True)
        pid = item["State"]["Pid"]
        rows = process_file(pid, "cgroup").splitlines()
        if len(rows) != 1 or not rows[0].startswith("0::"):
            raise ValueError("Only exact cgroup v2 process membership is accepted")
        group = self.b1.validated_cgroup(rows[0][3:], "docker-" + self.identifier + ".scope")
        parent_line = self.runtime.command(["systemctl", "show", LAB + ".slice", "--property=ControlGroup"], read_only=True).strip()
        if not parent_line.startswith("ControlGroup="):
            raise ValueError("The loaded slice identity is absent")
        parent = self.b1.validated_cgroup(parent_line.split("=", 1)[1], LAB + ".slice")
        limits = self.b1.controller_limits(group)
        status = dict(row.split(":", 1) for row in process_file(pid, "status").splitlines() if ":" in row)
        if (group.parent != parent or limits["memory"] != 2 * 1024**3 or limits["tasks"] != 1024
                or limits["quota"] is None or limits["quota"] * 10**9 != limits["period"] * 1_500_000_000
                or status.get("NoNewPrivs", "").strip() != "1" or status.get("Seccomp", "").strip() != "2"
                or any(re.fullmatch(r"0+", status.get(key, "").strip()) is None for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"))
                or process_file(pid, "attr/current") != "docker-default (enforce)"):
            raise ValueError("The running browser exceeds actual kernel confinement")
        with request_deadline(self.remaining(180)):
            self.runtime.effective_boundaries()
        self.record("kernel_limits_verified", container_id=self.identifier, memory_max_bytes=limits["memory"], pids_max=limits["tasks"])

    def launch(self):
        self.payload = self.container_payload()
        self.memory_budget()
        name = LAB + "-browser-" + self.run
        result = self.api.request("POST", "/containers/create?" + urlencode({"name": name}), body=self.payload, expected=201)
        self.identifier = self.core.checked_sha(result.get("Id"))
        # important: retain the accepted Docker ID durably before any inspect,
        # start, wait, or error can interrupt ownership accounting.
        self.record("container_accepted", container_id=self.identifier)
        if self.identifier in set(self.adapter["service_ids"].values()) | {self.adapter["network"]["id"]}:
            raise ValueError("A newly accepted distinct browser container ID is required")
        self.inspect(running=False)
        self.api.request("POST", "/containers/" + self.identifier + "/start", expected=204)
        self.kernel_boundary()
        self.verify_network(extra=self.identifier)
        new_file(self.evidence / "release.json", self.core.canonical({"run_id": self.run, "proposal_digest": self.manifest["proposal_digest"]}), uid=1000)
        self.record("harness_released", container_id=self.identifier)

    def wait(self):
        while True:
            self.remaining()
            state = self.inspect()["State"]
            if state.get("Running") is False:
                if type(state.get("ExitCode")) is not int or state["ExitCode"] != 0 or state.get("Status") != "exited":
                    raise RuntimeError("The frozen browser harness exited unsuccessfully")
                return
            time.sleep(min(1, self.remaining(1)))

    def accept(self):
        launch = self.core.strict_json(self.core.owned_bytes(self.evidence / "launch-result.json", 16384, uid=1000, mode=0o600))
        if self.core.canonical(launch) != self.core.canonical({"schema_version": 1, "run_id": self.run, "exit_code": 0, "status": "completed"}):
            raise ValueError("The frozen launcher did not finish successfully")
        path = self.evidence / "harness"
        result_bytes = self.core.owned_bytes(path / "sanitized-results.json", 65536, uid=1000, mode=0o600)
        private_ledger = self.core.owned_bytes(path / "private-ledger.jsonl", 65536, uid=1000, mode=0o600)
        verified = result_contract(self.core, self.core.strict_json(result_bytes), private_ledger)
        self.record("browser_acceptance_verified", **verified)
        summary = {"schema_version": 1, "phase": "B2-browser", "run_id": self.run, "b2_id": self.phase,
                   "status": "all_frozen_cases_verified", "runtime_pending": False, "container_id": self.identifier,
                   "proposal_digest": self.manifest["proposal_digest"], "source_tree_sha256": self.adapter["source_tree_sha256"],
                   "image_id": self.core.IMAGE_CONFIG, "sso_login_verified": True, "jwt_runtime_verified": True,
                   "automatic_retry": False, "automatic_cleanup": False, **verified}
        new_file(self.evidence / "browser-summary.json", self.core.canonical(summary))
        return summary

    def execute(self):
        try:
            self.preflight()
            self.begin()
            self.load_image()
            self.materialize()
            self.launch()
            self.wait()
            return self.accept()
        except Exception:
            if self.ledger is not None:
                self.record("execution_stopped", container_id=self.identifier, automatic_retry=False, automatic_cleanup=False)
            raise
        finally:
            if self.ledger is not None:
                self.ledger.close()
