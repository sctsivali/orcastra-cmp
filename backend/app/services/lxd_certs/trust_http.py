"""The HTTP side of removing trust: prove the identity, then delete exactly one fingerprint.

important: never the pooled client and never cert_files.materialize. The pool proves the
certificate a worker holds in memory, which after a rotation is the REPLACED one, so "trusted" from
the pool says nothing about whether removing that entry would lock the dashboard out. The
materialized files are shared by every pooled client of this worker, so writing the Vault
certificate there would switch a stale client's identity behind its back. Each session here is
built from the PEM it was handed, on its own staging files, unlinked on exit.

Every call blocks; callers run it through core.blocking.run_blocking.
"""

import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Iterator, Optional, Tuple

import requests

from app.core.cert_fingerprint import is_lxd_fingerprint
from app.core.url_safety import normalize_endpoint
from app.services.lxd import cert_files
from app.services.lxd.connection import (
    FingerprintHTTPAdapter,
    _install_lxd_response_guards,
    normalize_server_fingerprint,
)

logger = logging.getLogger(__name__)

# Per-request socket bound; run_blocking's wall clock sits above it.
REQUEST_TIMEOUT: Tuple[int, int] = (3, 10)


class RemovalOutcome(str, Enum):
    REMOVED = "removed"
    ALREADY_GONE = "already_gone"
    FORBIDDEN = "forbidden"
    FAILED = "failed"
    TRANSIENT = "transient"


@dataclass(frozen=True)
class IdentityProbe:
    """What the cluster said about the certificate the session presents.

    `restricted` is None when the dashboard could not read its own trust entry, which happens for a
    restricted client (LXD hides other entries from it) or a CA-trusted one. Removal needs it False.
    """

    reachable: bool
    trusted: bool
    restricted: Optional[bool]
    message: str
    # Removal is impossible by policy rather than by the cluster's answer, ex; pinning is off.
    blocked: bool = False

    @property
    def may_remove(self) -> bool:
        return self.reachable and self.trusted and self.restricted is False and not self.blocked


def server_identity_problem(
    endpoint: str, server_fingerprint: Optional[str], *, pinning_enabled: bool
) -> Optional[IdentityProbe]:
    """Why the cluster's server cannot be authenticated for a removal, or None when it can.

    important: without the pin, `verify=False` means no server authentication at all, and an
    on-path host could answer "trusted" and "removed" while the real entry stays trusted. That
    cannot cause a lockout or a foreign deletion, but the record would say "removed" for an entry
    that is still trusted. No pin, no removal: the pooled connection pins a cluster on first
    contact, and the row waits for it.
    """
    if not normalize_endpoint(endpoint).lower().startswith("https://"):
        return IdentityProbe(True, False, None, "the cluster endpoint is not HTTPS", blocked=True)
    if not pinning_enabled:
        return IdentityProbe(
            True, False, None, "server certificate pinning is disabled, so removal cannot be verified",
            blocked=True,
        )
    pin = normalize_server_fingerprint(server_fingerprint)
    if not is_lxd_fingerprint(pin):
        # Not reachable rather than untrusted: nothing was asked, so nothing may be concluded from
        # the answer. The pooled connection records the pin on first contact; the row retries.
        return IdentityProbe(False, False, None, "the cluster's server certificate is not pinned yet")
    return None


