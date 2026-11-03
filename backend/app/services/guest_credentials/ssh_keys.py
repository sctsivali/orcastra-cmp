"""Reading an SSH public key someone pasted, strictly, and recognising a private key before anything else.

The pasted text ends up as one line in a guest's authorized_keys, where sshd treats everything on
it as instructions: an options prefix such as `command=` or `from=` changes what the key may do, and
an embedded newline would add a second key nobody reviewed. So one key per paste, no options, no
control characters, and the wire blob must agree with the type written in front of it.

important: private-key detection runs first and on the raw text, and nothing it looks at is ever
logged or echoed. Someone who pasted a private key has to be told plainly, not shown it again.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import struct
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Tuple

from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.serialization import load_ssh_public_key

MAX_INPUT_BYTES = 16 * 1024
MAX_COMMENT_CHARS = 255
RSA_MIN_BITS = 2048
RSA_RECOMMENDED_BITS = 3072
RSA_MAX_BITS = 16384

_ECDSA_CURVES = {"nistp256": 256, "nistp384": 384, "nistp521": 521}
ACCEPTED_TYPES = frozenset({
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
})
_REFUSED_TYPES = {"ssh-dss": "dsa_refused"}
# A token that looks like a key type, for telling an options prefix from a mistyped type.
_TYPE_SHAPE = re.compile(r"^(?:ssh-[a-z0-9-]+|ecdsa-sha2-[a-z0-9-]+|sk-[a-z0-9@.-]+)$")

_PRIVATE_MARKERS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"PuTTY-User-Key-File-\d"),
    re.compile(r"---- BEGIN SSH2 ENCRYPTED PRIVATE KEY ----"),
    # "openssh-key-v1\0" in base64: the body of an OpenSSH private key pasted without its armour.
    re.compile(r"b3BlbnNzaC1rZXktdjEA"),
)
_RFC4716_BEGIN = "---- BEGIN SSH2 PUBLIC KEY ----"
_RFC4716_END = "---- END SSH2 PUBLIC KEY ----"


class KeyRejected(ValueError):
    """`reason` is a code from this module; the message never quotes the input."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PublicKey:
    key_type: str
    blob: bytes
    comment: str
    bits: int
    fingerprint: str
    warnings: Tuple[str, ...] = ()

    def line(self) -> str:
        encoded = base64.b64encode(self.blob).decode("ascii")
        return f"{self.key_type} {encoded} {self.comment}".rstrip()


def looks_private(text: str) -> bool:
    return any(marker.search(text) for marker in _PRIVATE_MARKERS)


def fingerprint(blob: bytes) -> str:
    """OpenSSH's SHA256 fingerprint, the form `ssh-keygen -lf` prints."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def _strings(blob: bytes) -> List[bytes]:
    """The length-prefixed strings of an SSH wire blob; refuses a short or trailing read."""
    parts: List[bytes] = []
    offset = 0
    while offset < len(blob):
        if offset + 4 > len(blob):
            raise KeyRejected("malformed_key")
        (length,) = struct.unpack(">I", blob[offset:offset + 4])
        offset += 4
        if length > len(blob) - offset:
            raise KeyRejected("malformed_key")
        parts.append(blob[offset:offset + length])
        offset += length
        if len(parts) > 8:
            raise KeyRejected("malformed_key")
    return parts


def blob_type(blob: bytes) -> Optional[str]:
    """The type a blob names for itself, or None when it does not parse."""
    try:
        parts = _strings(blob)
        return parts[0].decode("ascii") if parts else None
    except (KeyRejected, UnicodeDecodeError):
        return None


def _decode(encoded: str) -> bytes:
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise KeyRejected("malformed_key") from None


def _bits_and_check(key_type: str, blob: bytes) -> int:
    """Validates the blob's shape and maths for `key_type` and returns the key size in bits."""
    parts = _strings(blob)
    if not parts or parts[0] != key_type.encode("ascii"):
        raise KeyRejected("type_mismatch")
    if key_type == "sk-ssh-ed25519@openssh.com":
        if len(parts) != 3 or len(parts[1]) != 32 or not parts[2].startswith(b"ssh:"):
            raise KeyRejected("malformed_key")
        ed25519.Ed25519PublicKey.from_public_bytes(parts[1])
        return 256
    if key_type == "sk-ecdsa-sha2-nistp256@openssh.com":
        if len(parts) != 4 or parts[1] != b"nistp256" or not parts[3].startswith(b"ssh:"):
            raise KeyRejected("malformed_key")
        try:
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), parts[2])
        except ValueError:
            raise KeyRejected("malformed_key") from None
        return 256
    expected_parts = {"ssh-ed25519": 2, "ssh-rsa": 3}.get(key_type, 3)
    if len(parts) != expected_parts:
        raise KeyRejected("malformed_key")
    if key_type.startswith("ecdsa-sha2-") and parts[1].decode("ascii", "replace") != key_type.rsplit("-", 1)[-1]:
        raise KeyRejected("type_mismatch")
    try:
        loaded = load_ssh_public_key(key_type.encode("ascii") + b" " + base64.b64encode(blob))
    except (ValueError, TypeError):
        raise KeyRejected("malformed_key") from None
    if isinstance(loaded, rsa.RSAPublicKey):
        return loaded.key_size
    if isinstance(loaded, ec.EllipticCurvePublicKey):
        return _ECDSA_CURVES[key_type.rsplit("-", 1)[-1]]
    if isinstance(loaded, ed25519.Ed25519PublicKey):
        return 256
    raise KeyRejected("unsupported_type")


