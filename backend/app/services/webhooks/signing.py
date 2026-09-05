"""How a delivery proves it came from us, and how a receiver checks.

The wire format is the one thing here that cannot be changed later without breaking every add-on
already integrated, so it is written down once and the documentation quotes this module rather
than describing it separately.

    X-Orcastra-Signature: v1,k=<fingerprint>:<hex> [v1,k=<fingerprint>:<hex> ...]
    X-Orcastra-Timestamp: <unix seconds>
    X-Orcastra-Event-Id:  <stable across every attempt>
    X-Orcastra-Delivery-Id: <unique per attempt>

Four decisions, each of which a receiver depends on.

The version is inside the signed material, not only in the header. An attacker who could strip
`v1,` from the header and have a lenient verifier accept the rest would otherwise be downgrading
the scheme for free.

The header is a space-separated LIST. That is what makes secret rotation expressible: during the
overlap both signatures are sent, and a receiver that accepts if any entry verifies can switch
whenever it likes rather than on our schedule. It is also where an algorithm change would go.

The timestamp is signed, and a receiver is told to reject one outside five minutes. Without a
window, a signature captured from a receiver's own logs replays forever.

The body is signed as the exact bytes on the wire. A receiver that parses JSON first and
re-serialises will not match, and the difference is real: the sender emits compact separators.
"""

import hashlib
import hmac
from typing import Iterable, Sequence, Tuple

SIGNATURE_VERSION = "v1"
SIGNATURE_HEADER = "X-Orcastra-Signature"
TIMESTAMP_HEADER = "X-Orcastra-Timestamp"
EVENT_ID_HEADER = "X-Orcastra-Event-Id"
DELIVERY_ID_HEADER = "X-Orcastra-Delivery-Id"
EVENT_TYPE_HEADER = "X-Orcastra-Event-Type"

# Normative, not advice. Stated in the documentation as a requirement on the receiver.
REPLAY_WINDOW_SECONDS = 300


def secret_fingerprint(secret: str) -> str:
    """A short, non-reversible label so an operator can tell two secrets apart.

    Never the secret and never a prefix of it. A prefix would leak key material into the UI, the
    delivery log and every request header.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def signed_material(timestamp: int, body: bytes) -> bytes:
    return f"{SIGNATURE_VERSION}.{timestamp}.".encode("utf-8") + body


def sign(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), signed_material(timestamp, body), hashlib.sha256
    ).hexdigest()
    return f"{SIGNATURE_VERSION},k={secret_fingerprint(secret)}:{digest}"


def signature_header(secrets: Sequence[str], timestamp: int, body: bytes) -> str:
    """One entry per live secret, so a rotation overlap is a header a receiver can handle."""
    return " ".join(sign(secret, timestamp, body) for secret in secrets if secret)


def verify(
    secret: str,
    header_value: str,
    timestamp: int,
    body: bytes,
    *,
    now: int,
    window_seconds: int = REPLAY_WINDOW_SECONDS,
) -> bool:
    """The receiver's side, implemented here so the documented recipe is executable.

    Kept in the codebase rather than only in prose because a recipe nobody runs drifts from the
    sender, and the drift is silent until an integration fails in production.
    """
    if abs(now - timestamp) > window_seconds:
        return False
    expected = sign(secret, timestamp, body)
    for entry in header_value.split():
        # compare_digest on the whole entry, so the version and the key label are covered too.
        if hmac.compare_digest(entry, expected):
            return True
    return False


def parse_entries(header_value: str) -> Iterable[Tuple[str, str, str]]:
    """(version, key fingerprint, hex digest) for each entry, for diagnostics only."""
    for entry in header_value.split():
        try:
            version, rest = entry.split(",", 1)
            label, digest = rest.rsplit(":", 1)
            yield version, label.removeprefix("k="), digest
        except ValueError:
            continue
