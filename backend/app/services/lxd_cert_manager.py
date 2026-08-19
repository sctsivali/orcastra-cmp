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
from app.services.lxd import health
from app.services.lxd.connection import DEFAULT_LXD_TIMEOUT

logger = logging.getLogger(__name__)


class LXDCertManagerError(Exception):
    """Base exception for LXD certificate manager errors."""
    pass


def extract_cert_base64(pem_cert: str) -> str:
    """
    Extract pure base64 content from PEM certificate.
    LXD API expects base64-encoded certificate without PEM headers.
    """
    # Remove headers, footers, and whitespace
    lines = pem_cert.strip().split('\n')
    base64_lines = [
        line for line in lines 
        if not line.startswith('-----') and line.strip()
    ]
    return ''.join(base64_lines)


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
            "endpoint": endpoint,
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
    
    
    def connect_with_vault_cert(
        self, cluster_id: str, connection_manager=None, force: bool = False
    ) -> Optional[Client]:
        """
        Connect to LXD using certificate from Vault.
        
        If connection_manager is provided, delegates to it for proper
        connection pooling and lifecycle management. Otherwise creates standalone client.

        Returns None when the cluster is currently tripped in the breaker, unless ``force``.

        important: checked here, before the Vault reads, because those are two HTTP round
        trips paid before the LXD handshake even starts. Callers reach this path when
        get_client already returned None, so without this gate a dead cluster costs MORE per
        request after the breaker trips, not less.

        ``force`` is for registration, auth checks and certificate rotation: the operations
        that repair a broken cluster, which would be unreachable if the breaker sealed them.
        """
        if not force and health.is_tripped(cluster_id):
            logger.debug(
                "Skipping Vault connect to %s: recently unreachable (%s)",
                cluster_id, health.get_reason(cluster_id),
            )
            return None
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
                    verify=False,
                    force=force,
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
            
            # important: without an explicit timeout pylxd stores _timeout=None, so its own
            # constructor probe and every later client.api call on this standalone client
            # block forever against a peer that accepts TCP then goes silent. Matches the
            # value LXDConnectionManager builds pooled clients with.
            client = Client(
                endpoint=endpoint,
                cert=(cert_path, key_path),
                verify=False,
                timeout=DEFAULT_LXD_TIMEOUT
            )
            
            # Store temp file paths on client for later cleanup if needed
            client._temp_cert_path = cert_path
            client._temp_key_path = key_path
            
            logger.info(f"Created standalone connection to {cluster_id}")
            return client
                        
        except Exception as e:
            # important: WARNING, not ERROR. See connection.py: an offline cluster is
            # expected, and the caller still gets the exception to act on.
            logger.warning(f"Failed to connect to cluster {cluster_id}: {e}")
            raise LXDCertManagerError(f"Connection failed: {str(e)}")
    
    def verify_connection(self, cluster_id: str, use_breaker: bool = True) -> Tuple[bool, str]:
        """
        Verify connection to LXD cluster and authentication status.
        
        To tests if the certificate is trusted by attempting to
        access protected resources that require authentication.
        
        Uses temporary files that are immediately cleaned up after verification.
        Certificate data is fetched fresh from Vault each time.
        """
        # important: shared circuit breaker - skip clusters that failed to connect
        # within the TTL window so a dead cluster does not cost the full connect
        # timeout on every dashboard fan-out. User-initiated checks pass
        # use_breaker=False to force a fresh probe.
        from app.services.lxd.health import is_tripped, record_failure, clear, get_reason
        if use_breaker and is_tripped(cluster_id):
            return False, get_reason(cluster_id) or "Cluster recently unreachable - retry shortly"

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
            
            # Fail fast: short connect budget (3s) + bounded read (8s). Retry once
            # on a timeout, but never retry a refused/DNS failure - an unreachable
            # host must not cost 45s on every dashboard fan-out request.
            last_error = None
            response = None
            for attempt in range(2):
                try:
                    response = requests.get(
                        test_url,
                        cert=(cert_path, key_path),
                        verify=False,
                        timeout=(3, 8)
                    )
                    break  # got an HTTP response, exit retry loop
                except requests.exceptions.Timeout as e:
                    last_error = e
                    logger.warning(f"Timeout on attempt {attempt + 1} for {cluster_id}")
                    continue
                except requests.exceptions.ConnectionError as e:
                    last_error = e
                    logger.warning(f"Connection error for {cluster_id}: {e}")
                    break  # unreachable host - do not retry

            if response is None:
                # important: WARNING, not ERROR. Reached only after the retries above have
                # already logged the same host at WARNING; escalating the summary line for
                # a cluster that is merely offline is what makes ERROR unreadable.
                logger.warning(f"Connection failed for {cluster_id}: {last_error}")
                record_failure(cluster_id, "Cannot connect to LXD endpoint. Check if server is running.")
                return False, "Cannot connect to LXD endpoint. Check if server is running."
            
            # Host is reachable (we got an HTTP response) - clear any breaker trip.
            clear(cluster_id)

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
            # important: WARNING. Refused or unresolvable is an offline cluster, and the
            # breaker below is what the product acts on. The SSL branch above stays at
            # ERROR on purpose: an untrusted or expired certificate is a state somebody
            # has to fix, not one that clears itself when the host comes back.
            logger.warning(f"Connection error for {cluster_id}: {e}")
            record_failure(cluster_id, "Cannot connect to LXD endpoint. Check if server is running.")
            return False, "Cannot connect to LXD endpoint. Check if server is running."
        except requests.exceptions.Timeout as e:
            logger.warning(f"Timeout for {cluster_id}: {e}")
            record_failure(cluster_id, "Connection timeout. Check network connectivity.")
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

    def add_trust_with_existing_cert(
        self,
        cluster_id: str,
        new_cert_pem: str,
        cert_name: str,
        projects: Optional[list] = None,
        restricted: bool = False
    ) -> bool:
        """
        Add a new certificate to LXD trust store using existing trusted certificate.
        
        This allows seamless certificate renewal when the current cert is still valid.
        Uses the existing certificate to authenticate and add the new certificate.
        """
        cert_path = None
        key_path = None
        
        try:
            # Get cluster metadata and current certificate
            metadata = self.vault.vault_read(f"{self.clusters_path}/{cluster_id}/metadata")
            current_cert = self.get_cluster_certificate(cluster_id)
            
            endpoint = metadata["endpoint"]
            
            # Create temp files for current certificate
            cert_fd, cert_path = tempfile.mkstemp(suffix='.crt', prefix=f'lxd_add_trust_{cluster_id}_')
            key_fd, key_path = tempfile.mkstemp(suffix='.key', prefix=f'lxd_add_trust_{cluster_id}_')
            
            os.write(cert_fd, current_cert["client.crt"].encode('utf-8'))
            os.close(cert_fd)
            os.chmod(cert_path, 0o600)
            
            os.write(key_fd, current_cert["client.key"].encode('utf-8'))
            os.close(key_fd)
            os.chmod(key_path, 0o600)
            
            # Prepare payload for adding new certificate
            # LXD API expects base64-only format (no PEM headers)
            cert_base64 = extract_cert_base64(new_cert_pem)
            
            payload = {
                "type": "client",
                "certificate": cert_base64,
                "name": cert_name,
                "restricted": restricted
            }
            
            if projects:
                payload["projects"] = projects
            
            # Add new certificate using existing cert for authentication
            url = f"{endpoint}/1.0/certificates"
            
            response = requests.post(
                url,
                json=payload,
                cert=(cert_path, key_path),
                verify=False,
                timeout=15
            )
            
            if response.status_code in [200, 201]:
                logger.info(f"Successfully added new certificate to LXD trust for {cluster_id}")
                return True
            elif response.status_code == 409:
                # Certificate already exists - that's fine
                logger.info(f"Certificate already trusted for {cluster_id}")
                return True
            else:
                logger.warning(f"Failed to add trust: HTTP {response.status_code}")
                return False
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed when adding trust for {cluster_id}: {e}")
            raise LXDCertManagerError(f"Failed to add trust: {str(e)}")
        except VaultClientError as e:
            logger.error(f"Vault error: {e}")
            raise LXDCertManagerError(f"Vault error: {str(e)}")
        finally:
            # Cleanup temp files
            try:
                if cert_path and os.path.exists(cert_path):
                    os.unlink(cert_path)
                if key_path and os.path.exists(key_path):
                    os.unlink(key_path)
            except:
                pass

    def add_trust_with_password(
        self,
        cluster_id: str,
        new_cert_pem: str,
        cert_name: str,
        trust_password: str,
        projects: Optional[list] = None,
        restricted: bool = False
    ) -> bool:
        """
        Add a new certificate to LXD trust store using trust password.
        
        Used when the current certificate is expired and cannot be used
        for authentication.
        """
        try:
            # Get cluster metadata
            metadata = self.vault.vault_read(f"{self.clusters_path}/{cluster_id}/metadata")
            endpoint = metadata["endpoint"]
            
            # Extract base64 from PEM - LXD API expects base64-only format
            cert_base64 = extract_cert_base64(new_cert_pem)
            
            # Prepare payload
            payload = {
                "type": "client",
                "certificate": cert_base64,
                "name": cert_name,
                "restricted": restricted,
                "password": trust_password
            }
            
            if projects:
                payload["projects"] = projects
            
            # Add to LXD trust using password
            url = f"{endpoint}/1.0/certificates"
            
            response = requests.post(
                url,
                json=payload,
                verify=False,
                timeout=15
            )
            
            if response.status_code in [200, 201]:
                logger.info(f"Successfully added certificate with password for {cluster_id}")
                return True
            elif response.status_code == 409:
                logger.info(f"Certificate already trusted for {cluster_id}")
                return True
            else:
                logger.warning(f"Failed to add trust with password: HTTP {response.status_code}")
                return False
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed: {e}")
            raise LXDCertManagerError(f"Failed to add trust: {str(e)}")
        except VaultClientError as e:
            logger.error(f"Vault error: {e}")
            raise LXDCertManagerError(f"Vault error: {str(e)}")

    def verify_connection_with_cert(
        self,
        endpoint: str,
        cert_pem: str,
        key_pem: str
    ) -> bool:
        """
        Verify that a certificate can successfully connect to an LXD endpoint.
        
        Used to verify pending certificate before activating it.
        """
        cert_path = None
        key_path = None
        
        try:
            # Create temp files
            cert_fd, cert_path = tempfile.mkstemp(suffix='.crt', prefix='lxd_verify_')
            key_fd, key_path = tempfile.mkstemp(suffix='.key', prefix='lxd_verify_')
            
            os.write(cert_fd, cert_pem.encode('utf-8'))
            os.close(cert_fd)
            os.chmod(cert_path, 0o600)
            
            os.write(key_fd, key_pem.encode('utf-8'))
            os.close(key_fd)
            os.chmod(key_path, 0o600)
            
            # Test authenticated endpoint
            test_url = f"{endpoint}/1.0/instances"
            
            response = requests.get(
                test_url,
                cert=(cert_path, key_path),
                verify=False,
                timeout=10
            )
            
            if response.status_code == 200:
                logger.info(f"Certificate verified successfully for {endpoint}")
                return True
            elif response.status_code == 403:
                logger.warning(f"Certificate not trusted at {endpoint}")
                return False
            else:
                logger.warning(f"Unexpected status {response.status_code} for {endpoint}")
                return False
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Connection verification failed: {e}")
            return False
        finally:
            try:
                if cert_path and os.path.exists(cert_path):
                    os.unlink(cert_path)
                if key_path and os.path.exists(key_path):
                    os.unlink(key_path)
            except:
                pass
