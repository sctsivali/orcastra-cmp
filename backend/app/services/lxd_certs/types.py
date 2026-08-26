"""Shared vocabulary for LXD certificate management."""

from typing import NamedTuple, Optional


class LXDCertManagerError(Exception):
    """Base exception for LXD certificate manager errors."""
    pass


class ConnectionProbe(NamedTuple):
    """What a single connectivity probe actually observed.

    `reachable` and `trusted` are independent: a host can answer and still refuse us. Collapsing
    them into one bool is what made a dead cluster and an untrusted one indistinguishable, so
    verify_connection keeps returning a bool for its five existing callers while anything that
    needs the distinction reads this instead. `reachable` is None only when the probe never ran.
    """
    reachable: Optional[bool]
    trusted: bool
    message: str


def extract_cert_base64(pem_cert: str) -> str:
    """
    Extract pure base64 content from PEM certificate.
    LXD API expects base64-encoded certificate without PEM headers.
    """
    # Remove headers, footers, and whitespace
    lines = pem_cert.strip().split('\n')
    base64_lines = [
        line for line in lines 
        if not line.startswith('-----') and line.strip()
    ]
    return ''.join(base64_lines)
