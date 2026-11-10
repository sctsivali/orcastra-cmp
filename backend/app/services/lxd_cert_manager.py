"""LXD certificate manager: Vault PKI certificate generation and LXD trust registration.

The implementation lives in ``app.services.lxd_certs`` as one mixin per responsibility. This
module composes them into ``LXDCertificateManager`` and is the import site for callers.
"""

import logging

from app.core.vault_client import VaultClient

from app.services.lxd_certs import (
    ConnectionProbe,
    ConnectivityMixin,
    LXDCertManagerError,
    TrustMixin,
    VaultStorageMixin,
    extract_cert_base64,
)

logger = logging.getLogger(__name__)

__all__ = [
    "LXDCertificateManager",
    "LXDCertManagerError",
    "ConnectionProbe",
    "extract_cert_base64",
]


class LXDCertificateManager(VaultStorageMixin, ConnectivityMixin, TrustMixin):
    """LXD client certificate lifecycle: Vault PKI issue, LXD trust, Vault storage."""

    def __init__(self, vault_client: VaultClient, clusters_path: str = "secret/data/clusters"):
        self.vault = vault_client
        self.clusters_path = clusters_path
