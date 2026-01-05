"""
LXD Certificate Manager
Handles certificate generation via Vault PKI and LXD trust registration.
"""

import logging
import os
import tempfile
from typing import Dict, Any, Optional, Tuple
import requests
from pylxd import Client

from app.core.vault_client import VaultClient, VaultClientError

logger = logging.getLogger(__name__)


class LXDCertManagerError(Exception):
    """Base exception for LXD certificate manager errors."""
    pass


class LXDCertificateManager:
    """
    Manages LXD certificate lifecycle:
    - Generate certificates via Vault PKI
    - Register certificates with LXD trust
    - Store and retrieve certificates from Vault
    """
    
    def __init__(self, vault_client: VaultClient, clusters_path: str = "secret/data/clusters"):
        self.vault = vault_client
        self.clusters_path = clusters_path
    
    def save_cluster_metadata(
        self,
        cluster_id: str,
        display_name: str,
        endpoint: str,
        zone: str = "",
        cluster_type: Optional[str] = None,
        merchant_id: str = ""
    ) -> bool:
        """
        Save cluster metadata to Vault.

            cluster_id: Unique cluster identifier
            display_name: Human-readable display name
            endpoint: LXD API endpoint (e.g., https://xxx:8443)
            zone: Geographical or logical zone
            cluster_type: Type of cluster (auto-detected from hypervisor)
            merchant_id: Merchant/organization identifier
        """
        metadata = {
            "display_name": display_name,
            "endpoint": endpoint,
            "zone": zone,
            "type": cluster_type,
            "merchant_id": merchant_id
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
            common_name = f"{cluster_id}.orcastra.io"
        
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
    
    def add_lxd_trust(
        self,
        cluster_id: str,
        cert_name: Optional[str] = None,
        projects: Optional[list] = None,
        restricted: bool = False
    ) -> bool:
        """
        Register certificate with LXD trust store.
        """
        if cert_name is None:
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            cert_name = f"orcastra-{timestamp}"
        
        try:
            # Get cluster metadata and secrets
            metadata = self.vault.vault_read(f"{self.clusters_path}/{cluster_id}/metadata")
            secrets = self.vault.vault_read(f"{self.clusters_path}/{cluster_id}/secrets")
            cert_data = self.get_cluster_certificate(cluster_id)
            
            endpoint = metadata["endpoint"]
            trust_password = secrets.get("trust_password")
            certificate = cert_data["client.crt"]
            
            # Prepare payload
            payload = {
                "type": "client",
                "certificate": certificate,
                "name": cert_name,
                "restricted": restricted
            }
            
            if projects:
                payload["projects"] = projects
            
            # Add to LXD trust
            url = f"{endpoint}/1.0/certificates"
            
            # Use trust password for authentication if available
            auth = ("", trust_password) if trust_password else None
            
            response = requests.post(
                url,
                json=payload,
                auth=auth,
                verify=False,
                timeout=10
            )
            
            response.raise_for_status()
            logger.info(f"Successfully added certificate to LXD trust for cluster {cluster_id}")
            return True
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to add LXD trust for {cluster_id}: {e}")
            raise LXDCertManagerError(f"LXD trust registration failed: {str(e)}")
        except VaultClientError as e:
            logger.error(f"Failed to retrieve data from Vault: {e}")
            raise LXDCertManagerError(f"Vault data retrieval failed: {str(e)}")
    
    def connect_with_vault_cert(self, cluster_id: str, connection_manager=None) -> Client:
        """
        Connect to LXD using certificate from Vault.
        
        If connection_manager is provided, delegates to it for proper
        connection pooling and lifecycle management. Otherwise creates standalone client.
        """
        try:
            # Get metadata and certificate from Vault
            metadata = self.vault.vault_read(f"{self.clusters_path}/{cluster_id}/metadata")
            cert_data = self.get_cluster_certificate(cluster_id)
            
            endpoint = metadata["endpoint"]
            cert_content = cert_data["client.crt"]
            key_content = cert_data["client.key"]
            
            # Use connection_manager if provided (proper pooling)
            if connection_manager is not None:
                return connection_manager.connect_with_vault_cert(
                    cluster_id=cluster_id,
                    endpoint=endpoint,
                    cert_content=cert_content,
                    key_content=key_content,
                    verify=False
                )
            
            # Fallback: Create standalone client (for one-off operations)
            # Note: This path doesn't benefit from connection pooling
            # WARNING: Don't delete temp files in finally block - pylxd needs them
            cert_fd, cert_path = tempfile.mkstemp(suffix='.crt', prefix=f'lxd_cm_{cluster_id}_')
            key_fd, key_path = tempfile.mkstemp(suffix='.key', prefix=f'lxd_cm_{cluster_id}_')
            
            os.write(cert_fd, cert_content.encode('utf-8'))
            os.close(cert_fd)
            os.chmod(cert_path, 0o600)
            
            os.write(key_fd, key_content.encode('utf-8'))
            os.close(key_fd)
            os.chmod(key_path, 0o600)
            
            client = Client(
                endpoint=endpoint,
                cert=(cert_path, key_path),
                verify=False
            )
            
            # Store temp file paths on client for later cleanup if needed
            client._temp_cert_path = cert_path
            client._temp_key_path = key_path
            
            logger.info(f"Created standalone connection to {cluster_id}")
            return client
                        
        except Exception as e:
            logger.error(f"Failed to connect to cluster {cluster_id}: {e}")
            raise LXDCertManagerError(f"Connection failed: {str(e)}")
    
    def verify_connection(self, cluster_id: str) -> Tuple[bool, str]:
        """
        Verify connection to LXD cluster and authentication status.
        
        To tests if the certificate is trusted by attempting to
        access protected resources that require authentication.
        
        Uses temporary files that are immediately cleaned up after verification.
        Certificate data is fetched fresh from Vault each time.
        """
        cert_path = None
        key_path = None
        
        try:
            # Get cluster metadata and certificate fresh from Vault
            metadata = self.vault.vault_read(f"{self.clusters_path}/{cluster_id}/metadata")
            cert_data = self.get_cluster_certificate(cluster_id)
            
            endpoint = metadata["endpoint"]
            
            # Create secure temp files
            cert_fd, cert_path = tempfile.mkstemp(suffix='.crt', prefix=f'lxd_verify_{cluster_id}_')
            key_fd, key_path = tempfile.mkstemp(suffix='.key', prefix=f'lxd_verify_{cluster_id}_')
            
            # Write certificates with proper permissions
            os.write(cert_fd, cert_data["client.crt"].encode('utf-8'))
            os.close(cert_fd)
            os.chmod(cert_path, 0o600)
            
            os.write(key_fd, cert_data["client.key"].encode('utf-8'))
            os.close(key_fd)
            os.chmod(key_path, 0o600)
            
            # Test authenticated endpoint that requires trust
            # `/1.0/instances` requires authentication, `/1.0` doesn't always
            test_url = f"{endpoint}/1.0/instances"
            
            # Retry with increasing timeout for resilience
            last_error = None
            response = None
            for attempt, timeout_info in enumerate([(10, "first"), (15, "second"), (20, "third")], 1):
                try:
                    response = requests.get(
                        test_url,
                        cert=(cert_path, key_path),
                        verify=False,
                        timeout=timeout_info[0]
                    )
                    break  # Success, exit retry loop
                except requests.exceptions.Timeout as e:
                    last_error = e
                    logger.warning(f"Timeout on {timeout_info[1]} attempt for {cluster_id}, retrying...")
                    continue
                except requests.exceptions.ConnectionError as e:
                    last_error = e
                    logger.warning(f"Connection error on {timeout_info[1]} attempt for {cluster_id}: {e}")
                    continue
            
            if response is None:
                # All retries exhausted
                logger.error(f"All connection attempts failed for {cluster_id}: {last_error}")
                return False, "Connection failed after multiple retries. Check network connectivity."
            
            # Check response status
            if response.status_code == 200:
                # Successfully authenticated
                logger.info(f"Certificate trusted and authenticated for {cluster_id}")
                return True, "Certificate trusted and authenticated"
            elif response.status_code == 403:
                # Certificate not trusted
                logger.warning(f"Certificate not trusted for {cluster_id}")
                return False, "Certificate not trusted by hypervisor. Please add trust via CLI."
            else:
                logger.warning(f"Unexpected status {response.status_code} for {cluster_id}")
                return False, f"Unexpected response: HTTP {response.status_code}"
            
        except requests.exceptions.SSLError as e:
            logger.error(f"SSL error for {cluster_id}: {e}")
            return False, "Certificate error. Verify certificate is valid."
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error for {cluster_id}: {e}")
            return False, "Cannot connect to LXD endpoint. Check if server is running."
        except requests.exceptions.Timeout as e:
            logger.error(f"Timeout for {cluster_id}: {e}")
            return False, "Connection timeout. Check network connectivity."
        except Exception as e:
            logger.error(f"Verification failed for {cluster_id}: {e}")
            return False, f"Connection failed: {str(e)}"
        finally:
            # Clean up verify-specific temp files (not the main connection files)
            try:
                if cert_path and os.path.exists(cert_path):
                    os.unlink(cert_path)
                if key_path and os.path.exists(key_path):
                    os.unlink(key_path)
            except Exception:
                pass
    
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
        """
        try:
            # Convert secret/data/clusters to secret/metadata/clusters for LIST operation
            list_path = self.clusters_path.replace("/data/", "/metadata/")
            cluster_ids = self.vault.vault_list(list_path, use_cache=use_cache)
            # Remove trailing slashes from cluster IDs
            cluster_ids = [cid.rstrip('/') for cid in cluster_ids]
            logger.debug(f"Found {len(cluster_ids)} clusters in Vault (cached: {use_cache})")
            return cluster_ids
        except Exception as e:
            logger.warning(f"Failed to list clusters: {e}")
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
