"""The one way this codebase names a client certificate: the fingerprint LXD itself prints.

LXD identifies a TLS client by the lowercase hex SHA-256 of the certificate's DER bytes. That is
the FINGERPRINT column of `lxc config trust list` (which shows its first 12 characters), the key of
`/1.0/certificates/{fingerprint}`, and the `requestor` of an operation. Showing the same form lets
an operator match it against the trust list as is.

Stdlib only, and in core rather than services/lxd_certs, because importing that package runs its
__init__, which reaches services/lxd and from there lxd_service. connection.py needs this helper
and would otherwise import itself in a cycle.
"""

import base64
import binascii
import hashlib
import re

_LXD_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


def der_from_pem(pem: str) -> bytes:
    """The DER bytes inside the first PEM block. Raises ValueError on anything else."""
    body = []
    inside = False
    for raw in (pem or "").splitlines():
        line = raw.strip()
        if line.startswith("-----BEGIN"):
            inside = True
            continue
        if line.startswith("-----END"):
            break
        if inside and line:
            body.append(line)
    if not body:
        raise ValueError("no PEM block found")
    try:
        return base64.b64decode("".join(body), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("PEM body is not valid base64") from exc


def lxd_fingerprint(pem: str) -> str:
    """Lowercase hex SHA-256 of the DER certificate, exactly as LXD reports it."""
    return hashlib.sha256(der_from_pem(pem)).hexdigest()


def is_lxd_fingerprint(value: object) -> bool:
    """True only for a full 64 character lowercase fingerprint.

    important: LXD resolves `/1.0/certificates/{fp}` by prefix, so a short or truncated value sent
    to a DELETE can match a different entry. Anything that removes trust checks this first.
    """
    return isinstance(value, str) and bool(_LXD_FINGERPRINT.match(value))
