"""Client-certificate authentication helpers (mini / AUTH_MODE=client-cert).

The reverse proxy (nginx) terminates TLS, requests the browser's client cert, and
forwards it to the backend as X-SSL-* headers. These helpers enforce that those headers
genuinely came from the proxy (not a spoofing client) and turn the forwarded PEM into a
verified fingerprint + identity fields. The fingerprint is recomputed here, never trusted
from the header, and the role is always looked up from the local store - never derived
from the certificate subject (TOFU accepts any self-signed cert).
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
from cryptography.hazmat.backends import default_backend
from cryptography.x509.oid import NameOID
from starlette.requests import HTTPConnection

from app.core.config import Settings
from app.services.pkcs12_util import cert_fingerprint_sha256

logger = logging.getLogger(__name__)

# Header names the proxy sets. Lowercased for case-insensitive lookup.
HDR_PROXY_SECRET = "x-auth-proxy-secret"
HDR_SSL_VERIFY = "x-ssl-client-verify"
HDR_SSL_CERT = "x-ssl-client-cert"
HDR_SSL_SDN = "x-ssl-client-s-dn"


@dataclass
class ClientCertIdentity:
    fingerprint: str          # sha256 hex of DER cert
    common_name: str
    serial: Optional[str]
    not_after: Optional[datetime]
    subject_dn: Optional[str]  # display only - NEVER used for authorization


def _parse_cidrs(cidr_text: str):
    nets = []
    for raw in (cidr_text or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            nets.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid TRUSTED_AUTH_PROXY_CIDRS entry: %r", raw)
    return nets


def proxy_trusted(request: HTTPConnection, settings: Settings) -> bool:
    """True only when the request demonstrably came through the auth reverse proxy.

    Primary control: a shared secret header the proxy injects (constant-time compared).
    Defense in depth: if TRUSTED_AUTH_PROXY_CIDRS is set, the direct peer must also be
    within it. The broad TRUSTED_PROXY_CIDRS is deliberately NOT reused here.
    """
    expected = settings.auth_proxy_secret or ""
    if not expected:
        # No secret configured: only safe in DEBUG (startup refuses this otherwise).
        if not settings.debug:
            logger.error("AUTH_PROXY_SECRET unset outside DEBUG - rejecting cert headers")
            return False
    else:
        provided = request.headers.get(HDR_PROXY_SECRET, "")
        if not hmac.compare_digest(provided, expected):
            return False

    nets = _parse_cidrs(settings.trusted_auth_proxy_cidrs)
    if nets:
        peer = request.client.host if request.client else None
        try:
            peer_ip = ipaddress.ip_address(peer) if peer else None
        except ValueError:
            peer_ip = None
        if peer_ip is None or not any(peer_ip in n for n in nets):
            logger.warning("Cert headers from untrusted peer IP: %s", peer)
            return False
    return True


def extract_client_cert(request: HTTPConnection, settings: Settings) -> Optional[ClientCertIdentity]:
    """Return the verified client identity from proxy headers, or None.

    Accepts any Starlette ``HTTPConnection`` (an HTTP ``Request`` or a ``WebSocket``), so the same
    fingerprint extraction backs both the bearer login/binding path and the WebSocket handshake.

    None means: not behind the trusted proxy, no client cert presented, or an unparseable cert.
    Under ``optional_no_ca`` the proxy reports ``verify=NONE`` even when a cert IS presented, so
    ``X-SSL-Client-Verify`` is deliberately NOT used as a gate - trust is by fingerprint (see below).
    Callers must treat None as unauthenticated (fail closed).
    """
    if not proxy_trusted(request, settings):
        return None

    escaped_pem = request.headers.get(HDR_SSL_CERT, "")
    if not escaped_pem:
        # No client certificate was presented in the TLS handshake.
        return None

    # TOFU: with `ssl_verify_client optional_no_ca` there is no CA to validate against, so
    # the proxy reports verify=NONE even when a cert IS presented. We trust by fingerprint,
    # not by chain - the TLS handshake already proved possession of the private key - so a
    # presented + parseable cert is accepted here and the fingerprint->role lookup in the
    # store is the actual authorization gate. We never derive a role from the cert subject.

    pem = unquote(escaped_pem)
    try:
        cert = x509.load_pem_x509_certificate(pem.encode("utf-8"), default_backend())
    except Exception as e:
        logger.warning("Failed to parse forwarded client cert: %s", e)
        return None

    fingerprint = cert_fingerprint_sha256(pem.encode("utf-8"))

    try:
        cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        common_name = cn_attrs[0].value if cn_attrs else ""
    except Exception:
        common_name = ""

    # not_valid_after_utc is tz-aware (cryptography >= 42); fall back for older builds.
    not_after = getattr(cert, "not_valid_after_utc", None)
    if not_after is None:
        naive = cert.not_valid_after
        not_after = naive.replace(tzinfo=timezone.utc)

    return ClientCertIdentity(
        fingerprint=fingerprint,
        common_name=common_name,
        serial=format(cert.serial_number, "x"),
        not_after=not_after,
        subject_dn=request.headers.get(HDR_SSL_SDN) or None,
    )
