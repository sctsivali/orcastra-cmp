"""Server-certificate pinning for LXD endpoints.

Every connection in this codebase is made with `verify=False`, at roughly twenty-five call sites,
and nothing pins a fingerprint anywhere. That is not a shortcut around CA validation: LXD does not
use a public CA at all. Its own trust model is that the client remembers the server's certificate
fingerprint, which is why `lxc remote add` prints the fingerprint and asks you to accept it. With
neither CA validation nor pinning, there is no server authentication of any kind, and anyone on
the path between this control plane and a hypervisor can present their own certificate, take the
mTLS session, and read or forge everything on it: instance configs, exec streams, console traffic.

Trust on first use, then pin. That is the standard pattern for systems with no certificate
authority, and the one SSH and LXD itself already use, so it is not a weaker substitute for
something better. Its limit is worth stating plainly: it protects against an attacker who arrives
after enrolment, not one already in position at the moment a cluster is first seen.

The alternative considered and rejected was requiring an operator to type the fingerprint at
registration. It is strictly stronger and it does not survive contact with a fleet: eight clusters
are registered here and six of them are currently unreachable, so a hard requirement would refuse
them all when they came back.
"""

import hashlib
import logging
import socket
import ssl
from typing import Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

FINGERPRINT_FIELD = "server_cert_fingerprint"
PINNED_AT_FIELD = "server_cert_pinned_at"

_HANDSHAKE_TIMEOUT = 5


class LXDCertificatePinError(Exception):
    """The endpoint presented a certificate that is not the one this cluster was pinned to."""


def _host_port(endpoint: str) -> Tuple[str, int]:
    parsed = urlparse(endpoint if "://" in endpoint else f"https://{endpoint}")
    if not parsed.hostname:
        raise ValueError(f"Cannot parse an LXD endpoint out of {endpoint!r}")
    return parsed.hostname, parsed.port or 8443


def fetch_server_fingerprint(endpoint: str, timeout: int = _HANDSHAKE_TIMEOUT) -> Optional[str]:
    """SHA-256 of the endpoint's presented certificate, or None if it cannot be reached.

    note: deliberately its own TLS handshake rather than reading it off the pooled pylxd client.
    `requests` does not expose the peer certificate when `verify=False`, and reaching into the
    urllib3 connection to get it depends on internals that move between releases. One extra
    handshake per connection attempt is a price worth paying to keep this readable.
    """
    host, port = _host_port(endpoint)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # We are identifying the certificate, not trusting it: verification is what pinning replaces.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except Exception as exc:
        logger.debug("Could not read the server certificate for %s: %s", endpoint, exc)
        return None
    if not der:
        return None
    return hashlib.sha256(der).hexdigest()


def check_or_record_pin(
    cluster_id: str,
    endpoint: str,
    stored_fingerprint: Optional[str],
) -> Tuple[str, Optional[str]]:
    """Compare the live certificate against the pin, or mint one on first sight.

    Returns ``(outcome, fingerprint_to_store)``. ``outcome`` is ``"match"``, ``"pinned"`` for a
    first enrolment, or ``"unavailable"`` when the certificate could not be read at all. Raises
    ``LXDCertificatePinError`` on a genuine mismatch.

    important: "unavailable" is not a failure. A cluster that cannot complete a handshake will
    fail the connection immediately afterwards on its own terms, and turning an unreadable
    certificate into a pin error would report a network problem as a security incident.
    """
    live = fetch_server_fingerprint(endpoint)
    if live is None:
        return "unavailable", None

    if not stored_fingerprint:
        logger.info(
            "Pinned %s to server certificate %s… on first connection", cluster_id, live[:16]
        )
        return "pinned", live

    if live != stored_fingerprint:
        logger.error(
            "Certificate mismatch for %s: pinned %s… but the endpoint presented %s…. Refusing "
            "the connection. If this cluster's certificate was rotated deliberately, re-pin it; "
            "otherwise the endpoint is not the one this control plane enrolled.",
            cluster_id, stored_fingerprint[:16], live[:16],
        )
        raise LXDCertificatePinError(
            f"Cluster {cluster_id} presented an unexpected server certificate. "
            f"Re-pin the cluster if its certificate was rotated."
        )

    return "match", None
