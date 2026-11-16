"""Server-certificate pinning for LXD endpoints.

Connections to LXD use `verify=False` because LXD does not use a public CA: its trust model is that
the client remembers the server's certificate fingerprint, which `lxc remote add` asks the user to
accept. Without a pin there is no server authentication, and anyone on the path to a hypervisor can
present their own certificate and read or forge the mTLS session: instance configs, exec streams,
console traffic.

The certificate is trusted on first use and then pinned, as SSH and LXD itself do. This protects
against an attacker who arrives after enrolment, not one already in position when a cluster is
first seen. A fingerprint typed by the operator at registration would be stronger, but a required
one would refuse every registered cluster that has no pin yet when it comes back online.
"""

import hashlib
import ipaddress
import logging
import socket
import ssl
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

FINGERPRINT_FIELD = "server_cert_fingerprint"
PINNED_AT_FIELD = "server_cert_pinned_at"
TRUST_FIELD = "server_cert_trust"
PINNED_HOST_FIELD = "server_cert_pinned_host"
PREVIOUS_FIELD = "server_cert_previous"

TRUST_TOFU = "tofu"
TRUST_PUBLIC_CA = "public-ca"

# Six renewals is about a year of Let's Encrypt at its 60-day cadence. Anything that holds a
# fingerprint longer than that (a snapshot schedule) moves itself forward to the current pin when
# it runs, so the bound only has to cover the gap between two uses.
MAX_PREVIOUS = 6

_HANDSHAKE_TIMEOUT = 5

_unavailable_warned: set = set()
_unavailable_lock = threading.Lock()


class LXDCertificatePinError(Exception):
    """The endpoint presented a certificate that is not the one this cluster was pinned to."""


def endpoint_host_port(endpoint: str) -> Tuple[str, int]:
    """The host and port the LXD client dials for this endpoint.

    important: the port default follows the scheme, as requests does. Defaulting a portless
    endpoint to LXD's 8443 would send the pin probe and the real connection to different sockets:
    a cluster registered as `https://name` (served on 443) would never be pinned, and Migration
    Center would refuse every plan on it.
    """
    parsed = urlparse(endpoint if "://" in endpoint else f"https://{endpoint}")
    if not parsed.hostname:
        raise ValueError(f"Cannot parse an LXD endpoint out of {endpoint!r}")
    scheme = (parsed.scheme or "https").lower()
    # `wss://` and `ws://` are the same sockets as `https://` and `http://` (lxd_ws.ws_base).
    return parsed.hostname, parsed.port or (80 if scheme in ("http", "ws") else 443)


def _is_https(endpoint: str) -> bool:
    return "://" not in endpoint or endpoint.split("://", 1)[0].lower() == "https"


def _pinned_host(endpoint: str) -> str:
    """The host and port a public-CA pin is bound to.

    The port is part of it: another TLS service on the same name, with its own valid certificate,
    is not this cluster's API.
    """
    host, port = endpoint_host_port(endpoint)
    return f"{host.lower().rstrip('.')}:{port}"


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def fetch_server_fingerprint(endpoint: str, timeout: int = _HANDSHAKE_TIMEOUT) -> Optional[str]:
    """SHA-256 of the endpoint's presented certificate, or None if it cannot be reached.

    This is a separate TLS handshake: `requests` does not expose the peer certificate when
    `verify=False`, and reading it off the urllib3 connection depends on internals that change
    between releases.
    """
    if not _is_https(endpoint):
        return None
    host, port = endpoint_host_port(endpoint)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # The handshake only identifies the certificate; pinning takes the place of verification.
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


def _verified_leaf_fingerprint(host: str, port: int, timeout: int) -> Optional[str]:
    import certifi

    context = ssl.create_default_context(cafile=certifi.where())
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except Exception as exc:
        logger.debug("No publicly trusted certificate for %s:%s: %s", host, port, exc)
        return None
    return hashlib.sha256(der).hexdigest() if der else None


