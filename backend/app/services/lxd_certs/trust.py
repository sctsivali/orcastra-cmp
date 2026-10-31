"""Establishing trust for our certificate on a cluster."""

import logging
import os
import tempfile
from typing import Optional

import requests

from app.core.url_safety import normalize_endpoint
from app.core.vault_client import VaultClientError
from app.services.lxd import cert_files

from .types import LXDCertManagerError, extract_cert_base64

logger = logging.getLogger(__name__)


class TrustMixin:
    """Expects ``self.vault`` and ``self.clusters_path`` from the composing class."""

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
            
            endpoint = normalize_endpoint(metadata["endpoint"])
            
            # Create temp files for current certificate
            # dir= is load-bearing: see cert_files.staging_dir. Without it tempfile falls
            # through to /var/tmp when /tmp is full and the private key lands on disk.
            staging = cert_files.staging_dir()
            cert_fd, cert_path = tempfile.mkstemp(suffix='.crt', dir=staging, prefix=f'lxd_add_trust_{cluster_id}_')
            key_fd, key_path = tempfile.mkstemp(suffix='.key', dir=staging, prefix=f'lxd_add_trust_{cluster_id}_')
            
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
            raise LXDCertManagerError(f"Could not add trust: {str(e)}")
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
            endpoint = normalize_endpoint(metadata["endpoint"])
            
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
            raise LXDCertManagerError(f"Could not add trust: {str(e)}")
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
            # dir= is load-bearing: see cert_files.staging_dir.
            staging = cert_files.staging_dir()
            cert_fd, cert_path = tempfile.mkstemp(suffix='.crt', dir=staging, prefix='lxd_verify_')
            key_fd, key_path = tempfile.mkstemp(suffix='.key', dir=staging, prefix='lxd_verify_')
            
            os.write(cert_fd, cert_pem.encode('utf-8'))
            os.close(cert_fd)
            os.chmod(cert_path, 0o600)
            
            os.write(key_fd, key_pem.encode('utf-8'))
            os.close(key_fd)
            os.chmod(key_path, 0o600)
            
            # Test authenticated endpoint
            test_url = f"{normalize_endpoint(endpoint)}/1.0/instances"
            
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
