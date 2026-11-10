"""The rules a new guest password has to meet, after NIST SP 800-63B revision 4.

A guest login prompt (Serial, Graphics) is a single factor, and section 3.1.1.2 sets 15 characters
as the floor for a password used alone. Length resists an offline guess against a leaked
/etc/shadow; the same section forbids composition rules. Checked instead: length, a blocklist of
common passwords, the account and instance names, repeats, and alphabet, digit and keyboard runs.

important: printable ASCII only, departing from the standard's Unicode SHOULD. The password has to
be typed at a guest console with an unknown keymap, and a password that cannot be typed there is a
lockout.

Every refusal is a code. Nothing here returns, logs or formats the password.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import FrozenSet, Iterable, Optional

MIN_LENGTH = 15
MAX_LENGTH = 128
BCRYPT_MAX_BYTES = 72
MIN_REMAINDER = 10

_BLOCKLIST = Path(__file__).with_name("data") / "common_passwords_15plus.txt"
_SEQUENCES = (
    "abcdefghijklmnopqrstuvwxyz",
    "0123456789",
    "qwertyuiopasdfghjklzxcvbnm",
    "1qaz2wsx3edc4rfv5tgb6yhn7ujm8ik9ol0p",
)


class PasswordRejected(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@functools.lru_cache(maxsize=1)
def _common() -> FrozenSet[str]:
    with _BLOCKLIST.open(encoding="ascii") as handle:
        return frozenset(line.rstrip("\n") for line in handle if line and not line.startswith("#"))


def _is_repetitive(value: str) -> bool:
    if len(set(value)) < 5:
        return True
    for size in range(1, 5):
        unit = value[:size]
        if unit * (len(value) // size) + unit[: len(value) % size] == value:
            return True
    return False


def _is_sequential(value: str) -> bool:
    lowered = value.lower()
    for sequence in _SEQUENCES:
        forward = sequence * 4
        if lowered in forward or lowered in forward[::-1]:
            return True
    # A run of characters each one step from the last: "abcdefghij" or "9876543210" inside it.
    run = longest = 1
    step = 0
    for previous, current in zip(lowered, lowered[1:]):
        difference = ord(current) - ord(previous)
        if difference in (1, -1) and (step == 0 or difference == step):
            run, step = run + 1, difference
        else:
            run, step = (2, difference) if difference in (1, -1) else (1, 0)
        longest = max(longest, run)
    return longest >= MIN_REMAINDER


def _without_context(value: str, context: Iterable[str]) -> str:
    remainder = value.lower()
    for word in sorted({w.lower() for w in context if w and len(w) >= 3}, key=len, reverse=True):
        remainder = remainder.replace(word, "")
    return remainder


def check(password: str, *, context: Iterable[str], hash_scheme: Optional[str] = None) -> None:
    """Raises PasswordRejected with a reason code; returns None when the password may be set.

    `hash_scheme` is the guest's default (from login.defs), because bcrypt stops reading at
    72 bytes: a longer password would set less than the person typed, silently.
    """
    if not isinstance(password, str):
        raise PasswordRejected("password_invalid")
    if len(password) < MIN_LENGTH:
        raise PasswordRejected("password_too_short")
    if len(password) > MAX_LENGTH:
        raise PasswordRejected("password_too_long")
    if any(not (0x20 <= ord(char) <= 0x7E) for char in password):
        raise PasswordRejected("password_not_printable_ascii")
    if hash_scheme == "des":
        raise PasswordRejected("guest_hash_unsupported")
    if hash_scheme == "bcrypt" and len(password.encode("ascii")) > BCRYPT_MAX_BYTES:
        raise PasswordRejected("password_too_long_for_guest")
    if password.lower() in _common():
        raise PasswordRejected("password_common")
    if _is_repetitive(password):
        raise PasswordRejected("password_repetitive")
    if _is_sequential(password):
        raise PasswordRejected("password_sequential")
    remainder = _without_context(password, [*context, "orcastra"])
    if len(remainder) < MIN_REMAINDER or (remainder and (_is_repetitive(remainder) or _is_sequential(remainder))):
        raise PasswordRejected("password_contains_context")


def chpasswd_stdin(user: str, password: str) -> bytes:
    """The one line chpasswd reads. The user name was validated already: no colon, no newline."""
    if ":" in user or "\n" in user or "\n" in password or "\r" in password:
        raise PasswordRejected("password_invalid")
    return f"{user}:{password}\n".encode("ascii")
