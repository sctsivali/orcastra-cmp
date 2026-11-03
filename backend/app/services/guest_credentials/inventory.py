"""What the inventory script printed, as facts about the guest and the accounts a person logs in with.

Local accounts only, read from /etc/passwd and the shadow summary, never from `getent`: a directory
entry (LDAP, SSSD) can shadow a local one, and chpasswd and usermod change the local files. An
account the panel lists is therefore an account the panel's changes actually reach.

Everything here is guest output and untrusted: every field is validated, a malformed line is
dropped, and nothing guest-written is put in a log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import runtime

# A name an existing account may have and still be managed: no leading dash (it would read as an
# option), no colon or newline (chpasswd's separator), nothing a shell or path would reinterpret.
EXISTING_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}\$?$")
_NO_LOGIN = {"nologin", "false", "true", "sync", "shutdown", "halt", "git-shell"}
_SCHEMES = {
    "1": "md5", "2a": "bcrypt", "2b": "bcrypt", "2y": "bcrypt", "5": "sha256", "6": "sha512",
    "y": "yescrypt", "gy": "gost-yescrypt", "7": "scrypt", "des": "des",
}
_ENCRYPT_METHODS = {
    "SHA512": "sha512", "SHA256": "sha256", "YESCRYPT": "yescrypt", "MD5": "md5", "DES": "des",
    "BCRYPT": "bcrypt", "GOST_YESCRYPT": "gost-yescrypt",
}
_DEBIAN_FAMILY = {"debian", "ubuntu", "raspbian", "linuxmint", "pop", "kali", "proxmox"}


@dataclass
class Account:
    name: str
    uid: int
    gid: int
    home: str
    shell: str
    kind: str
    password: str = "unknown"
    must_change: bool = False
    scheme: Optional[str] = None
    admin_groups: List[str] = field(default_factory=list)
    sudo: Optional[str] = None


@dataclass
class GuestFacts:
    os_id: str
    os_like: List[str]
    os_name: str
    tools: Set[str]
    busybox: bool
    selinux: bool
    uid_min: int
    uid_max: int
    hash_default: Optional[str]
    group_members: Dict[str, List[str]]
    accounts: List[Account]
    accounts_omitted: int = 0

    @property
    def family(self) -> str:
        if self.busybox or ("useradd" not in self.tools and "adduser" in self.tools):
            return "busybox"
        return "shadow"

    @property
    def debian_like(self) -> bool:
        return bool({self.os_id, *self.os_like} & _DEBIAN_FAMILY)

    @property
    def sudo_group(self) -> Optional[str]:
        preferred = "sudo" if self.debian_like else "wheel"
        if preferred in self.group_members:
            return preferred
        other = "wheel" if preferred == "sudo" else "sudo"
        return other if other in self.group_members else None

    def account(self, name: str) -> Optional[Account]:
        return next((account for account in self.accounts if account.name == name), None)


def _blocks(stdout: str) -> Tuple[Dict[str, List[str]], List[str]]:
    blocks: Dict[str, List[str]] = {}
    loose: List[str] = []
    current: Optional[str] = None
    for line in stdout.splitlines():
        if line.startswith("ORCA_BEGIN "):
            current = line.split(" ", 1)[1].strip()
            blocks[current] = []
        elif line == "ORCA_END":
            current = None
        elif current is not None:
            blocks[current].append(line)
        else:
            loose.append(line)
    return blocks, loose


def _os_release(lines: List[str]) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[A-Z_]{1,32}", key):
            values[key] = value.strip().strip("\"'")[:200]
    return values


def _int(value: str, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if 0 <= number <= 2**32 else default


def _login_defs(lines: List[str]) -> Tuple[int, int, Optional[str]]:
    uid_min, uid_max, method = 1000, 60000, None
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        if parts[0] == "UID_MIN":
            uid_min = _int(parts[1], 1000)
        elif parts[0] == "UID_MAX":
            uid_max = _int(parts[1], 60000)
        elif parts[0] == "ENCRYPT_METHOD":
            method = _ENCRYPT_METHODS.get(parts[1].upper())
    return uid_min, uid_max, method


def _shadow(lines: List[str]) -> Dict[str, Tuple[str, Optional[str], Optional[str]]]:
    summary: Dict[str, Tuple[str, Optional[str], Optional[str]]] = {}
    for line in lines:
        parts = line.split(":")
        if len(parts) != 4 or parts[1] not in ("set", "none", "locked", "empty"):
            continue
        summary[parts[0]] = (parts[1], _SCHEMES.get(parts[2]) or (parts[2] and "other") or None, parts[3])
    return summary


def _groups(lines: List[str]) -> Dict[str, List[str]]:
    members: Dict[str, List[str]] = {}
    for line in lines:
        parts = line.split(":")
        if len(parts) == 4 and parts[0] in ("sudo", "wheel", "admin"):
            members[parts[0]] = [m for m in parts[3].split(",") if EXISTING_NAME.match(m)]
    return members


def _kind(name: str, uid: int, shell: str, uid_min: int, uid_max: int) -> Optional[str]:
    if uid == 0:
        return "root" if name == "root" else "root_equivalent"
    if not (uid_min <= uid <= uid_max) or uid == 65534:
        return None
    if not shell.startswith("/") or shell.rsplit("/", 1)[-1] in _NO_LOGIN:
        return None
    return "human"


def parse(stdout: str) -> GuestFacts:
    blocks, loose = _blocks(stdout)
    tools = {line.split("=", 1)[1] for line in loose if line.startswith("tool=")}
    release = _os_release(blocks.get("os-release", []))
    uid_min, uid_max, method = _login_defs(blocks.get("login.defs", []))
    busybox = "busybox=yes" in loose
    shadow = _shadow(blocks.get("shadow", []))
    groups = _groups(blocks.get("groups", []))

    accounts: List[Account] = []
    omitted = 0
    seen: Set[str] = set()
    for line in blocks.get("passwd", []):
        parts = line.split(":")
        if len(parts) != 7 or not EXISTING_NAME.match(parts[0]) or parts[0] in seen:
            continue
        name, _, uid_text, gid_text, _, home, shell = parts
        if not uid_text.isdigit() or not gid_text.isdigit():
            continue
        uid, gid = int(uid_text), int(gid_text)
        kind = _kind(name, uid, shell, uid_min, uid_max)
        if kind is None or not home.startswith("/") or len(home) > 4096 or any(ord(c) < 0x20 for c in home):
            continue
        if len(accounts) >= runtime.MAX_ACCOUNTS:
            omitted += 1
            continue
        seen.add(name)
        state, scheme, lastchg = shadow.get(name, ("unknown", None, None))
        accounts.append(Account(
            name=name, uid=uid, gid=gid, home=home, shell=shell, kind=kind,
            password=state, must_change=lastchg == "0" and state in ("set", "locked"), scheme=scheme,
            admin_groups=[group for group, members in sorted(groups.items()) if name in members],
        ))
    if busybox and method is None:
        method = "sha512"
    return GuestFacts(
        os_id=release.get("ID", "").lower(),
        os_like=release.get("ID_LIKE", "").lower().split(),
        os_name=release.get("PRETTY_NAME", "") or release.get("NAME", ""),
        tools=tools, busybox=busybox, selinux="selinux=enabled" in loose,
        uid_min=uid_min, uid_max=uid_max, hash_default=method,
        group_members=groups, accounts=accounts, accounts_omitted=omitted,
    )


def apply_sudo(facts: GuestFacts, stdout: str) -> None:
    """Folds the SUDO_VERDICTS output into the accounts it names."""
    if "ORCA_SUDO_ABSENT" in stdout.splitlines():
        for account in facts.accounts:
            account.sudo = "absent"
        return
    verdicts: Dict[str, str] = {}
    for line in stdout.splitlines():
        parts = line.split(" ")
        if len(parts) == 3 and parts[0] == "ORCA_SUDO" and parts[2] in ("yes", "no", "rootpw"):
            verdicts[parts[1]] = parts[2]
    for account in facts.accounts:
        account.sudo = verdicts.get(account.name)
