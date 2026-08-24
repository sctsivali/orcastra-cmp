"""
Cluster connection status vocabulary and the single derivation that produces it.

Every surface that reports whether a cluster can be used goes through `derive_status`.
It exists because three call sites used to answer the question three different ways:
`/clusters` collapsed any failure to NOT_AUTHORIZED, `/regions` collapsed any failure to
OFFLINE, and `/monitoring` invented "pending_trust" by substring-matching the error text.
An operator reading two pages about one dead cluster saw two different reasons.
"""

from enum import Enum
from typing import Optional, Tuple


class ClusterStatus(str, Enum):
    """Cluster connection status."""
    PENDING = "pending"
    NOT_AUTHORIZED = "not_authorized"
    ONLINE = "online"
    OFFLINE = "offline"
    ERROR = "error"


class AvailabilityReason(str, Enum):
    """Machine-readable reason behind a status.

    The frontend keys its copy off this, never off the human sentence, so rewording a
    message can never change what the UI decides.
    """
    OK = "ok"
    UNTRUSTED_CERTIFICATE = "untrusted_certificate"
    UNREACHABLE = "unreachable"
    NO_CERTIFICATE = "no_certificate"
    UNKNOWN = "unknown"


_UNTRUSTED_TEXT = (
    "Cluster answered but rejected our certificate. Add trust on the cluster."
)
_NO_CERT_TEXT = "No client certificate has been issued for this cluster yet."
_UNREACHABLE_TEXT = "Cluster did not answer."


def derive_status(
    *,
    connected: bool,
    reachable: Optional[bool],
    auth: Optional[str] = None,
    has_certificate: bool = True,
    error: Optional[str] = None,
) -> Tuple[ClusterStatus, AvailabilityReason, str]:
    """Map one probe result to the status vocabulary. Returns (status, reason_code, message).

    important: `reachable` is the whole discriminator, and nothing here inspects the text of
    `error`. NOT_AUTHORIZED means the host answered and refused our certificate; OFFLINE means
    it never answered. Guessing that from a message is what labelled seven dead clusters
    "Pending Trust" on the Clusters page.

    note: `has_certificate` is accepted but callers on read paths pass the default. Its only
    honest source, LXDCertificateManager.get_cluster_info, hardcodes True unless the caller
    also asks for the certificate body, which costs a Vault round trip per cluster. So PENDING
    reaches this function only from the registration flow, which does have the real answer.
    """
    if connected:
        return ClusterStatus.ONLINE, AvailabilityReason.OK, ""

    if reachable is True:
        detail = f"{_UNTRUSTED_TEXT} (auth={auth})" if auth else _UNTRUSTED_TEXT
        return ClusterStatus.NOT_AUTHORIZED, AvailabilityReason.UNTRUSTED_CERTIFICATE, detail

    if reachable is False:
        if not has_certificate:
            return ClusterStatus.PENDING, AvailabilityReason.NO_CERTIFICATE, _NO_CERT_TEXT
        return ClusterStatus.OFFLINE, AvailabilityReason.UNREACHABLE, error or _UNREACHABLE_TEXT

    # reachable is None: the probe predates this field, or the caller could not observe it.
    # `auth` is only ever populated after host_info was read, so its presence is a second
    # structural signal that the host answered.
    if auth is not None:
        return (
            ClusterStatus.NOT_AUTHORIZED,
            AvailabilityReason.UNTRUSTED_CERTIFICATE,
            error or f"Cluster answered but is not usable (auth={auth}).",
        )

    # important: default to OFFLINE, never ERROR. connected=False already means we could not
    # use the cluster, and a picker must not present an unconfirmed cluster as usable. ERROR is
    # reserved for a fault in us (a failed metadata read), not for a cluster that is merely down.
    return ClusterStatus.OFFLINE, AvailabilityReason.UNKNOWN, error or _UNREACHABLE_TEXT
