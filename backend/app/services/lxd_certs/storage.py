"""Vault-backed storage for cluster metadata, secrets and certificate material."""

import logging
from typing import Any, Dict, Optional

from app.core import vault_status
from app.core.url_safety import normalize_endpoint
from app.core.vault_client import VaultClientError

from .types import LXDCertManagerError

logger = logging.getLogger(__name__)


class VaultStorageMixin:
    """Reads and writes everything this service keeps in Vault.

    Expects ``self.vault`` and ``self.clusters_path`` from the composing class.
    """

    def save_cluster_metadata(
        self,
        cluster_id: str,
        display_name: str,
        endpoint: str,
        zone: str = "",
        cluster_type: Optional[str] = None,
        merchant_id: str = "",
        location: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Save cluster metadata to Vault.

            cluster_id: Unique cluster identifier
            display_name: Human-readable display name
            endpoint: LXD API endpoint (ex; https://xxx:8443)
            zone: Geographical or logical zone
            cluster_type: Type of cluster (auto-detected from hypervisor)
            merchant_id: Merchant/organization identifier
            location: Geographic location information (region, country, coordinates)
        """
        metadata = {
            "display_name": display_name,
            # Normalized here as well as at the request boundary, so the internal callers
            # that re-save metadata (rename, location update) also clean the stored value.
            "endpoint": normalize_endpoint(endpoint),
            "zone": zone,
            "type": cluster_type,
            "merchant_id": merchant_id,
            "location": location or {}
        }
        
        try:
            path = f"{self.clusters_path}/{cluster_id}/metadata"
            self.vault.vault_kv_put(path, metadata)
            logger.info(f"Saved metadata for cluster {cluster_id}")
            return True
        except VaultClientError as e:
            logger.error(f"Failed to save cluster metadata: {e}")
            raise LXDCertManagerError(f"Failed to save metadata: {str(e)}")
    
    def save_cluster_secrets(
        self,
        cluster_id: str,
        trust_password: Optional[str] = None
    ) -> bool:
        """
        Save cluster secrets to Vault.
        """
        secrets = {}
        if trust_password:
            secrets["trust_password"] = trust_password
        
        try:
            path = f"{self.clusters_path}/{cluster_id}/secrets"
            self.vault.vault_kv_put(path, secrets)
            logger.info(f"Saved secrets for cluster {cluster_id}")
            return True
        except VaultClientError as e:
            logger.error(f"Failed to save cluster secrets: {e}")
            raise LXDCertManagerError(f"Failed to save secrets: {str(e)}")
    
    def generate_and_store_certificate(
        self,
        cluster_id: str,
        common_name: Optional[str] = None,
        ttl: str = "720h"
    ) -> Dict[str, str]:
        """
        Generate certificate via Vault PKI and store it.
        """
        if common_name is None:
            from app.core.config import get_settings
            domain = get_settings().orcastra_domain
            common_name = f"{cluster_id}.{domain}"
        
        try:
            # Generate certificate (FQDN required for Vault PKI validation)
            cert_data = self.vault.generate_lxd_certificate(common_name, ttl)
            
            # Store certificate in Vault (only persistent storage)
            path = f"{self.clusters_path}/{cluster_id}/certs"
            self.vault.vault_kv_put(path, {
                "client.crt": cert_data["cert"],
                "client.key": cert_data["key"],
                "ca.pem": cert_data["ca"],
                "common_name": common_name
            })
            
            logger.info(f"Generated and stored certificate for cluster {cluster_id}")
            return cert_data
            
        except VaultClientError as e:
            logger.error(f"Failed to generate/store certificate: {e}")
            raise LXDCertManagerError(f"Certificate generation failed: {str(e)}")
    
    def get_cluster_certificate(self, cluster_id: str) -> Dict[str, str]:
        """
        Retrieve cluster certificate from Vault.
        
        Certificates are fetched directly from Vault.
        NO Redis caching - Vault is the only source of truth.
        """
        try:
            path = f"{self.clusters_path}/{cluster_id}/certs"
            cert_data = self.vault.vault_read(path)
            return cert_data
            
        except VaultClientError as e:
            logger.error(f"Failed to retrieve certificate for {cluster_id}: {e}")
            raise LXDCertManagerError(f"Certificate retrieval failed: {str(e)}")

    def remove_trust_password(self, cluster_id: str) -> bool:
        """
        Remove trust password from Vault after certificate is registered.
        """
        try:
            path = f"{self.clusters_path}/{cluster_id}/secrets"
            # Read current secrets
            secrets = self.vault.vault_read(path)
            # Remove trust_password if it exists
            if "trust_password" in secrets:
                del secrets["trust_password"]
                # Update secrets
                self.vault.vault_kv_put(path, secrets)
                logger.info(f"Removed trust password for cluster {cluster_id}")
            return True
        except Exception as e:
            logger.warning(f"Failed to remove trust password for {cluster_id}: {e}")
            return False
    
    def get_cluster_info(self, cluster_id: str, include_cert_content: bool = False) -> Dict[str, Any]:
        """
        Get all cluster information from Vault.
        """
        try:
            metadata = self.vault.vault_read(f"{self.clusters_path}/{cluster_id}/metadata")
            
            # Try to read secrets (may not exist if not provided during registration)
            try:
                secrets = self.vault.vault_read(f"{self.clusters_path}/{cluster_id}/secrets")
            except VaultClientError:
                logger.debug(f"No secrets found for cluster {cluster_id}")
                secrets = {}
            
            # For cert info, only read metadata unless full content is requested
            if include_cert_content:
                try:
                    cert_data = self.vault.vault_read(f"{self.clusters_path}/{cluster_id}/certs")
                except VaultClientError:
                    logger.debug(f"No certificate found for cluster {cluster_id}")
                    cert_data = {}
            else:
                # Just check if cert exists without reading full content (faster)
                cert_data = {"common_name": metadata.get("common_name", "")}
            
            # Sanitize secrets (don't return passwords)
            secrets_sanitized = {
                "has_trust_password": "trust_password" in secrets
            }
            
            # Return certificate metadata only (unless full content requested)
            cert_info = {
                "has_certificate": bool(cert_data.get("client.crt")) if include_cert_content else True,
                "common_name": cert_data.get("common_name", "")
            }
            
            return {
                "cluster_id": cluster_id,
                "metadata": metadata,
                "secrets": secrets_sanitized,
                "certificate": cert_info
            }
        except VaultClientError as e:
            logger.error(f"Failed to get cluster info for {cluster_id}: {e}")
            raise LXDCertManagerError(f"Failed to retrieve cluster info: {str(e)}")
    
    def list_clusters(self, use_cache: bool = True) -> list:
        """
        List all registered clusters.

        Results are automatically filtered by the current request's
        org-scope context variable (set by ``org_scope_dependency``).
        Admin / background tasks see all clusters.
        """
        try:
            # Convert secret/data/clusters to secret/metadata/clusters for LIST operation
            list_path = self.clusters_path.replace("/data/", "/metadata/")
            cluster_ids = self.vault.vault_list(list_path, use_cache=use_cache)
            # Remove trailing slashes from cluster IDs
            cluster_ids = [cid.rstrip('/') for cid in cluster_ids]
            logger.debug(f"Found {len(cluster_ids)} clusters in Vault (cached: {use_cache})")

            # apply org-scope filter (no-op for admin / background tasks)
            from app.core.org_scope import apply_cluster_filter
            cluster_ids = apply_cluster_filter(cluster_ids)

            vault_status.mark_readable()
            return cluster_ids
        except Exception as e:
            # important: this is the one method in the mixin that swallows instead of raising,
            # and thirteen callers depend on that. It stays, but the swallow is now recorded.
            # A sealed Vault answers 503 to LIST, so without this an empty list from a Vault
            # that never answered is indistinguishable from a Vault holding no clusters, and
            # the deployment presents as healthy and empty. Startup and /health read
            # vault_status to tell the two apart.
            vault_status.mark_unreadable(f"{type(e).__name__}: {e}")
            # ERROR, not WARNING: the success line beside it is debug, so at production log
            # level this was the only line about the registry and it did not stand out.
            logger.error(f"Failed to list clusters, registry is unreadable: {e}")
            return []
    
    def delete_cluster_data(self, cluster_id: str) -> bool:
        """
        Delete all cluster data from Vault.
        
        Removes:
        - Cluster metadata from Vault
        - Cluster secrets from Vault  
        - Cluster certificates from Vault
        """
        try:
            # Delete from Vault (metadata, secrets, certs)
            paths_to_delete = [
                f"{self.clusters_path}/{cluster_id}/metadata",
                f"{self.clusters_path}/{cluster_id}/secrets", 
                f"{self.clusters_path}/{cluster_id}/certs"
            ]
            
            for path in paths_to_delete:
                try:
                    self.vault.vault_delete(path)
                    logger.debug(f"Deleted Vault path: {path}")
                except VaultClientError as e:
                    logger.debug(f"Path {path} may not exist: {e}")
            
            # Invalidate cluster list cache (this is for API response caching, not certs)
            try:
                from app.core.cache import get_cache_manager
                cache = get_cache_manager()
                cache.invalidate_cluster_cache(cluster_id)
            except Exception as e:
                logger.debug(f"Cache invalidation error: {e}")
            
            logger.info(f"Deleted all data for cluster {cluster_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to delete cluster data for {cluster_id}: {e}")
            return False