def fetch_public_ca_fingerprint(endpoint: str, timeout: int = _HANDSHAKE_TIMEOUT) -> Optional[str]:
    """SHA-256 of the leaf when the chain verifies against public CAs for the endpoint's hostname.

    None for an IP literal: a public CA does not vouch for an address on a private network, so an
    IP endpoint is only ever pinned on first use. None for anything that fails verification,
    including a self-signed LXD certificate, which is the normal case.
    """
    if not _is_https(endpoint):
        return None
    host, port = endpoint_host_port(endpoint)
    if _is_ip_literal(host):
        return None
    return _verified_leaf_fingerprint(host, port, timeout)


def _is_fingerprint(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


@dataclass(frozen=True)
class PinState:
    """What a cluster's Vault metadata says about the certificate it must present."""

    fingerprint: str
    trust: str
    host: Optional[str]
    previous: Tuple[str, ...]

    @classmethod
    def from_metadata(cls, metadata: Optional[Mapping[str, Any]]) -> Optional["PinState"]:
        metadata = metadata or {}
        fingerprint = metadata.get(FINGERPRINT_FIELD)
        if not isinstance(fingerprint, str) or not fingerprint:
            return None
        trust = TRUST_PUBLIC_CA if metadata.get(TRUST_FIELD) == TRUST_PUBLIC_CA else TRUST_TOFU
        host = metadata.get(PINNED_HOST_FIELD)
        previous = tuple(
            entry["fingerprint"]
            for entry in (metadata.get(PREVIOUS_FIELD) or [])
            if isinstance(entry, Mapping) and _is_fingerprint(entry.get("fingerprint"))
        ) if isinstance(metadata.get(PREVIOUS_FIELD), list) else ()
        return cls(
            fingerprint=fingerprint,
            trust=trust,
            host=host if isinstance(host, str) and host else None,
            previous=previous[:MAX_PREVIOUS],
        )

    def accepts(self, fingerprint: Optional[str]) -> bool:
        """Whether a fingerprint recorded earlier still names this registration.

        A public-CA cluster's renewals are the same registration; a self-signed cluster has no
        lineage, so only its current pin counts.
        """
        if not fingerprint:
            return False
        if fingerprint == self.fingerprint:
            return True
        return self.trust == TRUST_PUBLIC_CA and fingerprint in self.previous


@dataclass(frozen=True)
class PinDecision:
    """``outcome`` is "unavailable", "pinned", "match" or "renewed".

    ``fingerprint`` is what the real socket must present (None: connect unpinned), and ``update``
    holds the pin fields to merge into the cluster's metadata, None when nothing changes.
    """

    outcome: str
    fingerprint: Optional[str]
    update: Optional[dict]


def _now_iso(now: Optional[datetime]) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def _classification(endpoint: str, fingerprint: str) -> dict:
    if fetch_public_ca_fingerprint(endpoint) == fingerprint:
        return {
            TRUST_FIELD: TRUST_PUBLIC_CA,
            PINNED_HOST_FIELD: _pinned_host(endpoint),
            PREVIOUS_FIELD: [],
        }
    return {TRUST_FIELD: TRUST_TOFU}


def repin_fields(endpoint: str, fingerprint: str, *, now: Optional[datetime] = None) -> dict:
    """A fresh pin: classified like a first sighting, with no lineage carried over.

    An Administrator re-pin is a new trust decision, so the previous pin's lineage is dropped and
    its fingerprints stop naming this registration.
    """
    fields = {FINGERPRINT_FIELD: fingerprint, PINNED_AT_FIELD: _now_iso(now)}
    fields.update(_classification(endpoint, fingerprint))
    fields.setdefault(PREVIOUS_FIELD, [])
    if fields[TRUST_FIELD] == TRUST_TOFU:
        fields[PINNED_HOST_FIELD] = None
    return fields


def _reset_unavailable_warnings() -> None:
    with _unavailable_lock:
        _unavailable_warned.clear()


def _warn_unavailable(cluster_id: str, endpoint: str) -> None:
    with _unavailable_lock:
        if cluster_id in _unavailable_warned:
            return
        _unavailable_warned.add(cluster_id)
    try:
        host, port = endpoint_host_port(endpoint)
        where = f"{host}:{port}"
    except ValueError:
        where = endpoint
    reason = "not an https endpoint" if not _is_https(endpoint) else "no TLS handshake"
    logger.warning(
        "Could not read the server certificate of %s at %s (%s); it stays unpinned until it can",
        cluster_id, where, reason,
    )


def _rearm_unavailable(cluster_id: str) -> None:
    with _unavailable_lock:
        _unavailable_warned.discard(cluster_id)


def decide_pin(
    cluster_id: str,
    endpoint: str,
    metadata: Optional[Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> PinDecision:
    """Compare the live certificate against the pin, mint one on first sight, or accept a renewal.

    important: "unavailable" is not a failure. A cluster that cannot complete a handshake fails the
    real connection right after on its own terms, and a pin error would report a network problem as
    a security incident. The stored pin is still returned so the real socket is held to it.

    A renewal is accepted only when the cluster was pinned as public-CA, the endpoint still names
    the pinned host, and a verifying handshake saw the same certificate as the identifying one.
    Any other change is refused, including a self-signed certificate replacing a public one: that
    downgrade is the shape an interception takes.
    """
    metadata = metadata or {}
    state = PinState.from_metadata(metadata)
    live = fetch_server_fingerprint(endpoint) if _is_https(endpoint) else None
    if live is None:
        _warn_unavailable(cluster_id, endpoint)
        return PinDecision("unavailable", state.fingerprint if state else None, None)
    _rearm_unavailable(cluster_id)

    if state is None:
        update = repin_fields(endpoint, live, now=now)
        logger.info(
            "Pinned %s to server certificate %s… (%s) on first connection",
            cluster_id, live[:16], update[TRUST_FIELD],
        )
        return PinDecision("pinned", live, update)

    if live == state.fingerprint:
        # A pin recorded before the trust kind existed is classified once, on a matching
        # connection, so it is settled before its certificate is next replaced.
        if metadata.get(TRUST_FIELD) not in (TRUST_TOFU, TRUST_PUBLIC_CA):
            return PinDecision("match", live, _classification(endpoint, live))
        return PinDecision("match", live, None)

    if (
        state.trust == TRUST_PUBLIC_CA
        and state.host == _pinned_host(endpoint)
        and fetch_public_ca_fingerprint(endpoint) == live
    ):
        replaced_at = _now_iso(now)
        lineage = [
            {
                "fingerprint": state.fingerprint,
                "pinned_at": metadata.get(PINNED_AT_FIELD),
                "replaced_at": replaced_at,
            }
        ]
        lineage += [
            entry for entry in (metadata.get(PREVIOUS_FIELD) or [])
            if isinstance(entry, Mapping) and _is_fingerprint(entry.get("fingerprint"))
        ]
        logger.warning(
            "Certificate of %s renewed: %s… replaced %s…, publicly trusted for %s",
            cluster_id, live[:16], state.fingerprint[:16], state.host,
        )
        return PinDecision(
            "renewed",
            live,
            {
                FINGERPRINT_FIELD: live,
                PINNED_AT_FIELD: replaced_at,
                PREVIOUS_FIELD: lineage[:MAX_PREVIOUS],
            },
        )

    logger.error(
        "Certificate mismatch for %s: pinned %s… (%s) but the endpoint presented %s…. Refusing "
        "the connection. If this cluster's certificate was rotated deliberately, re-pin it; "
        "otherwise the endpoint is not the one this control plane enrolled.",
        cluster_id, state.fingerprint[:16], state.trust, live[:16],
    )
    raise LXDCertificatePinError(
        f"Cluster {cluster_id} presented an unexpected server certificate. "
        f"Re-pin the cluster if its certificate was rotated."
    )
