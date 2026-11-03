"""A pasted public key becomes exactly one reviewed line of authorized_keys, or a reason code.

Fingerprints and sizes are pinned against what OpenSSH's own `ssh-keygen -lf` printed for the same
keys (`_ssh_key_vectors.py`), so the panel shows a person the value their own tools show them.
"""

import base64
import struct

import pytest

from app.services.guest_credentials.ssh_keys import (
    KeyRejected,
    MAX_INPUT_BYTES,
    fingerprint,
    looks_private,
    parse_public_key,
)
from tests._ssh_key_vectors import VECTORS

# Built at run time so no file in the repository carries an armoured private key.
ARMOUR = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
FAKE_PRIVATE = f"{ARMOUR}\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ==\n-----END " + "OPENSSH PRIVATE KEY-----\n"


def _reason(text: str) -> str:
    with pytest.raises(KeyRejected) as rejected:
        parse_public_key(text)
    assert str(text) not in str(rejected.value) or len(text) < 3
    return rejected.value.reason


@pytest.mark.parametrize("name", ["ed25519", "ecdsa256", "ecdsa384", "ecdsa521", "rsa2048", "rsa3072", "sk"])
def test_an_accepted_key_matches_ssh_keygen(name):
    line, bits, expected = VECTORS[name]
    key = parse_public_key(line + "\n")
    assert key.fingerprint == expected
    assert key.bits == bits
    assert key.line() == line


def test_rsa_below_3072_is_accepted_with_a_warning_and_below_2048_refused():
    assert parse_public_key(VECTORS["rsa2048"][0]).warnings == ("rsa_below_3072",)
    assert parse_public_key(VECTORS["rsa3072"][0]).warnings == ()
    assert _reason(VECTORS["rsa1024"][0]) == "rsa_too_short"


@pytest.mark.parametrize("text", [
    FAKE_PRIVATE,
    "PuTTY-User-Key-File-3: ssh-ed25519\nEncryption: none\n",
    "---- BEGIN SSH2 ENCRYPTED PRIVATE KEY ----\nAAAA\n---- END SSH2 ENCRYPTED PRIVATE KEY ----",
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----",
])
def test_a_private_key_is_recognised_before_anything_else(text):
    assert looks_private(text)
    assert _reason(text) == "private_key_pasted"


def test_a_private_key_hidden_after_a_public_one_is_still_refused():
    assert _reason(VECTORS["ed25519"][0] + "\n" + FAKE_PRIVATE) == "private_key_pasted"


@pytest.mark.parametrize("prefix", ['command="rm -rf /" ', 'from="10.0.0.0/8" ', "no-pty,restrict "])
def test_an_options_prefix_is_refused(prefix):
    assert _reason(prefix + VECTORS["ed25519"][0]) == "options_not_supported"


def test_two_keys_in_one_paste_are_refused():
    assert _reason(VECTORS["ed25519"][0] + "\n" + VECTORS["rsa3072"][0]) == "multiple_lines"


@pytest.mark.parametrize("comment, reason", [
    ("a\u202eb", "control_characters"),
    ("zero\u200bwidth", "control_characters"),
    ("ansi\x1b[31m", "control_characters"),
    ("x" * 256, "comment_too_long"),
])
def test_a_comment_that_would_read_differently_is_refused(comment, reason):
    assert _reason(f"{VECTORS['ed25519'][0].rsplit(' ', 2)[0]} {comment}") == reason


def test_a_comment_with_shell_and_html_is_kept_as_text():
    base = VECTORS["ed25519"][0].rsplit(" ", 2)[0]
    key = parse_public_key(f"{base} $(id) <img src=x onerror=alert(1)>")
    assert key.comment == "$(id) <img src=x onerror=alert(1)>"


def test_a_blob_that_disagrees_with_its_type_is_refused():
    blob = base64.b64decode(VECTORS["ed25519"][0].split()[1])
    assert _reason(f"ssh-rsa {base64.b64encode(blob).decode()} x") in ("type_mismatch", "malformed_key")
    assert _reason("ssh-ed25519 !!!notbase64 x") == "malformed_key"
    assert _reason(f"ssh-ed25519 {base64.b64encode(blob + b'extra').decode()} x") == "malformed_key"


def test_dsa_certificates_and_unknown_types_are_refused():
    def s(value: bytes) -> bytes:
        return struct.pack(">I", len(value)) + value

    dsa = base64.b64encode(s(b"ssh-dss") + s(b"\x01" * 128) + s(b"\x02" * 20) + s(b"\x03" * 128) + s(b"\x04" * 128))
    assert _reason(f"ssh-dss {dsa.decode()} old") == "dsa_refused"
    assert _reason("ssh-ed25519-cert-v01@openssh.com AAAA x") == "certificate_refused"
    assert _reason("ssh-foo AAAA x") == "unsupported_type"
    assert _reason("hello world") == "not_a_public_key"
    assert _reason("   ") == "empty"
    assert _reason("ssh-ed25519 " + "A" * MAX_INPUT_BYTES) == "too_long"


def test_a_putty_rfc4716_export_is_converted():
    line, bits, expected = VECTORS["rsa3072"]
    body = line.split()[1]
    wrapped = "\n".join(body[i:i + 70] for i in range(0, len(body), 70))
    text = f'---- BEGIN SSH2 PUBLIC KEY ----\nComment: "rsa-key-20261103"\n{wrapped}\n---- END SSH2 PUBLIC KEY ----\n'
    key = parse_public_key(text)
    assert (key.fingerprint, key.bits, key.comment) == (expected, bits, "rsa-key-20261103")


def test_the_fingerprint_is_openssh_shaped():
    blob = base64.b64decode(VECTORS["ed25519"][0].split()[1])
    assert fingerprint(blob) == VECTORS["ed25519"][2]
    assert not fingerprint(blob).endswith("=")
