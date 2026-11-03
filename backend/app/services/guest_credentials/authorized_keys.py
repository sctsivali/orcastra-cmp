"""One account's authorized_keys: read, add one key, remove one key, without touching other lines.

The file is bytes. Lines this panel did not add (comments, options, certificates, a key type it
does not know, CRLF endings) go back exactly as they came. An edit is made against the content it
read and written only if the file still holds that content (`KEYS_WRITE` compares sha256), so a
change made in the guest meanwhile is never lost: the intent is re-applied once to the new
content, and a second conflict is reported rather than forced.

Both scripts run as the account itself, so a link the account planted reaches only what it could
already read or write. See `scripts.py`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import errors, runtime, scripts, ssh_keys
from .inventory import Account, GuestFacts
from .transport import Guest

_FAILURES: Dict[str, Callable[[], errors.CredentialError]] = {
    "home_missing": errors.home_not_owned,
    "home_not_owned": errors.home_not_owned,
    "ssh_dir_not_owned": errors.ssh_dir_not_owned,
    "ssh_dir_create": errors.ssh_dir_not_owned,
    "key_file_is_link": errors.key_file_is_link,
    "key_file_not_regular": errors.key_file_not_regular,
    "locked": errors.busy,
    "wrong_identity": errors.guest_answer_invalid,
}


@dataclass
class Entry:
    line: int
    key_type: Optional[str]
    fingerprint: Optional[str]
    bits: Optional[int]
    comment: str
    options: List[str]
    weak: bool


@dataclass
class KeyFile:
    state: str
    content: bytes = b""
    modes: Dict[str, str] = field(default_factory=dict)
    entries: List[Entry] = field(default_factory=list)

    @property
    def token(self) -> str:
        return hashlib.sha256(self.content).hexdigest() if self.state == "present" else "absent"

    @property
    def warnings(self) -> List[str]:
        found: List[str] = []
        if _octal(self.modes.get("home")) & 0o022:
            found.append("home_writable_by_others")
        if _octal(self.modes.get("file")) & 0o022:
            found.append("key_file_writable_by_others")
        return found


def _octal(mode: Optional[str]) -> int:
    try:
        return int(mode or "0", 8)
    except ValueError:
        return 0


def _split_options(text: str) -> Tuple[str, str]:
    quoted = escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char in " \t" and not quoted:
            return text[:index], text[index:].lstrip(" \t")
    return text, ""


def _option_names(options: str) -> List[str]:
    names: List[str] = []
    current, quoted, escaped = "", False, False
    for char in options + ",":
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            name = current.split("=", 1)[0].strip().lower()
            if name and len(name) <= 64 and name.replace("-", "").isalnum():
                names.append(name)
            current = ""
            continue
        current += char
    return names


def _is_type(token: str) -> bool:
    return token in ssh_keys.ACCEPTED_TYPES or token == "ssh-dss" or token.startswith(("ssh-", "ecdsa-", "sk-"))


def parse_entry(raw: bytes, number: int) -> Optional[Entry]:
    text = raw.decode("utf-8", "replace").strip()
    if not text or text.startswith("#"):
        return None
    options: List[str] = []
    first = text.split(None, 1)[0]
    if not _is_type(first):
        prefix, text = _split_options(text)
        options = _option_names(prefix)
    tokens = text.split()
    if len(tokens) < 2 or not _is_type(tokens[0]):
        return Entry(number, None, None, None, "", options, False)
    try:
        blob = base64.b64decode(tokens[1], validate=True)
    except (binascii.Error, ValueError):
        return Entry(number, tokens[0], None, None, "", options, False)
    bits = ssh_keys.bits_of(tokens[0], blob)
    weak = tokens[0] == "ssh-dss" or (tokens[0] == "ssh-rsa" and bits is not None and bits < ssh_keys.RSA_MIN_BITS)
    # The guest wrote this comment: control, format (bidi, zero-width) and separator characters are
    # dropped before it is shown, so a confirmation cannot be made to read as something else.
    comment = ssh_keys.displayable(" ".join(tokens[2:]))[: ssh_keys.MAX_COMMENT_CHARS]
    return Entry(number, tokens[0], ssh_keys.fingerprint(blob), bits, comment, options, weak)


def _lines(content: bytes) -> List[bytes]:
    return content.splitlines(keepends=True)


def entries_of(content: bytes) -> List[Entry]:
    parsed = (parse_entry(raw, number) for number, raw in enumerate(_lines(content), start=1))
    return [entry for entry in parsed if entry is not None]


def _parse_read(stdout: str) -> KeyFile:
    lines = stdout.splitlines()
    modes: Dict[str, str] = {}
    state: Optional[str] = None
    body: List[str] = []
    in_body = False
    for line in lines:
        if in_body:
            body.append(line.strip())
            continue
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "ORCA_FAIL" and len(parts) == 2:
            raise _FAILURES.get(parts[1], errors.guest_answer_invalid)()
        if parts[0] == "ORCA_HOME_MODE" and len(parts) == 2:
            modes["home"] = parts[1]
        elif parts[0] == "ORCA_SSH_MODE" and len(parts) == 2:
            modes["ssh"] = parts[1]
        elif parts[0] == "ORCA_FILE_MODE" and len(parts) == 3:
            modes["file"] = parts[1]
        elif parts[0] == "ORCA_STATE" and len(parts) == 2:
            state = parts[1]
        elif parts[0] == "ORCA_BODY":
            in_body = True
    if state not in ("present", "absent", "link", "not_regular", "unreadable"):
        raise errors.guest_answer_invalid()
    if state != "present":
        return KeyFile(state=state, modes=modes)
    try:
        content = base64.b64decode("".join(body), validate=True)
    except (binascii.Error, ValueError):
        raise errors.guest_answer_invalid() from None
    if len(content) > runtime.KEY_FILE_LIMIT:
        raise errors.key_file_too_large(runtime.KEY_FILE_LIMIT)
    return KeyFile(state="present", content=content, modes=modes, entries=entries_of(content))


async def read(guest: Guest, account: Account) -> KeyFile:
    output = await guest.read(
        scripts.argv("keys-read", account.home, str(account.uid)),
        user=account.uid, group=account.gid, label="keys_read",
    )
    if output.truncated:
        raise errors.key_file_too_large(runtime.KEY_FILE_LIMIT)
    return _parse_read(output.stdout)


def with_key(content: bytes, key: ssh_keys.PublicKey) -> bytes:
    ending = b"\r\n" if content.endswith(b"\r\n") else b"\n"
    if content and not content.endswith(b"\n"):
        content += ending
    return content + key.line().encode("utf-8") + ending


def without_fingerprint(content: bytes, fingerprint: str) -> Tuple[bytes, int]:
    kept: List[bytes] = []
    removed = 0
    for number, raw in enumerate(_lines(content), start=1):
        entry = parse_entry(raw, number)
        if entry is not None and entry.fingerprint == fingerprint:
            removed += 1
            continue
        kept.append(raw)
    return b"".join(kept), removed


def _usable(current: KeyFile) -> None:
    if current.state == "link":
        raise errors.key_file_is_link()
    if current.state in ("not_regular", "unreadable"):
        raise errors.key_file_not_regular()


async def _write(guest: Guest, facts: GuestFacts, account: Account, token: str, content: bytes) -> str:
    if len(content) > runtime.KEY_FILE_LIMIT:
        raise errors.key_file_too_large(runtime.KEY_FILE_LIMIT)
    result = await guest.with_stdin(
        scripts.argv("keys-write", account.home, str(account.uid), token), content,
        user=account.uid, group=account.gid, label="keys_write",
    )
    for line in result.stdout.splitlines():
        parts = line.split(" ", 2)
        if parts[0] == "ORCA_FAIL" and len(parts) == 2:
            if parts[1] == "conflict":
                return "conflict"
            raise _FAILURES.get(parts[1], lambda: errors.guest_failed("key_write_failed"))()
        if parts[0] == "ORCA_OK" and len(parts) == 3:
            if parts[1] != hashlib.sha256(content).hexdigest():
                raise errors.outcome_unknown()
            await _relabel(guest, facts, account)
            return "written"
    raise errors.outcome_unknown() if result.exit_code == 0 else errors.guest_failed("key_write_failed")


async def _relabel(guest: Guest, facts: GuestFacts, account: Account) -> None:
    # A .ssh made by hand on an SELinux guest gets the wrong label and sshd ignores the key.
    # important: the path comes from /etc/passwd, never from what the account's script printed,
    # because this step runs as root (see RESTORECON in scripts.py).
    if not facts.selinux:
        return
    await guest.read(scripts.argv("restorecon", account.home, str(account.uid)), label="keys_relabel", limit=4096)


async def edit(
    guest: Guest, facts: GuestFacts, account: Account, change: Callable[[KeyFile], Optional[bytes]],
) -> Tuple[str, KeyFile]:
    """Applies `change` to the current file; `change` returns None when nothing needs writing."""
    for _attempt in range(2):
        current = await read(guest, account)
        _usable(current)
        content = change(current)
        if content is None:
            return "unchanged", current
        outcome = await _write(guest, facts, account, current.token, content)
        if outcome == "written":
            return "written", KeyFile("present", content, current.modes, entries_of(content))
    raise errors.edit_conflict()
