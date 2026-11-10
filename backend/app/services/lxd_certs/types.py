"""Shared vocabulary for LXD certificate management."""

from typing import NamedTuple, Optional


class LXDCertManagerError(Exception):
    """Base exception for LXD certificate manager errors."""
    pass


class ConnectionProbe(NamedTuple):
    """What a single connectivity probe observed.

    `reachable` and `trusted` are independent: a host can answer and still refuse the client,
    and a dead cluster must stay distinguishable from an untrusted one. verify_connection
    returns a bool; callers that need the distinction read this. `reachable` is None only when
    the probe never ran.
    """
    reachable: Optional[bool]
    trusted: bool
    message: str


def extract_cert_base64(pem_cert: str) -> str:
    """The base64 body of a PEM certificate, which is the form the LXD API expects."""
    lines = pem_cert.strip().split('\n')
    base64_lines = [
        line for line in lines 
        if not line.startswith('-----') and line.strip()
    ]
    return ''.join(base64_lines)
