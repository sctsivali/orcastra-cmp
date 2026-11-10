"""Account changes, as argv for the tool family a guest has: shadow-utils or busybox.

Each change is one plain argv with no shell, run as root through `record-output`, except the
password, which goes on chpasswd's stdin through the pinned exec (`pinned_exec.py`). Names reach
argv only after `NEW_NAME` or `inventory.EXISTING_NAME` accepted them, and none may start with a
dash, so no name can be read as an option.

A non-zero exit is reported by code. A tool's stderr is matched as a hint here and never returned
or logged, because a guest writes it.
"""

from __future__ import annotations

import re
from typing import List, Optional

from . import errors
from .inventory import Account, GuestFacts
from .passwords import chpasswd_stdin
from .transport import Guest

# New accounts: the portable subset useradd and busybox adduser both accept by default.
NEW_NAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
RESERVED = frozenset({
    "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail", "news", "uucp", "proxy",
    "www-data", "backup", "list", "irc", "gnats", "nobody", "nogroup", "sshd", "messagebus", "syslog",
    "adm", "admin", "sudo", "wheel", "shadow", "staff", "users", "operator", "halt", "shutdown",
    "postfix", "polkitd", "dbus", "ftp", "lxd", "incus", "docker", "systemd-network", "systemd-resolve",
    "systemd-timesync", "systemd-journal", "_apt", "tss", "uuidd", "tcpdump", "landscape", "pollinate",
    "fwupd-refresh", "chrony", "rpc", "rpcuser", "nfsnobody", "ntp", "avahi", "colord", "geoclue",
    "cups", "squid", "apache", "nginx", "mysql", "postgres", "redis", "guest", "orcastra",
})


def validate_new_name(name: str, facts: GuestFacts) -> None:
    if not isinstance(name, str) or not NEW_NAME.match(name) or name.endswith("-"):
        raise errors.refuse("invalid_username", 422, "Use 1 to 32 lowercase letters, digits, - or _, starting with a letter.")
    if name in RESERVED or name.startswith("systemd-"):
        raise errors.refuse("reserved_username", 422, "This name is reserved for the system. Choose another.")
    if facts.account(name) is not None:
        raise errors.user_exists()


def login_shell(facts: GuestFacts) -> str:
    return "/bin/bash" if "bash" in facts.tools else "/bin/sh"


async def _run(guest: Guest, argv: List[str], label: str) -> int:
    output = await guest.read(argv, label=label, limit=16 * 1024)
    return output.exit_code


async def create(guest: Guest, facts: GuestFacts, name: str) -> None:
    if facts.family == "busybox":
        if "adduser" not in facts.tools:
            raise errors.tool_missing("adduser")
        argv = ["adduser", "-D", "-s", login_shell(facts), name]
    else:
        if "useradd" not in facts.tools:
            raise errors.tool_missing("useradd")
        argv = ["useradd", "-m", "-U", "-s", login_shell(facts), "--", name]
    code = await _run(guest, argv, "user_create")
    # useradd answers 9 when the name or its group is taken, which the inventory cannot see for a
    # group without a passwd entry. busybox has no distinct code; its existence check came first.
    if code == 9:
        raise errors.user_exists()
    if code != 0:
        raise errors.guest_failed("user_create_failed")


async def set_sudo(guest: Guest, facts: GuestFacts, account: Account, enabled: bool) -> None:
    group = facts.sudo_group
    if group is None:
        raise errors.refuse("sudo_group_missing", 409, "This guest has neither a sudo nor a wheel group.")
    if enabled:
        if facts.family == "busybox":
            argv = ["addgroup", account.name, group]
            tool = "addgroup"
        else:
            argv = ["usermod", "-a", "-G", group, "--", account.name]
            tool = "usermod"
    else:
        if facts.family == "busybox":
            # busybox 1.37 `deluser` takes only a user; `delgroup USER GROUP` removes a membership.
            argv = ["delgroup", account.name, group]
            tool = "delgroup"
        else:
            argv = ["gpasswd", "-d", account.name, group]
            tool = "gpasswd"
    if tool not in facts.tools:
        raise errors.tool_missing(tool)
    code = await _run(guest, argv, "user_sudo")
    # gpasswd answers 3 for "not a member": already the state asked for.
    if code != 0 and not (not enabled and tool == "gpasswd" and code == 3):
        raise errors.guest_failed("user_sudo_failed")


async def set_password(guest: Guest, facts: GuestFacts, account: Account, password: str) -> None:
    if "chpasswd" not in facts.tools:
        raise errors.tool_missing("chpasswd")
    argv = ["chpasswd", "-c", "sha512"] if facts.family == "busybox" else ["chpasswd"]
    result = await guest.with_stdin(argv, chpasswd_stdin(account.name, password), label="password_set")
    if result.exit_code != 0:
        lowered = (result.stderr + result.stdout).lower()
        # important: a PAM quality module (pwquality, cracklib) refused it. Its message is
        # guest-written, so only the fact is returned, never the text.
        if "bad password" in lowered or "pam_chauthtok" in lowered or "authentication token manipulation" in lowered:
            raise errors.refuse(
                "password_rejected_by_guest", 422,
                "The guest's own password policy refused this password. Try a longer or different one.",
            )
        raise errors.guest_failed("password_set_failed")


def can_expire(facts: GuestFacts) -> bool:
    return "chage" in facts.tools or ("passwd" in facts.tools and not facts.busybox)


async def expire(guest: Guest, facts: GuestFacts, account: Account) -> None:
    if "chage" in facts.tools:
        argv = ["chage", "-d", "0", account.name]
    elif can_expire(facts):
        argv = ["passwd", "-e", account.name]
    else:
        raise errors.tool_missing("chage")
    if await _run(guest, argv, "password_expire") != 0:
        raise errors.guest_failed("password_expire_failed")


async def lock(guest: Guest, facts: GuestFacts, account: Account) -> None:
    if "usermod" in facts.tools and facts.family == "shadow":
        argv = ["usermod", "-L", "--", account.name]
    elif "passwd" in facts.tools:
        argv = ["passwd", "-l", account.name]
    else:
        raise errors.tool_missing("passwd")
    if await _run(guest, argv, "password_lock") != 0:
        raise errors.guest_failed("password_lock_failed")


def manageable(account: Optional[Account]) -> Account:
    if account is None:
        raise errors.user_not_found()
    return account
