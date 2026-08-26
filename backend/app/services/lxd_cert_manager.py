"""
LXD Certificate Manager
Handles certificate generation via Vault PKI and LXD trust registration.

The implementation lives in ``app.services.lxd_certs``, split by responsibility once this file
reached 744 lines doing four separable jobs. This module stays the single import site: fourteen
callers say ``from app.services.lxd_cert_manager import LXDCertificateManager``, and the mixins
compose back into exactly the class they already had. Nothing about the public surface moved.
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
    """
    Manages LXD certificate lifecycle:
    - Generate certificates via Vault PKI
    - Register certificates with LXD trust
    - Store and retrieve certificates from Vault
    """

    def __init__(self, vault_client: VaultClient, clusters_path: str = "secret/data/clusters"):
        self.vault = vault_client
        self.clusters_path = clusters_path
