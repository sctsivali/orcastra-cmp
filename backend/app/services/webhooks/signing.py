"""How a delivery proves it came from this deployment, and how a receiver checks.

important: the documentation quotes this wire format; changing it breaks every integrated
add-on.

    X-Orcastra-Signature: v1,k=<fingerprint>:<hex> [v1,k=<fingerprint>:<hex> ...]
    X-Orcastra-Timestamp: <unix seconds>
    X-Orcastra-Event-Id:  <stable across every attempt>
    X-Orcastra-Delivery-Id: <one per delivery row, repeated on its retries and redeliveries>

The version is signed too, so stripping `v1,` cannot downgrade a lenient verifier. Every live
secret adds an entry, so a receiver accepting any match rotates on its own schedule. Receivers
must reject a timestamp outside five minutes, or a captured signature replays forever. The body
is signed as the exact wire bytes; re-serialised JSON will not match.
"""

import hashlib
import hmac
from typing import Sequence

SIGNATURE_VERSION = "v1"
SIGNATURE_HEADER = "X-Orcastra-Signature"
TIMESTAMP_HEADER = "X-Orcastra-Timestamp"
EVENT_ID_HEADER = "X-Orcastra-Event-Id"
DELIVERY_ID_HEADER = "X-Orcastra-Delivery-Id"
EVENT_TYPE_HEADER = "X-Orcastra-Event-Type"

# A requirement on the receiver, stated as such in the documentation.
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

    A recipe kept only in prose drifts from the sender without anyone noticing.
    """
    if abs(now - timestamp) > window_seconds:
        return False
    expected = sign(secret, timestamp, body)
    for entry in header_value.split():
        # compare_digest on the whole entry, so the version and the key label are covered too.
        if hmac.compare_digest(entry, expected):
            return True
    return False