def _write_private(staging: str, prefix: str, suffix: str, content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix, dir=staging, prefix=prefix)
    try:
        try:
            os.write(fd, content.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(path, 0o600)
    except BaseException:
        # A full tmpfs must not leave half a private key behind with no path left to unlink.
        os.unlink(path)
        raise
    return path


@contextmanager
def pinned_session(
    cluster_id: str,
    cert_pem: str,
    key_pem: str,
    server_fingerprint: Optional[str],
) -> Iterator[requests.Session]:
    """A one-off mTLS session presenting `cert_pem`, pinned to the cluster's server certificate."""
    # important: dir= is load-bearing (see cert_files.staging_dir). Without it tempfile falls
    # through to /var/tmp when /tmp is full and the private key lands on disk.
    staging = cert_files.staging_dir()
    prefix = "lxd_trust_"
    cert_path = key_path = None
    session = requests.Session()
    try:
        cert_path = _write_private(staging, prefix, ".crt", cert_pem)
        key_path = _write_private(staging, prefix, ".key", key_pem)
        pin = normalize_server_fingerprint(server_fingerprint) or None
        adapter = FingerprintHTTPAdapter(max_retries=0, server_fingerprint=pin)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _install_lxd_response_guards(session)
        session.cert = (cert_path, key_path)
        session.verify = False
        yield session
    finally:
        session.close()
        for path in (cert_path, key_path):
            if path:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass


def _url(endpoint: str, path: str) -> str:
    return f"{normalize_endpoint(endpoint)}{path}"


def probe_identity(session: requests.Session, endpoint: str, own_fingerprint: str) -> IdentityProbe:
    """Is the presented certificate trusted, and is its own trust entry unrestricted?"""
    try:
        response = session.get(_url(endpoint, "/1.0"), timeout=REQUEST_TIMEOUT)
    except requests.exceptions.SSLError as exc:
        return IdentityProbe(False, False, None, f"TLS failed (server certificate pin?): {exc}")
    except requests.exceptions.RequestException as exc:
        return IdentityProbe(False, False, None, f"cluster unreachable: {type(exc).__name__}")
    if response.status_code != 200:
        return IdentityProbe(True, False, None, f"/1.0 answered HTTP {response.status_code}")
    auth = ((response.json() or {}).get("metadata") or {}).get("auth")
    if auth != "trusted":
        return IdentityProbe(True, False, None, "the cluster does not trust the current certificate yet")
    try:
        own = session.get(
            _url(endpoint, f"/1.0/certificates/{own_fingerprint}"), timeout=REQUEST_TIMEOUT
        )
    except requests.exceptions.RequestException as exc:
        return IdentityProbe(False, True, None, f"cluster unreachable: {type(exc).__name__}")
    if own.status_code != 200:
        return IdentityProbe(
            True, True, None,
            f"the dashboard cannot read its own trust entry (HTTP {own.status_code}); "
            "it is restricted or trusted through a CA",
        )
    restricted = bool(((own.json() or {}).get("metadata") or {}).get("restricted"))
    message = "restricted certificate cannot manage trust" if restricted else "trusted"
    return IdentityProbe(True, True, restricted, message)


def remove_fingerprint(
    session: requests.Session, endpoint: str, fingerprint: str
) -> Tuple[RemovalOutcome, str]:
    """Delete one trust entry, after checking the entry is exactly the one named.

    Only valid after probe_identity said may_remove: that is what makes a 404 here mean "gone"
    rather than "hidden from a restricted client".
    """
    if not is_lxd_fingerprint(fingerprint):
        return RemovalOutcome.FAILED, "refusing a fingerprint that is not 64 lowercase hex characters"
    url = _url(endpoint, f"/1.0/certificates/{fingerprint}")
    try:
        found = session.get(url, timeout=REQUEST_TIMEOUT)
        if found.status_code == 404:
            return RemovalOutcome.ALREADY_GONE, "not in the trust store"
        if found.status_code == 403:
            return RemovalOutcome.FORBIDDEN, "the cluster refused to show the entry (HTTP 403)"
        if found.status_code >= 500:
            return RemovalOutcome.TRANSIENT, f"lookup answered HTTP {found.status_code}"
        if found.status_code != 200:
            return RemovalOutcome.FAILED, f"lookup answered HTTP {found.status_code}"
        named = ((found.json() or {}).get("metadata") or {}).get("fingerprint")
        if named != fingerprint:
            # LXD resolves a prefix; only the exact entry may be deleted.
            return RemovalOutcome.FAILED, "the cluster resolved a different entry"
        gone = session.delete(url, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.SSLError as exc:
        return RemovalOutcome.FAILED, f"TLS failed (server certificate pin?): {exc}"
    except requests.exceptions.RequestException as exc:
        return RemovalOutcome.TRANSIENT, f"cluster unreachable: {type(exc).__name__}"
    if gone.status_code in (200, 202):
        return RemovalOutcome.REMOVED, "removed"
    if gone.status_code == 404:
        return RemovalOutcome.ALREADY_GONE, "not in the trust store"
    if gone.status_code == 403:
        return RemovalOutcome.FORBIDDEN, "the cluster refused the removal (HTTP 403)"
    if gone.status_code >= 500:
        return RemovalOutcome.TRANSIENT, f"removal answered HTTP {gone.status_code}"
    return RemovalOutcome.FAILED, f"removal answered HTTP {gone.status_code}"
