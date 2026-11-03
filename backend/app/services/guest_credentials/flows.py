"""What each route does once a guest is admitted: read the facts, change one thing, report.

Validation that needs no guest (a name's shape, the password policy, a key's syntax) runs in
`prevalidate_*` before admission, so a malformed request never costs a guest round trip or a slot.
Checks that need the guest (the account exists, the tool exists, the guest's hash scheme) run here,
before the first change.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import accounts, authorized_keys, errors, inventory, passwords, scripts, sshd, ssh_keys
from .inventory import Account, GuestFacts
from .transport import Guest

_KEY_MESSAGES = {
    "private_key_pasted": (
        "This is a private key. It was not saved or sent to the guest. Paste the public key (the .pub "
        "file) instead, and treat this private key as exposed if it was pasted anywhere else."
    ),
}
_PASSWORD_MESSAGES = {
    "password_too_short": f"Use at least {passwords.MIN_LENGTH} characters.",
    "password_too_long": f"Use at most {passwords.MAX_LENGTH} characters.",
    "password_not_printable_ascii": "Use letters, digits, spaces and symbols found on a US keyboard.",
    "password_common": "This password appears in lists of common passwords. Choose another.",
    "password_repetitive": "This password repeats itself too much. Choose another.",
    "password_sequential": "This password is a keyboard or alphabet sequence. Choose another.",
    "password_contains_context": "This password is mostly the account or instance name. Choose another.",
    "password_too_long_for_guest": f"This guest's password scheme reads only {passwords.BCRYPT_MAX_BYTES} characters.",
    "guest_hash_unsupported": "This guest stores passwords with DES, which this panel will not use.",
    "password_invalid": "This password cannot be used.",
}


def key_or_refuse(text: str) -> ssh_keys.PublicKey:
    try:
        return ssh_keys.parse_public_key(text)
    except ssh_keys.KeyRejected as rejected:
        if rejected.reason == "private_key_pasted":
            raise errors.refuse("private_key_pasted", 422, _KEY_MESSAGES["private_key_pasted"]) from None
        raise errors.refuse(
            "invalid_public_key", 422, "This is not a public key this panel accepts.", {"reason": rejected.reason}
        ) from None


def password_or_refuse(password: str, *, context: List[str], hash_scheme: Optional[str] = None) -> None:
    try:
        passwords.check(password, context=context, hash_scheme=hash_scheme)
    except passwords.PasswordRejected as rejected:
        raise errors.refuse(rejected.reason, 422, _PASSWORD_MESSAGES.get(rejected.reason, "This password cannot be used.")) from None


def name_or_refuse(name: str) -> None:
    if not accounts.NEW_NAME.match(name) or name.endswith("-"):
        raise errors.refuse("invalid_username", 422, "Use 1 to 32 lowercase letters, digits, - or _, starting with a letter.")


async def facts_of(guest: Guest, *, with_sudo: bool = False) -> GuestFacts:
    output = await guest.read(scripts.argv("inventory"), label="inventory")
    if output.truncated:
        raise errors.guest_answer_invalid()
    if "ORCA_BEGIN passwd" not in output.stdout:
        raise errors.guest_failed()
    facts = inventory.parse(output.stdout)
    if with_sudo:
        names = [account.name for account in facts.accounts if account.kind == "human"]
        if "sudo" not in facts.tools:
            for account in facts.accounts:
                account.sudo = "absent"
        elif names:
            verdicts = await guest.read(scripts.argv("sudo-verdicts", *names), label="sudo_verdicts", limit=64 * 1024)
            inventory.apply_sudo(facts, verdicts.stdout)
    return facts


def account_or_refuse(facts: GuestFacts, name: str) -> Account:
    account = facts.account(name)
    if account is None:
        raise errors.user_not_found()
    return account


def account_view(account: Account) -> Dict[str, Any]:
    return {
        "name": account.name, "uid": account.uid, "kind": account.kind, "home": account.home,
        "shell": account.shell, "password": account.password, "must_change": account.must_change,
        "scheme": account.scheme, "admin_groups": account.admin_groups, "sudo": account.sudo,
    }


def overview(guest: Guest, facts: GuestFacts) -> Dict[str, Any]:
    tools = facts.tools
    sudo_change_tools = ("addgroup", "delgroup") if facts.family == "busybox" else ("usermod", "gpasswd")
    return {
        "guest": {
            "os_id": facts.os_id, "os_name": facts.os_name, "family": facts.family, "selinux": facts.selinux,
            "hash_default": facts.hash_default, "instance_type": guest.instance_type,
        },
        "accounts": [account_view(account) for account in facts.accounts],
        "accounts_omitted": facts.accounts_omitted,
        "capabilities": {
            "create_user": ("adduser" if facts.family == "busybox" else "useradd") in tools,
            "set_password": "chpasswd" in tools,
            "expire": accounts.can_expire(facts),
            "lock": "usermod" in tools or "passwd" in tools,
            "sudo_group": facts.sudo_group,
            "sudo_installed": "sudo" in tools,
            "sudo_change": facts.sudo_group is not None and all(t in tools for t in sudo_change_tools),
        },
        "password_policy": {"min_length": passwords.MIN_LENGTH, "max_length": passwords.MAX_LENGTH},
    }


async def ssh_server(guest: Guest) -> Dict[str, Any]:
    output = await guest.read(scripts.argv("sshd-facts"), label="sshd_facts", limit=128 * 1024)
    facts = sshd.parse(output.stdout)
    facts["addresses"] = sshd.addresses(guest.state)
    return facts


def entry_view(entry: authorized_keys.Entry) -> Dict[str, Any]:
    return {
        "line": entry.line, "key_type": entry.key_type, "fingerprint": entry.fingerprint, "bits": entry.bits,
        "comment": entry.comment, "options": entry.options, "weak": entry.weak,
    }


async def add_key(guest: Guest, facts: GuestFacts, account: Account, key: ssh_keys.PublicKey,
                  record: Dict[str, Any]) -> Dict[str, Any]:
    def change(current: authorized_keys.KeyFile) -> Optional[bytes]:
        if any(entry.fingerprint == key.fingerprint for entry in current.entries):
            return None
        return authorized_keys.with_key(current.content, key)

    outcome, keyfile = await authorized_keys.edit(guest, facts, account, change)
    record["outcome"] = outcome
    entry = next(entry for entry in keyfile.entries if entry.fingerprint == key.fingerprint)
    return {
        "added": outcome == "written", "already_present": outcome == "unchanged", "entry": entry_view(entry),
        "warnings": [*key.warnings, *keyfile.warnings],
    }


async def remove_key(guest: Guest, facts: GuestFacts, account: Account, fingerprint: str,
                     record: Dict[str, Any]) -> Dict[str, Any]:
    removed: List[int] = []

    def change(current: authorized_keys.KeyFile) -> Optional[bytes]:
        content, count = authorized_keys.without_fingerprint(current.content, fingerprint)
        if count == 0:
            raise errors.key_not_found()
        removed[:] = [count]
        return content

    await authorized_keys.edit(guest, facts, account, change)
    record["removed"] = removed[0]
    return {"removed": removed[0]}


async def set_password(guest: Guest, facts: GuestFacts, account: Account, password: str, must_change: bool,
                       instance: str) -> Dict[str, Any]:
    password_or_refuse(password, context=[account.name, instance], hash_scheme=facts.hash_default)
    if must_change and not accounts.can_expire(facts):
        raise errors.refuse("expire_unsupported", 409, "This guest cannot require a password change at next login.")
    await accounts.set_password(guest, facts, account, password)
    if must_change:
        await accounts.expire(guest, facts, account)
    return {"user": account.name, "password": "set", "must_change_applied": must_change, "warnings": []}


async def lock_warnings(guest: Guest) -> List[str]:
    """What a lock means for SSH on this guest, read from its sshd rather than assumed.

    important: OpenSSH without PAM (Alpine's build) refuses every login of an account whose
    password is locked, keys included; with PAM, key logins keep working. Measured live on
    2026-11-03: Alpine 3.22 answered "Permission denied (publickey,...)" after a lock, Ubuntu
    24.04 let the same key in.
    """
    facts = await ssh_server(guest)
    if not facts["installed"]:
        return []
    effective = facts.get("effective")
    if effective is None:
        return ["key_logins_may_still_work"]
    if str(effective.get("usepam", "")).lower() == "yes":
        return ["key_logins_still_work"]
    return ["key_logins_blocked_too"]


async def sudo_verdict(guest: Guest, facts: GuestFacts, account: Account) -> Optional[str]:
    if "sudo" not in facts.tools:
        return "absent"
    output = await guest.read(scripts.argv("sudo-verdicts", account.name), label="sudo_verdicts", limit=4096)
    inventory.apply_sudo(facts, output.stdout)
    return account.sudo


async def create_user(guest: Guest, facts: GuestFacts, name: str, *, sudo: bool, password: Optional[str],
                      must_change: bool, key: Optional[ssh_keys.PublicKey], instance: str) -> Dict[str, Any]:
    accounts.validate_new_name(name, facts)
    if password is not None:
        password_or_refuse(password, context=[name, instance], hash_scheme=facts.hash_default)
        if "chpasswd" not in facts.tools:
            raise errors.tool_missing("chpasswd")
        if must_change and not accounts.can_expire(facts):
            raise errors.refuse("expire_unsupported", 409, "This guest cannot require a password change at next login.")
    if sudo and facts.sudo_group is None:
        raise errors.refuse("sudo_group_missing", 409, "This guest has neither a sudo nor a wheel group.")

    await accounts.create(guest, facts, name)
    steps: List[Dict[str, Any]] = [{"step": "create", "result": "ok"}]
    facts = await facts_of(guest)
    account = facts.account(name)
    if account is None:
        # useradd succeeded but the account is not one this panel manages (no home, odd uid range).
        steps.append({"step": "read_back", "result": "failed", "code": "user_not_manageable"})
        return {"account": None, "steps": steps}

    async def step(label: str, action: Any) -> None:
        try:
            outcome = await action()
            steps.append({"step": label, "result": outcome or "ok"})
        except errors.CredentialError as failure:
            steps.append({"step": label, "result": "failed", "code": failure.code})

    if sudo:
        async def grant() -> str:
            await accounts.set_sudo(guest, facts, account, True)
            verdict = await sudo_verdict(guest, facts, account)
            return "ok" if verdict == "yes" else "not_effective"
        await step("sudo", grant)
    if password is not None:
        async def set_it() -> None:
            await accounts.set_password(guest, facts, account, password)
        await step("password", set_it)
        if must_change and steps[-1]["result"] == "ok":
            async def expire_it() -> None:
                await accounts.expire(guest, facts, account)
            await step("expire", expire_it)
    if key is not None:
        async def add_it() -> None:
            await add_key(guest, facts, account, key, {})
        await step("key", add_it)

    facts = await facts_of(guest, with_sudo=True)
    final = facts.account(name) or account
    return {"account": account_view(final), "steps": steps}
