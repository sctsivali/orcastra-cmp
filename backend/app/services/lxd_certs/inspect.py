"""Reading a client certificate: what it says about itself, and how close it is to expiry.

Shared by the certificate, warnings and permissions routers and by certificate rotation.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from cryptography import x509
from cryptography.hazmat.backends import default_backend

from app.core.cert_fingerprint import lxd_fingerprint
from app.core.type_utils import normalize_cluster_type
from app.models.schemas import CertificateInfo, CertificateLifetime, CertificateStatus

logger = logging.getLogger(__name__)

# Days before expiry at which a certificate is flagged, by severity.
WARNING_THRESHOLDS = {
    "critical": 3,
    "high": 7,
    "warning": 14,
}


def _common_name(name: x509.Name) -> Optional[str]:
    for attr in name:
        if attr.oid == x509.oid.NameOID.COMMON_NAME:
            return attr.value
    return None


def _utc(cert: x509.Certificate, field: str) -> datetime:
    # The *_utc accessors exist from cryptography 42; requirements still admit 41.
    aware = getattr(cert, f"{field}_utc", None)
    if aware is not None:
        return aware
    return getattr(cert, field).replace(tzinfo=timezone.utc)


def parse_certificate_pem(cert_pem: str) -> Dict[str, Any]:
    """Issuer, subject, validity, serial and the LXD fingerprint of one PEM certificate.

    Returns an empty dict when the PEM cannot be parsed, which callers render as unknown.
    """
    try:
        cert = x509.load_pem_x509_certificate(cert_pem.encode(), default_backend())
        return {
            "issuer": _common_name(cert.issuer),
            "subject": _common_name(cert.subject),
            "not_before": _utc(cert, "not_valid_before"),
            "not_after": _utc(cert, "not_valid_after"),
            "serial_number": format(cert.serial_number, "x").upper(),
            "fingerprint": lxd_fingerprint(cert_pem),
        }
    except Exception as exc:  # noqa: BLE001 - an unreadable certificate is reported, not raised
        logger.error("Failed to parse certificate: %s", exc)
        return {}


def days_until(expires_at: Optional[datetime]) -> Optional[int]:
    if not expires_at:
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return (expires_at - datetime.now(timezone.utc)).days


def get_certificate_status(expires_at: Optional[datetime], has_pending: bool = False) -> CertificateStatus:
    """Valid, expiring or expired from the expiry date; a pending renewal outranks all three."""
    if has_pending:
        return CertificateStatus.PENDING_RENEWAL
    remaining = days_until(expires_at)
    if remaining is None:
        return CertificateStatus.UNKNOWN
    if remaining < 0:
        return CertificateStatus.EXPIRED
    if remaining <= WARNING_THRESHOLDS["warning"]:
        return CertificateStatus.EXPIRING
    return CertificateStatus.VALID


def get_warning_severity(days_until_expiry: int) -> str:
    """Warning severity for a certificate this many days from expiry."""
    if days_until_expiry <= WARNING_THRESHOLDS["critical"]:
        return "critical"
    if days_until_expiry <= WARNING_THRESHOLDS["high"]:
        return "high"
    if days_until_expiry <= WARNING_THRESHOLDS["warning"]:
        return "warning"
    return "info"


def _stored_lifetime(value: Any) -> Optional[CertificateLifetime]:
    try:
        return CertificateLifetime(value) if value else None
    except ValueError:
        return None


def read_certificate_row(cert_manager: Any, cluster_id: str) -> Optional[Tuple[CertificateInfo, Optional[str]]]:
    """Blocking. One cluster's certificate row and its hypervisor type, or None when it has none.

    A Vault failure becomes an UNKNOWN row rather than an error, so one bad cluster does not blank
    the page.
    """
    from app.core.vault_client import VaultClientError

    base = f"{cert_manager.clusters_path}/{cluster_id}"
    try:
        certs = cert_manager.vault.vault_read(f"{base}/certs") or {}
        cert_pem = certs.get("client.crt", "")
        if not cert_pem:
            # A cluster still waiting for its first trust has nothing to show here.
            return None
        metadata = cert_manager.vault.vault_read(f"{base}/metadata") or {}
        pending_fp = None
        try:
            pending = cert_manager.vault.vault_read(f"{base}/pending_certs") or {}
            if pending.get("client.crt"):
                pending_fp = parse_certificate_pem(pending["client.crt"]).get("fingerprint")
        except VaultClientError:
            pending = {}
        parsed = parse_certificate_pem(cert_pem)
        expires_at = parsed.get("not_after")
        has_pending = bool(pending.get("client.crt"))
        info = CertificateInfo(
            cluster_id=cluster_id,
            common_name=certs.get("common_name") or parsed.get("subject"),
            endpoint=metadata.get("endpoint"),
            issuer=parsed.get("issuer"),
            issued_at=parsed.get("not_before"),
            expires_at=expires_at,
            days_until_expiry=days_until(expires_at),
            status=get_certificate_status(expires_at, has_pending),
            fingerprint=parsed.get("fingerprint"),
            has_pending_renewal=has_pending,
            serial_number=parsed.get("serial_number"),
            pending_fingerprint=pending_fp,
            cluster_type=normalize_cluster_type(metadata["type"]) if metadata.get("type") else None,
            lifetime=_stored_lifetime(certs.get("lifetime")),
        )
        return info, metadata.get("type")
    except VaultClientError as exc:
        logger.warning("Failed to get certificate for %s: %s", cluster_id, exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("Error processing certificate for %s: %s", cluster_id, exc)
    return CertificateInfo(cluster_id=cluster_id, status=CertificateStatus.UNKNOWN), None
