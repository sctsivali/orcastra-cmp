"""The certificate a browser presented to nginx, read from the headers nginx forwards.

nginx asks for a client certificate on every TLS handshake (``ssl_verify_client optional_no_ca``:
any certificate, trust decided here) and forwards it URL-escaped in ``X-SSL-Client-Cert``, with
``X-Auth-Proxy-Secret`` proving the headers came from nginx (or from the frontend, which checked
nginx's secret first). A header that arrives without the secret is ignored, never trusted.

important: the handshake proved the browser holds the certificate's private key; whose it is and
what it may do is decided only by the identity store, never by the certificate's subject.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import unquote

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID
from starlette.requests import HTTPConnection

from app.editions.mini.settings import MiniSettings
from app.editions.mini.tokens import thumbprint

logger = logging.getLogger(__name__)

HDR_PROXY_SECRET = "x-auth-proxy-secret"
HDR_CERT = "x-ssl-client-cert"
CERT_HEADERS = ("x-ssl-client-cert", "x-ssl-client-verify", "x-ssl-client-s-dn")
_MAX_PEM_BYTES = 16 * 1024


@dataclass(frozen=True)
class PresentedCertificate:
    fingerprint: str  # sha256 hex of the DER certificate, lowercase
    thumbprint: str   # base64url sha256 (RFC 8705), what session tokens are bound to
    common_name: str
    serial: str
    not_before: datetime
    not_after: datetime

    def valid_at(self, when: datetime) -> bool:
        return self.not_before <= when <= self.not_after


def _networks(text: str):
    nets = []
    for raw in (text or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            nets.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            logger.warning("Ignoring an invalid TRUSTED_AUTH_PROXY_CIDRS entry")
    return nets


def proxy_trusted(connection: HTTPConnection, settings: MiniSettings) -> bool:
    """Whether this request demonstrably came through nginx (or the frontend that checked it)."""
    expected = settings.auth_proxy_secret
    if not expected:
        return False  # startup refuses this configuration; refuse here too
    provided = connection.headers.get(HDR_PROXY_SECRET, "")
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        return False
    nets = _networks(settings.trusted_auth_proxy_cidrs)
    if nets:
        peer = connection.client.host if connection.client else ""
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            return False
        if not any(address in net for net in nets):
            logger.warning("Certificate headers from a peer outside TRUSTED_AUTH_PROXY_CIDRS")
            return False
    return True


def parse_pem(pem: bytes) -> Optional[PresentedCertificate]:
    try:
        cert = x509.load_pem_x509_certificate(pem)
    except ValueError:
        return None
    der = cert.public_bytes(serialization.Encoding.DER)
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return PresentedCertificate(
        fingerprint=cert.fingerprint(hashes.SHA256()).hex(),
        thumbprint=thumbprint(der),
        common_name=str(cn[0].value) if cn else "",
        serial=format(cert.serial_number, "x"),
        not_before=cert.not_valid_before_utc,
        not_after=cert.not_valid_after_utc,
    )


def presented_certificate(connection: HTTPConnection, settings: MiniSettings) -> Optional[PresentedCertificate]:
    """The certificate behind this request, or None (not through nginx, none presented, unreadable)."""
    if not proxy_trusted(connection, settings):
        return None
    escaped = connection.headers.get(HDR_CERT, "")
    if not escaped or len(escaped) > 3 * _MAX_PEM_BYTES:
        return None
    pem = unquote(escaped).encode("utf-8")
    if b"-----BEGIN CERTIFICATE-----" not in pem or len(pem) > _MAX_PEM_BYTES:
        return None
    return parse_pem(pem)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
