"""The guest password rules, after NIST SP 800-63B rev 4, and the generator the console offers.

Length and a blocklist, no composition rules, printable ASCII so the password can be typed at a
guest console, and the guest's own hash scheme respected (bcrypt reads only 72 bytes).
"""

import secrets

import pytest

from app.services.guest_credentials.passwords import (
    MAX_LENGTH,
    MIN_LENGTH,
    PasswordRejected,
    _common,
    check,
    chpasswd_stdin,
)

STRONG = "bcdfghjknprstuvxbcdfg"
GENERATOR_ALPHABET = "bcdefghjknprstuvx"


def _reason(password: str, **kwargs) -> str:
    with pytest.raises(PasswordRejected) as rejected:
        check(password, context=kwargs.pop("context", []), **kwargs)
    assert password not in str(rejected.value)
    return rejected.value.reason


def test_fifteen_is_the_floor_and_128_the_ceiling():
    assert _reason("Kq7#mZ2!wP9$rT") == "password_too_short"
    check("Kq7#mZ2!wP9$rTx", context=[])
    check("Kq7#mZ2!wP9$rTx" * 8 + "Abcdefgh", context=[])
    assert _reason("K" + "q7#mZ2!wP9$rTx" * 10) == "password_too_long"
    assert (MIN_LENGTH, MAX_LENGTH) == (15, 128)


def test_no_composition_rule_refuses_a_long_lowercase_passphrase():
    check("correct horse battery staple", context=[])


@pytest.mark.parametrize("char", ["\t", "\n", "\x7f", "\xe9", "\xa0", "\U0001f600"])
def test_only_printable_ascii_is_accepted(char):
    assert _reason(f"Kq7#mZ2!wP9$rTx{char}") == "password_not_printable_ascii"


def test_a_common_password_is_refused_whatever_its_case():
    common = sorted(_common())[0]
    assert len(common) >= MIN_LENGTH
    assert _reason(common.upper() if common.isascii() else common) == "password_common"
    assert _reason("passwordpassword") in ("password_common", "password_repetitive")


@pytest.mark.parametrize("password, reason", [
    ("aaaaaaaaaaaaaaaa", "password_repetitive"),
    ("abababababababab", "password_repetitive"),
    ("abcabcabcabcabcabc", "password_repetitive"),
    ("abcdefghijklmnopq", "password_sequential"),
    ("9876543210987654", "password_sequential"),
    ("qwertyuiopasdfghj", "password_sequential"),
])
def test_runs_a_person_types_without_thinking_are_refused(password, reason):
    assert _reason(password) in (reason, "password_common")


def test_the_account_and_instance_names_do_not_count_towards_the_length():
    assert _reason("ubuntu-webserver-1", context=["ubuntu", "webserver"]) == "password_contains_context"
    assert _reason("orcastra-ubuntu-2026", context=["ubuntu"]) == "password_contains_context"
    check("ubuntu Kq7#mZ2!wP9$", context=["ubuntu"])


def test_the_guest_hash_scheme_is_respected():
    long = "Kq7#mZ2!wP9$rTx" * 5 + "Abcdefgh"
    assert _reason(long, hash_scheme="bcrypt") == "password_too_long_for_guest"
    check(long, context=[], hash_scheme="yescrypt")
    assert _reason("Kq7#mZ2!wP9$rTx", hash_scheme="des") == "guest_hash_unsupported"


def test_the_generator_alphabet_always_passes():
    # The frontend's lib/guest-credentials/password.ts draws 22 of these; every draw must pass.
    for _ in range(1000):
        candidate = "".join(secrets.choice(GENERATOR_ALPHABET) for _ in range(22))
        check(candidate, context=["ubuntu", "instance"])


def test_the_chpasswd_line_carries_nothing_that_could_add_a_second_line():
    assert chpasswd_stdin("ubuntu", STRONG) == f"ubuntu:{STRONG}\n".encode()
    for user, password in (("a:b", STRONG), ("a\nb", STRONG), ("ubuntu", STRONG + "\nroot:x"), ("ubuntu", STRONG + "\r")):
        with pytest.raises(PasswordRejected):
            chpasswd_stdin(user, password)
