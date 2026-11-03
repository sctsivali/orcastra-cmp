"""A guest in memory for the credentials tests: it answers the package's scripts and commands.

It keeps passwd, the shadow summary, the sudo group and each account's authorized_keys, and records
every call with its argv, the uid it ran as and its stdin, so a test can say where a password went
and who touched a key file. The real scripts are exercised against real shells in
test_the_guest_scripts_are_fixed_and_never_interpolate.py; this fake checks the Python around them.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import AuthenticatedUser, get_current_user
from app.services.guest_credentials.pinned_exec import PinnedExecResult
from app.services.guest_credentials.transport import Output


@dataclass
class Call:
    argv: List[str]
    user: Optional[int]
    group: Optional[int]
    stdin: Optional[bytes]


@dataclass
class FakeVM:
    family: str = "shadow"
    os_id: str = "ubuntu"
    selinux: bool = False
    tools: set = field(default_factory=lambda: {
        "useradd", "usermod", "gpasswd", "chpasswd", "chage", "passwd", "sudo", "flock", "sha256sum",
        "base64", "stat", "mktemp", "awk", "bash", "sshd", "systemctl", "restorecon",
    })
    passwd: Dict[str, tuple] = field(default_factory=lambda: {
        "root": (0, 0, "/root", "/bin/bash"),
        "daemon": (1, 1, "/usr/sbin", "/usr/sbin/nologin"),
        "ubuntu": (1000, 1000, "/home/ubuntu", "/bin/bash"),
        "nobody": (65534, 65534, "/nonexistent", "/usr/sbin/nologin"),
    })
    shadow: Dict[str, str] = field(default_factory=lambda: {"root": "none", "daemon": "none", "ubuntu": "none"})
    must_change: set = field(default_factory=set)
    sudo_members: set = field(default_factory=lambda: {"ubuntu"})
    keys: Dict[int, bytes] = field(default_factory=dict)
    calls: List[Call] = field(default_factory=list)
    before_write: Optional[Callable[["FakeVM"], None]] = None
    chpasswd_exit: int = 0
    chpasswd_stderr: str = ""

    def _inventory(self) -> str:
        lines = [f"tool={t}" for t in sorted(self.tools)]
        if self.family == "busybox":
            lines.append("busybox=yes")
        if self.selinux:
            lines.append("selinux=enabled")
        lines += ["ORCA_BEGIN os-release", f'ID={self.os_id}', f'PRETTY_NAME="{self.os_id.title()} test"', "ORCA_END"]
        lines += ["ORCA_BEGIN login.defs", "UID_MIN 1000", "UID_MAX 60000", "ENCRYPT_METHOD YESCRYPT", "ORCA_END"]
        lines.append("ORCA_BEGIN passwd")
        for name, (uid, gid, home, shell) in self.passwd.items():
            lines.append(f"{name}:x:{uid}:{gid}::{home}:{shell}")
        lines += ["ORCA_END", "ORCA_BEGIN groups"]
        group = "sudo" if self.os_id in ("ubuntu", "debian") else "wheel"
        lines.append(f"{group}:x:27:{','.join(sorted(self.sudo_members))}")
        lines += ["ORCA_END", "ORCA_BEGIN shadow"]
        for name, state in self.shadow.items():
            scheme = "y" if state in ("set", "locked") else ""
            lines.append(f"{name}:{state}:{scheme}:{'0' if name in self.must_change else '19000'}")
        lines.append("ORCA_END")
        return "\n".join(lines) + "\n"

    def _keys_read(self, home: str, uid: int, user: Optional[int]) -> str:
        if user != uid:
            return "ORCA_FAIL wrong_identity\n"
        content = self.keys.get(uid)
        if content is None:
            return "ORCA_HOME_MODE 750\nORCA_STATE absent\n"
        return (
            "ORCA_HOME_MODE 750\nORCA_SSH_MODE 700\nORCA_FILE_MODE 600 %d\nORCA_STATE present\nORCA_BODY\n%s\n"
            % (uid, base64.encodebytes(content).decode())
        )

    def _keys_write(self, home: str, uid: int, expected: str, stdin: bytes, user: Optional[int]) -> str:
        if user != uid:
            return "ORCA_FAIL wrong_identity\n"
        if self.before_write is not None:
            hook, self.before_write = self.before_write, None
            hook(self)
        current = self.keys.get(uid)
        token = hashlib.sha256(current).hexdigest() if current is not None else "absent"
        if token != expected:
            return "ORCA_FAIL conflict\n"
        self.keys[uid] = stdin
        return f"ORCA_OK {hashlib.sha256(stdin).hexdigest()} {home}/.ssh\n"

    def run(self, argv: List[str], user: Optional[int], group: Optional[int], stdin: Optional[bytes]) -> tuple:
        self.calls.append(Call(list(argv), user, group, stdin))
        if argv[:2] == ["/bin/sh", "-c"]:
            name, args = argv[3][len("orcastra-"):], argv[4:]
            if name == "inventory":
                return self._inventory(), 0
            if name == "sudo-verdicts":
                return "".join(f"ORCA_SUDO {n} {'yes' if n in self.sudo_members else 'no'}\n" for n in args), 0
            if name == "keys-read":
                return self._keys_read(args[0], int(args[1]), user), 0
            if name == "keys-write":
                return self._keys_write(args[0], int(args[1]), args[2], stdin or b"", user), 0
            if name == "sshd-facts":
                # Alpine's OpenSSH is built without PAM, so sshd -G prints no usepam line there.
                pam = "" if self.family == "busybox" else "eff=usepam yes\n"
                return "installed=yes\nprocess=yes\neff=passwordauthentication no\neff=permitrootlogin prohibit-password\n" \
                       "eff=authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2\n" + pam, 0
            if name == "restorecon":
                return "", 0
            raise AssertionError(f"unknown script {name}")
        tool = argv[0]
        if tool == "useradd":
            name = argv[-1]
            uid = max(u for u, *_ in self.passwd.values() if u < 60000) + 1
            self.passwd[name] = (uid, uid, f"/home/{name}", argv[argv.index("-s") + 1])
            self.shadow[name] = "none"
            return "", 0
        if tool == "usermod" and "-G" in argv:
            self.sudo_members.add(argv[-1])
            return "", 0
        if tool == "gpasswd":
            self.sudo_members.discard(argv[2])
            return "", 0
        if tool == "usermod" and "-L" in argv:
            self.shadow[argv[-1]] = "locked"
            return "", 0
        if tool == "chpasswd":
            if self.chpasswd_exit:
                return self.chpasswd_stderr, self.chpasswd_exit
            name = (stdin or b"").decode().split(":", 1)[0]
            self.shadow[name] = "set"
            return "", 0
        if tool == "chage":
            self.must_change.add(argv[-1])
            return "", 0
        if tool == "passwd" and "-l" in argv:
            self.shadow[argv[-1]] = "locked"
            return "", 0
        raise AssertionError(f"unexpected command {argv}")


class FakeGuest:
    def __init__(self, vm: FakeVM, instance_type: str = "container") -> None:
        self.vm = vm
        self.host, self.project, self.name = "lab", "default", "qa-cred"
        self.instance_type = instance_type
        self.state: Dict[str, Any] = {"network": {
            "eth0": {"addresses": [{"family": "inet", "address": "10.66.3.12", "scope": "global"},
                                   {"family": "inet6", "address": "fe80::1", "scope": "link"}]},
            "lo": {"addresses": [{"family": "inet", "address": "127.0.0.1", "scope": "local"}]},
        }}

    async def read(self, argv, *, user=None, group=None, limit=0, label="read") -> Output:
        stdout, code = self.vm.run(argv, user, group, None)
        return Output(stdout, "", code, False)

    async def with_stdin(self, argv, stdin, *, user=None, group=None, label="write") -> PinnedExecResult:
        stdout, code = self.vm.run(argv, user, group, stdin)
        return PinnedExecResult(stdout if code == 0 else "", stdout if code else "", code)


class Audit:
    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    def log_activity(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


def client(monkeypatch, vm: FakeVM, *, groups=("role_admin",), grant_id=None) -> tuple:
    """A TestClient on the credentials router with the guest replaced by `vm`."""
    from contextlib import asynccontextmanager

    from app.routers import guest_credentials as route
    from app.routers import instance_files
    from app.core.org_scope import org_scope_dependency
    from app.services.guest_credentials import gates
    from app.services.instance_files import audit as file_audit

    audit = Audit()
    monkeypatch.setattr(file_audit, "activity_logger", audit)

    async def no_org(host):
        return None, None

    monkeypatch.setattr(file_audit, "_org_of", no_org)
    monkeypatch.setattr(route, "verify_guest_access", lambda host, project, instance: project or "default")
    guest = FakeGuest(vm)

    @asynccontextmanager
    async def admitted(user, host, project, instance, *, kind, record=None, route=""):
        from app.services.instance_files.session import http_failures

        async with http_failures():
            gates.spend(user, host, project, instance, kind)
            if record is not None:
                record["break_glass_grant_id"] = grant_id
            yield guest

    monkeypatch.setattr(route, "admitted", admitted)
    monkeypatch.setattr(gates, "_shared_allow", lambda *args: None)
    monkeypatch.setattr(gates, "_budget", {})
    from app.services.guest_credentials import audit as cred_audit

    monkeypatch.setattr(cred_audit, "_viewed", {})
    app = FastAPI()
    app.include_router(route.router)

    async def nothing():
        return None

    app.dependency_overrides[org_scope_dependency] = nothing
    app.dependency_overrides[instance_files.release_request_db] = nothing
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="qa-1", email="qa@example.test", groups=list(groups))
    return TestClient(app, raise_server_exceptions=False), audit, guest


QUERY = "?host=lab&project=default&instance_name=qa-cred"