# Cc control, Cf format (bidi overrides, zero-width), Zl/Zp separators, private use, surrogates and
# unassigned: each can make a line in authorized_keys read differently in a terminal from what it is.
_UNSHOWABLE = ("Cc", "Cf", "Zl", "Zp", "Co", "Cs", "Cn")


def _check_comment(comment: str) -> str:
    if len(comment) > MAX_COMMENT_CHARS:
        raise KeyRejected("comment_too_long")
    if any(unicodedata.category(char) in _UNSHOWABLE for char in comment):
        raise KeyRejected("comment_invalid")
    return comment


def displayable(text: str) -> str:
    """`text` without the characters `_check_comment` refuses, for showing what a guest wrote."""
    return "".join(char for char in text if unicodedata.category(char) not in _UNSHOWABLE)


def _from_rfc4716(text: str) -> Tuple[str, bytes, str]:
    lines = [line.strip() for line in text.strip().splitlines()]
    if not lines or lines[0] != _RFC4716_BEGIN or lines[-1] != _RFC4716_END:
        raise KeyRejected("malformed_key")
    body: List[str] = []
    comment = ""
    pending = ""
    for line in lines[1:-1]:
        if pending:
            line, pending = pending + line, ""
        if line.endswith("\\"):
            pending = line[:-1]
            continue
        if ":" in line and not body:
            tag, _, value = line.partition(":")
            if tag.strip().lower() == "comment":
                comment = value.strip().strip('"')
            continue
        body.append(line)
    blob = _decode("".join(body))
    key_type = blob_type(blob)
    if not key_type:
        raise KeyRejected("malformed_key")
    return key_type, blob, comment


def parse_public_key(text: str) -> PublicKey:
    """One public key as pasted, or KeyRejected with a reason code."""
    if not isinstance(text, str) or not text.strip():
        raise KeyRejected("empty")
    if len(text.encode("utf-8", "surrogatepass")) > MAX_INPUT_BYTES:
        raise KeyRejected("too_long")
    if looks_private(text):
        raise KeyRejected("private_key_pasted")

    stripped = text.strip()
    if stripped.startswith("---- BEGIN SSH2"):
        key_type, blob, comment = _from_rfc4716(stripped)
    else:
        if "\n" in stripped or "\r" in stripped:
            raise KeyRejected("multiple_lines")
        if any(unicodedata.category(c) in ("Cc", "Cf") and c != "\t" for c in stripped):
            raise KeyRejected("control_characters")
        tokens = stripped.split()
        key_type = tokens[0]
        if key_type not in ACCEPTED_TYPES and key_type not in _REFUSED_TYPES:
            if key_type.endswith("-cert-v01@openssh.com"):
                raise KeyRejected("certificate_refused")
            if any(token in ACCEPTED_TYPES or token in _REFUSED_TYPES for token in tokens[1:]):
                raise KeyRejected("options_not_supported")
            raise KeyRejected("unsupported_type" if _TYPE_SHAPE.match(key_type) else "not_a_public_key")
        if len(tokens) < 2:
            raise KeyRejected("malformed_key")
        blob = _decode(tokens[1])
        comment = " ".join(tokens[2:])

    if key_type in _REFUSED_TYPES:
        raise KeyRejected(_REFUSED_TYPES[key_type])
    if key_type.endswith("-cert-v01@openssh.com"):
        raise KeyRejected("certificate_refused")
    if key_type not in ACCEPTED_TYPES:
        raise KeyRejected("unsupported_type")
    bits = _bits_and_check(key_type, blob)
    warnings: Tuple[str, ...] = ()
    if key_type == "ssh-rsa":
        if bits < RSA_MIN_BITS:
            raise KeyRejected("rsa_too_short")
        if bits > RSA_MAX_BITS:
            raise KeyRejected("rsa_too_long")
        if bits < RSA_RECOMMENDED_BITS:
            warnings = ("rsa_below_3072",)
    return PublicKey(key_type, blob, _check_comment(comment), bits, fingerprint(blob), warnings)


def bits_of(key_type: str, blob: bytes) -> Optional[int]:
    """Key size of a line already in a guest's file, without refusing what this module refuses."""
    if key_type == "ssh-dss":
        parts = _safe_strings(blob)
        return (len(parts[1].lstrip(b"\0")) * 8) if len(parts) > 1 else None
    try:
        return _bits_and_check(key_type, blob)
    except (KeyRejected, KeyError, UnicodeDecodeError):
        return None


def _safe_strings(blob: bytes) -> List[bytes]:
    try:
        return _strings(blob)
    except KeyRejected:
        return []
